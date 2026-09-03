"""Waiting/cancellation persistence and non-destructive version-one migration."""
import json
import queue
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.database import Database
from app.jobs import JobManager
from app.selftest import fixture_worker
from app.server import create_app


def waiting_worker(payload, staging_path, events):
    events.put({"type": "waiting", "retry_at": time.time() + 120, "attempt": 1,
                "message": "Lichess reports an active export. Waiting before retrying.",
                "reason": "concurrency", "status_code": 429,
                "server_message": "Please only run 1 request(s) at a time"})
    time.sleep(120)


def resume_worker(payload, staging_path, events):
    events.put({"type": "waiting", "retry_at": time.time() + 0.15, "attempt": 1,
                "message": "A controlled test cooldown."})
    time.sleep(0.2)
    events.put({"type": "resumed", "message": "Retrying after the test cooldown."})
    fixture_worker(payload, staging_path, events)


def connect(tmp_path, worker):
    return TestClient(create_app(tmp_path / "data", "test", worker=worker),
                      base_url="http://127.0.0.1", headers={"X-App-Token": "test"})


def payload(tmp_path):
    return {"server": "lichess", "username": "Player", "output_dir": str(tmp_path / "exports")}


def wait_status(client, status):
    end = time.monotonic() + 10
    while time.monotonic() < end:
        job = client.get("/api/jobs").json()[0]
        if job["status"] == status:
            return job
        time.sleep(0.02)
    raise AssertionError(f"Expected {status}, received {job}")


def test_waiting_is_cancelable_and_cooldown_survives_restart(tmp_path):
    with connect(tmp_path, waiting_worker) as client:
        client.post("/api/jobs", json=payload(tmp_path)).raise_for_status()
        job = wait_status(client, "waiting")
        deadline = job["retry_at"]
        assert deadline > time.time()
        assert job["attempt"] == 1
        assert job["error"] is None
        assert job["partial_path"] == ""
        assert client.post("/api/jobs", json=payload(tmp_path)).status_code == 409
        client.post(f"/api/jobs/{job['id']}/cancel").raise_for_status()
        cancelled = wait_status(client, "cancelled")
        assert cancelled["partial_path"] == ""
        assert cancelled["retry_at"] is None
    with connect(tmp_path, fixture_worker) as reopened:
        # If the worker starts here, it would write an export immediately.
        reopened.post("/api/jobs", json=payload(tmp_path)).raise_for_status()
        pending = wait_status(reopened, "waiting")
        assert pending["retry_at"] == deadline
        assert not Path(pending["output_path"]).exists()
        reopened.post(f"/api/jobs/{pending['id']}/cancel").raise_for_status()
        wait_status(reopened, "cancelled")


def test_retry_completion_clears_cooldown_and_preserves_single_export(tmp_path):
    with connect(tmp_path, resume_worker) as client:
        client.post("/api/jobs", json=payload(tmp_path)).raise_for_status()
        completed = wait_status(client, "completed")
        assert completed["attempt"] == 1
        assert completed["retry_at"] is None
        assert completed["games"] == 1
        assert client.app.state.db.cooldown("lichess") is None
        assert len(list((tmp_path / "exports").glob("*.pgn"))) == 1


def test_waiting_shutdown_has_no_phantom_partial(tmp_path):
    with connect(tmp_path, waiting_worker) as client:
        client.post("/api/jobs", json=payload(tmp_path)).raise_for_status()
        job = wait_status(client, "waiting")
        manager = client.app.state.manager
        database = client.app.state.db
    assert not manager.thread.is_alive()
    assert database.get(job["id"])["status"] == "cancelled"
    assert database.get(job["id"])["partial_path"] == ""
    assert database.cooldown("lichess")["retry_at"] > time.time()


