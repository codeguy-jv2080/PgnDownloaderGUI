"""Adapt the vendored downloader inside an isolated worker process.

Provider download and filtering logic remains in pgn_downloader. The worker's
small adapters add network bounds, Windows-safe output, and GUI progress.
"""

from __future__ import annotations

import builtins
from contextlib import contextmanager
from datetime import timezone
from email.utils import parsedate_to_datetime
import json
import math
from pathlib import Path
import re
import sys
import time
from typing import Any, Callable, Iterator
from urllib.parse import urljoin, urlsplit

import requests

from .models import DownloadRequest


VENDOR_DIRECTORY = Path(__file__).resolve().parents[1] / "vendor"
if str(VENDOR_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(VENDOR_DIRECTORY))

from pgn_downloader import chess_com, lichess  # noqa: E402


UPSTREAM_COMMIT = "d133caef155244b12e04b225ad5e1a9321d9fc1c"
_ALLOWED_HOSTS = frozenset({"api.chess.com", "lichess.org"})
MAX_TOTAL_WAIT = 3600
MAX_ATTEMPTS = 16
MAX_ERROR_BODY = 4096


def get_backend() -> dict[str, str]:
    return {"name": "pgn-downloader", "commit": UPSTREAM_COMMIT}


class DownloadError(RuntimeError):
    """An error suitable for display in the local GUI."""

    def __init__(self, message: str, *, status_code: int | None = None,
                 server_message: str | None = None, reason: str | None = None,
                 retry_at: float | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.server_message = server_message
        self.reason = reason
        self.retry_at = retry_at

    def details(self) -> dict[str, Any]:
        return {name: value for name in ("status_code", "server_message", "reason", "retry_at")
                if (value := getattr(self, name)) is not None}


def _server_error(response: Any) -> str:
    """Read a bounded JSON error, never display an HTML error document."""
    body = bytearray()
    try:
        for chunk in response.iter_content(chunk_size=1024):
            if chunk:
                body.extend(chunk[:MAX_ERROR_BODY - len(body)])
                if len(body) >= MAX_ERROR_BODY:
                    break
        payload = json.loads(body.decode("utf-8"))
        value = payload.get("error") if isinstance(payload, dict) else None
        if not isinstance(value, str) or "<" in value or ">" in value:
            return ""
        return " ".join("".join(character for character in value
                                if character.isprintable() or character.isspace()).split())[:240]
    except (ValueError, UnicodeError, requests.RequestException):
        return ""


def _retry_delay(header: Any, now: float, backoff: float) -> float:
    """Honor Retry-After while retaining the minimum and exponential backoff."""
    requested = 0.0
    if isinstance(header, str):
        value = header.strip()
        try:
            if value.isdecimal():
                requested = float(value)
            else:
                date = parsedate_to_datetime(value)
                if date.tzinfo is None:
                    date = date.replace(tzinfo=timezone.utc)
                requested = date.timestamp() - now
        except (ValueError, TypeError, IndexError, OverflowError):
            requested = 0.0
    if not math.isfinite(requested):
        requested = 0.0
    return max(60.0, backoff, requested)


def _network_message(error: Exception) -> str:
    if isinstance(error, requests.Timeout):
        return "The chess site took too long to respond. Please try again."
    if isinstance(error, requests.ConnectionError):
        return "Could not connect to the chess site. Check your connection and try again."
    return "The download connection failed. Please try again."


class _Response:
    def __init__(self, response: Any):
        self.response = response

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_: object) -> None:
        self.response.close()

    def raise_for_status(self) -> None:
        self.response.raise_for_status()

    def json(self) -> Any:
        try:
            return self.response.json()
        except ValueError as error:
            raise DownloadError("The chess site returned an unreadable response. Please try again.") from error
        except requests.RequestException as error:
            raise DownloadError(_network_message(error)) from error
        finally:
            self.response.close()

    def iter_content(self, *args: Any, **kwargs: Any) -> Iterator[bytes]:
        try:
            for chunk in self.response.iter_content(*args, **kwargs):
                if chunk:
                    yield chunk
        except requests.RequestException as error:
            raise DownloadError(_network_message(error)) from error


