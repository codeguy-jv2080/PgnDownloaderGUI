"""Appearance and history controls use isolated data and never contact a chess site."""
import pytest
from fastapi.testclient import TestClient

from app.server import create_app


def connect(tmp_path):
    return TestClient(create_app(tmp_path / "data", "preferences-test"),
                      base_url="http://127.0.0.1", headers={"X-App-Token": "preferences-test"})


def seed_history(client, tmp_path, status="completed"):
    database = client.app.state.db
    output = tmp_path / "existing.pgn"
    partial = tmp_path / "existing.pgn.part"
    output.write_bytes(b'[Event "Existing saved game"]\n\n*\n')
    partial.write_bytes(b'[Event "Existing unfinished game"]\n')
    request = {"server": "lichess", "username": "ExistingPlayer", "output_dir": str(tmp_path)}
    database.add("existing-job", request, output.name, output, partial)
    database.update("existing-job", status=status, games=1)
    database.save_settings(output_dir=str(tmp_path), last_request=request)
    database.set_cooldown("lichess", 4102444800, "Existing server cooldown")
    return output, partial


def test_theme_persists_without_changing_download_settings_or_history(tmp_path):
    with connect(tmp_path) as client:
        initial = client.get("/api/bootstrap").json()
        assert initial["settings"]["theme"] == "light"
        assert '<html lang="en" data-theme="light">' in client.get("/").text
        seed_history(client, tmp_path)
        before = client.get("/api/bootstrap").json()

        response = client.put("/api/appearance", json={"theme": "dark"})

        assert response.status_code == 200
        assert response.json() == {**before["settings"], "theme": "dark"}
        after = client.get("/api/bootstrap").json()
        assert after["settings"] == response.json()
        assert after["jobs"] == before["jobs"]
        assert client.app.state.db.cooldown("lichess")["retry_at"] == 4102444800
        page = client.get("/")
        assert page.status_code == 200
        assert page.headers["content-type"].startswith("text/html")
        assert page.headers["cache-control"] == "no-store"
        assert page.headers["x-content-type-options"] == "nosniff"
        assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
        assert '<html lang="en" data-theme="dark">' in page.text

    with connect(tmp_path) as reopened:
        assert reopened.get("/api/bootstrap").json()["settings"]["theme"] == "dark"
        assert '<html lang="en" data-theme="dark">' in reopened.get("/").text
        saved = reopened.put("/api/settings", json={"output_dir": str(tmp_path / "different-output")})
        assert saved.status_code == 200
        assert saved.json()["theme"] == "dark"
        assert reopened.get("/api/jobs").json() == before["jobs"]
        restored = reopened.put("/api/appearance", json={"theme": "light"})
        assert restored.status_code == 200
        assert restored.json()["output_dir"] == str(tmp_path / "different-output")
        assert '<html lang="en" data-theme="light">' in reopened.get("/").text


@pytest.mark.parametrize("payload", [
    {}, {"theme": None}, {"theme": "sepia"}, {"theme": "DARK"}, {"theme": 1},
    {"theme": "dark", "output_dir": "do-not-change"},
    {"theme": "dark", "last_request": None},
])
def test_invalid_appearance_request_changes_nothing(tmp_path, payload):
    with connect(tmp_path) as client:
        seed_history(client, tmp_path)
        before = client.get("/api/bootstrap").json()
        assert client.put("/api/appearance", json=payload).status_code == 422
        assert client.get("/api/bootstrap").json() == before


def test_invalid_stored_theme_is_not_inserted_into_html(tmp_path):
    with connect(tmp_path) as client:
        client.app.state.db.save_settings(theme='dark"><script>unexpected()</script>')
        assert client.get("/api/bootstrap").json()["settings"]["theme"] == "light"
        page = client.get("/").text
        assert '<html lang="en" data-theme="light">' in page
        assert "unexpected()" not in page


def test_clear_history_preserves_exports_partials_settings_and_cooldowns(tmp_path):
    with connect(tmp_path) as client:
        output, partial = seed_history(client, tmp_path)
        client.put("/api/appearance", json={"theme": "dark"}).raise_for_status()
        settings = client.get("/api/bootstrap").json()["settings"]
        cooldown = client.app.state.db.cooldown("lichess")
        originals = {path: path.read_bytes() for path in (output, partial)}

        response = client.delete("/api/jobs")

        assert response.status_code == 200
        assert response.json() == {"cleared": 1, "jobs": []}
        assert client.get("/api/jobs").json() == []
        assert client.get("/api/bootstrap").json()["settings"] == settings
        assert client.app.state.db.cooldown("lichess") == cooldown
        assert {path: path.read_bytes() for path in originals} == originals
        assert client.delete("/api/jobs").json() == {"cleared": 0, "jobs": []}

    with connect(tmp_path) as reopened:
        assert reopened.get("/api/jobs").json() == []
        assert reopened.get("/api/bootstrap").json()["settings"] == settings
        assert reopened.app.state.db.cooldown("lichess") == cooldown
        assert {path: path.read_bytes() for path in originals} == originals


@pytest.mark.parametrize("status", ["queued", "running", "waiting"])
def test_clear_history_rejected_during_active_download(tmp_path, status):
    with connect(tmp_path) as client:
        output, partial = seed_history(client, tmp_path, status=status)
        manager = client.app.state.manager
        with manager.lock:
            manager.active_id = "existing-job"
        before = client.get("/api/bootstrap").json()

        response = client.delete("/api/jobs")

        assert response.status_code == 409
        assert "current download" in response.json()["detail"]
        assert client.get("/api/bootstrap").json() == before
        assert output.is_file() and partial.is_file()


def test_new_mutating_endpoints_require_local_app_session(tmp_path):
    with connect(tmp_path) as client:
        seed_history(client, tmp_path)
        before = client.get("/api/bootstrap").json()
        for headers in ({"X-App-Token": "wrong"}, {"Origin": "https://untrusted.example"}):
            assert client.put("/api/appearance", json={"theme": "dark"}, headers=headers).status_code in (401, 403)
            assert client.delete("/api/jobs", headers=headers).status_code in (401, 403)
        assert client.get("/api/bootstrap").json() == before
