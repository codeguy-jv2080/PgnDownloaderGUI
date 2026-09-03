"""Update the one fixed Windows installation only when the application is stopped."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from app.paths import InstanceLock, default_data_dir


APP_NAME = "PgnDownloaderGUI.exe"
CLOSE_MESSAGE = "Close PGN Downloader, then rerun the build."
_ACTIVE_BUILD_ROOT: Path | None = None


def _project_path(project: Path, *parts: str) -> Path:
    """Reject redirected output components, including Windows junctions."""
    candidate = project.joinpath(*parts)
    resolved = candidate.resolve()
    if resolved != candidate or not resolved.is_relative_to(project):
        raise ValueError(f"Build paths must stay inside this project without redirects: {candidate}")
    return candidate


def _ensure_not_running() -> None:
    if os.name != "nt":
        return
    try:
        result = subprocess.run(
            ["tasklist.exe", "/FI", f"IMAGENAME eq {APP_NAME}", "/FO", "CSV", "/NH"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, errors="replace", creationflags=subprocess.CREATE_NO_WINDOW,
            timeout=15, check=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("Could not check whether PGN Downloader is running. Build stopped.") from exc
    for row in csv.reader(io.StringIO(result.stdout)):
        if row and row[0].casefold() == APP_NAME.casefold():
            raise RuntimeError(CLOSE_MESSAGE)


@contextmanager
def _existing_instance_guard():
    """Check the default source app's existing lock without creating user data."""
    path = default_data_dir() / "instance.lock"
    try:
        handle = path.open("rb")
    except FileNotFoundError:
        yield
        return
    try:
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError(CLOSE_MESSAGE) from exc
        yield
    finally:
        handle.close()


def check_ready(project_dir: Path | None = None) -> Path:
    project = (project_dir or Path(__file__).parent).resolve()
    if not (project / "PgnDownloaderGUI.spec").is_file():
        raise FileNotFoundError("PgnDownloaderGUI.spec was not found in this project.")
    _project_path(project, "build")
    _project_path(project, "build", "PgnDownloaderGUI")
    _project_path(project, "dist")
    executable = _project_path(project, "dist", "PgnDownloaderGUI", APP_NAME)
    _ensure_not_running()
    with _existing_instance_guard():
        pass
    return executable


def require_build_context(project: Path, dist_dir: Path, work_dir: Path) -> Path:
    """The spec refuses direct invocations that skip the running-app checks."""
    project = project.resolve()
    if _ACTIVE_BUILD_ROOT != project:
        raise RuntimeError("Run build.bat so the running-app checks happen before building.")
    if dist_dir.resolve() != _project_path(project, "dist") or work_dir.resolve() != _project_path(project, "build", "PgnDownloaderGUI"):
        raise ValueError("Only the fixed dist/PgnDownloaderGUI installation and build cache are allowed.")
    return _project_path(project, "dist", "PgnDownloaderGUI")


def collect_in_place(collector) -> None:
    """Use PyInstaller's collector while disabling its whole-folder deletion."""
    from PyInstaller.building import api
    from PyInstaller.config import CONF

    root = _ACTIVE_BUILD_ROOT
    if root is None:
        raise RuntimeError("A build with running-app checks is required.")
    output = require_build_context(root, Path(CONF["distpath"]), Path(CONF["workpath"]))
    if Path(collector.name).resolve() != output:
        raise ValueError("The application must be collected into dist/PgnDownloaderGUI.")
    # Check every file PyInstaller will replace. Unrelated files are preserved.
    for name, _source, kind in collector.toc:
        if kind == "DEPENDENCY":
            continue
        if Path(name).is_absolute() or ".." in Path(name).parts:
            raise ValueError(f"Unsafe bundled file path: {name}")
        prefix = () if kind in {"EXECUTABLE", "PKG"} else (collector.contents_directory or "",)
        destination = _project_path(root, "dist", "PgnDownloaderGUI", *prefix, name)
        if not destination.is_relative_to(output):
            raise ValueError(f"Bundled files must remain inside the application folder: {destination}")
    _ensure_not_running()
    original_cleaner = api._make_clean_directory

    def preserve_directory(path):
        if Path(path).resolve() != output:
            raise ValueError("Unexpected collection destination; build stopped.")
        output.mkdir(parents=True, exist_ok=True)

    # COLLECT normally recursively deletes the destination, including user files.
    # Keep its standard copying behavior but preserve all non-bundled files.
    api._make_clean_directory = preserve_directory
    try:
        api.COLLECT.assemble(collector)
    finally:
        api._make_clean_directory = original_cleaner


