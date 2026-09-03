import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.database import Database
from app.jobs import JobManager
from app.selftest import SAMPLE, fixture_worker
from app.server import create_app


def slow_worker(payload, staging_path, events):
    with open(staging_path, "xb") as handle:
        handle.write(b'[Event "unfinished"]\n')
        handle.flush()
    events.put({"type": "progress", "message": "Fixture waiting", "bytes": 21, "games": 0})
    time.sleep(60)


def failed_worker(payload, staging_path, events):
    with open(staging_path, "xb") as handle:
        handle.write(b"partial")
    events.put({"type": "error", "message": "Fixture connection error"})


def wait_job(client, terminal=True):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        job = client.get("/api/jobs").json()[0]
        if terminal and job["status"] not in ("queued", "running", "waiting"):
            return job
        if not terminal and job["message"] == "Fixture waiting":
            return job
        time.sleep(0.05)
    pytest.fail(f"Download did not settle: {job}")


def client_for(tmp_path, worker=fixture_worker):
    return TestClient(create_app(tmp_path / "data", "test-token", worker=worker),
                      base_url="http://127.0.0.1", headers={"X-App-Token": "test-token"})


def request_for(tmp_path):
    return {"server": "lichess", "username": "TestPlayer", "output_dir": str(tmp_path / "output")}


def test_complete_history_settings_and_no_overwrite(tmp_path):
    with client_for(tmp_path) as client:
        response = client.post("/api/jobs", json=request_for(tmp_path))
        assert response.status_code == 201
        job = wait_job(client)
        assert job["status"] == "completed"
        assert job["games"] == 1
        assert Path(job["output_path"]).read_text(encoding="utf-8") == SAMPLE
        assert client.post("/api/jobs", json=request_for(tmp_path)).status_code == 409
        assert Path(job["output_path"]).read_text(encoding="utf-8") == SAMPLE
    with client_for(tmp_path) as client:
        state = client.get("/api/bootstrap").json()
        assert state["jobs"][0]["id"] == job["id"]
        assert state["settings"]["last_request"]["username"] == "TestPlayer"


def test_cancel_preserves_partial_and_allows_next_job(tmp_path):
    with client_for(tmp_path, slow_worker) as client:
        job_id = client.post("/api/jobs", json=request_for(tmp_path)).json()["id"]
        wait_job(client, terminal=False)
        assert client.post("/api/jobs", json=request_for(tmp_path)).status_code == 409
        assert client.post(f"/api/jobs/{job_id}/cancel").status_code == 200
        job = wait_job(client)
        assert job["status"] == "cancelled"
        assert not Path(job["output_path"]).exists()
        assert Path(job["partial_path"]).read_bytes().startswith(b"[Event")
        partial = Path(job["partial_path"])
        original_partial = partial.read_bytes()

        # Wait for cancellation cleanup, then reuse this same manager and filename.
        manager = client.app.state.manager
        manager.thread.join(timeout=5)
        assert not manager.thread.is_alive()
        manager.worker = fixture_worker
        next_response = client.post("/api/jobs", json=request_for(tmp_path))
        assert next_response.status_code == 201, next_response.text
        assert next_response.json()["id"] != job_id
        completed = wait_job(client)
        assert completed["status"] == "completed"
        assert Path(completed["output_path"]).read_text(encoding="utf-8") == SAMPLE
        assert partial.read_bytes() == original_partial
        assert client.app.state.db.get(job_id)["status"] == "cancelled"


def test_shutdown_cancels_active_worker_and_preserves_partial(tmp_path):
    with client_for(tmp_path, slow_worker) as client:
        response = client.post("/api/jobs", json=request_for(tmp_path))
        assert response.status_code == 201
        active = wait_job(client, terminal=False)
        assert active["status"] == "running"
        partial = Path(active["partial_path"])
        original_partial = partial.read_bytes()
        manager = client.app.state.manager
        database = client.app.state.db
        assert manager.thread.is_alive()

    # Exiting the app lifespan must perform cancellation, without a cancel request.
    assert not manager.thread.is_alive()
    assert manager.active_id is None
    stopped = database.get(active["id"])
    assert stopped["status"] == "cancelled"
    assert stopped["finished_at"] is not None
    assert stopped["bytes"] == len(original_partial)
    assert partial.read_bytes() == original_partial
    assert not Path(stopped["output_path"]).exists()
    with client_for(tmp_path) as reopened:
        saved = reopened.app.state.db.get(active["id"])
        assert saved["status"] == "cancelled"
        assert Path(saved["partial_path"]).read_bytes() == original_partial


