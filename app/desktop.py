"""Native Windows window around the authenticated, loopback-only web interface."""

from __future__ import annotations

import ctypes
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
from platform import machine
import socket
import threading
import time
from urllib.parse import quote


logger = logging.getLogger(__name__)


def _require_edge_runtime() -> None:
    """Check the same local runtime registrations used by pywebview's renderer."""
    if os.name != "nt":
        raise RuntimeError("The desktop window requires Windows.")
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full"
        ) as key:
            release, _ = winreg.QueryValueEx(key, "Release")
        if int(release) < 394802:
            raise ValueError("The installed .NET Framework is too old.")
    except (OSError, TypeError, ValueError) as exc:
        raise RuntimeError("The desktop window requires Microsoft .NET Framework 4.6.2 or newer.") from exc

    # These are the Evergreen, Beta, Dev and Canary registrations recognized by pywebview.
    runtime_ids = (
        "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",
        "{2CD8A007-E189-409D-A2C8-9AF4EF3C72AA}",
        "{0D50BFEC-CD6A-4F9A-964C-C7416E3ACB10}",
        "{65C35B14-6C1D-4122-AC46-7148CC9D6497}",
    )
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        registry_base = r"SOFTWARE\Microsoft\EdgeUpdate\Clients"
        if hive == winreg.HKEY_LOCAL_MACHINE and machine() != "x86":
            registry_base = r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients"
        for runtime_id in runtime_ids:
            try:
                with winreg.OpenKey(hive, registry_base + "\\" + runtime_id) as key:
                    version, _ = winreg.QueryValueEx(key, "pv")
                if int(str(version).split(".")[0]) >= 86:
                    return
            except (OSError, TypeError, ValueError):
                continue
    raise RuntimeError(
        "Microsoft Edge WebView2 Runtime is required for the desktop window. "
        "Install the Microsoft Edge WebView2 Evergreen Runtime, then launch PGN Downloader again."
    )


def _local_path(value: str) -> Path:
    """Resolve a real local path; never pass URLs or network paths to the shell."""
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError("Choose an existing local folder or PGN file.")
    if "://" in value or value.startswith(("\\\\", "//")):
        raise ValueError("Only local folders and PGN files can be opened.")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError("An absolute local path is required.")
    path = path.resolve(strict=True)
    if str(path).startswith(("\\\\", "//")):
        raise ValueError("Network paths are not supported.")
    if os.name == "nt" and ctypes.windll.kernel32.GetDriveTypeW(str(path.anchor)) == 4:
        raise ValueError("Network drives are not supported.")
    return path


class DesktopBridge:
    """Small native API invoked by the page's folder/file action buttons."""

    def __init__(self) -> None:
        self._window = None

    def _bind_window(self, window) -> None:
        self._window = window

    def choose_folder(self, current: str | None = None) -> str | None:
        if self._window is None:
            return None
        try:
            import webview

            directory = str(Path.home())
            if current:
                try:
                    candidate = _local_path(current)
                    if candidate.is_dir():
                        directory = str(candidate)
                except (OSError, ValueError):
                    pass
            selection = self._window.create_file_dialog(
                webview.FileDialog.FOLDER,
                directory=directory,
                allow_multiple=False,
            )
            if not selection:
                return None
            selected = selection if isinstance(selection, str) else selection[0]
            folder = _local_path(selected)
            return str(folder) if folder.is_dir() else None
        except Exception:
            logger.exception("Could not choose an output folder")
            return None

    def open_path(self, path: str) -> dict[str, bool | str | None]:
        try:
            target = _local_path(path)
            if not target.is_dir() and not (target.is_file() and target.suffix.lower() == ".pgn"):
                raise ValueError("Only existing local folders and .pgn files can be opened.")
            os.startfile(str(target))
            return {"ok": True, "error": None}
        except (OSError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}


def run_desktop(data_dir: Path, token: str) -> None:
    """Start the server, wait for its lifespan, and run an Edge WebView2 window."""
    import uvicorn
    import webview

    from app.server import create_app

    _require_edge_runtime()
    data_dir.mkdir(parents=True, exist_ok=True)
    log_handler = RotatingFileHandler(
        data_dir / "app.log", maxBytes=2 * 1024 * 1024, backupCount=2, encoding="utf-8"
    )
    log_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root_logger = logging.getLogger()
    root_logger.addHandler(log_handler)
    root_logger.setLevel(logging.INFO)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server = None
    server_thread = None
    server_errors: list[BaseException] = []

    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        port = listener.getsockname()[1]
        listener.setblocking(False)
        application = create_app(data_dir=data_dir, token=token)
        server = uvicorn.Server(
            uvicorn.Config(
                application,
                host="127.0.0.1",
                port=port,
                access_log=False,
                lifespan="on",
                log_config=None,
            )
        )

        def serve() -> None:
            try:
                server.run(sockets=[listener])
            except BaseException as exc:
                server_errors.append(exc)
                logger.exception("The local server stopped unexpectedly")

        server_thread = threading.Thread(target=serve, name="pgn-local-server", daemon=True)
        server_thread.start()
        deadline = time.monotonic() + 20
        while not server.started:
            if not server_thread.is_alive():
                if server_errors:
                    raise RuntimeError(f"The local server could not start: {server_errors[0]}") from server_errors[0]
                raise RuntimeError(
                    "The local server could not start. Check whether PGN Downloader is already running "
                    f"and see {data_dir / 'app.log'} for details."
                )
            if time.monotonic() >= deadline:
                raise RuntimeError("The local server did not finish starting within 20 seconds.")
            time.sleep(0.05)

        bridge = DesktopBridge()
        window = webview.create_window(
            "PGN Downloader",
            f"http://127.0.0.1:{port}/#token={quote(token, safe='')}",
            js_api=bridge,
            width=1180,
            height=820,
            min_size=(900, 650),
            text_select=True,
            background_color="#20262E" if application.state.db.settings()["theme"] == "dark" else "#DFE3E8",
        )
        if window is None:
            raise RuntimeError("The native desktop window could not be created.")
        bridge._bind_window(window)

        def on_closed() -> None:
            server.should_exit = True

        window.events.closed += on_closed
        # Fail clearly when WebView2 is missing; never fall back to an external browser.
        webview.start(gui="edgechromium", debug=False, http_server=False, private_mode=True)
    finally:
        if server is not None:
            server.should_exit = True
        if server_thread is not None:
            server_thread.join(timeout=15)
            if server_thread.is_alive():
                logger.warning("The local server is taking longer than expected to stop")
                server.force_exit = True
                server_thread.join(timeout=3)
        listener.close()
        root_logger.removeHandler(log_handler)
        log_handler.close()
