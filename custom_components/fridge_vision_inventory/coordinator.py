"""Data coordinator for Fridge Vision Inventory.

Responsibilities:
* persist and mutate the inventory (``Store``-backed),
* grab camera frames at ~4 FPS while the door is open (max 30 s buffer),
* post the buffered sequence to the Vision AI add-on on door close,
* apply INSERTED / REMOVED actions to item quantities,
* dispatch actionable notifications for ambiguous movements,
* re-parameterize the add-on model whenever items change.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
from collections import deque
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
import json
import logging
import time
import uuid
from typing import Any, Callable

import aiohttp

from homeassistant.components import camera
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import Event, HomeAssistant, State, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    ACTION_IGNORE,
    ACTION_INSERT,
    ACTION_REMOVE,
    ANALYZE_TIMEOUT,
    BUFFER_SECONDS,
    CAPTURE_SNAPSHOT_TIMEOUT,
    CONF_AI_SERVICE_URL,
    CONF_CAMERA_ENTITY,
    CONF_CAPTURE_FPS,
    CONF_CONFIDENCE_THRESHOLD,
    CONF_DOOR_SENSOR_ENTITY,
    CONF_MOTION_VECTOR_INSERT_DIRECTION,
    CONF_NOTIFY_DEVICE,
    DEFAULT_CAPTURE_FPS,
    DEFAULT_CONFIDENCE_THRESHOLD,
    DEFAULT_LOCATION,
    DETECTION_AMBIGUOUS,
    DETECTION_INSERTED,
    DETECTION_REMOVED,
    DOMAIN,
    EVENT_NOTIFICATION_ACTION,
    HEALTH_TIMEOUT,
    MAX_BUFFERED_FRAMES,
    MIN_FRAMES_FOR_ANALYSIS,
    NOTIFY_DOMAIN,
    NOTIFICATION_TITLE_AMBIGUOUS,
    NOTIFICATION_TITLE_UNKNOWN_ITEM,
    PENDING_LIFETIME_MINUTES,
    SAVE_DELAY,
    SET_CLASSES_TIMEOUT,
    SIGNAL_ITEMS_UPDATED,
    STORAGE_KEY,
    STORAGE_VERSION,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class InventoryItem:
    """A single inventory entry persisted in the Store."""

    id: str
    name: str
    ai_prompt: str
    quantity: int
    location: str
    last_updated: str

    def to_dict(self) -> dict[str, Any]:
        """Serialize for storage and WebSocket responses."""
        return {
            "id": self.id,
            "name": self.name,
            "ai_prompt": self.ai_prompt,
            "quantity": self.quantity,
            "location": self.location,
            "last_updated": self.last_updated,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> InventoryItem | None:
        """Deserialize one stored entry; returns None when malformed."""
        try:
            item_id = str(payload["id"]).strip()
            name = str(payload["name"]).strip()
            # The AI prompt is optional on stored entries: an item without a
            # prompt stays in the inventory but is not auto-detected until a
            # prompt is set again.
            ai_prompt = str(payload.get("ai_prompt", "")).strip()
            if not item_id or not name:
                return None
            quantity = max(0, int(payload.get("quantity", 0)))
            location = str(payload.get("location") or DEFAULT_LOCATION)
            last_updated = str(
                payload.get("last_updated") or dt_util.utcnow().isoformat()
            )
        except (KeyError, TypeError, ValueError):
            return None
        return cls(
            id=item_id,
            name=name,
            ai_prompt=ai_prompt,
            quantity=quantity,
            location=location,
            last_updated=last_updated,
        )


class InventoryStore:
    """Store-backed persistence for inventory items."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._store = Store(hass, STORAGE_VERSION, STORAGE_KEY)

    async def async_load(self) -> dict[str, InventoryItem]:
        """Load all items from storage."""
        raw = await self._store.async_load()
        items: dict[str, InventoryItem] = {}
        if not raw or not isinstance(raw.get("items"), dict):
            return items
        for key, payload in raw["items"].items():
            item = InventoryItem.from_dict(payload)
            if item is None:
                _LOGGER.warning("Skipping malformed inventory entry %s", key)
                continue
            items[item.id] = item
        return items

    def async_delay_save(self, items: dict[str, InventoryItem]) -> None:
        """Schedule a debounced save of the current item map."""
        self._store.async_delay_save(
            lambda: {
                item_id: item.to_dict() for item_id, item in items.items()
            },
            SAVE_DELAY,
        )

    async def async_save(self, items: dict[str, InventoryItem]) -> None:
        """Save the item map immediately (used on shutdown)."""
        await self._store.async_save(
            {item_id: item.to_dict() for item_id, item in items.items()}
        )


