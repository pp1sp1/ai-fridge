"""Coordinator for the door-open frame capture and analysis cycle."""

from __future__ import annotations

import asyncio
import base64
import logging
from pathlib import Path
from typing import Any
import uuid

import async_timeout

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import aiohttp_client
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util import dt as dt_util

from .const import (
    ACTION_IGNORE,
    ACTION_INSERT,
    ACTION_REMOVE,
    CONF_AI_SERVICE_URL,
    CONF_CAPTURE_FPS,
    CONF_CONFIDENCE_THRESHOLD,
    CONF_CAMERA_ENTITY,
    CONF_DOOR_SENSOR_ENTITY,
    CONF_MOTION_VECTOR_INSERT_DIRECTION,
    CONF_NOTIFY_DEVICE,
    DEFAULT_CAPTURE_FPS,
    DEFAULT_CONFIDENCE_THRESHOLD,
    DOMAIN,
    MAX_CAPTURE_SECONDS,
    ANALYSIS_TIMEOUT_SECONDS,
    REPARAMETERIZE_TIMEOUT_SECONDS,
    NOTIFICATION_MESSAGE,
    NOTIFICATION_TITLE,
    SIGNAL_ITEM_UPDATED,
)

_LOGGER = logging.getLogger(__name__)

SNAPSHOT_PREFIX = "fridge_vision_"
EVENT_NOTIFICATION_ACTION = "mobile_app_notification_action"


