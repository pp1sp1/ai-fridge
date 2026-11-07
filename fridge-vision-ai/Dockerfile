# Fridge Vision AI add-on: slim Python 3.11 base with CPU-only PyTorch.
ARG BUILD_FROM
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    OMP_NUM_THREADS=2 \
    MKL_NUM_THREADS=2

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
    && pip install --no-cache-dir -r requirements.txt

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

CMD ["python3", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--app-dir", "/usr/src/fridge-vision-ai"]