class CancelRaceContext:
    """Deliver a server event and cancellation before the parent's first read."""

    def __init__(self, manager, event):
        self.manager = manager
        self.event = event
        self.events = queue.Queue()
        self.events.close = lambda: None
        self.pid = None
        self.alive = False

    def Queue(self):
        return self.events

    def Process(self, **kwargs):
        return self

    def start(self):
        self.pid = 1
        self.alive = True
        self.events.put({"type": "progress", "games": 0})
        self.events.put(self.event)
        self.manager.cancel_event.set()

    def is_alive(self):
        return self.alive

    def terminate(self):
        # The cooldown must reach persistent storage before worker termination.
        assert self.manager.db.cooldown("lichess")["retry_at"] == self.event["retry_at"]
        self.alive = False

    def join(self, timeout):
        pass


@pytest.mark.parametrize("kind", ["waiting", "error"])
def test_cancel_preserves_cooldown_queued_before_parent_read(tmp_path, kind):
    database = Database(tmp_path / "data")
    manager = JobManager(database, tmp_path / "data")
    event = {"type": kind, "retry_at": time.time() + 120, "attempt": 1,
             "message": "Lichess requested a cooldown."}
    manager.context = CancelRaceContext(manager, event)
    staging, output = tmp_path / "download.part", tmp_path / "download.pgn"
    database.add("cancel-race", payload(tmp_path), output.name, output, staging)
    manager.active_id = "cancel-race"

    manager._run("cancel-race", payload(tmp_path), staging, output)

    assert database.get("cancel-race")["status"] == "cancelled"
    assert database.cooldown("lichess")["retry_at"] == event["retry_at"]
    assert not output.exists()
    assert manager.active_id is None


def test_cancelled_queued_job_does_not_start_worker(tmp_path):
    database = Database(tmp_path / "data")
    manager = JobManager(database, tmp_path / "data")
    context = CancelRaceContext(manager, {})
    manager.context = context
    staging, output = tmp_path / "download.part", tmp_path / "download.pgn"
    database.add("cancel-before-start", payload(tmp_path), output.name, output, staging)
    manager.active_id = "cancel-before-start"
    manager.cancel_event.set()

    manager._run("cancel-before-start", payload(tmp_path), staging, output)

    assert context.pid is None
    assert database.get("cancel-before-start")["status"] == "cancelled"
    assert not output.exists()


def test_version_one_migration_backs_up_history_and_settings(tmp_path):
    path = tmp_path / "app.sqlite3"
    request = payload(tmp_path)
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE jobs (
                id TEXT PRIMARY KEY, created_at TEXT NOT NULL, finished_at TEXT,
                status TEXT NOT NULL, request TEXT NOT NULL,
                filename TEXT NOT NULL, output_path TEXT NOT NULL, partial_path TEXT NOT NULL,
                games INTEGER NOT NULL DEFAULT 0, bytes INTEGER NOT NULL DEFAULT 0,
                message TEXT NOT NULL DEFAULT '', error TEXT
            );
            PRAGMA user_version=1;
        """)
        connection.execute("INSERT INTO settings VALUES (?,?)", ("output_dir", json.dumps(str(tmp_path))))
        connection.execute("""INSERT INTO jobs(id,created_at,status,request,filename,output_path,partial_path,games,bytes)
            VALUES (?,?,?,?,?,?,?,?,?)""", ("existing", "2026-09-01", "completed", json.dumps(request),
            "saved.pgn", str(tmp_path / "saved.pgn"), "", 21485, 65821949))
    (tmp_path / "saved.pgn").write_bytes(b"Existing user games remain untouched")
    original = path.read_bytes()

    database = Database(tmp_path)
    assert database.get("existing")["games"] == 21485
    assert database.get("existing")["status"] == "completed"
    assert database.get("existing")["retry_at"] is None
    assert database.get("existing")["attempt"] == 0
    assert database.settings()["output_dir"] == str(tmp_path)
    backups = list(tmp_path.glob("app-before-v2-*.sqlite3"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as backup:
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 1
        assert backup.execute("SELECT games FROM jobs WHERE id='existing'").fetchone()[0] == 21485
    assert (tmp_path / "saved.pgn").read_bytes() == b"Existing user games remain untouched"
    assert path.read_bytes() != original
    Database(tmp_path)
    assert len(list(tmp_path.glob("app-before-v2-*.sqlite3"))) == 1
