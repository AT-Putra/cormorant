"""Settings API + yt-dlp self-update (fresh-interpreter version read, idle-guarded restart)."""

import asyncio
import os
import signal
import subprocess
import sys

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import CONFIG_DIR
from app.db import get_session
from app.models import DownloadJob, LiveRecording
from app.services.settings_store import SettingsModel, aget_settings, save_settings

router = APIRouter(prefix="/api/settings", tags=["settings"])


@router.get("")
async def read_settings(session: AsyncSession = Depends(get_session)):
    s = await aget_settings(session)
    return {
        "folder_template": s.folder_template,
        "concurrency_cap": s.concurrency_cap,
        "poll_interval_seconds": s.poll_interval_seconds,
        "space_floor_pct": s.space_floor_pct,
        "default_quality": s.default_quality,
    }


@router.put("")
async def write_settings(payload: dict, session: AsyncSession = Depends(get_session)):
    try:
        saved = await save_settings(session, payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    applied = True
    if "concurrency_cap" in payload:
        # DownloadManager reads the cap at start(); no live resize in v1 —
        # new cap applies on next restart. Documented in response.
        applied = False
    return {"saved": True, "applied_immediately": applied, "settings": await _dump(saved)}


async def _dump(s: SettingsModel) -> dict:
    return {
        "folder_template": s.folder_template,
        "concurrency_cap": s.concurrency_cap,
        "poll_interval_seconds": s.poll_interval_seconds,
        "space_floor_pct": s.space_floor_pct,
        "default_quality": s.default_quality,
    }


# ---- yt-dlp version / update ---------------------------------------------

_FRESH_VERSION_SNIPPET = 'import importlib.metadata as m; print(m.version("yt-dlp"))'


async def _run_fresh_version() -> str:
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", _FRESH_VERSION_SNIPPET,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await asyncio.wait_for(proc.communicate(), timeout=30)
    if proc.returncode != 0:
        raise RuntimeError(err.decode().strip())
    return out.decode().strip()


@router.get("/ytdlp/version")
async def ytdlp_version():
    """Version from a FRESH interpreter (importlib.metadata), not the loaded module."""
    try:
        return {"version": await _run_fresh_version()}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"version probe failed: {exc}")


async def is_engine_busy(session: AsyncSession) -> bool:
    jobs = (
        await session.execute(
            select(DownloadJob.id).where(DownloadJob.status.in_(["downloading", "probing"]))
        )
    ).first()
    recs = (
        await session.execute(select(LiveRecording.id).where(LiveRecording.status == "recording"))
    ).first()
    return jobs is not None or recs is not None


@router.post("/ytdlp/update")
async def ytdlp_update(session: AsyncSession = Depends(get_session)):
    if await is_engine_busy(session):
        raise HTTPException(status_code=409, detail="deferred_until_idle")

    venv_pip = CONFIG_DIR / "venv" / "bin" / "pip"
    if venv_pip.exists():
        cmd = [str(venv_pip), "install", "-U", "yt-dlp"]
    else:
        cmd = [sys.executable, "-m", "pip", "install", "-U", "yt-dlp"]

    def _pip():
        return subprocess.run(cmd, capture_output=True, text=True, timeout=600)

    try:
        result = await asyncio.to_thread(_pip)
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="pip upgrade timed out")
    if result.returncode != 0:
        raise HTTPException(status_code=500, detail=f"pip failed: {result.stderr[-400:]}")

    # Graceful restart so upgraded code actually loads; under compose
    # `restart: unless-stopped` recycles the container. Dev: user restarts manually.
    async def _exit_soon():
        await asyncio.sleep(1.5)
        os.kill(os.getpid(), signal.SIGTERM)

    asyncio.create_task(_exit_soon())
    return {"updated": True, "restarting": True}
