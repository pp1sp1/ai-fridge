"""FastAPI service for the Fridge Vision AI add-on.

Endpoints:
* ``GET  /api/health``           - service and model status
* ``POST /api/set_classes``      - zero-shot re-parameterization with prompts
* ``POST /api/analyze_sequence`` - classify a door-open frame sequence

Add-on options are read from ``/data/options.json`` (written by the
Home Assistant Supervisor); sane defaults are used when the file is
absent so the service also runs outside of the Supervisor for testing.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import os
import time
from collections import Counter
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Literal, Sequence

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from .model import (
    DEFAULT_IMAGE_SIZE,
    DEFAULT_MODEL_NAME,
    SUPPORTED_MODELS,
    ModelNotLoadedError,
    VisionModel,
)
from .tracker import (
    ACTION_AMBIGUOUS,
    ACTION_INSERTED,
    ACTION_REMOVED,
    DEFAULT_MOTION_THRESHOLD,
    Detection,
    CentroidTracker,
    analyze_tracks,
    annotate_frame,
    decode_jpeg,
    encode_jpeg,
)

LOGGER = logging.getLogger("fridge_vision_ai")

OPTIONS_FILE = Path(os.environ.get("FRIDGE_VISION_OPTIONS_FILE", "/data/options.json"))

MAX_FRAMES = 150
MAX_TOTAL_BASE64_BYTES = 160 * 1024 * 1024


@dataclass(slots=True, frozen=True)
class Settings:
    """Static settings derived from the add-on options."""

    model_name: str
    image_size: int
    log_level: str


def _load_settings() -> Settings:
    """Read add-on options; fall back to defaults outside of Supervisor."""
    options: dict[str, Any] = {}
    try:
        if OPTIONS_FILE.is_file():
            options = json.loads(OPTIONS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        LOGGER.warning("Could not read options file %s: %s", OPTIONS_FILE, err)

    model_name = str(options.get("model", DEFAULT_MODEL_NAME))
    if model_name not in SUPPORTED_MODELS:
        LOGGER.warning(
            "Unsupported model %r in options, falling back to %s",
            model_name,
            DEFAULT_MODEL_NAME,
        )
        model_name = DEFAULT_MODEL_NAME

    try:
        image_size = int(options.get("image_size", DEFAULT_IMAGE_SIZE))
    except (TypeError, ValueError):
        image_size = DEFAULT_IMAGE_SIZE
    image_size = min(max(image_size, 320), 1280)

    log_level = str(options.get("log_level", "info")).upper()
    return Settings(model_name=model_name, image_size=image_size, log_level=log_level)


SETTINGS = _load_settings()

logging.basicConfig(
    level=getattr(logging, SETTINGS.log_level, logging.INFO),
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)

MODEL = VisionModel(model_name=SETTINGS.model_name, image_size=SETTINGS.image_size)


class _FrameDecodeError(Exception):
    """Raised when a frame cannot be decoded; carries the frame index."""

    def __init__(self, index: int, reason: str) -> None:
        super().__init__(f"frame {index}: {reason}")
        self.index = index
        self.reason = reason


class SetClassesRequest(BaseModel):
    """Payload for ``POST /api/set_classes``."""

    classes: list[str] = Field(min_length=1, max_length=64)

    @field_validator("classes")
    @classmethod
    def _validate_classes(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value if item and item.strip()]
        if not cleaned:
            raise ValueError("at least one non-empty class prompt is required")
        return cleaned


class SetClassesResponse(BaseModel):
    """Response for ``POST /api/set_classes``."""

    classes: list[str]


class AnalyzeRequest(BaseModel):
    """Payload for ``POST /api/analyze_sequence``."""

    frames: list[str] = Field(min_length=1, max_length=MAX_FRAMES)
    vector_direction: Literal["top_to_bottom", "bottom_to_top"] = "top_to_bottom"
    confidence_threshold: float = Field(default=0.25, ge=0.01, le=0.95)
    motion_threshold: float = Field(
        default=DEFAULT_MOTION_THRESHOLD, ge=0.005, le=0.5
    )


class DetectionOut(BaseModel):
    """One classified motion track."""

    ai_prompt: str
    action: str
    confidence: float
    track_length_frames: int
    delta_y_px: float = 0.0


class AnalyzeResponse(BaseModel):
    """Response for ``POST /api/analyze_sequence``."""

    detections: list[DetectionOut]
    annotated_image_base64: str


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Load the model once at startup, in a worker thread."""
    model: VisionModel = app.state.model
    if not model.ready:
        await asyncio.to_thread(model.load)
    LOGGER.info(
        "Fridge Vision AI ready (model=%s, classes=%s)",
        model.model_name,
        list(model.classes),
    )
    yield


