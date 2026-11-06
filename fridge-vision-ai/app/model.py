"""YOLO-World v2 model lifecycle and CLIP re-parametrization."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import List, Optional

import numpy as np
from ultralytics import YOLOWorld


class YOLOWorldEngine:
    """Loads YOLO-World v2 once at startup and manages class re-parametrization.

    The CLIP text encoder is re-parametrized on every ``set_classes`` call, so
    the inventory can switch the zero-shot detection vocabulary without
    reloading the model weights.
    """

    def __init__(self, model_name: str = "yolov8s-worldv2.pt") -> None:
        self.model_name = model_name
        self._model: Optional[YOLOWorld] = None
        self._lock = threading.RLock()
        self._classes: List[str] = []

    @property
    def is_loaded(self) -> bool:
        """Return True if the model weights are loaded."""
        return self._model is not None

    def load(self) -> None:
        """Load the model weights and seed the default class vocabulary."""
        with self._lock:
            if self._model is not None:
                return
            model_path = self._resolve_model_path(self.model_name)
            self._model = YOLOWorld(model=model_path)
            # Seed with a small default fridge vocabulary so the CLIP text
            # encoder is re-parametrized at startup.
            default_classes = [
                "milk carton",
                "butter",
                "cheese",
                "egg",
                "yogurt",
                "juice bottle",
                "jam jar",
            ]
            self._model.set_classes(default_classes)
            self._classes = list(default_classes)

    def _resolve_model_path(self, model_name: str) -> str:
        """Resolve the model file, falling back to common locations."""
        path = Path(model_name)
        if path.exists():
            return str(path)
        for candidate in (Path("models") / model_name, Path("/app/models") / model_name):
            if candidate.exists():
                return str(candidate)
        # ultralytics downloads known model names from its asset server automatically
        return model_name

    def set_classes(self, classes: List[str]) -> None:
        """Re-parametrizes the CLIP text encoder for the given prompts."""
        if not classes:
            raise ValueError("classes must not be empty")
        with self._lock:
            if self._model is None:
                raise RuntimeError("Model not loaded")
            self._model.set_classes(classes)
            self._classes = list(classes)

    def classes(self) -> List[str]:
        """Return the currently active class prompts."""
        with self._lock:
            return list(self._classes)

    def predict(self, image: np.ndarray, conf: float = 0.25):
        """Run YOLO-World inference on a single BGR image."""
        with self._lock:
            if self._model is None:
                raise RuntimeError("Model not loaded")
            return self._model.predict(image, conf=conf, verbose=False)
