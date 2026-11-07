"""Data coordinator for Fridge Vision Inventory.

Responsibilities:
* persist and mutate the inventory (``Store``-backed, simple_inventory-
  style items + change history, see ``inventory.py``),
* grab camera frames at ~4 FPS while the door is open (max 30 s buffer),
* post the buffered sequence to the Vision AI add-on on door close,
* apply INSERTED / REMOVED actions to item quantities (recorded in the
  change history with source="vision"),
* dispatch actionable notifications for ambiguous movements,
* fire ``fridge_vision_inventory_item_*`` events for automations,
* re-parameterize the add-on model whenever items change.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
from collections import deque
from contextlib import suppress
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
    EVENT_ITEM_ADDED,
    EVENT_ITEM_DELETED,
    EVENT_ITEM_QUANTITY_CHANGED,
    EVENT_ITEM_UPDATED,
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
from .inventory import (
    EVENT_INCREMENT,
    SOURCE_MANUAL,
    SOURCE_VISION,
    InventoryItem,
    InventoryState,
)

_LOGGER = logging.getLogger(__name__)


async def _async_migrate_inventory_data(
    old_version: int, old_minor_version: int, data: dict[str, Any]
) -> dict[str, Any]:
    """Migrate a stored inventory payload to ``STORAGE_VERSION`` (v2).

    The payload layout itself needs no conversion: version 1 (written by
    integration <= 1.3.x) kept only ``{"items": {...}}`` and
    ``InventoryState.from_storage`` fills in everything added in v2
    (unit/category/... fields default to "", history starts empty), so
    the data passes through unchanged. HA re-stores the payload under
    the new version right after the migration returns.
    """
    return data


class _MigratingStore(Store):
    """Store that provides the migration hook the way HA actually wants it.

    HA invokes ``await self._async_migrate_func(...)`` whenever the
    version stored on disk differs from ``STORAGE_VERSION``; the base
    implementation just raises ``NotImplementedError``, which failed the
    whole config entry setup when it found data written by integration
    <= 1.3.x (bug in 1.4.0). There is no constructor argument for the
    hook either — passing one crashes setup with ``TypeError:
    Store.__init__() got an unexpected keyword argument
    'async_migrate_func'`` (bug in 1.4.1). The supported way is to
    subclass Store and override the method: HA decides the call form by
    counting the override's parameters — exactly two parameters get
    ``(old_version, old_data)``, any other count gets
    ``(old_version, old_minor_version, old_data)`` — and older releases
    also invoke the hook under its public name ``async_migrate_func``.
    The override below tolerates all of those call forms.
    """

    async def _async_migrate_func(
        self,
        old_version: int,
        old_minor_version: int | dict[str, Any],
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Pass the stored payload through (see ``_async_migrate_inventory_data``)."""
        if data is None:
            # Defensive: a two-argument call passes (old_version, old_data).
            data = old_minor_version  # type: ignore[assignment]
            old_minor_version = 0
        return await _async_migrate_inventory_data(
            old_version, old_minor_version, data
        )

    # Same hook under the public name used by older HA releases.
    async def async_migrate_func(self, *args: Any) -> dict[str, Any]:
        return await self._async_migrate_func(*args)


