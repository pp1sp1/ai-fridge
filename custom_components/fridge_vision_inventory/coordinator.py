"""Data coordinator for Fridge Vision Inventory with scaling, motion filter & visual debug."""

from __future__ import annotations

import asyncio
import base64
import binascii
from collections import deque
from contextlib import suppress
from datetime import datetime, timedelta
from io import BytesIO
import json
import logging
import os
import shutil
import time
from typing import Any, Callable

import aiohttp
from PIL import Image, ImageDraw

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

# ==================== USTAWIENIA DEBUGOWANIA ====================
DEBUG_SAVE_FRAMES = True         # True = zapisuje klatki z opisem do /config/www/fridge_debug/
DEBUG_MAX_SESSIONS = 5           # Ile ostatnich sesji otwarcia drzwi trzymać na dysku
# ================================================================

# Parametry skalowania i antyszumowego filtra ruchu
TARGET_MAX_DIMENSION = 640       # Skalowanie dłuższego boku pod YOLO-World (px)
JPEG_QUALITY = 80                # Jakość kompresji JPEG wysyłanych klatek
MOTION_GRID_SIZE = (64, 48)      # Siatka uśredniająca wycinająca szum ISO/JPEG
NOISE_PIXEL_THRESHOLD = 18       # Martwa strefa szumu na pikselu (0-255) po korekcji światła
MIN_MOTION_AREA_RATIO = 0.015    # Min. 1.5% powierzchni kadru musi się poruszyć
MOTION_CONTEXT_FRAMES = 1        # Ile sąsiednich klatek zachować przed i po ruchu

_RETRYABLE_ERRORS: tuple[type[Exception], ...] = (
    aiohttp.ClientConnectorError,
    aiohttp.ClientOSError,
    aiohttp.ServerDisconnectedError,
)
_RETRY_DELAYS: tuple[float, ...] = (0.75, 2.0)


def _cleanup_old_debug_sessions(base_dir: str, max_sessions: int) -> None:
    """Keep only the latest N debug sessions to save disk space."""
    try:
        if not os.path.isdir(base_dir):
            return
        entries = [
            os.path.join(base_dir, d)
            for d in os.listdir(base_dir)
            if os.path.isdir(os.path.join(base_dir, d)) and d.startswith("sesja_")
        ]
        entries.sort(key=os.path.getmtime)
        while len(entries) > max_sessions:
            oldest = entries.pop(0)
            shutil.rmtree(oldest, ignore_errors=True)
    except Exception as err:
        _LOGGER.debug("Błąd czyszczenia starych sesji debug: %s", err)


