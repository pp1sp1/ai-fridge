"""YOLO-World v2 model lifecycle for the Fridge Vision AI add-on.

The model is loaded exactly once at startup and re-parameterized in place
with zero-shot text prompts (CLIP text encoder) via ``set_classes``. All
access is serialized through a lock because ``set_classes`` mutates the
detection head while inference reads it.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import numpy as np

LOGGER = logging.getLogger(__name__)

DEFAULT_MODEL_NAME = "yolov8n-worldv2.pt"
SUPPORTED_MODELS: tuple[str, ...] = ("yolov8n-worldv2.pt", "yolov8s-worldv2.pt")
DEFAULT_IMAGE_SIZE = 640
DEVICE = "cpu"

# Persistent add-on storage (mount point of the add-on data volume).
DATA_DIR = Path(os.environ.get("FRIDGE_VISION_DATA_DIR", "/data"))

# Any valid *-worldv2.pt weights file is far larger than this; smaller files
# are truncated leftovers of an interrupted download.
_MIN_VALID_WEIGHTS_BYTES = 10 * 1024 * 1024

_MAX_PROMPT_CHARS = 60


class ModelNotLoadedError(RuntimeError):
    """Raised when inference is requested before the model finished loading."""


@dataclass(slots=True, frozen=True)
class RawBox:
    """A single raw detection returned by the model."""

    prompt: str
    confidence: float
    bbox: tuple[float, float, float, float]


class VisionModel:
    """Thread-safe wrapper around a YOLO-World v2 model."""

    def __init__(
        self,
        model_name: str = DEFAULT_MODEL_NAME,
        image_size: int = DEFAULT_IMAGE_SIZE,
    ) -> None:
        if model_name not in SUPPORTED_MODELS:
            raise ValueError(
                f"Unsupported model {model_name!r}; choose one of {SUPPORTED_MODELS}"
            )
        self._model_name = model_name
        self._image_size = int(image_size)
        self._model: Any | None = None
        self._classes: tuple[str, ...] = ()
        self._lock = threading.Lock()

    @property
    def model_name(self) -> str:
        """Name of the configured weights file."""
        return self._model_name

    @property
    def classes(self) -> tuple[str, ...]:
        """Active zero-shot class prompts."""
        return self._classes

    @property
    def ready(self) -> bool:
        """True once the weights are loaded and inference is possible."""
        return self._model is not None

    def _resolve_weights_path(self) -> Path:
        """Return the path the weights file lives at (or will download to)."""
        for folder in (DATA_DIR, Path.cwd()):
            candidate = folder / self._model_name
            if candidate.is_file():
                return candidate
        target = DATA_DIR / self._model_name
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            probe = DATA_DIR / ".write_probe"
            probe.touch()
            probe.unlink()
        except OSError:
            target = Path.cwd() / self._model_name
        return target

    def load(self) -> None:
        """Load the model once; weights are auto-downloaded on first start."""
        with self._lock:
            if self._model is not None:
                return
            try:
                # Imported lazily so importing this module stays cheap.
                from ultralytics import YOLO
            except ImportError as exc:
                raise RuntimeError("The ultralytics package is not installed") from exc

            weights_path = self._resolve_weights_path()
            if weights_path.is_file() and weights_path.stat().st_size < _MIN_VALID_WEIGHTS_BYTES:
                LOGGER.warning("Removing truncated weights file %s", weights_path)
                weights_path.unlink()

            LOGGER.info(
                "Loading %s from %s (first start downloads the weights)",
                self._model_name,
                weights_path,
            )
            try:
                self._model = YOLO(str(weights_path))
            except Exception as exc:  # noqa: BLE001 - surface a clear startup error
                raise RuntimeError(
                    f"Could not load YOLO-World weights {weights_path}: {exc}. "
                    "Ensure the add-on has internet access on first start, then restart it."
                ) from exc

            try:
                import torch

                torch.set_num_threads(max(1, min(2, os.cpu_count() or 1)))
                torch.set_grad_enabled(False)
            except ImportError:  # pragma: no cover - torch is always installed
                pass
            LOGGER.info("Model %s loaded on %s", self._model_name, DEVICE)

    def set_classes(self, classes: Sequence[str]) -> tuple[str, ...]:
        """Re-parameterize the detection head with zero-shot text prompts."""
        cleaned: list[str] = []
        for entry in classes:
            text = str(entry).strip()
            if text and text not in cleaned:
                if len(text) > _MAX_PROMPT_CHARS:
                    raise ValueError(
                        f"Prompt {text!r} exceeds {_MAX_PROMPT_CHARS} characters"
                    )
                cleaned.append(text)
        if not cleaned:
            raise ValueError("At least one non-empty class prompt is required")
        with self._lock:
            if self._model is None:
                raise ModelNotLoadedError("Model is not loaded yet")
            if not hasattr(self._model, "set_classes"):
                raise RuntimeError(
                    "The loaded model does not support zero-shot classes; "
                    "use a *-worldv2.pt model"
                )
            self._model.set_classes(cleaned)
            self._classes = tuple(cleaned)
        LOGGER.info(
            "Model re-parameterized with %d class(es): %s", len(cleaned), list(cleaned)
        )
        return self._classes

    def infer(self, frame_bgr: np.ndarray, confidence: float) -> list[RawBox]:
        """Run one inference pass and return parsed boxes with prompt labels."""
        with self._lock:
            if self._model is None:
                raise ModelNotLoadedError("Model is not loaded yet")
            results = self._model.predict(
                frame_bgr,
                conf=confidence,
                imgsz=self._image_size,
                device=DEVICE,
                verbose=False,
                half=False,
            )
        boxes: list[RawBox] = []
        if not results:
            return boxes
        prediction = results[0]
        names = dict(prediction.names)
        detected = getattr(prediction, "boxes", None)
        if detected is None:
            return boxes
        for coords, box_conf, cls_idx in zip(
            detected.xyxy.tolist(), detected.conf.tolist(), detected.cls.tolist()
        ):
            prompt = str(names.get(int(cls_idx), cls_idx))
            boxes.append(
                RawBox(
                    prompt=prompt,
                    confidence=float(box_conf),
                    bbox=(
                        float(coords[0]),
                        float(coords[1]),
                        float(coords[2]),
                        float(coords[3]),
                    ),
                )
            )
        return boxes
