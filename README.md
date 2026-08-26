# Cormorant

Self-hosted video & live-stream downloader for **Bilibili · Instagram · TikTok · Douyin · RedNote (Xiaohongshu)** — best quality by default, watchlist auto-recording, cookie-based login, all behind a password-gated local web UI.

Powered by yt-dlp (extraction), streamlink/ffmpeg (live capture & merging). Python FastAPI backend + React frontend in one container.

## Quick start

### Dev machine (Podman — this laptop)

```bash
podman compose up --build
```

Then open **http://localhost:8000** — first launch asks you to set an access password.

> Rootless Podman note: for containers to come back after a reboot, enable once:
> `systemctl --user enable podman-restart.service`
> (`restart: unless-stopped` ≈ `always` under podman; fixed upstream in 5.8.x.)

### Production (Docker)

The server runs published images, it does not build. Every push to `main` runs
the test suite, then publishes `ghcr.io/at-putra/cormorant:latest` and
`:<git-sha>`.

```bash
git clone https://github.com/AT-Putra/cormorant.git /opt/cormorant
cd /opt/cormorant && cp .env.example .env   # set TZ
docker compose up -d
```

Same compose file as dev — plain Compose Spec, no engine-specific extensions.

### Shipping an update

```bash
./update.sh                    # pull :latest, recreate, wait for HEALTHCHECK
TAG=<git-sha> ./update.sh      # pin or roll back to one build
```

Volumes are not touched by container recreation: the SQLite DB, the Fernet key
and any yt-dlp version installed from the UI all survive an update.

## Volume layout (named volumes)

| Volume | Mount | Contents |
|---|---|---|
| `vd_config` | `/config` | `secret.key` (Fernet key), app-owned venv with yt-dlp/streamlink (runtime updates land here and survive recreation) |
| `vd_data` | `/data` | `app.db` (SQLite, WAL) — jobs, watchlist, credentials, activity |
| `vd_media` | `/media` | downloaded media under `{platform}/{creator}/` |

**Do not replace `/config` with a host bind** — bind mounts hide the image-seeded venv and break non-root writes. Wipe volumes for a factory reset: `podman volume rm vd_config vd_data vd_media`.

## Login cookies (unlock max quality)

Each platform gates its highest tiers behind login (bilibili >1080p needs SESSDATA, etc.):

1. In your normal browser, log into the platform.
2. Export cookies:
   - Easiest: install a "Get cookies.txt" / "Cookie-Editor" extension and export as Netscape `cookies.txt`.
   - Or copy the raw `Cookie:` request header from DevTools → Network.
3. In Cormorant: **Settings → Platform cookies → click the platform** → paste the text → *Save & validate*.

The app validates via a real authenticated probe before storing; blobs are Fernet-encrypted at rest and decrypted only in-process for engine calls. Instagram Stories expire fast — paste their URL while they're live.

## Watchlist

**Watchlist → paste a creator's profile/room URL**, pick scope:

- *lives only* — polls the room; auto-records from join point to stream end
- *posts only* — downloads each new post at default (best) quality
- *both*

Duplicate posts are skipped automatically; per-download "Re-download" overrides that.

## Queue controls

Pause/resume (resumes `.part` state, no re-probe), cancel, retry, configurable concurrency cap. Auto jobs pause themselves when free disk drops below your floor % and resume with hysteresis when space recovers — manual jobs are never gated.

## yt-dlp updates

Douyin/XHS extractors move fast. **Settings → Engine → Update now**: upgrades inside the container's config-volume venv and restarts the app to load it (deferred while downloads/recordings are active). No image rebuild needed.

## Notifications

One webhook channel: ntfy, Telegram bot, or Discord webhook. Per-creator event toggles + global quiet hours (suppressed sends are logged in Activity).

## Env vars

| Var | Default | Purpose |
|---|---|---|
| `TZ` | UTC | quiet-hours correctness |
| `MEDIA_ROOT` / `CONFIG_DIR` / `DATA_DIR` | `/media` `/config` `/data` | set by compose; local dev uses repo dirs |
| `POLL_INTERVAL_SECONDS` | 300 | watchlist poll cadence (also runtime-settable) |

## Development

```bash
# backend
cd backend && python -m pip install -r requirements.txt
python -m uvicorn app.main:app --reload --port 8000

# frontend (separate shell)
cd frontend && npm install && npm run dev   # proxies /api + /ws to :8000

# tests
cd backend && python -m pytest tests/ -q
cd frontend && npm run build                 # zero-TS-error gate
```