def test_failed_download_never_publishes_partial(tmp_path):
    with client_for(tmp_path, failed_worker) as client:
        client.post("/api/jobs", json=request_for(tmp_path))
        job = wait_job(client)
        assert job["status"] == "failed"
        assert "connection error" in job["error"]
        assert not Path(job["output_path"]).exists()
        assert Path(job["partial_path"]).read_bytes() == b"partial"


def test_crash_recovery_and_no_data_deletion(tmp_path):
    db = Database(tmp_path)
    partial = tmp_path / "unfinished.part"
    partial.write_bytes(b"untouched")
    db.add("crashed", request_for(tmp_path), "game.pgn", tmp_path / "game.pgn", partial)
    recovered = Database(tmp_path).get("crashed")
    assert recovered["status"] == "interrupted"
    assert partial.read_bytes() == b"untouched"


def test_newer_database_schema_is_rejected_without_changes(tmp_path):
    database_path = tmp_path / "app.sqlite3"
    future_version = Database.VERSION + 1
    with sqlite3.connect(database_path) as connection:
        connection.execute(f"PRAGMA user_version = {future_version}")
        connection.execute("CREATE TABLE future_data (value TEXT NOT NULL)")
        connection.execute("INSERT INTO future_data VALUES (?)", ("Keep this user data",))
    original_bytes = database_path.read_bytes()
    original_files = {path.name for path in tmp_path.iterdir()}

    with pytest.raises(RuntimeError, match="newer PGN Downloader"):
        Database(tmp_path)

    assert database_path.read_bytes() == original_bytes
    assert {path.name for path in tmp_path.iterdir()} == original_files
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == future_version
        assert connection.execute("SELECT value FROM future_data").fetchone()[0] == "Keep this user data"


def test_destination_race_never_overwrites(tmp_path):
    source = tmp_path / "job.part"
    output = tmp_path / "existing.pgn"
    source.write_bytes(b"new")
    output.write_bytes(b"original")
    with pytest.raises(FileExistsError):
        JobManager.publish(source, output)
    assert output.read_bytes() == b"original"
    assert source.read_bytes() == b"new"
    assert list(tmp_path.glob(".*.part")) == []


@pytest.mark.parametrize("failure", ["destination_race", "flush_failure"])
def test_publication_failure_keeps_staging_and_records_failure(tmp_path, monkeypatch, failure):
    if failure == "destination_race":
        original_publish = JobManager.publish

        def raced_publish(staging, output, **kwargs):
            # Another program creates the chosen name after job validation.
            with output.open("xb") as handle:
                handle.write(b"Existing user export")
            return original_publish(staging, output, **kwargs)

        monkeypatch.setattr(JobManager, "publish", staticmethod(raced_publish))
    else:
        def failed_flush(descriptor):
            raise OSError("Fixture disk full during publication")

        monkeypatch.setattr("app.jobs.os.fsync", failed_flush)

    with client_for(tmp_path) as client:
        assert client.post("/api/jobs", json=request_for(tmp_path)).status_code == 201
        job = wait_job(client)
        assert job["status"] == "failed"
        assert job["finished_at"] is not None
        assert Path(job["partial_path"]).read_bytes() == SAMPLE.encode("utf-8")
        assert job["bytes"] == len(SAMPLE.encode("utf-8"))
        output = Path(job["output_path"])
        if failure == "destination_race":
            assert "not overwritten" in job["error"]
            assert output.read_bytes() == b"Existing user export"
        else:
            assert "disk full" in job["error"]
            assert not output.exists()
        assert list(output.parent.glob(".*.part")) == []

    with client_for(tmp_path) as reopened:
        saved = reopened.app.state.db.get(job["id"])
        assert saved["status"] == "failed"
        assert Path(saved["partial_path"]).read_bytes() == SAMPLE.encode("utf-8")


def test_auth_origin_and_validation(tmp_path):
    with client_for(tmp_path) as client:
        assert client.get("/api/jobs", headers={"X-App-Token": "wrong"}).status_code == 401
        assert client.get("/api/jobs", headers={"Origin": "https://untrusted.example"}).status_code == 403
        assert client.get("/api/jobs", headers={"Host": "untrusted.example"}).status_code == 400
        assert client.post("/api/jobs", json={**request_for(tmp_path), "filename": "../x.pgn"}).status_code == 422
        assert client.post("/api/jobs/missing/cancel").status_code == 404
        assert client.get("/").status_code == 200
        assert client.get("/static/app.js").status_code == 200


def test_single_instance_lock_preserves_running_jobs(tmp_path):
    with client_for(tmp_path):
        with pytest.raises(RuntimeError, match="already running"):
            with client_for(tmp_path):
                pass
