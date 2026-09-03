"""Windows desktop entry point and deliberately headless maintenance commands."""

from __future__ import annotations

import argparse
import multiprocessing
from pathlib import Path
import secrets
import sys
import traceback
from datetime import datetime, timezone


def _port(value: str) -> int:
    port = int(value)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="PGN Downloader for Windows")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--serve", action="store_true", help="run the local server without a desktop window")
    mode.add_argument("--self-test", action="store_true", help="run offline checks without opening a window")
    mode.add_argument("--headless-smoke", action="store_true", help="alias for --self-test")
    parser.add_argument("--data-dir", type=Path, help="use this local data directory")
    parser.add_argument("--report", type=Path, help="write the self-test JSON report here")
    parser.add_argument("--port", type=_port, default=8765, help="loopback port for --serve (default: 8765)")
    parser.add_argument("--token", help="use this session token instead of generating one")
    return parser


def _record_startup_error(data_dir: Path | None) -> Path | None:
    if data_dir is None:
        return None
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        log_path = data_dir / "startup-error.log"
        with log_path.open("a", encoding="utf-8") as log:
            log.write(f"\n{datetime.now(timezone.utc).isoformat()}\n")
            traceback.print_exc(file=log)
        return log_path
    except OSError:
        return None


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    is_self_test = args.self_test or args.headless_smoke
    if args.report is not None and not is_self_test:
        parser.error("--report is only available with --self-test or --headless-smoke")
    if args.token == "":
        parser.error("--token cannot be empty")
    data_dir: Path | None = None

    try:
        from app.paths import default_data_dir

        data_dir = (args.data_dir or default_data_dir()).expanduser().resolve()
        data_dir.mkdir(parents=True, exist_ok=True)

        if is_self_test:
            from app.selftest import run

            report_path = (args.report or data_dir / "selftest-report.json").expanduser().resolve()
            return 0 if run(data_dir, report_path) else 1

        token = args.token or secrets.token_urlsafe(32)
        if args.serve:
            from urllib.parse import quote

            import uvicorn

            from app.server import create_app

            print(f"Local interface: http://127.0.0.1:{args.port}/#token={quote(token, safe='')}")
            print("Press Ctrl+C to stop the server.")
            uvicorn.run(
                create_app(data_dir=data_dir, token=token),
                host="127.0.0.1",
                port=args.port,
                access_log=False,
                lifespan="on",
            )
            return 0

        from app.desktop import run_desktop

        run_desktop(data_dir=data_dir, token=token)
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        log_path = _record_startup_error(data_dir)
        message = f"PGN Downloader could not start.\n\n{exc}"
        if log_path is not None:
            message += f"\n\nDetails were saved to:\n{log_path}"
        if not getattr(sys, "frozen", False):
            message += "\n\nIf dependencies are missing, run setup.bat first."
        if args.serve or is_self_test:
            if sys.stderr is not None:
                print(message, file=sys.stderr)
        else:
            # Only an actual desktop launch may show a native error dialog.
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, message, "PGN Downloader", 0x10)
        return 1


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
