"""FastAPI app: /api/health, dev CORS, DB init on startup, optional static SPA mount."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.auth import AuthMiddleware, router as auth_router
from app.config import REPO_ROOT
from app.db import init_db
from app.routers import (
    activity,
    credentials,
    downloads,
    library,
    notifications,
    recordings,
    settings,
    watchlist,
    ws,
)
from app.services.downloader import manager
from app.services.poller import poller
from app.services.recovery import recovery
from app.services.recorder import recorder


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    await init_db()
    # Importing the module runs its install() and subscribes the activity
    # mirror. Nothing else imports it (routers.activity only reads the table),
    # so without this the event bus has no durable listener and the activity
    # log stays empty.
    from app.services import activity as activity_service

    activity_service.install()
    # Before any worker can create a new one: collect decrypted cookie files
    # stranded in /tmp by a process that was killed rather than stopped. Has
    # to be here, while nothing is running — the periodic sweeps would race a
    # live capture, which legitimately holds its file open for hours.
    from app.routers.credentials import sweep_stale_cookiefiles

    sweep_stale_cookiefiles()
    await manager.start()
    await poller.start()
    # Reconcile BEFORE the sweeps start. recovery decides a .part is an orphan
    # by checking it against the output_path of every ACTIVE recording, and a
    # row left in 'recording' by a killed process still counts as active until
    # this runs -- so a boot sweep that got there first skipped the very
    # orphans it exists to collect, and they waited a full 10-minute cycle.
    await recorder.reconcile_on_boot()
    await recovery.start()
    yield
    await recorder.shutdown()
    await poller.stop()
    await recovery.stop()
    await manager.stop()


def create_app() -> FastAPI:
    app = FastAPI(title="Cormorant", lifespan=lifespan)

    # Auth added first => CORS (added last = outermost) still decorates 401s
    # and answers preflights for the dev frontend.
    app.add_middleware(AuthMiddleware)

    # Dev CORS (Vite dev server on :5173 proxies to us, but allow direct hits too)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
        allow_credentials=True,
    )

    @app.get("/api/health")
    async def health() -> dict:
        return {"status": "ok"}

    app.include_router(downloads.router)
    app.include_router(watchlist.router)
    app.include_router(recordings.router)
    app.include_router(library.router)
    app.include_router(credentials.router)
    app.include_router(settings.router)
    app.include_router(activity.router)
    app.include_router(notifications.router)
    app.include_router(ws.router)
    app.include_router(auth_router)

    dist = REPO_ROOT / "frontend" / "dist"
    if dist.is_dir():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="spa-assets")

        # SPA fallback: any non-API GET serves index.html so deep links
        # (/queue, /settings, ...) work with client-side routing.
        index_file = dist / "index.html"

        from fastapi.responses import FileResponse

        dist_root = dist.resolve()

        @app.get("/{full_path:path}", include_in_schema=False)
        async def spa_fallback(full_path: str):
            if full_path.startswith("api/"):
                raise HTTPException(status_code=404, detail="Not Found")
            # vite copies public/ into dist's ROOT, not into assets/, so
            # favicon.svg and icons.svg are reachable only through this route.
            # Falling straight through to index.html answered them 200 with
            # text/html, which a browser quietly discards -- no favicon, no
            # broken-image, nothing in the console to notice.
            candidate = (dist_root / full_path).resolve()
            if (
                full_path
                and candidate.is_relative_to(dist_root)  # full_path is user input
                and candidate.is_file()
            ):
                return FileResponse(candidate)
            return FileResponse(index_file)

    return app


app = create_app()