class InventoryStore:
    """Store-backed persistence for the inventory state (items + history)."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._store = _MigratingStore(hass, STORAGE_VERSION, STORAGE_KEY)

    async def async_load(self) -> InventoryState:
        """Load items and change history (tolerant of the v1 layout)."""
        raw = await self._store.async_load()
        if raw is not None and not isinstance(raw, dict):
            _LOGGER.warning(
                "Inventory storage payload has unexpected type %s; "
                "starting with an empty inventory",
                type(raw).__name__,
            )
            return InventoryState()
        state = InventoryState.from_storage(raw)
        if isinstance(raw, dict) and "items" in raw and "history" not in raw:
            # Payload written by integration <= 1.3.x (Store version 1):
            # persist it once in the v2 layout so the migration pass does
            # not run on every restart.
            _LOGGER.info(
                "Migrated inventory storage to v2: %d item(s), "
                "change history starts empty",
                len(state.items),
            )
            try:
                await self._store.async_save(state.to_storage())
            except OSError as err:
                _LOGGER.warning(
                    "Could not write the migrated inventory yet (%s); "
                    "it will be saved on the next change or shutdown",
                    err,
                )
        return state

    def async_delay_save(self, state: InventoryState) -> None:
        """Schedule a debounced save of the current state."""
        self._store.async_delay_save(state.to_storage, SAVE_DELAY)

    async def async_save(self, state: InventoryState) -> None:
        """Save the state immediately (used on shutdown)."""
        await self._store.async_save(state.to_storage())


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
        self.state = InventoryState()
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

    @property
    def items(self) -> dict[str, InventoryItem]:
        """Live item map (id -> item), kept for entities and the WS API."""
        return self.state.items

    async def async_setup(self) -> None:
        """Load stored items and subscribe to door + notification events."""
        self.state = await self._store.async_load()
        _LOGGER.info(
            "Loaded %d inventory item(s) and %d history event(s)",
            len(self.items),
            len(self.state.history),
        )

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
            item = self.state.find_by_prompt(prompt)
            if action == DETECTION_INSERTED:
                if item is None:
                    await self._async_notify_unknown_item(prompt, action)
                else:
                    await self._async_apply_quantity_change(
                        item.id,
                        1,
                        source=SOURCE_VISION,
                        detail=f"{DETECTION_INSERTED} (pewno\u015b\u0107 {confidence:.2f})",
                    )
            elif action == DETECTION_REMOVED:
                if item is None:
                    await self._async_notify_unknown_item(prompt, action)
                else:
                    await self._async_apply_quantity_change(
                        item.id,
                        -1,
                        source=SOURCE_VISION,
                        detail=f"{DETECTION_REMOVED} (pewno\u015b\u0107 {confidence:.2f})",
                    )
            elif action == DETECTION_AMBIGUOUS:
                await self._async_notify_ambiguous(item, prompt)
            else:
                _LOGGER.debug("Ignoring unknown action %r", action)

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

    async def _async_schedule_save(self) -> None:
        self._dirty = True
        self._store.async_delay_save(self.state)

    @callback
    def _async_signal_items_updated(self) -> None:
        async_dispatcher_send(self.hass, SIGNAL_ITEMS_UPDATED)

    # ---------- change bookkeeping (history + HA events) ----------

    _EVENT_TYPE_MAP = {
        "add": EVENT_ITEM_ADDED,
        "delete": EVENT_ITEM_DELETED,
        "update": EVENT_ITEM_UPDATED,
        "increment": EVENT_ITEM_QUANTITY_CHANGED,
        "decrement": EVENT_ITEM_QUANTITY_CHANGED,
    }

    @callback
    def _fire_inventory_event(
        self, event: Any, item: InventoryItem
    ) -> None:
        """Fire the HA event matching a recorded history change."""
        ha_event = self._EVENT_TYPE_MAP.get(event.event_type)
        if ha_event is None:
            return
        payload = event.to_dict()
        payload["item_id"] = item.id
        try:
            self.hass.bus.async_fire(ha_event, payload)
        except HomeAssistantError as err:
            _LOGGER.debug("Could not fire %s: %s", ha_event, err)

    async def _async_persist(self) -> None:
        """Debounced save + real-time signal to entities and the card."""
        await self._async_schedule_save()
        self._async_signal_items_updated()

    async def _async_apply_quantity_change(
        self,
        item_id: str,
        delta: float,
        *,
        source: str,
        detail: str = "",
    ) -> InventoryItem:
        """Apply one quantity change: history, HA event, save, signal."""
        now = dt_util.utcnow().isoformat()
        item, event = self.state.change_quantity(
            item_id, delta, source=source, now=now, detail=detail
        )
        if event is None:
            return item
        await self._async_persist()
        self._fire_inventory_event(event, item)
        _LOGGER.info(
            "%s %s: %s -> %s [%s]%s",
            "Zwi\u0119kszono" if event.event_type == EVENT_INCREMENT else "Zmniejszono",
            item.name,
            event.quantity_before,
            event.quantity_after,
            event.source,
            f" ({event.detail})" if event.detail else "",
        )
        return item

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
        ai_prompt: str = "",
        quantity: float = 1.0,
        location: str = DEFAULT_LOCATION,
        unit: str = "",
        category: str = "",
        expiry_date: str = "",
        description: str = "",
    ) -> InventoryItem:
        """Create an inventory item and re-parameterize the model.

        The AI prompt is optional: an item without one is stored but never
        auto-detected by the vision model (simple_inventory-style manual
        entry). Every creation is recorded in the change history.
        """
        now = dt_util.utcnow().isoformat()
        try:
            item, event = self.state.add_item(
                name=name,
                ai_prompt=ai_prompt,
                quantity=quantity,
                unit=unit,
                category=category,
                location=location,
                expiry_date=expiry_date,
                description=description,
                now=now,
            )
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err
        await self._async_persist()
        self._fire_inventory_event(event, item)
        _LOGGER.info(
            "Added item %s (prompt: %s, qty: %s)",
            item.name,
            item.ai_prompt or "-",
            event.amount,
        )
        if item.ai_prompt:
            self._schedule_reparameterize()
        return item

    async def async_update_quantity(self, item_id: str, delta: float) -> InventoryItem:
        """Increment or decrement an item quantity (clamped at zero)."""
        return await self._async_apply_quantity_change(
            item_id, delta, source=SOURCE_MANUAL
        )

    def _resolve_item(
        self, item_id: str | None, name: str | None
    ) -> InventoryItem:
        """Find an item by id or (case-insensitive) display name."""
        if item_id:
            item = self.items.get(str(item_id))
            if item is None:
                raise HomeAssistantError(f"Unknown item id: {item_id}")
            return item
        if name and str(name).strip():
            item = self.state.find_by_name(str(name))
            if item is None:
                raise HomeAssistantError(
                    f"No inventory item named {name!r}"
                )
            return item
        raise HomeAssistantError("Provide either item_id or name")

    async def async_increment_item(
        self,
        amount: float = 1.0,
        item_id: str | None = None,
        name: str | None = None,
    ) -> InventoryItem:
        """Increase an item's quantity (by id or name) and record history."""
        item = self._resolve_item(item_id, name)
        try:
            amount = float(amount)
        except (TypeError, ValueError) as err:
            raise HomeAssistantError(f"Invalid amount: {amount!r}") from err
        if amount <= 0:
            raise HomeAssistantError("Amount must be greater than zero")
        return await self._async_apply_quantity_change(item.id, amount, source=SOURCE_MANUAL)

    async def async_decrement_item(
        self,
        amount: float = 1.0,
        item_id: str | None = None,
        name: str | None = None,
    ) -> InventoryItem:
        """Decrease an item's quantity (by id or name) and record history."""
        item = self._resolve_item(item_id, name)
        try:
            amount = float(amount)
        except (TypeError, ValueError) as err:
            raise HomeAssistantError(f"Invalid amount: {amount!r}") from err
        if amount <= 0:
            raise HomeAssistantError("Amount must be greater than zero")
        return await self._async_apply_quantity_change(item.id, -amount, source=SOURCE_MANUAL)

    def get_history(self, limit: int | None = None) -> list[dict[str, Any]]:
        """Change history newest-first, optionally limited."""
        return [event.to_dict() for event in self.state.recent_history(limit)]

    async def async_clear_history(self) -> int:
        """Drop all recorded history; returns the removed event count."""
        removed = self.state.clear_history()
        await self._async_persist()
        _LOGGER.info("Cleared %d history event(s)", removed)
        return removed

    async def async_update_item(
        self,
        item_id: str,
        name: str | None = None,
        ai_prompt: str | None = None,
        quantity: float | None = None,
        location: str | None = None,
        unit: str | None = None,
        category: str | None = None,
        expiry_date: str | None = None,
        description: str | None = None,
    ) -> InventoryItem:
        """Update item fields; re-parameterize when the AI prompt changes.

        The AI prompt is optional everywhere: an item without a prompt
        stays in the inventory and is simply no longer auto-detected.
        Every non-empty update is recorded in the change history.
        """
        now = dt_util.utcnow().isoformat()
        try:
            item, event = self.state.update_item(
                item_id,
                now=now,
                name=name,
                ai_prompt=ai_prompt,
                quantity=quantity,
                location=location,
                unit=unit,
                category=category,
                expiry_date=expiry_date,
                description=description,
            )
        except KeyError as err:
            raise HomeAssistantError(str(err)) from err
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err
        if event is None:
            return item
        await self._async_persist()
        self._fire_inventory_event(event, item)
        _LOGGER.info("Updated item %s (%s)", item.name, event.detail)
        if "ai_prompt" in (event.detail or ""):
            self._schedule_reparameterize()
        return item

    async def async_delete_item(self, item_id: str) -> None:
        """Remove an inventory item and re-parameterize the model."""
        now = dt_util.utcnow().isoformat()
        try:
            item, event = self.state.delete_item(item_id, now=now)
        except KeyError as err:
            raise HomeAssistantError(str(err)) from err
        await self._async_persist()
        self._fire_inventory_event(event, item)
        _LOGGER.info("Deleted item %s", item.name)
        self._schedule_reparameterize()

    def sorted_items(self) -> list[InventoryItem]:
        """Items sorted by display name (for the WebSocket API)."""
        return sorted(self.items.values(), key=lambda item: item.name.casefold())

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
            await self._store.async_save(self.state)
            self._dirty = False
