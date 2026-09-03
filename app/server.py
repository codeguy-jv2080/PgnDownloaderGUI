"""Authenticated loopback API and bundled static interface."""
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .database import Database
from .downloader import get_backend, run_download
from .jobs import JobConflict, JobManager
from .models import DEFAULT_MODES, MODES, AppearanceSettings, DownloadRequest, OutputSettings
from .paths import InstanceLock, default_data_dir, default_output_dir


def create_app(data_dir: Path | None = None, token: str | None = None, worker=run_download) -> FastAPI:
    directory = Path(data_dir or default_data_dir()).resolve()
    access_token = token or secrets.token_urlsafe(32)

    @asynccontextmanager
    async def lifespan(app):
        lock = InstanceLock(directory)
        lock.acquire()
        try:
            app.state.db = Database(directory)
            app.state.manager = JobManager(app.state.db, directory, worker=worker)
            try:
                yield
            finally:
                app.state.manager.close()
        finally:
            lock.release()

    app = FastAPI(title="PGN Downloader", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.token = access_token
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
    assets = Path(__file__).resolve().parents[1] / "web"

    @app.middleware("http")
    async def local_access(request: Request, call_next):
        origin = request.headers.get("origin")
        if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
            return JSONResponse({"detail": "Requests must come from this app."}, status_code=403)
        if request.url.path.startswith("/api/"):
            if not secrets.compare_digest(request.headers.get("x-app-token", ""), access_token):
                return JSONResponse({"detail": "App session expired. Close and reopen PGN Downloader."}, status_code=401)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            # pywebview builds its local native bridge using new Function/eval.
            "default-src 'self'; script-src 'self' 'unsafe-eval'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'"
        )
        return response

    @app.get("/api/bootstrap")
    def bootstrap():
        return {"settings": app.state.db.settings(), "paths": {"data_dir": str(directory),
                "default_output_dir": str(default_output_dir())}, "backend": get_backend(),
                "modes": MODES, "defaults": {"server": "lichess", "modes": DEFAULT_MODES},
                "jobs": app.state.db.jobs()}

    @app.get("/api/jobs")
    def list_jobs():
        return app.state.db.jobs()

    @app.delete("/api/jobs")
    def clear_history():
        try:
            count = app.state.manager.clear_history()
            return {"cleared": count, "jobs": []}
        except JobConflict as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/jobs", status_code=201)
    def start_job(payload: DownloadRequest):
        try:
            return app.state.manager.start(payload)
        except JobConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except (ValueError, OSError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: str):
        try:
            return app.state.manager.cancel(job_id)
        except KeyError as exc:
            raise HTTPException(404, "Download not found.") from exc

    @app.put("/api/settings")
    def save_settings(payload: OutputSettings):
        return app.state.db.save_settings(**payload.model_dump())

    @app.put("/api/appearance")
    def save_appearance(payload: AppearanceSettings):
        return app.state.db.save_settings(theme=payload.theme)

    @app.get("/")
    def index():
        theme = app.state.db.settings()["theme"]
        html = (assets / "index.html").read_text(encoding="utf-8")
        html = html.replace('<html lang="en" data-theme="light">',
                            f'<html lang="en" data-theme="{theme}">', 1)
        return HTMLResponse(html)

    app.mount("/static", StaticFiles(directory=assets), name="static")
    return app