def _run_pipeline(
    model: VisionModel,
    frames_base64: Sequence[str],
    confidence_threshold: float,
    vector_direction: str,
    motion_threshold: float,
) -> tuple[list[DetectionOut], bytes]:
    """Decode, infer and track the whole sequence (runs in a worker thread)."""
    started = time.perf_counter()
    total = len(frames_base64)
    LOGGER.info(
        "Analysis started: %d frame(s), conf>=%.2f, motion>=%.1f%% of frame "
        "height, direction=%s",
        total,
        confidence_threshold,
        motion_threshold * 100.0,
        vector_direction,
    )
    tracker = CentroidTracker()
    frame_height: int | None = None
    for index, frame_b64 in enumerate(frames_base64):
        try:
            raw = base64.b64decode(frame_b64, validate=False)
        except (binascii.Error, ValueError) as err:
            raise _FrameDecodeError(index, "payload is not valid base64") from err
        frame = decode_jpeg(raw)
        if frame is None:
            raise _FrameDecodeError(index, "payload is not a decodable JPEG image")
        height, width = frame.shape[:2]
        if frame_height is None:
            frame_height = height
        raw_boxes = model.infer(frame, confidence_threshold)
        if raw_boxes:
            boxes_summary = ", ".join(
                f"'{box.prompt}' {box.confidence:.2f}" for box in raw_boxes
            )
            LOGGER.info(
                "  frame %d/%d: %d detection(s): %s",
                index + 1,
                total,
                len(raw_boxes),
                boxes_summary,
            )
        else:
            LOGGER.info("  frame %d/%d: no detections", index + 1, total)
        detections = [
            Detection(
                frame_index=index,
                bbox=box.bbox,
                prompt=box.prompt,
                confidence=box.confidence,
            )
            for box in raw_boxes
        ]
        tracker.update(index, detections, width, height)

    assert frame_height is not None
    track_results, best_index = analyze_tracks(
        tracker.tracks, vector_direction, motion_threshold, frame_height
    )

    for result in track_results:
        LOGGER.info(
            "  track #%d '%s': %s, best confidence %.2f, seen in %d frame(s), "
            "dy=%+.1f px",
            result.track_id,
            result.ai_prompt,
            result.action,
            result.confidence,
            result.track_length_frames,
            result.delta_y,
        )
    action_counts = Counter(result.action for result in track_results)
    LOGGER.info(
        "Analysis done: %d track(s) -> %s=%d, %s=%d, %s=%d; best frame "
        "%d/%d; took %.1f s",
        len(track_results),
        ACTION_INSERTED,
        action_counts.get(ACTION_INSERTED, 0),
        ACTION_REMOVED,
        action_counts.get(ACTION_REMOVED, 0),
        ACTION_AMBIGUOUS,
        action_counts.get(ACTION_AMBIGUOUS, 0),
        best_index + 1,
        total,
        time.perf_counter() - started,
    )

    try:
        best_raw = base64.b64decode(frames_base64[best_index], validate=False)
    except (binascii.Error, ValueError) as err:
        raise _FrameDecodeError(best_index, "best frame is not valid base64") from err
    best_frame = decode_jpeg(best_raw)
    if best_frame is None:
        raise _FrameDecodeError(best_index, "best frame is not a decodable JPEG image")
    annotated = annotate_frame(best_frame, track_results)

    outputs = [
        DetectionOut(
            ai_prompt=result.ai_prompt,
            action=result.action,
            confidence=result.confidence,
            track_length_frames=result.track_length_frames,
            delta_y_px=result.delta_y,
        )
        for result in track_results
    ]
    return outputs, encode_jpeg(annotated)


