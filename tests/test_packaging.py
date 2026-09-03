"""Packaging guard tests use fake bundles and mocked launches; no app or network."""

from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

import package_app as packaging
from app.paths import InstanceLock


@pytest.fixture(autouse=True)
def no_actual_launches(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Packaging tests must not launch processes")
    monkeypatch.setattr(packaging.subprocess, "run", blocked)
    monkeypatch.setattr("platform.win32_ver", lambda: ("10", "10.0.19045", "SP0", "Multiprocessor Free"))
    monkeypatch.setattr(packaging, "_ACTIVE_BUILD_ROOT", None)


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    (root / "PgnDownloaderGUI.spec").write_text("# fixture", encoding="utf-8")
    monkeypatch.setattr(packaging, "default_data_dir", lambda: tmp_path / "appdata")
    monkeypatch.setattr(packaging, "_ensure_not_running", lambda: None)
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("A stopped app needs no prompt"))
    return root


def test_running_executable_stops_before_build_changes(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    (root / "PgnDownloaderGUI.spec").touch()
    monkeypatch.setattr(packaging, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(packaging.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    calls = []
    def process_list(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(stdout='"PgnDownloaderGUI.exe","1234","Console","1","32,000 K"\n')
    monkeypatch.setattr(packaging.subprocess, "run", process_list)
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("Must not ask to build while app runs"))
    with pytest.raises(RuntimeError, match="Close PGN Downloader"):
        packaging.build_app(root)
    assert calls[0][0][0] == "tasklist.exe"
    assert calls[0][1]["creationflags"] == 0x08000000
    assert not (root / "dist").exists()


def test_failed_process_check_stops_build(monkeypatch):
    monkeypatch.setattr(packaging, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(packaging.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    def denied(*args, **kwargs):
        raise subprocess.CalledProcessError(1, "tasklist.exe")
    monkeypatch.setattr(packaging.subprocess, "run", denied)
    with pytest.raises(RuntimeError, match="Could not check"):
        packaging._ensure_not_running()


def test_readiness_check_does_not_create_data(project):
    expected = project / "dist" / "PgnDownloaderGUI" / packaging.APP_NAME
    assert packaging.check_ready(project) == expected
    assert not packaging.default_data_dir().exists()
    assert not (project / "dist").exists()


def test_running_source_instance_blocks_build(project, monkeypatch):
    app_lock = InstanceLock(packaging.default_data_dir())
    app_lock.acquire()
    try:
        monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("Source app is still running"))
        with pytest.raises(RuntimeError, match="Close PGN Downloader"):
            packaging.build_app(project)
    finally:
        app_lock.release()


def test_app_opened_after_initial_check_blocks_build(project, monkeypatch):
    calls = []
    def check():
        calls.append("check")
        if len(calls) == 2:
            raise RuntimeError(packaging.CLOSE_MESSAGE)
    monkeypatch.setattr(packaging, "_ensure_not_running", check)
    monkeypatch.setattr(packaging, "_run_pyinstaller", lambda _: pytest.fail("Must not build"))
    with pytest.raises(RuntimeError, match="Close PGN Downloader"):
        packaging.build_app(project)
    assert len(calls) == 2
    assert not (project / "dist").exists()
    assert packaging._ACTIVE_BUILD_ROOT is None


def test_stopped_app_builds_without_prompt_into_fixed_executable_and_holds_lock(project, monkeypatch):
    existing = project / "dist" / "PgnDownloaderGUI"
    existing.mkdir(parents=True)
    executable = existing / packaging.APP_NAME
    executable.write_bytes(b"old executable fixture")
    pgn = existing / "my-games.pgn"
    pgn.write_bytes(b"saved user games")
    stages = []
    def build(root):
        assert packaging.require_build_context(root, root / "dist", root / "build" / "PgnDownloaderGUI") == existing
        with pytest.raises(RuntimeError):
            InstanceLock(packaging.default_data_dir()).acquire()
        stages.append("build")
        executable.write_bytes(b"updated executable fixture")
    def verify(root, exe):
        assert exe == executable
        assert stages == ["build"]
        stages.append("verify")
        return root / "build" / "packaging-selftest-report.json"
    monkeypatch.setattr(packaging, "_run_pyinstaller", build)
    monkeypatch.setattr(packaging, "_verify_offline", verify)
    assert packaging.build_app(project) == executable
    assert sorted(p.name for p in (project / "dist").iterdir()) == ["PgnDownloaderGUI"]
    assert executable.read_bytes() == b"updated executable fixture"
    assert pgn.read_bytes() == b"saved user games"
    assert packaging._ACTIVE_BUILD_ROOT is None
    lock = InstanceLock(packaging.default_data_dir())
    lock.acquire()
    lock.release()


def test_spec_cannot_skip_running_app_checks(project):
    with pytest.raises(RuntimeError, match="running-app checks"):
        packaging.require_build_context(project, project / "dist", project / "build" / "PgnDownloaderGUI")


def test_spec_rejects_alternate_distribution(project, monkeypatch):
    monkeypatch.setattr(packaging, "_ACTIVE_BUILD_ROOT", project)
    with pytest.raises(ValueError, match="fixed"):
        packaging.require_build_context(project, project / "dist" / "releases", project / "build" / "PgnDownloaderGUI")


def test_collection_updates_in_place_without_removing_user_files(project, monkeypatch):
    from PyInstaller.building import api
    from PyInstaller.config import CONF
    monkeypatch.setattr(packaging, "_ACTIVE_BUILD_ROOT", project)
    monkeypatch.setitem(CONF, "distpath", str(project / "dist"))
    monkeypatch.setitem(CONF, "workpath", str(project / "build" / "PgnDownloaderGUI"))
    output = project / "dist" / "PgnDownloaderGUI"
    output.mkdir(parents=True)
    (output / "user-games.pgn").write_bytes(b"do not delete")
    (output / packaging.APP_NAME).write_bytes(b"old fixture")
    source = project / "compiler-output.fixture"
    source.write_bytes(b"new fixture")
    collector = SimpleNamespace(
        name=str(output), contents_directory="_internal", tocbasename="fixture",
        toc=[(packaging.APP_NAME, str(source), "EXECUTABLE")],
    )
    cleaner = api._make_clean_directory
    packaging.collect_in_place(collector)
    assert api._make_clean_directory is cleaner
    assert (output / packaging.APP_NAME).read_bytes() == b"new fixture"
    assert (output / "user-games.pgn").read_bytes() == b"do not delete"


@pytest.mark.parametrize("destination", ["../user-games.pgn", "../../outside.pgn"])
def test_collection_rejects_paths_outside_app(project, monkeypatch, destination):
    from PyInstaller.config import CONF
    monkeypatch.setattr(packaging, "_ACTIVE_BUILD_ROOT", project)
    monkeypatch.setitem(CONF, "distpath", str(project / "dist"))
    monkeypatch.setitem(CONF, "workpath", str(project / "build" / "PgnDownloaderGUI"))
    collector = SimpleNamespace(
        name=str(project / "dist" / "PgnDownloaderGUI"), contents_directory="_internal",
        toc=[(destination, "irrelevant-source", "DATA")],
    )
    with pytest.raises(ValueError, match="Unsafe"):
        packaging.collect_in_place(collector)


def test_selftest_is_offline_hidden_and_cleans_temporary_data(project, monkeypatch):
    (project / "build").mkdir()
    seen = []
    executable = project / "dist" / "PgnDownloaderGUI" / packaging.APP_NAME
    def verify(args, **kwargs):
        seen.append(Path(args[args.index("--data-dir") + 1]))
        assert args[:2] == [str(executable), "--self-test"]
        assert seen[0].is_relative_to(project / "build")
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["creationflags"] == (subprocess.CREATE_NO_WINDOW if packaging.os.name == "nt" else 0)
        Path(args[args.index("--report") + 1]).write_text('{"ok": true}', encoding="utf-8")
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(packaging.subprocess, "run", verify)
    report = packaging._verify_offline(project, executable)
    assert report == project / "build" / "packaging-selftest-report.json"
    assert report.exists()
    assert not seen[0].exists()
    assert list((project / "build").iterdir()) == [report]


def test_failed_selftest_reports_updated_unverified_install(project, monkeypatch):
    (project / "build").mkdir()
    def verify(args, **kwargs):
        Path(args[args.index("--report") + 1]).write_text('{"ok": false}', encoding="utf-8")
        return SimpleNamespace(returncode=1)
    monkeypatch.setattr(packaging.subprocess, "run", verify)
    with pytest.raises(RuntimeError, match="installation was updated.*failed"):
        packaging._verify_offline(project, project / "dist" / "PgnDownloaderGUI" / packaging.APP_NAME)
    assert not list((project / "build").glob("package-check-*"))