class _Requests:
    """A module-local proxy; it never changes the global requests module."""

    def __init__(self, on_event: Callable[[dict], Any] | None = None, *,
                 clock: Callable[[], float] | None = None,
                 sleep: Callable[[float], Any] | None = None,
                 max_total_wait: float = MAX_TOTAL_WAIT, max_attempts: int = MAX_ATTEMPTS):
        self.on_event = on_event
        self.clock = clock or time.time
        self.sleep = sleep or time.sleep
        self.max_total_wait = max(0.0, min(float(max_total_wait), MAX_TOTAL_WAIT))
        self.max_attempts = max(1, min(int(max_attempts), MAX_ATTEMPTS))
        # One instance is shared by every archive request in a worker, so retries
        # cannot multiply the waiting budget by the number of Chess.com months.
        self.total_wait = 0.0
        self.retries = 0

    def _limited(self, response: Any, host: str) -> None:
        try:
            server_message = _server_error(response)
            retry_after = response.headers.get("Retry-After")
        finally:
            response.close()
        reason = "concurrency" if re.search(
            r"\bonly run\b.*\brequest.*\bat a time\b", server_message, re.IGNORECASE,
        ) else "rate_limit"
        site = "Lichess" if host == "lichess.org" else "Chess.com"
        explanation = (f"{site} reports another active download for this connection."
                       if reason == "concurrency"
                       else f"{site} has temporarily limited requests.")
        now = self.clock()
        delay = _retry_delay(retry_after, now, min(60 * 2 ** self.retries, 300))
        retry_at = now + delay
        details = {"status_code": 429, "server_message": server_message,
                   "reason": reason, "retry_at": retry_at}
        if (self.on_event is None or self.retries >= self.max_attempts - 1
                or self.total_wait + delay > self.max_total_wait):
            stopped = (f" Automatic retries stopped after {self.retries} retries and "
                       f"{self.total_wait:g} seconds of waiting." if self.on_event is not None else "")
            raise DownloadError(
                f"{explanation}{stopped} The app cannot clear the server quota. "
                "A new attempt will wait for the remaining server cooldown.", **details,
            )
        self.retries += 1
        self.on_event({
            "type": "waiting", "attempt": self.retries,
            "message": f"{explanation} Retrying automatically when the countdown ends.",
            **details,
        })
        # Sleeping is confined to the killable worker process. Its parent retains
        # the waiting event and can cancel immediately by terminating the worker.
        self.sleep(delay)
        self.total_wait += delay
        self.on_event({"type": "resumed", "message": f"Retrying {site} download…"})

    def get(self, url: str, **kwargs: Any) -> _Response:
        kwargs["timeout"] = (15, 60)
        kwargs["allow_redirects"] = False
        # Inspect status before downloading the body, including Chess.com JSON,
        # so even a large error document is subject to the bounded error reader.
        kwargs["stream"] = True
        headers = {
            name: value for name, value in (kwargs.get("headers") or {}).items()
            if name.lower() not in ("user-agent", "accept")
        }
        headers["User-Agent"] = "PgnDownloaderGUI/1.0 (backend: https://github.com/Simske/pgn-downloader)"
        redirects = 0
        while True:
            try:
                parsed = urlsplit(url)
                permitted = (
                    parsed.scheme == "https"
                    and parsed.hostname in _ALLOWED_HOSTS
                    and parsed.port in (None, 443)
                    and parsed.username is None
                    and parsed.password is None
                    and not any(character.isspace() or ord(character) == 127 for character in url)
                )
            except ValueError:
                permitted = False
            if not permitted:
                raise DownloadError("The chess site returned an unsupported download address.")
            kwargs["headers"] = {
                **headers,
                "Accept": "application/x-chess-pgn"
                if parsed.hostname == "lichess.org" and parsed.path.startswith("/api/games/user/")
                else "application/json",
            }
            try:
                response = requests.get(url, **kwargs)
            except requests.RequestException as error:
                raise DownloadError(_network_message(error)) from error
            status = response.status_code
            if status == 429:
                self._limited(response, parsed.hostname)
                continue
            if 300 <= status < 400:
                location = response.headers.get("Location")
                response_url = getattr(response, "url", None) or url
                response.close()
                if redirects >= 3:
                    raise DownloadError("The chess site redirected the download too many times. Please try again later.")
                try:
                    if status not in (301, 302, 303, 307, 308) or not isinstance(location, str) or not location:
                        raise ValueError("Missing redirect destination")
                    if any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in location):
                        raise ValueError("Invalid redirect characters")
                    destination = urlsplit(location)
                    if (destination.scheme or location.startswith("//")) and not destination.netloc:
                        raise ValueError("Incomplete redirect address")
                    url = urljoin(response_url, location)
                except ValueError as error:
                    raise DownloadError("The chess site redirected the download without a valid destination.") from error
                # The Location URL supplies its own query, including canonicalization.
                kwargs.pop("params", None)
                redirects += 1
                continue
            try:
                response.raise_for_status()
            except requests.HTTPError as error:
                response.close()
                if status == 404:
                    message = "The player or game archive was not found. Check the username and selected site."
                elif status in (401, 403):
                    message = "The chess site denied access to these games. Please try again later."
                elif status >= 500:
                    message = "The chess site is temporarily unavailable. Please try again later."
                else:
                    message = f"The chess site rejected the download (HTTP {status}). Check your filters and try again."
                raise DownloadError(message) from error
            return _Response(response)


