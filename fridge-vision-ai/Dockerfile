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

# Persistent add-on storage; also the working directory, so the YOLO-World
# weights downloaded on first start are written to the persistent /data volume.
RUN mkdir -p /data
WORKDIR /data

EXPOSE 8000

CMD ["python3", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--app-dir", "/usr/src/fridge-vision-ai"]