def _run_pyinstaller(project: Path) -> None:
    from PyInstaller.__main__ import run
    run([
        "--distpath", str(project / "dist"),
        "--workpath", str(project / "build"),
        str(project / "PgnDownloaderGUI.spec"),
    ])


def _verify_offline(project: Path, executable: Path) -> Path:
    build_dir = _project_path(project, "build")
    saved_report = _project_path(project, "build", "packaging-selftest-report.json")
    print("Running the packaged offline self-test in isolated build data...", flush=True)
    # This directory contains test data only; it is removed when the check ends.
    with tempfile.TemporaryDirectory(prefix="package-check-", dir=build_dir) as temporary:
        test_dir = Path(temporary).resolve()
        if not test_dir.is_relative_to(build_dir):
            raise ValueError("Self-test data must remain inside the build cache.")
        report_path = test_dir / "report.json"
        try:
            completed = subprocess.run(
                [str(executable), "--self-test", "--data-dir", str(test_dir), "--report", str(report_path)],
                cwd=project, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                timeout=90, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("The offline self-test timed out. The installation was updated but is unverified.") from exc
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError("The installation was updated but verification produced no valid report.") from exc
        saved_report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        if completed.returncode != 0 or not isinstance(report, dict) or report.get("ok") is not True:
            raise RuntimeError(f"The installation was updated but its offline self-test failed. Report: {saved_report}")
    return saved_report


def build_app(project_dir: Path | None = None) -> Path:
    global _ACTIVE_BUILD_ROOT
    project = (project_dir or Path(__file__).parent).resolve()
    executable = check_ready(project)
    # Serialize builders and hold the app's own default-data lock until finished.
    # A normal app launch cannot get past startup while this update is running.
    build_lock = InstanceLock(_project_path(project, "build", ".packaging"))
    try:
        build_lock.acquire()
    except RuntimeError as exc:
        raise RuntimeError("Another build is running. Wait for it to finish.") from exc
    app_lock = None
    try:
        app_lock = InstanceLock(default_data_dir())
        try:
            app_lock.acquire()
        except RuntimeError as exc:
            raise RuntimeError(CLOSE_MESSAGE) from exc
        _ensure_not_running()
        _project_path(project, "dist", "PgnDownloaderGUI", APP_NAME)
        _ACTIVE_BUILD_ROOT = project
        print(f"Updating the existing application: {executable}", flush=True)
        _run_pyinstaller(project)
        if not executable.is_file():
            raise FileNotFoundError(f"The build did not produce {executable}")
        report = _verify_offline(project, executable)
    finally:
        _ACTIVE_BUILD_ROOT = None
        if app_lock is not None:
            app_lock.release()
        build_lock.release()
    print(f"Build verified: {executable}\nOffline verification report: {report}", flush=True)
    return executable


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-only", action="store_true", help="check paths and app state without building")
    args = parser.parse_args(argv)
    try:
        if args.check_only:
            print(f"Application is stopped; ready to update: {check_ready()}")
        else:
            build_app()
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"Packaging stopped: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    # The spec imports this canonical module to check the in-process build guard.
    from package_app import main as package_main
    raise SystemExit(package_main())
