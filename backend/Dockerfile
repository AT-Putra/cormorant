# ---- Stage 1: frontend build (node 26, LTS from Oct 2026) ----
FROM node:26-slim AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ .
RUN npm run build

# ---- Stage 2: runtime (python 3.14-slim + ffmpeg) ----
FROM python:3.14-slim AS runtime

# ffmpeg does NOT come from apt, and that is deliberate. Debian 13 is current
# stable — which is exactly the problem: trixie froze ffmpeg on the 7.1 branch
# (September 2024) for the life of the release, so `apt install ffmpeg` gives
# 7.1.5, the newest POINT release of a branch two majors behind upstream, and
# it stays that way until Debian 14. trixie-backports has nothing newer.
#
# What that costs concretely: TikTok (and bilibili, and douyin) serve their top
# live tiers as HEVC-in-FLV — the non-standard codec id 12 extension. FFmpeg
# only learned to demux it in 8.0 (flv.h: "non-standard protocol extension that
# is in use in the wild", FLV_CODECID_X_HEVC = 12). Under 7.1.5 those captures
# land as bytes ffprobe reads as codec unknown at 0x0, and `-c copy` refuses
# with "dimensions not set" — an unplayable file where 1080p60 was on offer.
#
# So: a pinned upstream build, GPL variant to match Debian's --enable-gpl,
# installed into /usr/local/bin, which precedes /usr/bin on PATH.
#
# Pinned three ways because this is a third-party binary, not an apt package:
# the dated release tag (BtbN's "latest" tag holds master snapshots only, so a
# version pin has to name the autobuild), the filename (which embeds ffmpeg's
# own git describe), and the sha256 BtbN publishes alongside it. To bump:
# read the new tag, filename and hash out of
# https://api.github.com/repos/BtbN/FFmpeg-Builds/releases/latest and its
# checksums.sha256 asset. A wrong hash fails the build rather than shipping.
ARG FFMPEG_RELEASE=autobuild-2026-08-26-13-06
ARG FFMPEG_BUILD=ffmpeg-n9.0.1-8-g16dfae5c88-linux64-gpl-9.0
ARG FFMPEG_SHA256=7648eac8acab5c1be8d064c1055cc0429663a7462b4ddcd591134ae4a3a6110e
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl xz-utils \
    && curl -fsSL -o /tmp/ffmpeg.tar.xz \
        "https://github.com/BtbN/FFmpeg-Builds/releases/download/${FFMPEG_RELEASE}/${FFMPEG_BUILD}.tar.xz" \
    && echo "${FFMPEG_SHA256}  /tmp/ffmpeg.tar.xz" | sha256sum -c - \
    && tar -xJf /tmp/ffmpeg.tar.xz -C /tmp \
    && install -m 0755 "/tmp/${FFMPEG_BUILD}/bin/ffmpeg" "/tmp/${FFMPEG_BUILD}/bin/ffprobe" /usr/local/bin/ \
    && rm -rf /tmp/ffmpeg.tar.xz "/tmp/${FFMPEG_BUILD}" \
    && apt-get purge -y curl xz-utils \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/* \
    && ffmpeg -version | head -1

# ---- Google Chrome, for the one page yt-dlp cannot read ----
# TikTok serves www.tiktok.com HTML behind a JS WAF challenge (SlardarWAF), and
# that page is the only place the HEVC ladder lives -- webcast/room/info
# carries the H.264 one and nothing else. yt-dlp cannot run the challenge
# script. Measured from the deploy host on live rooms: headless Chrome
# returned the real ~447 KB page with hevcStreamData, and uhd_60 (1080p60) on
# the rooms that publish it, while every yt-dlp request -- browser headers,
# curl_cffi impersonation on each target it offers, valid cookies -- got the
# 1.1 KB stub. Without this the ceiling is the H.264 720p tier.
#
# Only a starting capture pays for it (see app/services/browser.py); liveness
# and room ids come from the JSON API, which is not challenged, so the poll
# sweep never launches a browser.
#
# From Google's own apt repo rather than Debian's chromium: the measurement
# above was made against Chrome, and a WAF that fingerprints browsers is the
# last place to assume the two are interchangeable.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl gnupg \
    && curl -fsSL https://dl.google.com/linux/linux_signing_key.pub \
        | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] https://dl.google.com/linux/chrome/deb/ stable main" \
        > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends google-chrome-stable \
    && apt-get purge -y gnupg curl \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/* \
    && google-chrome --version

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