def _preprocess_frames_sync(
    buffered_items: list[tuple[datetime, bytes]],
    debug_base_dir: str | None = None,
) -> tuple[list[str], dict[str, Any]]:
    """Filter out static/noisy frames, scale moving frames to 640px, and save debug images."""
    total_in = len(buffered_items)
    raw_bytes_total = sum(len(b) for _, b in buffered_items)

    decoded: list[Image.Image] = []
    timestamps: list[datetime] = []
    grids: list[bytes] = []

    box_filter = getattr(Image, "Resampling", Image).BOX
    bilinear_filter = getattr(Image, "Resampling", Image).BILINEAR

    for ts, data in buffered_items:
        try:
            with Image.open(BytesIO(data)) as im:
                rgb = im.convert("RGB")
                small_gray = rgb.convert("L").resize(MOTION_GRID_SIZE, box_filter)
                decoded.append(rgb)
                timestamps.append(ts)
                grids.append(small_gray.tobytes())
        except Exception as err:
            _LOGGER.debug("Pominięto uszkodzoną klatkę JPEG: %s", err)

    valid_count = len(decoded)
    if valid_count < MIN_FRAMES_FOR_ANALYSIS:
        return [], {
            "total_in": total_in,
            "valid": valid_count,
            "kept": 0,
            "raw_kb": round(raw_bytes_total / 1024, 1),
            "out_kb": 0.0,
            "debug_dir": None,
        }

    # Wykrywanie ruchu odporne na szum i oświetlenie
    grid_len = MOTION_GRID_SIZE[0] * MOTION_GRID_SIZE[1]
    motion_flags = [False] * valid_count
    motion_ratios = [0.0] * valid_count

    for idx in range(1, valid_count):
        prev_b = grids[idx - 1]
        curr_b = grids[idx]
        shift = (sum(curr_b) - sum(prev_b)) / grid_len
        changed_cells = sum(
            1
            for p1, p2 in zip(prev_b, curr_b)
            if abs((p2 - p1) - shift) > NOISE_PIXEL_THRESHOLD
        )
        ratio = changed_cells / grid_len
        motion_ratios[idx] = ratio
        if ratio >= MIN_MOTION_AREA_RATIO:
            motion_flags[idx] = True
            motion_flags[idx - 1] = True

    # Dylatacja czasowa: margines kontekstu wokół ruchu
    keep_indices: set[int] = set()
    for idx, has_motion in enumerate(motion_flags):
        if has_motion:
            start_i = max(0, idx - MOTION_CONTEXT_FRAMES)
            end_i = min(valid_count - 1, idx + MOTION_CONTEXT_FRAMES)
            for k in range(start_i, end_i + 1):
                keep_indices.add(k)

    # Przygotowanie katalogu sesji debugowania
    session_dir: str | None = None
    if DEBUG_SAVE_FRAMES and debug_base_dir:
        session_name = f"sesja_{dt_util.now().strftime('%Y%m%d_%H%M%S')}"
        session_dir = os.path.join(debug_base_dir, session_name)
        try:
            os.makedirs(session_dir, exist_ok=True)
            _cleanup_old_debug_sessions(debug_base_dir, DEBUG_MAX_SESSIONS)
        except Exception as err:
            _LOGGER.warning("Nie udało się utworzyć katalogu debug: %s", err)
            session_dir = None

    frames_b64: list[str] = []
    out_bytes_total = 0

    for idx in range(valid_count):
        img = decoded[idx]
        ts = timestamps[idx]
        is_kept = idx in keep_indices
        ratio = motion_ratios[idx]

        # Zapis debug z nadrukiem informacji na zdjęciu
        if session_dir is not None:
            try:
                debug_img = img.copy()
                draw = ImageDraw.Draw(debug_img)
                w, h = debug_img.size

                banner_h = max(24, int(h * 0.045))
                status_txt = "RUCH [WYBRANA]" if is_kept else "STATYCZNA [ODRZUCONA]"
                text = (
                    f"#{idx + 1:03d}/{valid_count:03d} | "
                    f"{ts.strftime('%H:%M:%S.%f')[:-3]} | "
                    f"{status_txt} | "
                    f"delta: {ratio * 100:.1f}%"
                )

                # Czarny pasek i tekst na górze zdjęcia
                draw.rectangle([(0, 0), (w, banner_h)], fill=(0, 0, 0))
                draw.text(
                    (8, int(banner_h * 0.15)),
                    text,
                    fill=(0, 255, 128) if is_kept else (200, 200, 200),
                )

                file_status = "RUCH_WYBRANA" if is_kept else "STATYCZNA_ODRZUCONA"
                fname = f"klatka_{idx + 1:03d}_{ts.strftime('%H%M%S_%f')[:-3]}_{file_status}.jpg"
                debug_img.save(os.path.join(session_dir, fname), "JPEG", quality=85)
                debug_img.close()
            except Exception as err:
                _LOGGER.debug("Błąd zapisu klatki debug: %s", err)

        # Skalowanie i wysyłka do AI tylko dla klatek z ruchem
        if is_kept:
            w, h = img.size
            if max(w, h) > TARGET_MAX_DIMENSION:
                img.thumbnail((TARGET_MAX_DIMENSION, TARGET_MAX_DIMENSION), bilinear_filter)
            buf = BytesIO()
            img.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=False)
            jpeg_bytes = buf.getvalue()
            out_bytes_total += len(jpeg_bytes)
            frames_b64.append(base64.b64encode(jpeg_bytes).decode("ascii"))

    for img in decoded:
        img.close()

    stats = {
        "total_in": total_in,
        "valid": valid_count,
        "kept": len(frames_b64),
        "raw_kb": round(raw_bytes_total / 1024, 1),
        "out_kb": round(out_bytes_total / 1024, 1),
        "debug_dir": session_dir,
    }
    return frames_b64, stats


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
            raise VisionApiError(
                f"{method} {url} odpowiedź nie jest poprawnym JSON: {body[:200]}"
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
            "=== Inicjalizacja Fridge Vision ===\n* Magazyn: %s\n* Kamera: %s\n* Drzwi: %s\n* Debug klatek: %s\n* Add-on URL: %s",
            self.inventory_entity_id,
            self.entry.data.get(CONF_CAMERA_ENTITY),
            self.entry.data.get(CONF_DOOR_SENSOR_ENTITY),
            "WŁĄCZONY" if DEBUG_SAVE_FRAMES else "WYŁĄCZONY",
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
        inv_id = self._get_inventory_id()
        data: dict[str, Any] = {}
        if inv_id:
            data["inventory_id"] = inv_id
        else:
            state = self.hass.states.get(self.inventory_entity_id)
            if state and state.name:
                data["inventory_name"] = state.name

        _LOGGER.debug("[simple_inventory] Pobieram produkty przez service 'get_items': %s", data)
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
                _LOGGER.info("[simple_inventory] Pomyślnie pobrano %d produktów.", len(items))
                return items
        except Exception as err:
            _LOGGER.error("[simple_inventory] Błąd podczas wywoływania get_items: %s", err)

        state = self.hass.states.get(self.inventory_entity_id)
        if state and "items" in state.attributes:
            items = state.attributes["items"]
            return list(items.values()) if isinstance(items, dict) else items
        return []

    async def _async_sync_and_reparameterize(self, force: bool = False) -> bool:
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
            "[simple_inventory] Znalezione prompty AI: %s (mapowanie: %s)",
            list(new_prompts),
            new_mapping,
        )

        if not force and new_prompts == self._current_prompts:
            self._prompt_to_item = new_mapping
            _LOGGER.debug("[AI Reparameterize] Prompty bez zmian, pomijam kodowanie CLIP.")
            return True

        self._current_prompts = new_prompts
        self._prompt_to_item = new_mapping

        if not new_prompts:
            _LOGGER.warning("[AI Reparameterize] Brak promptów 'ai:' w magazynie!")
            return False

        prompts_list = sorted(list(new_prompts))
        _LOGGER.info("[AI Reparameterize] Wysyłam %d promptów do Add-ona AI...", len(prompts_list))
        try:
            await self._client.async_set_classes(prompts_list)
            _LOGGER.info("SUKCES: Model AI zreparametryzowany klasami: %s", prompts_list)
            return True
        except VisionApiError as err:
            _LOGGER.error("BŁĄD reparametryzacji modelu w Add-onie: %s", err)
            return False

    @callback
    def _handle_inventory_changed(self, event: Event) -> None:
        new_state: State | None = event.data.get("new_state")
        if new_state is None or new_state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            return
        _LOGGER.info("[simple_inventory] Wykryto zmianę stanu magazynu. Planuję synchronizację promptów...")
        if self._debounce_unsub:
            self._debounce_unsub()
        self._debounce_unsub = async_call_later(
            self.hass, 3.0, self._async_debounced_sync
        )

    async def _async_debounced_sync(self, _now: Any) -> None:
        self._debounce_unsub = None
        await self._async_sync_and_reparameterize(force=False)

    async def async_reparameterize_model(self) -> bool:
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
            "Czujnik drzwi: %s -> %s",
            old_state.state if old_state else "?",
            new_state.state,
        )

        if new_state.state == STATE_ON and (
            old_state is None or old_state.state != STATE_ON
        ):
            _LOGGER.info(
                "DRZWI OTWARTE: Start nagrywania klatek z kamery %s...",
                self.entry.data[CONF_CAMERA_ENTITY],
            )
            self._buffer.clear()
            if self._capture_task is None or self._capture_task.done():
                self._capture_task = self.hass.async_create_task(
                    self._async_capture_loop()
                )

        elif new_state.state == STATE_OFF and (
            old_state is None or old_state.state != STATE_OFF
        ):
            _LOGGER.info(
                "DRZWI ZAMKNIĘTE: Zatrzymuję nagrywanie. Zebrano %d surowych klatek.",
                len(self._buffer),
            )
            if self._capture_task is not None:
                self._capture_task.cancel()
                self._capture_task = None
            self._analysis_task = self.hass.async_create_task(
                self._async_process_door_close()
            )

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
                    _LOGGER.warning("Błąd pobierania klatki z kamery: %s", err)
                else:
                    data = _extract_snapshot_bytes(snapshot)
                    if data:
                        self._buffer.append((dt_util.utcnow(), data))
                        self._trim_buffer()
                        frames_count += 1
                        if frames_count % 10 == 0:
                            _LOGGER.debug(
                                "[Kamera] Przechwycono dotąd %d klatek...", frames_count
                            )
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
        buffered_items = list(self._buffer)
        self._buffer.clear()

        if len(buffered_items) < MIN_FRAMES_FOR_ANALYSIS:
            _LOGGER.warning(
                "Zbyt mało klatek do analizy (%d klatek, minimum to %d). Pomijam analizę.",
                len(buffered_items),
                MIN_FRAMES_FOR_ANALYSIS,
            )
            return

        debug_dir = self.hass.config.path("www", "fridge_debug") if DEBUG_SAVE_FRAMES else None

        t0 = time.monotonic()
        frames_b64, stats = await self.hass.async_add_executor_job(
            _preprocess_frames_sync, buffered_items, debug_dir
        )
        prep_ms = (time.monotonic() - t0) * 1000

        if stats.get("debug_dir"):
            _LOGGER.info(
                "📸 [DEBUG] Zapisano klatki z nadrukiem w: %s (dostępne przez /local/fridge_debug/)",
                stats["debug_dir"],
            )

        _LOGGER.info(
            "[Filtr ruchu + Skalowanie %.0f ms] Klatki: %d -> %d aktywnych | Rozmiar danych: %.1f KB -> %.1f KB",
            prep_ms,
            stats["total_in"],
            stats["kept"],
            stats["raw_kb"],
            stats["out_kb"],
        )

        if len(frames_b64) < MIN_FRAMES_FOR_ANALYSIS:
            _LOGGER.info("Brak realnego ruchu powyżej progu szumu — pomijam wysyłanie do AI.")
            self.last_detection_summary = "Brak ruchu"
            self.last_detection_failed = False
            self.last_detection_details = {
                "timestamp": dt_util.utcnow().isoformat(),
                "filtered_stats": stats,
                "detections": [],
            }
            self._notify_sensors_updated()
            return

        threshold = float(
            self.entry.data.get(
                CONF_CONFIDENCE_THRESHOLD, DEFAULT_CONFIDENCE_THRESHOLD
            )
        )
        vector_dir = str(self.entry.data[CONF_MOTION_VECTOR_INSERT_DIRECTION])

        _LOGGER.info(
            "Wysyłam %d przeskalowanych klatek z ruchem do Add-ona AI (próg: %.2f, wektor: %s)...",
            len(frames_b64),
            threshold,
            vector_dir,
        )

        t_ai = time.monotonic()
        try:
            result = await self._client.async_analyze(frames_b64, vector_dir, threshold)
            ai_sec = time.monotonic() - t_ai
            _LOGGER.info(
                "Odpowiedź z Add-ona AI w %.2f s: %s",
                ai_sec,
                json.dumps(
                    {k: v for k, v in result.items() if k != "annotated_image_base64"},
                    ensure_ascii=False,
                ),
            )
        except VisionApiError as err:
            _LOGGER.error("Analiza sekwencji w Add-onie nie powiodła się: %s", err)
            self.last_detection_failed = True
            self.last_detection_summary = f"Błąd: {err}"
            self._notify_sensors_updated()
            return

        await self._async_apply_analysis(result, stats)

    async def _async_apply_analysis(
        self, result: dict[str, Any], stats: dict[str, Any] | None = None
    ) -> None:
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
            "filtered_stats": stats or {},
            "detections": detections,
        }

        if not detections:
            _LOGGER.info("Model AI nie wykrył żadnego ruchu produktów powyżej progu pewności.")
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
                _LOGGER.info(
                    "   Pomijam detekcję (pewność %.2f poniżej progu %.2f)",
                    confidence,
                    threshold,
                )
                continue

            item_name = self._prompt_to_item.get(prompt)

            if action in (DETECTION_INSERTED, DETECTION_REMOVED):
                if not item_name:
                    has_failure = True
                    _LOGGER.warning(
                        "   Wykryto prompt '%s', ale brak przypisanego produktu w simple_inventory!",
                        prompt,
                    )
                    detected_summaries.append(f"Nieznany: {prompt}")
                else:
                    service_name = (
                        "increment_item"
                        if action == DETECTION_INSERTED
                        else "decrement_item"
                    )
                    action_label = (
                        "Włożono" if action == DETECTION_INSERTED else "Wyjęto"
                    )
                    _LOGGER.info(
                        "   SUKCES: %s -> %s. Wołam simple_inventory.%s",
                        action_label,
                        item_name,
                        service_name,
                    )
                    detected_summaries.append(f"{item_name} ({action_label})")
                    await self._async_call_simple_inventory(
                        service_name, item_name, inv_id
                    )

            elif action == DETECTION_AMBIGUOUS:
                has_failure = True
                _LOGGER.warning("   Ruch niejednoznaczny dla promptu '%s'", prompt)
                detected_summaries.append(f"Niejednoznaczne: {item_name or prompt}")

        self.last_detection_failed = has_failure
        self.last_detection_summary = (
            ", ".join(detected_summaries) if detected_summaries else "Brak zmian"
        )
        self._notify_sensors_updated()

    async def _async_call_simple_inventory(
        self, service: str, item_name: str, inventory_id: str | None
    ) -> None:
        data: dict[str, Any] = {"name": item_name, "amount": 1}
        if inventory_id:
            data["inventory_id"] = inventory_id
        try:
            _LOGGER.info(
                "[simple_inventory] Wywołuję usługę %s z parametrami: %s", service, data
            )
            await self.hass.services.async_call(
                "simple_inventory", service, data, blocking=True
            )
        except Exception as err:
            _LOGGER.error(
                "[simple_inventory] Błąd podczas wywoływania usługi %s: %s",
                service,
                err,
            )
            self.last_detection_failed = True

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