class FridgeVisionCoordinator:
    """Owns the door-open detection cycle for a single config entry."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: Any,
        store: Any,
        items: list[dict[str, Any]],
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.store = store
        self.items = items
        self._session = aiohttp_client.async_get_clientsession(hass)
        self._capture_task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._unsub_door = None
        self._unsub_notification = None
        self._door_open = False
        self._buffer: list[str] = []
        self._pending: dict[str, dict[str, Any]] = {}

    @property
    def ai_service_url(self) -> str:
        return self.entry.data[CONF_AI_SERVICE_URL]

    async def async_setup(self) -> None:
        """Listen to the door sensor and notification action events."""
        door_entity = self.entry.data[CONF_DOOR_SENSOR_ENTITY]
        self._unsub_door = async_track_state_change_event(
            self.hass, [door_entity], self._async_door_changed
        )
        initial_state = self.hass.states.get(door_entity)
        self._door_open = (
            initial_state is not None and initial_state.state == "on"
        )
        self._unsub_notification = self.hass.bus.async_listen(
            EVENT_NOTIFICATION_ACTION, self._async_notification_action
        )

    async def async_unload(self) -> None:
        """Unsubscribe listeners and stop any in-progress capture."""
        if self._unsub_door is not None:
            self._unsub_door()
            self._unsub_door = None
        if self._unsub_notification is not None:
            self._unsub_notification()
            self._unsub_notification = None
        self._stop_event.set()
        if self._capture_task is not None:
            try:
                await self._capture_task
            except asyncio.CancelledError:
                pass
            self._capture_task = None

    @callback
    def _async_door_changed(self, event: Any) -> None:
        new_state = event.data.get("new_state")
        if new_state is None:
            return
        is_open = new_state.state == "on"
        if is_open and not self._door_open:
            self.hass.async_create_task(self._async_door_opened())
        elif not is_open and self._door_open:
            self.hass.async_create_task(self._async_door_closed())
        self._door_open = is_open

    async def _async_door_opened(self) -> None:
        self._buffer = []
        self._stop_event.clear()
        self._capture_task = asyncio.create_task(self._async_capture_loop())

    async def _async_door_closed(self) -> None:
        self._stop_event.set()
        task = self._capture_task
        self._capture_task = None
        if task is not None:
            await task
        await self._async_process_buffer()

    async def _async_capture_loop(self) -> None:
        """Capture camera frames at the configured FPS while the door is open."""
        fps = self.entry.data.get(CONF_CAPTURE_FPS, DEFAULT_CAPTURE_FPS)
        interval = max(0.1, 1.0 / float(fps))
        deadline = asyncio.get_running_loop().time() + MAX_CAPTURE_SECONDS
        camera_entity = self.entry.data[CONF_CAMERA_ENTITY]

        while not self._stop_event.is_set():
            if asyncio.get_running_loop().time() > deadline:
                break
            try:
                frame = await self._async_snapshot_frame(camera_entity)
            except Exception as err:  # network timeouts / dropped frames
                _LOGGER.warning(
                    "Failed to capture frame from %s: %s", camera_entity, err
                )
                await asyncio.sleep(interval)
                continue
            if frame is not None:
                self._buffer.append(frame)
            await asyncio.sleep(interval)

    async def _async_snapshot_frame(self, camera_entity: str) -> str | None:
        """Capture a single camera snapshot and return it as base64 JPEG."""
        snapshot_name = f"{SNAPSHOT_PREFIX}{uuid.uuid4().hex}.jpg"
        snapshot_path = Path(self.hass.config.config_dir) / snapshot_name
        try:
            await self.hass.services.async_call(
                "camera",
                "snapshot",
                {"entity_id": camera_entity, "filename": str(snapshot_path)},
                blocking=True,
            )
            data = await self.hass.async_add_executor_job(
                snapshot_path.read_bytes
            )
            return base64.b64encode(data).decode("ascii")
        finally:
            await self.hass.async_add_executor_job(
                lambda: snapshot_path.unlink(missing_ok=True)
            )

    async def _async_process_buffer(self) -> None:
        """Send buffered frames to the add-on and apply the analysis result."""
        frames = self._buffer
        self._buffer = []
        if not frames:
            return

        direction = self.entry.data.get(
            CONF_MOTION_VECTOR_INSERT_DIRECTION,
            "top_to_bottom",
        )
        threshold = float(
            self.entry.data.get(CONF_CONFIDENCE_THRESHOLD, DEFAULT_CONFIDENCE_THRESHOLD)
        )

        try:
            async with async_timeout.timeout(ANALYSIS_TIMEOUT_SECONDS):
                response = await self._session.post(
                    f"{self.ai_service_url}/api/analyze_sequence",
                    json={
                        "frames": frames,
                        "vector_direction": direction,
                        "confidence_threshold": threshold,
                    },
                )
                response.raise_for_status()
                result = await response.json()
        except Exception as err:
            _LOGGER.error("Analysis request to %s failed: %s", self.ai_service_url, err)
            return

        # Show the last annotated frame in the image entity.
        annotated_b64 = result.get("annotated_image_base64")
        if annotated_b64:
            try:
                image_bytes = base64.b64decode(annotated_b64)
            except Exception:
                image_bytes = None
            if image_bytes:
                domain_data = self.hass.data[DOMAIN].get(self.entry.entry_id, {})
                image_entity = domain_data.get("image_entity")
                if image_entity is not None:
                    image_entity.async_set_image(image_bytes)

        changed_items: list[dict[str, Any]] = []
        for detection in result.get("detections", []):
            action = detection.get("action")
            confidence = float(detection.get("confidence", 0.0))
            prompt = detection.get("ai_prompt")
            if not prompt:
                continue
            item = self.find_item_by_prompt(prompt)

            if action == ACTION_INSERT and confidence >= threshold and item is not None:
                item["quantity"] = int(item.get("quantity", 0)) + 1
                item["last_updated"] = dt_util.utcnow().isoformat()
                changed_items.append(item)
            elif (
                action == ACTION_REMOVE
                and confidence >= threshold
                and item is not None
            ):
                item["quantity"] = max(0, int(item.get("quantity", 0)) - 1)
                item["last_updated"] = dt_util.utcnow().isoformat()
                changed_items.append(item)
            elif action == "AMBIGUOUS":
                self._async_dispatch_ambiguous(item, prompt)

        if changed_items:
            await self.async_save()
            for item in changed_items:
                async_dispatcher_send(
                    self.hass,
                    SIGNAL_ITEM_UPDATED.format(item["id"]),
                    item,
                )

    def _async_dispatch_ambiguous(
        self, item: dict[str, Any] | None, prompt: str
    ) -> None:
        """Fire an actionable notification for an ambiguous detection."""
        notify_device = self.entry.data.get(CONF_NOTIFY_DEVICE)
        if not notify_device:
            _LOGGER.debug(
                "Ambiguous detection for %s but no notify device configured", prompt
            )
            return

        item_name = item["name"] if item is not None else prompt
        notification_id = f"fridge_vision_ambiguous_{uuid.uuid4().hex}"
        self._pending[notification_id] = {
            "item_id": item["id"] if item is not None else None,
            "item_name": item_name,
        }

        message = NOTIFICATION_MESSAGE.format(item_name=item_name)
        data = {
            "notification_id": notification_id,
            "actions": [
                {"action": ACTION_INSERT, "title": "Włożyłem"},
                {"action": ACTION_REMOVE, "title": "Wyjąłem"},
                {"action": ACTION_IGNORE, "title": "Ignoruj"},
            ],
        }
        self.hass.async_create_task(
            self.hass.services.async_call(
                "notify",
                notify_device,
                {"title": NOTIFICATION_TITLE, "message": message, "data": data},
            )
        )

    async def _async_notification_action(self, event: Any) -> None:
        """Handle mobile_app_notification_action events."""
        data = event.data or {}
        action = data.get("action")
        notification_id = data.get("notification_id")
        if action not in (ACTION_INSERT, ACTION_REMOVE, ACTION_IGNORE):
            return
        pending = self._pending.pop(notification_id, None)
        if pending is None:
            return
        item_id = pending.get("item_id")
        if action == ACTION_IGNORE or item_id is None:
            return
        item = self.find_item(item_id)
        if item is None:
            return
        delta = 1 if action == ACTION_INSERT else -1
        item["quantity"] = max(0, int(item.get("quantity", 0)) + delta)
        item["last_updated"] = dt_util.utcnow().isoformat()
        await self.async_save()
        async_dispatcher_send(
            self.hass, SIGNAL_ITEM_UPDATED.format(item_id), item
        )

    def find_item(self, item_id: str) -> dict[str, Any] | None:
        for item in self.items:
            if item.get("id") == item_id:
                return item
        return None

    def find_item_by_prompt(self, prompt: str) -> dict[str, Any] | None:
        for item in self.items:
            if item.get("ai_prompt", "").lower() == prompt.lower():
                return item
        return None

    async def async_save(self) -> None:
        """Persist the inventory to HA storage."""
        await self.store.async_save({"items": self.items})

    async def async_reparameterize_model(self) -> None:
        """Send all unique inventory prompts to the add-on (/api/set_classes)."""
        prompts = sorted(
            {
                item.get("ai_prompt")
                for item in self.items
                if item.get("ai_prompt")
            }
        )
        if not prompts:
            return
        try:
            async with async_timeout.timeout(REPARAMETERIZE_TIMEOUT_SECONDS):
                response = await self._session.post(
                    f"{self.ai_service_url}/api/set_classes",
                    json={"classes": prompts},
                )
                response.raise_for_status()
        except Exception as err:
            _LOGGER.warning(
                "Failed to reparameterize model at %s: %s", self.ai_service_url, err
            )