class _Progress:
    def __init__(self, events: Any, server: str):
        self.events = events
        self.server = server
        self.games = 0
        self.bytes = 0
        self.last_event = 0.0

    def emit(self, *, force: bool = False) -> None:
        current = time.monotonic()
        if not force and current - self.last_event < 0.25:
            return
        self.last_event = current
        message = (
            f"Downloading games · {self.games:,} saved"
            if self.server == "chess.com"
            else "Downloading PGN from Lichess"
        )
        self.events.put({
            "type": "progress", "games": self.games,
            "bytes": self.bytes, "message": message,
        })


class _ProgressBar:
    def __init__(self, progress: _Progress, **_: Any):
        self.progress = progress

    def __enter__(self) -> _ProgressBar:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def update(self, amount: int = 1) -> None:
        # Lichess's original counter counts blank lines, not actual games.
        if self.progress.server == "chess.com":
            self.progress.games += amount
        self.progress.emit()


class _Output:
    def __init__(self, file: Any, progress: _Progress):
        self.file = file
        self.progress = progress

    def __enter__(self) -> _Output:
        self.file.__enter__()
        return self

    def __exit__(self, *args: Any) -> Any:
        return self.file.__exit__(*args)

    def write(self, content: bytes | str) -> int:
        written = self.file.write(content)
        self.progress.bytes += len(content.encode("utf-8")) if isinstance(content, str) else len(content)
        return written

    def __getattr__(self, name: str) -> Any:
        return getattr(self.file, name)


@contextmanager
def _adapt_provider(module: Any, progress: _Progress) -> Iterator[None]:
    missing = object()
    originals: dict[str, Any] = {}

    def replace(name: str, value: Any) -> None:
        originals[name] = getattr(module, name, missing)
        setattr(module, name, value)

    def output_open(path: Any, mode: str = "r", *args: Any, **kwargs: Any) -> _Output:
        if "b" not in mode:
            kwargs["encoding"] = "utf-8"
            kwargs["newline"] = "\n"
        return _Output(builtins.open(path, mode, *args, **kwargs), progress)

    try:
        replace("requests", _Requests(on_event=progress.events.put))
        replace("tqdm", lambda **kwargs: _ProgressBar(progress, **kwargs))
        replace("open", output_open)
        if module is chess_com:
            original_filter = module.filter_game

            def filter_game(game: dict, username: str, color: str | None = None,
                            since: Any = None, until: Any = None, modes: Any = None) -> bool:
                if color is not None:
                    game = dict(game)
                    game[color] = dict(game[color])
                    game[color]["username"] = game[color]["username"].casefold()
                    username = username.casefold()
                return original_filter(game, username, color, since, until, modes)

            replace("filter_game", filter_game)
        yield
    finally:
        for name, value in originals.items():
            if value is missing:
                delattr(module, name)
            else:
                setattr(module, name, value)


def _count_games(path: str) -> int:
    with builtins.open(path, "rb") as output:
        return sum(line.startswith(b"[Event ") for line in output)


def run_download(request: dict, staging_path: str, events: Any) -> None:
    """Multiprocessing entry point. Write only to the assigned staging path."""
    try:
        options = DownloadRequest.model_validate(request)
        since, until = options.parsed_dates()
        module = lichess if options.server == "lichess" else chess_com
        progress = _Progress(events, options.server)
        progress.emit(force=True)
        with _adapt_provider(module, progress):
            module.download_pgn(
                options.username, staging_path, color=options.color,
                since=since, until=until, modes=options.modes,
            )
        progress.games = _count_games(staging_path)
        progress.bytes = Path(staging_path).stat().st_size
        progress.emit(force=True)
        events.put({"type": "done", "games": progress.games, "bytes": progress.bytes})
    except DownloadError as error:
        events.put({"type": "error", "message": str(error), **error.details()})
    except FileExistsError:
        events.put({"type": "error", "message": "A file already exists at the temporary download path. No file was overwritten."})
    except PermissionError:
        events.put({"type": "error", "message": "Windows denied access to the output folder. Choose a writable folder and try again."})
    except OSError as error:
        events.put({"type": "error", "message": f"Could not write the PGN file: {error}"})
    except Exception as error:
        events.put({"type": "error", "message": str(error) or "The download failed. Please try again."})
