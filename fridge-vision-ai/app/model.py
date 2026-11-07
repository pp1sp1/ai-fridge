"""YOLO-World v2 model lifecycle for the Fridge Vision AI add-on.

The model is loaded exactly once at startup and re-parameterized in place
with zero-shot text prompts (CLIP text encoder) via ``set_classes``. All
access is serialized through a lock because ``set_classes`` mutates the
detection head while inference reads it.

Weights resolution order:
1. an existing file in the persistent add-on data directory (``/data``)
   or the working directory,
2. a copy baked into the image at ``/opt/weights`` by the Dockerfile,
3. an explicit download from the Ultralytics release CDN into ``/data``
   (streamed, retried, size- and format-verified, written atomically).

Only *s* and *m* sizes are supported: Ultralytics publishes YOLO-World
v2 checkpoints as ``yolov8{s,m,l,x}-worldv2.pt`` — there is no nano
variant, so requesting one would always fail to resolve.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import numpy as np

LOGGER = logging.getLogger(__name__)

DEFAULT_MODEL_NAME = "yolov8s-worldv2.pt"
SUPPORTED_MODELS: tuple[str, ...] = ("yolov8s-worldv2.pt", "yolov8m-worldv2.pt")
DEFAULT_IMAGE_SIZE = 640
DEVICE = "cpu"

# Persistent add-on storage (mount point of the add-on data volume).
DATA_DIR = Path(os.environ.get("FRIDGE_VISION_DATA_DIR", "/data"))

# Image-local directory the Dockerfile bakes weights into at build time.
BAKED_WEIGHTS_DIR = Path(os.environ.get("FRIDGE_VISION_WEIGHTS_DIR", "/opt/weights"))

# Direct CDN link (no GitHub API involved, so no API rate limits apply).
WEIGHTS_RELEASE_BASE_URL = (
    "https://github.com/ultralytics/assets/releases/download/v8.3.0"
)

# Override for self-hosted mirrors, e.g. FRIDGE_VISION_WEIGHTS_URL.
WEIGHTS_URL_OVERRIDE_ENV = "FRIDGE_VISION_WEIGHTS_URL"

# Any valid supported *-worldv2.pt file is far larger than this (the s model
# is ~25.9 MB); smaller files are truncated leftovers of an interrupted
# download or an HTML error page saved under the wrong name.
_MIN_VALID_WEIGHTS_BYTES = 10 * 1024 * 1024

# torch serializes checkpoints as zip archives, so a healthy file starts
# with the local-file-header magic "PK\x03\x04".
_TORCH_ZIP_MAGIC = b"PK\x03\x04"

_DOWNLOAD_ATTEMPTS = 3
_DOWNLOAD_BACKOFF_SECONDS = 5.0
_DOWNLOAD_SOCKET_TIMEOUT_SECONDS = 60.0
_DOWNLOAD_STALL_LIMIT_SECONDS = 900.0
_DOWNLOAD_CHUNK_BYTES = 1024 * 1024
_DOWNLOAD_USER_AGENT = "fridge-vision-ai/1.0"

_MAX_PROMPT_CHARS = 60


class ModelNotLoadedError(RuntimeError):
    """Raised when inference is requested before the model finished loading."""


def _looks_like_torch_checkpoint(path: Path) -> bool:
    """True if ``path`` starts with the zip magic of a torch checkpoint."""
    try:
        with path.open("rb") as handle:
            return handle.read(len(_TORCH_ZIP_MAGIC)) == _TORCH_ZIP_MAGIC
    except OSError:
        return False


def _weights_source_urls(model_name: str) -> list[str]:
    """Mirror-aware candidate URLs for ``model_name``, highest priority first."""
    urls: list[str] = []
    override = os.environ.get(WEIGHTS_URL_OVERRIDE_ENV, "").strip()
    if override:
        urls.append(override)
    urls.append(f"{WEIGHTS_RELEASE_BASE_URL}/{model_name}")
    return urls


def _stream_to_file(url: str, destination: Path) -> int:
    """Stream ``url`` into ``destination`` and return the received byte count.

    Socket operations time out after ``_DOWNLOAD_SOCKET_TIMEOUT_SECONDS``;
    the whole transfer must finish within ``_DOWNLOAD_STALL_LIMIT_SECONDS``.
    A response whose byte count disagrees with its ``Content-Length`` header
    (or that falls under ``_MIN_VALID_WEIGHTS_BYTES``) raises ``RuntimeError``.
    """
    request = urllib.request.Request(
        url, headers={"User-Agent": _DOWNLOAD_USER_AGENT}
    )
    deadline = time.monotonic() + _DOWNLOAD_STALL_LIMIT_SECONDS
    received = 0
    with urllib.request.urlopen(
        request, timeout=_DOWNLOAD_SOCKET_TIMEOUT_SECONDS
    ) as response, destination.open("wb") as handle:
        length_header = response.headers.get("Content-Length")
        expected = int(length_header) if length_header and length_header.isdigit() else None
        while True:
            if time.monotonic() > deadline:
                raise RuntimeError(
                    f"download stalled past {_DOWNLOAD_STALL_LIMIT_SECONDS:.0f}s budget"
                )
            chunk = response.read(_DOWNLOAD_CHUNK_BYTES)
            if not chunk:
                break
            handle.write(chunk)
            received += len(chunk)
    if expected is not None and received != expected:
        raise RuntimeError(
            f"truncated download: got {received} of {expected} advertised bytes"
        )
    if received < _MIN_VALID_WEIGHTS_BYTES:
        raise RuntimeError(f"downloaded file is only {received} bytes")
    return received


def _download_weights(model_name: str, target: Path) -> Path:
    """Download ``model_name`` to ``target`` atomically, with retries.

    Each attempt streams to ``<target>.part`` and is moved into place only
    after both the size and the torch-zip format checks pass, so a crash or
    a corrupted transfer never leaves a half-written weights file behind.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".part")
    url_errors: dict[str, str] = {}
    for attempt in range(1, _DOWNLOAD_ATTEMPTS + 1):
        for url in _weights_source_urls(model_name):
            try:
                size = _stream_to_file(url, partial)
                if not _looks_like_torch_checkpoint(partial):
                    raise RuntimeError(
                        "downloaded file is not a torch checkpoint (missing zip header)"
                    )
                os.replace(partial, target)
                LOGGER.info(
                    "Downloaded %s (%.1f MB) from %s", model_name, size / 1e6, url
                )
                return target
            except (OSError, RuntimeError, urllib.error.URLError) as err:
                url_errors[url] = str(err)
                LOGGER.warning(
                    "Weights download attempt %d/%d from %s failed: %s",
                    attempt,
                    _DOWNLOAD_ATTEMPTS,
                    url,
                    err,
                )
                partial.unlink(missing_ok=True)
        if attempt < _DOWNLOAD_ATTEMPTS:
            time.sleep(_DOWNLOAD_BACKOFF_SECONDS * attempt)
    summary = "; ".join(f"{url} -> {err}" for url, err in url_errors.items())
    raise RuntimeError(
        f"Could not download {model_name} after {_DOWNLOAD_ATTEMPTS} attempts. "
        f"Errors: {summary}. Remedies: verify the host has internet access; point "
        f"the {WEIGHTS_URL_OVERRIDE_ENV} environment variable at a reachable mirror "
        f"URL; or download {model_name} manually and place it at {target}."
    )


