"""Data coordinator for Fridge Vision Inventory with rich logging."""

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
                _LOGGER.debug("[AI Client] %s -> %s", method, url)
                async with session.request(
                    method,
                    url,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as response:
                    body = await response.text()
                    if response.status >= 400:
                        raise VisionApiError(
                            f"{method} {url} błąd HTTP {response.status}: {body[:200]}"
                        )
            except (asyncio.TimeoutError, aiohttp.ClientError) as err:
                if isinstance(err, _RETRYABLE_ERRORS) and attempt <= len(_RETRY_DELAYS):
                    delay = _RETRY_DELAYS[attempt - 1]
                    _LOGGER.warning(
                        "[AI Client] %s %s błąd (%s); ponawiam za %.1f s (próba %d/%d)",
                        method,
                        url,
                        err,
                        delay,
                        attempt + 1,
                        len(_RETRY_DELAYS) + 1,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise VisionApiError(f"{method} {url} błąd połączenia: {err}") from err
            break
        if not body:
            return {}
        try:
            return json.loads(body)
        except ValueError as err:
            raise VisionApiError(f"{method} {url} odpowiedź nie jest poprawnym JSON: {body[:200]}") from err

    async def async_health(self, timeout: float = HEALTH_TIMEOUT) -> dict[str, Any]:
        return await self._request("GET", "/api/health", timeout=timeout)

    async def async_set_classes(
        self, classes: list[str], timeout: float = SET_CLASSES_TIMEOUT
    ) -> dict[str, Any]:
        return await self._request("POST", "/api/set_classes", {"classes": classes}, timeout=timeout)

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

        self.inventory_entity_id: str = (
            entry.data.get(CONF_INVENTORY_ENTITY)
            or entry.options.get(CONF_INVENTORY_ENTITY)
            or ""
        )
        self._current_prompts: set[str] = set()
        self._prompt_to_item: dict[str, str] = {}
        self._debounce_unsub: Callable[[], None] | None = None

        self.last_detection_summary: str = "Brak detekcji"
        self.last_detection_failed: bool = False
        self.last_detection_details: dict[str, Any] = {}

    async def async_setup(self) -> None:
        _LOGGER.info(
            "=== Inicjalizacja Fridge Vision ===\n* Magazyn: %s\n* Kamera: %s\n* Drzwi: %s\n* Add-on URL: %s",
            self.inventory_entity_id,
            self.entry.data.get(CONF_CAMERA_ENTITY),
            self.entry.data.get(CONF_DOOR_SENSOR_ENTITY),
            self._client.base_url,
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

        if self.inventory_entity_id:
            self._unsubs.append(
                async_track_state_change_event(
                    self.hass,
                    [self.inventory_entity_id],
                    self._handle_inventory_changed,
                )
            )
            # Pobieramy produkty i inicjujemy prompty
            self.hass.async_create_task(self._async_sync_and_reparameterize(force=True))

    def _get_inventory_id(self) -> str | None:
        if not self.inventory_entity_id:
            return None
        state = self.hass.states.get(self.inventory_entity_id)
        if state:
            inv_id = state.attributes.get("inventory_id") or state.attributes.get("id")
            if inv_id:
                return str(inv_id)
        return None

    async def _async_fetch_items(self) -> list[dict[str, Any]]:
        """Pobiera listę produktów z simple_inventory za pomocą usługi get_items."""
        inv_id = self._get_inventory_id()
        data: dict[str, Any] = {}
        if inv_id:
            data["inventory_id"] = inv_id
        else:
            state = self.hass.states.get(self.inventory_entity_id)
            if state and state.name:
                data["inventory_name"] = state.name

        _LOGGER.debug("[simple_inventory] Pobieram produkty przez service 'get_items' z danymi: %s", data)
        try:
            res = await self.hass.services.async_call(
                "simple_inventory",
                "get_items",
                data,
                blocking=True,
                return_response=True,
            )
            if isinstance(res, dict):
                items = res.get("items", [])
                _LOGGER.info("[simple_inventory] Pomyślnie pobrano %d produktów z magazynu.", len(items))
                return items
        except Exception as err:
            _LOGGER.error("[simple_inventory] Błąd podczas wywoływania get_items: %s", err)

        # Fallback do atrybutów jeśli usługa nie zadziała
        state = self.hass.states.get(self.inventory_entity_id)
        if state and "items" in state.attributes:
            items = state.attributes["items"]
            return list(items.values()) if isinstance(items, dict) else items
        return []

    async def _async_sync_and_reparameterize(self, force: bool = False) -> bool:
        """Pobiera produkty z magazynu, parsuje aliasy 'ai:' i wysyła do modelu."""
        items = await self._async_fetch_items()
        new_prompts: set[str] = set()
        new_mapping: dict[str, str] = {}

        for item in items:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip()
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
                        new_prompts.add(prompt)
                        new_mapping[prompt] = name

        _LOGGER.info(
            "[simple_inventory] Znalezione prompty AI: %s (mapowanie na produkty: %s)",
            list(new_prompts),
            new_mapping,
        )

        if not force and new_prompts == self._current_prompts:
            self._prompt_to_item = new_mapping
            _LOGGER.debug("[AI Reparameterize] Prompty się nie zmieniły, pomijam CLIP encoding.")
            return True

        self._current_prompts = new_prompts
        self._prompt_to_item = new_mapping

        if not new_prompts:
            _LOGGER.warning("[AI Reparameterize] Brak promptów 'ai:' w magazynie! Dodaj do aliasu produktu np. 'ai: carton of milk'.")
            return False

        prompts_list = sorted(list(new_prompts))
        _LOGGER.info("[AI Reparameterize] Wysyłam %d promptów do Add-ona AI...", len(prompts_list))
        try:
            await self._client.async_set_classes(prompts_list)
            _LOGGER.info(" SUKCES: Model AI zreparametryzowany klasami: %s", prompts_list)
            return True
        except VisionApiError as err:
            _LOGGER.error(" BŁĄD reparametryzacji modelu w Add-onie: %s", err)
            return False

    @callback
    def _handle_inventory_changed(self, event: Event) -> None:
        new_state: State | None = event.data.get("new_state")
        if new_state is None or new_state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            return
        _LOGGER.info("[simple_inventory] Wykryto zmianę stanu sensora magazynu. Planuję synchronizację promptów...")
        if self._debounce_unsub:
            self._debounce_unsub()
        self._debounce_unsub = async_call_later(
            self.hass, 3.0, self._async_debounced_sync
        )

    async def _async_debounced_sync(self, _now: Any) -> None:
        self._debounce_unsub = None
        await self._async_sync_and_reparameterize(force=False)

    async def async_reparameterize_model(self) -> bool:
        """Ręczne wywołanie usługi."""
        _LOGGER.info("[Ręczne] Wymuszono reparametryzację modelu.")
        return await self._async_sync_and_reparameterize(force=True)

    # ---------- Przechwytywanie klatek kamery ----------

    @callback
    def _handle_door_event(self, event: Event) -> None:
        new_state: State | None = event.data.get("new_state")
        old_state: State | None = event.data.get("old_state")
        if new_state is None or new_state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            return

        _LOGGER.info(
            " Czujnik drzwi zmienił stan: %s -> %s",
            old_state.state if old_state else "?",
            new_state.state,
        )

        if new_state.state == STATE_ON and (old_state is None or old_state.state != STATE_ON):
            _LOGGER.info(" DRZWI OTWARTE: Start nagrywania klatek z kamery %s...", self.entry.data[CONF_CAMERA_ENTITY])
            self._buffer.clear()
            if self._capture_task is None or self._capture_task.done():
                self._capture_task = self.hass.async_create_task(self._async_capture_loop())

        elif new_state.state == STATE_OFF and (old_state is None or old_state.state != STATE_OFF):
            _LOGGER.info(" DRZWI ZAMKNIĘTE: Zatrzymuję nagrywanie. Zebrano %d klatek.", len(self._buffer))
            if self._capture_task is not None:
                self._capture_task.cancel()
                self._capture_task = None
            self._analysis_task = self.hass.async_create_task(self._async_process_door_close())

    async def _async_capture_loop(self) -> None:
        fps = int(self.entry.data.get(CONF_CAPTURE_FPS, DEFAULT_CAPTURE_FPS))
        interval = 1.0 / max(1, fps)
        frames_count = 0
        try:
            while True:
                started = time.monotonic()
                try:
                    snapshot = await camera.async_get_image(
                        self.hass,
                        self.entry.data[CONF_CAMERA_ENTITY],
                        timeout=CAPTURE_SNAPSHOT_TIMEOUT,
                    )
                except Exception as err:
                    _LOGGER.warning(" Błąd pobierania klatki z kamery: %s", err)
                else:
                    data = _extract_snapshot_bytes(snapshot)
                    if data:
                        self._buffer.append((dt_util.utcnow(), data))
                        self._trim_buffer()
                        frames_count += 1
                        if frames_count % 10 == 0:
                            _LOGGER.debug("[Kamera] Przechwycono dotąd %d klatek...", frames_count)
                elapsed = time.monotonic() - started
                await asyncio.sleep(max(0.02, interval - elapsed))
        except asyncio.CancelledError:
            _LOGGER.debug("Pętla przechwytywania klatek anulowana.")

    def _trim_buffer(self) -> None:
        cutoff = dt_util.utcnow() - timedelta(seconds=BUFFER_SECONDS)
        while self._buffer and self._buffer[0][0] < cutoff:
            self._buffer.popleft()
        while len(self._buffer) > MAX_BUFFERED_FRAMES:
            self._buffer.popleft()

    # ---------- Analiza sekwencji ----------

    async def _async_process_door_close(self) -> None:
        buffer = list(self._buffer)
        self._buffer.clear()
        if len(buffer) < MIN_FRAMES_FOR_ANALYSIS:
            _LOGGER.warning(
                " Zbyt mało klatek do analizy (%d klatek, minimum to %d). Pomijam analizę.",
                len(buffer),
                MIN_FRAMES_FOR_ANALYSIS,
            )
            return

        frames_b64 = [base64.b64encode(data).decode("ascii") for _, data in buffer]
        threshold = float(self.entry.data.get(CONF_CONFIDENCE_THRESHOLD, DEFAULT_CONFIDENCE_THRESHOLD))
        vector_dir = str(self.entry.data[CONF_MOTION_VECTOR_INSERT_DIRECTION])

        _LOGGER.info(
            " Wysyłam %d klatek do Add-ona AI (próg pewności: %.2f, wektor: %s)...",
            len(frames_b64),
            threshold,
            vector_dir,
        )

        try:
            result = await self._client.async_analyze(frames_b64, vector_dir, threshold)
            _LOGGER.info(" Odpowiedź z Add-ona AI: %s", json.dumps(result, ensure_ascii=False))
        except VisionApiError as err:
            _LOGGER.error(" Analiza sekwencji w Add-onie nie powiodła się: %s", err)
            self.last_detection_failed = True
            self.last_detection_summary = f"Błąd: {err}"
            self._notify_sensors_updated()
            return

        await self._async_apply_analysis(result)

    async def _async_apply_analysis(self, result: dict[str, Any]) -> None:
        annotated_b64 = result.get("annotated_image_base64")
        if annotated_b64 and self._image_entity is not None:
            with suppress(binascii.Error, ValueError):
                self._image_entity.async_set_detection_image(base64.b64decode(annotated_b64))

        threshold = float(self.entry.data.get(CONF_CONFIDENCE_THRESHOLD, DEFAULT_CONFIDENCE_THRESHOLD))
        detections = result.get("detections", [])
        self.last_detection_details = {
            "timestamp": dt_util.utcnow().isoformat(),
            "detections": detections,
        }

        if not detections:
            _LOGGER.info(" Model AI nie wykrył żadnego ruchu produktów powyżej progu pewności.")
            self.last_detection_summary = "Brak ruchu"
            self.last_detection_failed = False
            self._notify_sensors_updated()
            return

        inv_id = self._get_inventory_id()
        detected_summaries: list[str] = []
        has_failure = False

        for detection in detections:
            action = str(detection.get("action", ""))
            prompt = str(detection.get("ai_prompt", "")).strip()
            confidence = float(detection.get("confidence", 0.0))

            _LOGGER.info(
                "-> Detekcja: prompt='%s', akcja='%s', pewność=%.2f (próg: %.2f)",
                prompt,
                action,
                confidence,
                threshold,
            )

            if confidence < threshold:
                _LOGGER.info("   Pomijam detekcję (pewność %.2f poniżej progu %.2f)", confidence, threshold)
                continue

            item_name = self._prompt_to_item.get(prompt)

            if action in (DETECTION_INSERTED, DETECTION_REMOVED):
                if not item_name:
                    has_failure = True
                    _LOGGER.warning("   Wykryto prompt '%s', ale brak przypisanego produktu w simple_inventory!", prompt)
                    detected_summaries.append(f"Nieznany: {prompt}")
                else:
                    service_name = "increment_item" if action == DETECTION_INSERTED else "decrement_item"
                    action_label = "Włożono" if action == DETECTION_INSERTED else "Wyjęto"
                    _LOGGER.info("   SUKCES: %s -> %s. Wołam simple_inventory.%s", action_label, item_name, service_name)
                    detected_summaries.append(f"{item_name} ({action_label})")
                    await self._async_call_simple_inventory(service_name, item_name, inv_id)

            elif action == DETECTION_AMBIGUOUS:
                has_failure = True
                _LOGGER.warning("   Ruch niejednoznaczny dla promptu '%s'", prompt)
                detected_summaries.append(f"Niejednoznaczne: {item_name or prompt}")

        self.last_detection_failed = has_failure
        self.last_detection_summary = ", ".join(detected_summaries) if detected_summaries else "Brak zmian"
        self._notify_sensors_updated()

    async def _async_call_simple_inventory(self, service: str, item_name: str, inventory_id: str | None) -> None:
        data: dict[str, Any] = {"name": item_name, "amount": 1}
        if inventory_id:
            data["inventory_id"] = inventory_id
        try:
            _LOGGER.info("[simple_inventory] Wywołuję usługę %s z parametrami: %s", service, data)
            await self.hass.services.async_call("simple_inventory", service, data, blocking=True)
        except Exception as err:
            _LOGGER.error("[simple_inventory] Błąd podczas wywoływania usługi %s: %s", service, err)
            self.last_detection_failed = True

    # ---------- Reszta pomocnicza ----------

    @callback
    def _handle_notification_action(self, event: Event) -> None:
        pass

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
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        if self._capture_task and not self._capture_task.done():
            self._capture_task.cancel()
        if self._analysis_task and not self._analysis_task.done():
            self._analysis_task.cancel()
        await self._client.aclose()