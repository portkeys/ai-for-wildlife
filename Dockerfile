# AI for Wildlife — container image for Google Cloud Run.
#
# Runs the FastAPI app as-is (native-video analysis needs server-side ffmpeg).
# Served with hypercorn for HTTP/2 (h2c) support, which removes Cloud Run's 32 MiB
# HTTP/1 request-body cap so large clips (40–150 MB) can upload. Deploy with --use-http2.
FROM python:3.12-slim

# ffmpeg/ffprobe are hard runtime deps: transcode-to-mp4, thumbnails, frame extraction.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first so this layer caches across code changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code only. data/, .venv, .env, videos/, screenshots, etc. are excluded via .dockerignore.
COPY backend ./backend
COPY static ./static

# Cloud Run provides $PORT (default 8080). Bind hypercorn to it; h2c is auto-negotiated.
ENV PORT=8080
CMD exec hypercorn backend.app:app --bind "0.0.0.0:$PORT" --workers 1