def _ensure_weights_file(model_name: str, target: Path) -> Path:
    """Return a usable weights file path for ``model_name`` at ``target``.

    Invalid leftovers (truncated or non-checkpoint files) are removed and
    re-downloaded; a healthy file is returned untouched.
    """
    if target.is_file() and (
        target.stat().st_size < _MIN_VALID_WEIGHTS_BYTES
        or not _looks_like_torch_checkpoint(target)
    ):
        LOGGER.warning("Removing invalid weights file %s", target)
        target.unlink()
    if not target.is_file():
        _download_weights(model_name, target)
    return target


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
        for folder in (DATA_DIR, Path.cwd(), BAKED_WEIGHTS_DIR):
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
        """Load the model once; weights are provisioned on first start."""
        with self._lock:
            if self._model is not None:
                return
            try:
                # Imported lazily so importing this module stays cheap.
                from ultralytics import YOLO
            except ImportError as exc:
                raise RuntimeError("The ultralytics package is not installed") from exc

            weights_path = self._resolve_weights_path()
            if not weights_path.is_file():
                LOGGER.info(
                    "Weights %s not found locally; they will be provisioned at %s",
                    self._model_name,
                    weights_path,
                )
            weights_path = _ensure_weights_file(self._model_name, weights_path)

            LOGGER.info("Loading %s from %s", self._model_name, weights_path)
            try:
                self._model = YOLO(str(weights_path))
            except Exception as exc:  # noqa: BLE001 - surface a clear startup error
                raise RuntimeError(
                    f"Could not load YOLO-World weights {weights_path}: {exc}. "
                    "If the file is corrupt, delete it from the add-on data "
                    "directory so it is re-downloaded, then restart the add-on."
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
