"""Offline source/frozen smoke test: real HTTP server and spawned worker, no GUI."""
import json
import secrets
import socket
import threading
import time
import traceback
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import uvicorn

from .database import Database
from .server import create_app

SAMPLE = ('[Event "Offline test"]\n[White "Player"]\n[Black "José"]\n[Result "1-0"]\n\n'
          '1. e4 e5 1-0\n\n')


def fixture_worker(payload, staging_path, events):
    """Only selected explicitly by the offline self-test; never used for real jobs."""
    content = SAMPLE.encode("utf-8")
    with open(staging_path, "xb") as handle:
        handle.write(content)
    events.put({"type": "done", "games": 1, "bytes": len(content)})


def run(data_dir: Path, report: Path) -> bool:
    directory = Path(data_dir).resolve()
    report = Path(report).resolve()
    checks = []
    server = None
    thread = None
    success = False
    try:
        if (directory / "app.sqlite3").exists():
            raise ValueError("Use a fresh data directory for a self-test; existing data is never reused.")
        token = secrets.token_urlsafe(32)
        app = create_app(directory, token=token, worker=fixture_worker)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.listen(128)
        port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_config=None, access_log=False, log_level="warning"))
        thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
        thread.start()
        deadline = time.monotonic() + 15
        while not server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("Local HTTP server did not start.")
            time.sleep(0.05)

        def fetch(path, method="GET", body=None, authenticated=True, origin=None):
            headers = {"Content-Type": "application/json"}
            if authenticated:
                headers["X-App-Token"] = token
            if origin:
                headers["Origin"] = origin
            request = Request(f"http://127.0.0.1:{port}{path}", method=method,
                              data=json.dumps(body).encode() if body is not None else None, headers=headers)
            try:
                with urlopen(request, timeout=10) as response:
                    return response.status, response.read()
            except HTTPError as exc:
                return exc.code, exc.read()

        status, html = fetch("/")
        assert status == 200 and b"PGN" in html
        assert fetch("/static/app.js")[0] == 200
        assert fetch("/static/styles.css")[0] == 200
        checks.append("Bundled HTML, CSS and JavaScript served over loopback")
        assert fetch("/api/jobs", authenticated=False)[0] == 401
        assert fetch("/api/jobs", origin="https://example.org")[0] == 403
        checks.append("API token and origin protection")
        bootstrap = json.loads(fetch("/api/bootstrap")[1])
        assert bootstrap["backend"]["name"] == "pgn-downloader"
        output_dir = directory / "exports"
        payload = {"server": "lichess", "username": "OfflineTest", "output_dir": str(output_dir)}
        code, raw = fetch("/api/jobs", "POST", payload)
        assert code == 201, raw
        job_id = json.loads(raw)["id"]
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            jobs = json.loads(fetch("/api/jobs")[1])
            job = next(item for item in jobs if item["id"] == job_id)
            if job["status"] not in ("queued", "running"):
                break
            time.sleep(0.1)
        assert job["status"] == "completed", job
        assert job["games"] == 1
        assert Path(job["output_path"]).read_text(encoding="utf-8") == SAMPLE
        checks.append("Spawned worker completes and exports UTF-8 PGN")
        assert fetch("/api/jobs", "POST", payload)[0] == 409
        assert Path(job["output_path"]).read_text(encoding="utf-8") == SAMPLE
        checks.append("Existing output is preserved on a repeated request")
        server.should_exit = True
        thread.join(timeout=15)
        assert not thread.is_alive()
        assert Database(directory).get(job_id)["status"] == "completed"
        checks.append("Clean shutdown and SQLite history persistence")
        success = True
    except Exception:
        checks.append(traceback.format_exc())
    finally:
        if server:
            server.should_exit = True
        if thread:
            thread.join(timeout=15)
        report.parent.mkdir(parents=True, exist_ok=True)
        with report.open("x", encoding="utf-8") as handle:
            json.dump({"ok": success, "checks": checks}, handle, indent=2)
    return success
