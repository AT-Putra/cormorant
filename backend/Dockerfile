# ---- Stage 1: frontend build (node 26, LTS from Oct 2026) ----
FROM node:26-slim AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ .
RUN npm run build

# ---- Stage 2: runtime (python 3.14-slim + ffmpeg) ----
FROM python:3.14-slim AS runtime

# ffmpeg for merging/recording; curl unused; clean apt lists
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Non-root app user; owns config/data/media mount points
RUN useradd --create-home --uid 1000 vduser

WORKDIR /app

# App-owned venv ON the config volume mount point so runtime pip -U persists
# (Decision C: yt-dlp self-update as non-root). Seed it now; the volume's
# copy-on-first-use carries it into a fresh named volume.
ENV VENV=/config/venv
RUN python -m venv $VENV \
    && $VENV/bin/pip install --no-cache-dir --upgrade pip \
    && chown -R vduser:vduser /config

COPY backend/requirements.txt /app/backend/requirements.txt
RUN $VENV/bin/pip install --no-cache-dir -r /app/backend/requirements.txt

COPY backend/ /app/backend/
COPY --from=frontend /build/dist /app/frontend/dist

# Data dir also lives on a volume; ensure ownership of app tree
RUN chown -R vduser:vduser /app

# /media and /data are named-volume mount points. They must exist AND be
# vduser-owned in the image: a volume mounted over a missing (or root-owned)
# path inherits root:root, and the non-root app then cannot mkdir into it.
RUN mkdir -p /media /data \
    && chown vduser:vduser /media /data

USER vduser

ENV PATH="$VENV/bin:$PATH" \
    PYTHONPATH=/app/backend \
    MEDIA_ROOT=/media \
    CONFIG_DIR=/config \
    DATA_DIR=/data \
    HOST=0.0.0.0 \
    PORT=8000

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4)"

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
