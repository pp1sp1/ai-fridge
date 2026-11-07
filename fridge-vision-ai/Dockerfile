# Fridge Vision AI add-on: slim Python 3.11 base with CPU-only PyTorch.
ARG BUILD_FROM
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    OMP_NUM_THREADS=2 \
    MKL_NUM_THREADS=2 \
    YOLO_CONFIG_DIR=/data/ultralytics-config

# Runtime libraries required by the opencv and torch wheels in a slim image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        libgl1 \
        libglib2.0-0 \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /usr/src/fridge-vision-ai

COPY requirements.txt .

# Install torch/torchvision first, strictly from the CPU wheel index, so the
# CUDA-enabled builds from PyPI (and their multi-GB nvidia dependencies) are
# never pulled in. The subsequent requirements install sees them as satisfied.
RUN pip install --no-cache-dir \
        torch==2.6.0 torchvision==0.21.0 \
        --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir \
        "https://github.com/ultralytics/CLIP/archive/refs/heads/master.zip"

# The `clip` module above is the CLIP fork Ultralytics expects for
# YOLO-World zero-shot re-parameterization (set_classes -> CLIP text
# encoder). It is installed from a GitHub zip archive, which pip fetches
# over plain HTTPS -- no `git` binary is needed in the image. Without it
# every set_classes call dies with `ModuleNotFoundError: No module named
# 'clip'` and Ultralytics' pip AutoInstall fallback cannot help either
# (it shells out to `git clone`, which fails in this slim image).
# torch/torchvision are already installed at this point, so the CLIP
# fork's unpinned torch requirement resolves to the existing CPU build.

COPY app/ ./app/

# Bake the supported YOLO-World weights into the image when the build host
# has internet access, so the default model works fully offline on first
# start. Each download writes to a temp file and is moved into place only on
# success; failures are non-fatal because app/model.py falls back to
# downloading the selected weights at runtime into the persistent /data
# volume (with retries, timeouts and integrity checks).
RUN mkdir -p /opt/weights \
    && for model in yolov8s-worldv2.pt yolov8m-worldv2.pt; do \
        curl -fsSL --retry 3 --retry-delay 5 --connect-timeout 30 --max-time 600 \
            -o "/opt/weights/$model.tmp" \
            "https://github.com/ultralytics/assets/releases/download/v8.3.0/$model" \
            && mv "/opt/weights/$model.tmp" "/opt/weights/$model" \
            || true; \
    done \
    && rm -f /opt/weights/*.tmp

# Persistent add-on storage; also the working directory. Weights resolution
# order: /data, the working directory, the baked /opt/weights copy, then an
# explicit runtime download into /data (see app/model.py).
RUN mkdir -p /data
WORKDIR /data

EXPOSE 8000

# --timeout-keep-alive 75: uvicorn's default of 5 s closes idle keep-alive
# connections aggressively; a client that reuses such a pooled connection at
# the wrong moment gets "[Errno 104] Connection reset by peer". 75 s keeps
# idle sockets (Supervisor watchdog, curl, HA) alive far longer; the
# integration additionally uses per-request connections (force_close).
CMD ["python3", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--app-dir", "/usr/src/fridge-vision-ai", "--timeout-keep-alive", "75"]
