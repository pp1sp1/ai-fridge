"""Data coordinator for Fridge Vision Inventory.

Integrates directly with simple_inventory:
* Automatically extracts 'ai:' prompts from item aliases,
* Automatically triggers debounced model re-parameterization when prompts change,
* Grabs camera frames while the door is open (FIFO buffer),
* Posts the sequence to the Vision AI add-on on door close,
* Updates simple_inventory items (increment_item / decrement_item),
* Updates the last detection image and status sensors.
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
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
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
    SET_CLASSES_TIMEOUT,
)

_LOGGER = logging.getLogger(__name__)

CONF_INVENTORY_ENTITY = "inventory_entity"
SIGNAL_DETECTION_UPDATED = f"{DOMAIN}_detection_updated"

_RETRYABLE_ERRORS: tuple[type[Exception], ...] = (
    aiohttp.ClientConnectorError,
    aiohttp.ClientOSError,
    aiohttp.ServerDisconnectedError,
)
_RETRY_DELAYS: tuple[float, ...] = (0.75, 2.0)


class VisionApiError(HomeAssistantError):
    """Raised when the Vision AI add-on cannot be reached or fails."""


class VisionApiClient:
    """Small aiohttp client for the Vision AI add-on REST API."""

    def __init__(self, session: aiohttp.ClientSession | None, base_url: str) -> None:
        self._session = session
        self._owns_session = session is None
        self._base_url = base_url.rstrip("/")

    @property
    def base_url(self) -> str:
        return self._base_url

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(force_close=True)
            )
            self._owns_session = True
        return self._session

    async def aclose(self) -> None:
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
                            f"{method} {url} failed with HTTP {response.status}: {body[:200]}"
                        )
            except (asyncio.TimeoutError, aiohttp.ClientError) as err:
                if isinstance(err, _RETRYABLE_ERRORS) and attempt <= len(_RETRY_DELAYS):
                    delay = _RETRY_DELAYS[attempt - 1]
                    _LOGGER.warning(
                        "%s %s failed (%s); retrying in %.1f s (attempt %d/%d)",
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
                f"{method} {url} returned non-JSON response: {body[:200]}"
            ) from err

    async def async_health(self, timeout: float = HEALTH_TIMEOUT) -> dict[str, Any]:
        return await self._request("GET", "/api/health", timeout=timeout)

    async def async_set_classes(
        self, classes: list[str], timeout: float = SET_CLASSES_TIMEOUT
    ) -> dict[str, Any]:
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
    return DeviceInfo(
        identifiers={(DOMAIN, entry_id)},
        name="Fridge Vision Inventory",
        manufacturer="Fridge Vision",
        model="Fridge Vision AI (YOLO-World v2)",
    )


def _extract_snapshot_bytes(snapshot: Any) -> bytes | None:
    if snapshot is None:
        return None
    if isinstance(snapshot, (bytes, bytearray)):
        return bytes(snapshot)
    content = getattr(snapshot, "content", None)
    return bytes(content) if content else None


class FridgeVisionCoordinator:
    """Runtime coordinator coordinating camera capture, AI analysis, and simple_inventory."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._client = VisionApiClient(None, str(entry.data[CONF_AI_SERVICE_URL]))
        self._buffer: deque[tuple[datetime, bytes]] = deque()
        self._capture_task: asyncio.Task | None = None
        self._analysis_task: asyncio.Task | None = None
        self._background_tasks: list[asyncio.Task] = []
        self._pending_ambiguous: dict[str, dict[str, Any]] = {}
        self._image_entity: Any | None = None
        self._unsubs: list[Callable[[], None]] = []

        # Simple inventory sync properties
        self.inventory_entity_id: str = (
            entry.data.get(CONF_INVENTORY_ENTITY)
            or entry.options.get(CONF_INVENTORY_ENTITY)
            or ""
        )
        self._current_prompts: set[str] = set()
        self._prompt_to_item: dict[str, str] = {}  # prompt -> item_name
        self._debounce_unsub: Callable[[], None] | None = None

        # State properties for entities in sensor.py
        self.last_detection_summary: str = "Brak detekcji"
        self.last_detection_failed: bool = False
        self.last_detection_details: dict[str, Any] = {}

    async def async_setup(self) -> None:
        """Subscribe to door sensor, simple_inventory entity, and notifications."""
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

        # Track simple_inventory sensor
        if self.inventory_entity_id:
            self._unsubs.append(
                async_track_state_change_event(
                    self.hass,
                    [self.inventory_entity_id],
                    self._handle_inventory_changed,
                )
            )
            # Initial prompt extraction & model initialization
            inv_state = self.hass.states.get(self.inventory_entity_id)
            if inv_state:
                self._process_inventory_state(inv_state, initial=True)

    # ---------- simple_inventory Prompt Sync & Auto-Reparameterization ----------

    def _extract_prompts(
        self, state: State
    ) -> tuple[set[str], dict[str, str]]:
        """Extract prompt -> item_name mapping from simple_inventory items attribute."""
        items = state.attributes.get("items", [])
        if isinstance(items, dict):
            items = list(items.values())
        elif not isinstance(items, list):
            items = []

        prompts: set[str] = set()
        prompt_to_item: dict[str, str] = {}

        for item in items:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip()
            if not name:
                continue

            raw_aliases = item.get("aliases", [])
            aliases: list[str] = []
            if isinstance(raw_aliases, str):
                aliases = [a.strip() for a in raw_aliases.split(",") if a.strip()]
            elif isinstance(raw_aliases, list):
                aliases = [str(a).strip() for a in raw_aliases if str(a).strip()]

            for alias in aliases:
                if alias.lower().startswith("ai:"):
                    prompt = alias[3:].strip()
                    if prompt:
                        prompts.add(prompt)
                        prompt_to_item[prompt] = name

        return prompts, prompt_to_item

    def _process_inventory_state(self, state: State, initial: bool = False) -> None:
        """Check for prompt changes and trigger auto-reparameterization with debounce."""
        new_prompts, new_mapping = self._extract_prompts(state)

        if initial:
            self._current_prompts = new_prompts
            self._prompt_to_item = new_mapping
            if new_prompts:
                self._schedule_reparameterize()
            return

        if new_prompts != self._current_prompts:
            _LOGGER.info(
                "Detected change in 'ai:' prompts (%d -> %d). Scheduling re-parameterization...",
                len(self._current_prompts),
                len(new_prompts),
            )
            self._current_prompts = new_prompts
            self._prompt_to_item = new_mapping

            if self._debounce_unsub:
                self._debounce_unsub()
                self._debounce_unsub = None

            self._debounce_unsub = async_call_later(
                self.hass, 3.0, self._async_debounced_reparameterize
            )
        else:
            self._prompt_to_item = new_mapping

    @callback
    def _handle_inventory_changed(self, event: Event) -> None:
        new_state: State | None = event.data.get("new_state")
        if new_state is None or new_state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            return
        self._process_inventory_state(new_state)

    @callback
    def _async_debounced_reparameterize(self, _now: Any) -> None:
        self._debounce_unsub = None
        self._schedule_reparameterize()

    def _schedule_reparameterize(self) -> None:
        self._background_tasks = [t for t in self._background_tasks if not t.done()]
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
            _LOGGER.warning("Automatic re-parameterization failed: %s", err)

    async def async_reparameterize_model(self) -> bool:
        """Send all AI prompts to the YOLO-World model via add-on."""
        if not self._current_prompts and self.inventory_entity_id:
            inv_state = self.hass.states.get(self.inventory_entity_id)
            if inv_state:
                self._current_prompts, self._prompt_to_item = self._extract_prompts(inv_state)

        prompts_list = sorted(list(self._current_prompts))
        if not prompts_list:
            _LOGGER.info("No AI prompts configured in simple_inventory (aliases starting with 'ai:')")
            return False

        await self._client.async_set_classes(prompts_list)
        _LOGGER.info(
            "Model successfully re-parameterized with %d prompt(s): %s",
            len(prompts_list),
            prompts_list,
        )
        return True

    # ---------- Door Frame Capture Loop ----------

    @callback
    def _handle_door_event(self, event: Event) -> None:
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
            raise

    def _trim_buffer(self) -> None:
        cutoff = dt_util.utcnow() - timedelta(seconds=BUFFER_SECONDS)
        while self._buffer and self._buffer[0][0] < cutoff:
            self._buffer.popleft()
        while len(self._buffer) > MAX_BUFFERED_FRAMES:
            self._buffer.popleft()

    # ---------- Sequence Analysis & simple_inventory Calls ----------

    async def _async_process_door_close(self) -> None:
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
            self.last_detection_failed = True
            self.last_detection_summary = f"Błąd: {err}"
            self._notify_sensors_updated()
            await self._async_report_analysis_error(str(err))
            return

        await self._async_apply_analysis(result)

    async def _async_apply_analysis(self, result: dict[str, Any]) -> None:
        """Apply detections to simple_inventory and update entities."""
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
        detections = result.get("detections", [])
        self.last_detection_details = {
            "timestamp": dt_util.utcnow().isoformat(),
            "detections": detections,
        }

        if not detections:
            self.last_detection_summary = "Brak ruchu"
            self.last_detection_failed = False
            self._notify_sensors_updated()
            return

        inventory_id = self._get_inventory_id()
        detected_summaries: list[str] = []
        has_failure = False

        for detection in detections:
            action = str(detection.get("action", ""))
            prompt = str(detection.get("ai_prompt", "")).strip()
            try:
                confidence = float(detection.get("confidence", 0.0))
            except (TypeError, ValueError):
                confidence = 0.0

            if confidence < threshold:
                _LOGGER.debug("Ignoring detection %r below confidence threshold", prompt)
                continue

            item_name = self._prompt_to_item.get(prompt)

            if action in (DETECTION_INSERTED, DETECTION_REMOVED):
                if not item_name:
                    has_failure = True
                    detected_summaries.append(f"Nieznany: {prompt}")
                    await self._async_notify_unknown_item(prompt, action)
                else:
                    service_name = "increment_item" if action == DETECTION_INSERTED else "decrement_item"
                    action_label = "Włożono" if action == DETECTION_INSERTED else "Wyjęto"
                    detected_summaries.append(f"{item_name} ({action_label})")
                    await self._async_call_simple_inventory(service_name, item_name, inventory_id)
            elif action == DETECTION_AMBIGUOUS:
                has_failure = True
                detected_summaries.append(f"Niejednoznaczne: {item_name or prompt}")
                await self._async_notify_ambiguous(item_name, prompt, inventory_id)

        self.last_detection_failed = has_failure
        self.last_detection_summary = ", ".join(detected_summaries) if detected_summaries else "Brak zmian"
        self._notify_sensors_updated()

    def _get_inventory_id(self) -> str | None:
        """Read inventory_id from simple_inventory sensor attributes if available."""
        if not self.inventory_entity_id:
            return None
        state = self.hass.states.get(self.inventory_entity_id)
        if state:
            return state.attributes.get("inventory_id") or state.attributes.get("id")
        return None

    async def _async_call_simple_inventory(
        self, service: str, item_name: str, inventory_id: str | None
    ) -> None:
        """Call simple_inventory increment_item or decrement_item."""
        data: dict[str, Any] = {"name": item_name, "amount": 1}
        if inventory_id:
            data["inventory_id"] = inventory_id

        try:
            await self.hass.services.async_call("simple_inventory", service, data, blocking=True)
            _LOGGER.info("Called simple_inventory.%s for %s", service, item_name)
        except Exception as err:
            _LOGGER.error("Failed to update simple_inventory for %s: %s", item_name, err)
            self.last_detection_failed = True

    # ---------- Ambiguous & Actionable Notifications ----------

    @callback
    def _handle_notification_action(self, event: Event) -> None:
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
        self._prune_pending()
        tag = str(data.get("tag") or "")
        pending = self._pending_ambiguous.pop(tag, None) if tag else None
        item_name = data.get("item_name") or (pending or {}).get("item_name")
        inventory_id = data.get("inventory_id") or (pending or {}).get("inventory_id")

        if action == ACTION_IGNORE or not item_name:
            return

        service = "increment_item" if action == ACTION_INSERT else "decrement_item"
        await self._async_call_simple_inventory(service, item_name, inventory_id)

    def _prune_pending(self) -> None:
        now = dt_util.utcnow()
        expired = [
            tag
            for tag, payload in self._pending_ambiguous.items()
            if payload["expires"] < now
        ]
        for tag in expired:
            self._pending_ambiguous.pop(tag, None)

    async def _async_notify(
        self,
        title: str,
        message: str,
        actions: list[dict[str, Any]] | None = None,
        tag: str | None = None,
    ) -> None:
        target = str(self.entry.data.get(CONF_NOTIFY_DEVICE, "") or "").strip()
        if not target:
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
        except Exception as err:
            _LOGGER.warning("Could not send notification via %s: %s", target, err)

    async def _async_notify_ambiguous(
        self, item_name: str | None, prompt: str, inventory_id: str | None
    ) -> None:
        if not item_name:
            await self._async_notify_unknown_item(prompt, DETECTION_AMBIGUOUS)
            return
        tag = f"fridge_vision_{uuid.uuid4().hex[:8]}"
        self._pending_ambiguous[tag] = {
            "item_name": item_name,
            "inventory_id": inventory_id,
            "expires": dt_util.utcnow() + timedelta(minutes=PENDING_LIFETIME_MINUTES),
        }
        actions = [
            {"action": ACTION_INSERT, "title": "Włożyłem", "item_name": item_name, "tag": tag},
            {"action": ACTION_REMOVE, "title": "Wyjąłem", "item_name": item_name, "tag": tag},
            {"action": ACTION_IGNORE, "title": "Ignoruj", "item_name": item_name, "tag": tag},
        ]
        await self._async_notify(
            NOTIFICATION_TITLE_AMBIGUOUS,
            f"Wykryto ruch dla {item_name}. Co zrobiłeś?",
            actions=actions,
            tag=tag,
        )

    async def _async_notify_unknown_item(self, prompt: str, action: str) -> None:
        await self._async_notify(
            NOTIFICATION_TITLE_UNKNOWN_ITEM,
            f"Wykryto ruch ({action}) dla promptu \"{prompt}\", ale nie znaleziono produktu z tym promptem w simple_inventory.",
        )

    async def _async_report_analysis_error(self, message: str) -> None:
        try:
            await self.hass.services.async_call(
                "persistent_notification",
                "create",
                {
                    "title": "Fridge Vision: błąd analizy",
                    "message": f"Nie udało się przeanalizować sekwencji: {message}",
                    "notification_id": f"{DOMAIN}_analysis_error",
                },
                blocking=False,
            )
        except Exception as err:
            _LOGGER.warning("Could not create persistent notification: %s", err)

    # ---------- Entity Communication ----------

    @callback
    def _notify_sensors_updated(self) -> None:
        async_dispatcher_send(self.hass, SIGNAL_DETECTION_UPDATED)

    @callback
    def register_image_entity(self, entity: Any) -> None:
        self._image_entity = entity

    @callback
    def unregister_image_entity(self, entity: Any) -> None:
        if self._image_entity is entity:
            self._image_entity = None

    async def async_shutdown(self) -> None:
        if self._debounce_unsub:
            self._debounce_unsub()
            self._debounce_unsub = None
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()

        tasks = [
            t
            for t in (
                self._capture_task,
                self._analysis_task,
                *self._background_tasks,
            )
            if t is not None and not t.done()
        ]
        for t in tasks:
            t.cancel()
        for t in tasks:
            with suppress(asyncio.CancelledError):
                try:
                    await t
                except Exception as err:
                    _LOGGER.debug("Background task error on shutdown: %s", err)

        self._capture_task = None
        self._analysis_task = None
        self._background_tasks.clear()
        await self._client.aclose()