class VisionApiError(HomeAssistantError):
    """Raised when the Vision AI add-on cannot be reached or fails."""


# Connection errors that mean "the request was most likely never
# processed" and are safe to retry for this API: every endpoint here is
# idempotent (health probe, set_classes, analyze_sequence). A retry
# always opens a fresh connection, so a stale pooled socket can never
# poison a second attempt.
_RETRYABLE_ERRORS: tuple[type[Exception], ...] = (
    aiohttp.ClientConnectorError,
    aiohttp.ClientOSError,  # covers [Errno 104] Connection reset by peer
    aiohttp.ServerDisconnectedError,
)
_RETRY_DELAYS: tuple[float, ...] = (0.75, 2.0)


class VisionApiClient:
    """Small aiohttp client for the Vision AI add-on REST API.

    ``session`` may be ``None``: the client then owns a dedicated
    ``ClientSession`` with keep-alive disabled (``force_close``). That is
    deliberate — uvicorn closes idle keep-alive connections after a few
    seconds, and reusing such a pooled connection races the server's
    FIN, which surfaces as ``[Errno 104] Connection reset by peer`` on
    the next call (typically the first analysis after a quiet period or
    an add-on restart). Fresh connections per request cost nothing on
    localhost and remove the race entirely; the retry loop below covers
    the remaining restart windows.
    """

    def __init__(
        self, session: aiohttp.ClientSession | None, base_url: str
    ) -> None:
        self._session = session
        self._owns_session = session is None
        self._base_url = base_url.rstrip("/")

    @property
    def base_url(self) -> str:
        """Base URL of the add-on API."""
        return self._base_url

    def _get_session(self) -> aiohttp.ClientSession:
        """Return the injected session, or create the owned one lazily."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(force_close=True)
            )
            self._owns_session = True
        return self._session

    async def aclose(self) -> None:
        """Close the owned session (no-op when one was injected)."""
        if self._owns_session and self._session is not None:
            if not self._session.closed:
                await self._session.close()
            self._session = None

    async def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        timeout: float = HEALTH_TIMEOUT,
    ) -> dict[str, Any]:
        url = f"{self._base_url}{path}"
        session = self._get_session()
        attempt = 0
        while True:
            attempt += 1
            try:
                async with session.request(
                    method,
                    url,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as response:
                    body = await response.text()
                    if response.status >= 400:
                        raise VisionApiError(
                            f"{method} {url} failed with HTTP {response.status}: "
                            f"{body[:200]}"
                        )
            except (asyncio.TimeoutError, aiohttp.ClientError) as err:
                # A real HTTP answer (any status) is never retried; only
                # connection-level failures get another attempt.
                if (
                    isinstance(err, _RETRYABLE_ERRORS)
                    and attempt <= len(_RETRY_DELAYS)
                ):
                    delay = _RETRY_DELAYS[attempt - 1]
                    _LOGGER.warning(
                        "%s %s failed (%s); retrying in %.1f s "
                        "(attempt %d/%d)",
                        method,
                        url,
                        err,
                        delay,
                        attempt + 1,
                        len(_RETRY_DELAYS) + 1,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise VisionApiError(f"{method} {url} failed: {err}") from err
            break
        if not body:
            return {}
        try:
            return json.loads(body)
        except ValueError as err:
            raise VisionApiError(
                f"{method} {url} returned a non-JSON response: {body[:200]}"
            ) from err

    async def async_health(self, timeout: float = HEALTH_TIMEOUT) -> dict[str, Any]:
        """Probe the add-on health endpoint."""
        return await self._request("GET", "/api/health", timeout=timeout)

    async def async_set_classes(
        self, classes: list[str], timeout: float = SET_CLASSES_TIMEOUT
    ) -> dict[str, Any]:
        """Push zero-shot prompts to the model."""
        return await self._request(
            "POST", "/api/set_classes", {"classes": classes}, timeout=timeout
        )

    async def async_analyze(
        self,
        frames: list[str],
        vector_direction: str,
        confidence_threshold: float,
        timeout: float = ANALYZE_TIMEOUT,
    ) -> dict[str, Any]:
        """Analyze a buffered door-open sequence."""
        return await self._request(
            "POST",
            "/api/analyze_sequence",
            {
                "frames": frames,
                "vector_direction": vector_direction,
                "confidence_threshold": confidence_threshold,
            },
            timeout=timeout,
        )


def device_info(entry_id: str) -> DeviceInfo:
    """Device that all integration entities attach to."""
    return DeviceInfo(
        identifiers={(DOMAIN, entry_id)},
        name="Fridge Vision Inventory",
        manufacturer="Fridge Vision",
        model="Fridge Vision AI (YOLO-World v2)",
    )


def _extract_snapshot_bytes(snapshot: Any) -> bytes | None:
    """Support both the historic bytes return and the newer Image dataclass."""
    if snapshot is None:
        return None
    if isinstance(snapshot, (bytes, bytearray)):
        return bytes(snapshot)
    content = getattr(snapshot, "content", None)
    return bytes(content) if content else None


class FridgeVisionCoordinator:
    """Runtime coordinator bound to a config entry."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.items: dict[str, InventoryItem] = {}
        self._store = InventoryStore(hass)
        # session=None -> the client owns a dedicated force_close session
        # (see VisionApiClient for why keep-alive must not be reused).
        self._client = VisionApiClient(None, str(entry.data[CONF_AI_SERVICE_URL]))
        self._buffer: deque[tuple[datetime, bytes]] = deque()
        self._capture_task: asyncio.Task | None = None
        self._analysis_task: asyncio.Task | None = None
        self._background_tasks: list[asyncio.Task] = []
        self._pending_ambiguous: dict[str, dict[str, Any]] = {}
        self._image_entity: Any | None = None
        self._unsubs: list[Callable[[], None]] = []
        self._dirty = False

    async def async_setup(self) -> None:
        """Load stored items and subscribe to door + notification events."""
        self.items = await self._store.async_load()
        _LOGGER.info("Loaded %d inventory item(s)", len(self.items))

        self._unsubs.append(
            async_track_state_change_event(
                self.hass,
                [self.entry.data[CONF_DOOR_SENSOR_ENTITY]],
                self._handle_door_event,
            )
        )
        self._unsubs.append(
            self.hass.bus.async_listen(
                EVENT_NOTIFICATION_ACTION, self._handle_notification_action
            )
        )
        if self.items:
            self._schedule_reparameterize()

    @callback
    def _handle_door_event(self, event: Event) -> None:
        """Start capture on door-open, analyze on door-close."""
        new_state: State | None = event.data.get("new_state")
        old_state: State | None = event.data.get("old_state")
        if new_state is None or new_state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            return
        if new_state.state == STATE_ON and (
            old_state is None or old_state.state != STATE_ON
        ):
            _LOGGER.debug("Door opened - starting frame capture")
            self._buffer.clear()
            if self._capture_task is None or self._capture_task.done():
                self._capture_task = self.hass.async_create_task(
                    self._async_capture_loop()
                )
        elif new_state.state == STATE_OFF and (
            old_state is None or old_state.state != STATE_OFF
        ):
            _LOGGER.debug("Door closed - stopping capture and analyzing")
            if self._capture_task is not None:
                self._capture_task.cancel()
                self._capture_task = None
            self._analysis_task = self.hass.async_create_task(
                self._async_process_door_close()
            )

    async def _async_capture_loop(self) -> None:
        """Grab frames from the camera at the configured FPS until cancelled."""
        fps = int(self.entry.data.get(CONF_CAPTURE_FPS, DEFAULT_CAPTURE_FPS))
        interval = 1.0 / max(1, fps)
        try:
            while True:
                started = time.monotonic()
                try:
                    snapshot = await camera.async_get_image(
                        self.hass,
                        self.entry.data[CONF_CAMERA_ENTITY],
                        timeout=CAPTURE_SNAPSHOT_TIMEOUT,
                    )
                except (HomeAssistantError, asyncio.TimeoutError, aiohttp.ClientError) as err:
                    _LOGGER.debug("Frame grab failed: %s", err)
                else:
                    data = _extract_snapshot_bytes(snapshot)
                    if data:
                        self._buffer.append((dt_util.utcnow(), data))
                        self._trim_buffer()
                elapsed = time.monotonic() - started
                await asyncio.sleep(max(0.02, interval - elapsed))
        except asyncio.CancelledError:
            _LOGGER.debug("Frame capture cancelled")
            raise

    def _trim_buffer(self) -> None:
        """Keep at most BUFFER_SECONDS worth of frames (FIFO)."""
        cutoff = dt_util.utcnow() - timedelta(seconds=BUFFER_SECONDS)
        while self._buffer and self._buffer[0][0] < cutoff:
            self._buffer.popleft()
        while len(self._buffer) > MAX_BUFFERED_FRAMES:
            self._buffer.popleft()

    async def _async_process_door_close(self) -> None:
        """Send the buffered frames to the add-on and apply the result."""
        buffer = list(self._buffer)
        self._buffer.clear()
        if len(buffer) < MIN_FRAMES_FOR_ANALYSIS:
            _LOGGER.info(
                "Door closed but fewer than %d frames captured; skipping analysis",
                MIN_FRAMES_FOR_ANALYSIS,
            )
            return
        frames_b64 = [base64.b64encode(data).decode("ascii") for _, data in buffer]
        _LOGGER.info("Analyzing door sequence with %d frame(s)", len(frames_b64))
        try:
            result = await self._client.async_analyze(
                frames_b64,
                str(self.entry.data[CONF_MOTION_VECTOR_INSERT_DIRECTION]),
                float(
                    self.entry.data.get(
                        CONF_CONFIDENCE_THRESHOLD, DEFAULT_CONFIDENCE_THRESHOLD
                    )
                ),
            )
        except VisionApiError as err:
            _LOGGER.error("Vision AI analysis failed: %s", err)
            await self._async_report_analysis_error(str(err))
            return
        await self._async_apply_analysis(result)

    async def _async_apply_analysis(self, result: dict[str, Any]) -> None:
        """Update the image entity and item quantities from the analysis."""
        annotated_b64 = result.get("annotated_image_base64")
        if annotated_b64 and self._image_entity is not None:
            with suppress(binascii.Error, ValueError):
                self._image_entity.async_set_detection_image(
                    base64.b64decode(annotated_b64)
                )

        threshold = float(
            self.entry.data.get(
                CONF_CONFIDENCE_THRESHOLD, DEFAULT_CONFIDENCE_THRESHOLD
            )
        )
        changed = False
        for detection in result.get("detections", []):
            action = str(detection.get("action", ""))
            prompt = str(detection.get("ai_prompt", "")).strip()
            try:
                confidence = float(detection.get("confidence", 0.0))
            except (TypeError, ValueError):
                confidence = 0.0
            if confidence < threshold:
                _LOGGER.debug(
                    "Ignoring detection %r below confidence threshold", prompt
                )
                continue
            item = self._find_item_by_prompt(prompt)
            if action == DETECTION_INSERTED:
                if item is None:
                    await self._async_notify_unknown_item(prompt, action)
                else:
                    item.quantity += 1
                    self._touch(item)
                    changed = True
                    _LOGGER.info(
                        "Inserted %s -> quantity %d", item.name, item.quantity
                    )
            elif action == DETECTION_REMOVED:
                if item is None:
                    await self._async_notify_unknown_item(prompt, action)
                else:
                    item.quantity = max(0, item.quantity - 1)
                    self._touch(item)
                    changed = True
                    _LOGGER.info(
                        "Removed %s -> quantity %d", item.name, item.quantity
                    )
            elif action == DETECTION_AMBIGUOUS:
                await self._async_notify_ambiguous(item, prompt)
            else:
                _LOGGER.debug("Ignoring unknown action %r", action)
        if changed:
            await self._async_schedule_save()
            self._async_signal_items_updated()

    @callback
    def _handle_notification_action(self, event: Event) -> None:
        """React to taps on the actionable notification buttons."""
        data = event.data
        action = data.get("action")
        if action not in (ACTION_INSERT, ACTION_REMOVE, ACTION_IGNORE):
            return
        self.hass.async_create_task(
            self._async_handle_notification_action(str(action), dict(data))
        )

    async def _async_handle_notification_action(
        self, action: str, data: dict[str, Any]
    ) -> None:
        """Apply the chosen action to the pending ambiguous detection."""
        self._prune_pending()
        tag = str(data.get("tag") or "")
        pending = self._pending_ambiguous.pop(tag, None) if tag else None
        item_id = data.get("item_id") or (pending or {}).get("item_id")
        if action == ACTION_IGNORE:
            _LOGGER.debug("Ambiguous detection ignored via notification")
            return
        if not item_id or str(item_id) not in self.items:
            _LOGGER.warning(
                "Notification action %s without a resolvable item", action
            )
            return
        delta = 1 if action == ACTION_INSERT else -1
        try:
            await self.async_update_quantity(str(item_id), delta)
        except HomeAssistantError as err:
            _LOGGER.warning(
                "Could not apply notification action %s: %s", action, err
            )

    def _prune_pending(self) -> None:
        """Drop stale pending ambiguous detections."""
        now = dt_util.utcnow()
        expired = [
            tag
            for tag, payload in self._pending_ambiguous.items()
            if payload["expires"] < now
        ]
        for tag in expired:
            self._pending_ambiguous.pop(tag, None)

    def _touch(self, item: InventoryItem) -> None:
        item.last_updated = dt_util.utcnow().isoformat()

    async def _async_schedule_save(self) -> None:
        self._dirty = True
        self._store.async_delay_save(self.items)

    @callback
    def _async_signal_items_updated(self) -> None:
        async_dispatcher_send(self.hass, SIGNAL_ITEMS_UPDATED)

    async def _async_notify(
        self,
        title: str,
        message: str,
        actions: list[dict[str, Any]] | None = None,
        tag: str | None = None,
    ) -> None:
        """Send a (optionally actionable) notification to the configured target."""
        target = str(self.entry.data.get(CONF_NOTIFY_DEVICE, "") or "").strip()
        if not target:
            _LOGGER.debug(
                "No notify device configured; skipping notification %r", title
            )
            return
        target = target.replace("/", ".")
        if "." not in target:
            target = f"{NOTIFY_DOMAIN}.{target}"
        domain, _, service = target.partition(".")
        service_data: dict[str, Any] = {"title": title, "message": message}
        if actions:
            service_data["data"] = {"tag": tag, "actions": actions}
        try:
            await self.hass.services.async_call(
                domain, service, service_data, blocking=True
            )
        except (HomeAssistantError, asyncio.TimeoutError, aiohttp.ClientError) as err:
            _LOGGER.warning(
                "Could not send notification via %s: %s", target, err
            )

    async def _async_notify_ambiguous(
        self, item: InventoryItem | None, prompt: str
    ) -> None:
        """Dispatch the actionable notification for an ambiguous movement."""
        if item is None:
            await self._async_notify_unknown_item(prompt, DETECTION_AMBIGUOUS)
            return
        tag = f"fridge_vision_{uuid.uuid4().hex[:8]}"
        self._pending_ambiguous[tag] = {
            "item_id": item.id,
            "prompt": prompt,
            "expires": dt_util.utcnow()
            + timedelta(minutes=PENDING_LIFETIME_MINUTES),
        }
        actions = [
            {
                "action": ACTION_INSERT,
                "title": "Włożyłem",
                "item_id": item.id,
                "tag": tag,
            },
            {
                "action": ACTION_REMOVE,
                "title": "Wyjąłem",
                "item_id": item.id,
                "tag": tag,
            },
            {
                "action": ACTION_IGNORE,
                "title": "Ignoruj",
                "item_id": item.id,
                "tag": tag,
            },
        ]
        await self._async_notify(
            NOTIFICATION_TITLE_AMBIGUOUS,
            f"Wykryto: {item.name}. Co zrobiłeś?",
            actions=actions,
            tag=tag,
        )

    async def _async_notify_unknown_item(self, prompt: str, action: str) -> None:
        """Inform about a detection that has no matching inventory item."""
        await self._async_notify(
            NOTIFICATION_TITLE_UNKNOWN_ITEM,
            f"Wykryto ruch ({action}) dla produktu \"{prompt}\", "
            "ale nie znaleziono go w inwentarzu.",
        )

    async def _async_report_analysis_error(self, message: str) -> None:
        """Surface analysis failures as a persistent notification."""
        try:
            await self.hass.services.async_call(
                "persistent_notification",
                "create",
                {
                    "title": "Fridge Vision Inventory: błąd analizy",
                    "message": f"Nie udało się przeanalizować sekwencji: {message}",
                    "notification_id": f"{DOMAIN}_analysis_error",
                },
                blocking=False,
            )
        except HomeAssistantError as err:
            _LOGGER.warning("Could not create persistent notification: %s", err)

    def _schedule_reparameterize(self) -> None:
        """Fire-and-forget model re-parameterization in the background."""
        self._background_tasks = [
            task for task in self._background_tasks if not task.done()
        ]
        self._background_tasks.append(
            self.hass.async_create_background_task(
                self._async_reparameterize_best_effort(),
                f"{DOMAIN}_reparameterize",
            )
        )

    async def _async_reparameterize_best_effort(self) -> None:
        try:
            await self.async_reparameterize_model()
        except VisionApiError as err:
            _LOGGER.warning("Re-parameterization failed: %s", err)

    async def async_reparameterize_model(self) -> bool:
        """Push all unique ai_prompts to the add-on; raises on API failure."""
        prompts = sorted(
            {
                item.ai_prompt.strip()
                for item in self.items.values()
                if item.ai_prompt.strip()
            }
        )
        if not prompts:
            _LOGGER.info("No AI prompts configured; skipping re-parameterization")
            return False
        await self._client.async_set_classes(prompts)
        _LOGGER.info(
            "Model re-parameterized with %d class(es): %s", len(prompts), prompts
        )
        return True

    async def async_add_item(
        self,
        name: str,
        ai_prompt: str,
        quantity: int = 1,
        location: str = DEFAULT_LOCATION,
    ) -> InventoryItem:
        """Create an inventory item and re-parameterize the model."""
        name_clean = str(name).strip()
        prompt_clean = str(ai_prompt).strip()
        if not name_clean:
            raise HomeAssistantError("Item name must not be empty")
        if not prompt_clean:
            raise HomeAssistantError("AI prompt must not be empty")
        item = InventoryItem(
            id=uuid.uuid4().hex,
            name=name_clean,
            ai_prompt=prompt_clean,
            quantity=max(0, int(quantity)),
            location=str(location).strip() or DEFAULT_LOCATION,
            last_updated=dt_util.utcnow().isoformat(),
        )
        self.items[item.id] = item
        await self._async_schedule_save()
        self._async_signal_items_updated()
        _LOGGER.info("Added item %s (prompt: %s)", item.name, item.ai_prompt)
        self._schedule_reparameterize()
        return item

    async def async_update_quantity(self, item_id: str, delta: int) -> InventoryItem:
        """Increment or decrement an item quantity (clamped at zero)."""
        item = self.items.get(item_id)
        if item is None:
            raise HomeAssistantError(f"Unknown item id: {item_id}")
        item.quantity = max(0, item.quantity + int(delta))
        self._touch(item)
        await self._async_schedule_save()
        self._async_signal_items_updated()
        return item

    async def async_update_item(
        self,
        item_id: str,
        name: str | None = None,
        ai_prompt: str | None = None,
        quantity: int | None = None,
        location: str | None = None,
    ) -> InventoryItem:
        """Update item fields; re-parameterize when the AI prompt changes.

        The AI prompt is mandatory when adding an item, but it can be cleared
        while editing: an item without a prompt stays in the inventory and is
        simply no longer auto-detected by the vision model.
        """
        item = self.items.get(item_id)
        if item is None:
            raise HomeAssistantError(f"Unknown item id: {item_id}")
        if name is None and ai_prompt is None and quantity is None and location is None:
            raise HomeAssistantError("Provide at least one field to update")

        if name is not None:
            cleaned_name = str(name).strip()
            if not cleaned_name:
                raise HomeAssistantError("Item name must not be empty")
            item.name = cleaned_name
        if ai_prompt is not None:
            # An empty prompt is allowed here: it clears the detection class
            # and the item stops being auto-detected (it is kept in the
            # inventory so the user can set a new prompt later).
            cleaned_prompt = str(ai_prompt).strip()
            if cleaned_prompt != item.ai_prompt:
                item.ai_prompt = cleaned_prompt
                self._touch(item)
                self._schedule_reparameterize()
        if quantity is not None:
            item.quantity = max(0, int(quantity))
        if location is not None:
            item.location = str(location).strip() or DEFAULT_LOCATION
        self._touch(item)
        await self._async_schedule_save()
        self._async_signal_items_updated()
        return item

    async def async_delete_item(self, item_id: str) -> None:
        """Remove an inventory item and re-parameterize the model."""
        item = self.items.pop(item_id, None)
        if item is None:
            raise HomeAssistantError(f"Unknown item id: {item_id}")
        await self._async_schedule_save()
        self._async_signal_items_updated()
        _LOGGER.info("Deleted item %s", item.name)
        self._schedule_reparameterize()

    def sorted_items(self) -> list[InventoryItem]:
        """Items sorted by display name (for the WebSocket API)."""
        return sorted(self.items.values(), key=lambda item: item.name.casefold())

    def _find_item_by_prompt(self, prompt: str) -> InventoryItem | None:
        wanted = prompt.strip().casefold()
        if not wanted:
            return None
        for item in self.items.values():
            if item.ai_prompt.strip().casefold() == wanted:
                return item
        return None

    @callback
    def register_image_entity(self, entity: Any) -> None:
        """Register the image entity that displays annotated frames."""
        self._image_entity = entity

    @callback
    def unregister_image_entity(self, entity: Any) -> None:
        """Unregister the image entity."""
        if self._image_entity is entity:
            self._image_entity = None

    async def async_shutdown(self) -> None:
        """Cancel tasks, remove listeners and flush the store."""
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        tasks = [
            task
            for task in (
                self._capture_task,
                self._analysis_task,
                *self._background_tasks,
            )
            if task is not None and not task.done()
        ]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                try:
                    await task
                except Exception as err:  # noqa: BLE001 - shutdown must not fail
                    _LOGGER.debug("Background task ended with error: %s", err)
        self._capture_task = None
        self._analysis_task = None
        self._background_tasks.clear()
        await self._client.aclose()
        if self._dirty:
            await self._store.async_save(self.items)
            self._dirty = False