def create_app(vision_model: VisionModel | None = None) -> FastAPI:
    """Create the FastAPI application; ``vision_model`` allows test injection."""
    application = FastAPI(
        title="Fridge Vision AI", version="1.3.1", lifespan=_lifespan
    )
    application.state.model = vision_model if vision_model is not None else MODEL
    application.state.settings = SETTINGS

    @application.get("/")
    async def root() -> dict[str, str]:
        """Service banner."""
        return {
            "service": "Fridge Vision AI",
            "endpoints": "/api/health, /api/set_classes, /api/analyze_sequence",
        }

    @application.get("/api/health")
    async def health(request: Request) -> dict[str, Any]:
        """Health endpoint used by the Supervisor watchdog and the integration."""
        model: VisionModel = request.app.state.model
        return {
            "status": "ok",
            "model": model.model_name,
            "model_ready": model.ready,
            "classes": list(model.classes),
            "device": "cpu",
        }

    @application.post("/api/set_classes", response_model=SetClassesResponse)
    async def set_classes(
        payload: SetClassesRequest, request: Request
    ) -> SetClassesResponse:
        """Re-parameterize the model with zero-shot text prompts."""
        model: VisionModel = request.app.state.model
        if not model.ready:
            LOGGER.warning("set_classes rejected: model is still loading")
            raise HTTPException(
                status_code=503, detail="model is still loading; retry shortly"
            )
        try:
            classes = await asyncio.to_thread(model.set_classes, payload.classes)
        except ModelNotLoadedError as err:
            raise HTTPException(status_code=503, detail=str(err)) from err
        except ValueError as err:
            raise HTTPException(status_code=422, detail=str(err)) from err
        return SetClassesResponse(classes=list(classes))

    @application.post("/api/analyze_sequence", response_model=AnalyzeResponse)
    async def analyze_sequence(
        payload: AnalyzeRequest, request: Request
    ) -> AnalyzeResponse:
        """Classify motions in a buffered door-open sequence."""
        model: VisionModel = request.app.state.model
        if not model.ready:
            raise HTTPException(
                status_code=503, detail="model is still loading; retry shortly"
            )
        total_bytes = sum(len(frame) for frame in payload.frames)
        if total_bytes > MAX_TOTAL_BASE64_BYTES:
            raise HTTPException(status_code=413, detail="frame payload too large")
        LOGGER.info(
            "analyze_sequence request: %d frame(s), %.1f MB payload",
            len(payload.frames),
            total_bytes / 1e6,
        )
        try:
            detections, annotated = await asyncio.to_thread(
                _run_pipeline,
                model,
                payload.frames,
                payload.confidence_threshold,
                payload.vector_direction,
                payload.motion_threshold,
            )
        except _FrameDecodeError as err:
            LOGGER.warning("analyze_sequence: bad frame rejected: %s", err)
            raise HTTPException(status_code=400, detail=str(err)) from err
        except ModelNotLoadedError as err:
            LOGGER.warning("analyze_sequence rejected: model is still loading")
            raise HTTPException(status_code=503, detail=str(err)) from err
        return AnalyzeResponse(
            detections=detections,
            annotated_image_base64=base64.b64encode(annotated).decode("ascii"),
        )

    return application


app = create_app()
