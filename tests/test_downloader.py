"""Provider contract tests use local responses; no chess-site requests are made."""

from datetime import datetime, timezone
from email.utils import format_datetime
import json
from queue import Empty, Queue

import pytest
import requests
from pydantic import ValidationError

from app import downloader
from app.models import DEFAULT_MODES, DownloadRequest, OutputSettings
from pgn_downloader import date_parser


class Response:
    def __init__(self, *, data=None, chunks=(), status=200, headers=None, body=None):
        self.data = data
        self.chunks = (body,) if body is not None else chunks
        self.status_code = status
        self.headers = headers or {}
        self.closed = False

    def json(self):
        return self.data

    def iter_content(self, chunk_size):
        assert chunk_size in (8192, 1024)
        yield from self.chunks

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def prevent_live_downloads(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("This test attempted an unmocked network request.")

    monkeypatch.setattr(downloader.requests, "get", blocked)


class FakeClock:
    def __init__(self):
        self.current = 1_700_000_000.0
        self.sleeps = []
        self.on_sleep = None

    def now(self):
        return self.current

    def sleep(self, seconds):
        if self.on_sleep:
            self.on_sleep(seconds)
        self.sleeps.append(seconds)
        self.current += seconds


@pytest.fixture
def worker_clock(monkeypatch):
    clock = FakeClock()
    original = downloader._Requests
    monkeypatch.setattr(downloader, "_Requests", lambda on_event=None, **kwargs: original(
        on_event, clock=clock.now, sleep=clock.sleep, **kwargs,
    ))
    return clock


def request_for(tmp_path, **changes):
    values = {"server": "lichess", "username": "Example", "output_dir": str(tmp_path)}
    values.update(changes)
    return DownloadRequest(**values)


def run(request, path):
    events = Queue()
    downloader.run_download(request.model_dump(), str(path), events)
    output = []
    while True:
        try:
            output.append(events.get_nowait())
        except Empty:
            return output


def test_absolute_dates_preserve_inclusive_local_boundaries(tmp_path):
    request = request_for(tmp_path, since=" 2024-02 ", until="2024-02")
    since, until = request.parsed_dates()
    assert (since.year, since.month, since.day, since.hour) == (2024, 2, 1, 0)
    assert (until.year, until.month, until.day, until.hour, until.microsecond) == (2024, 2, 29, 23, 999999)
    assert since.tzinfo is not None and until.tzinfo is not None


def test_relative_dates_reuse_upstream_calendar_semantics(tmp_path, monkeypatch):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2024, 6, 4, 16, 30, tzinfo=tz)

    monkeypatch.setattr(date_parser, "datetime", FixedDatetime)
    since, until = request_for(tmp_path, since="3m", until="1d").parsed_dates()
    assert (since.year, since.month, since.day, since.hour) == (2024, 3, 1, 0)
    assert (until.year, until.month, until.day, until.hour, until.microsecond) == (2024, 6, 3, 23, 999999)


def test_provider_defaults_and_output_names(tmp_path):
    request = request_for(tmp_path, server="chess.com", username=" MixedCase ", color="black", since=" ")
    assert request.modes == list(DEFAULT_MODES["chess.com"])
    assert request.since is None
    assert request.generated_filename == "chess.com_MixedCase-black.pgn"
    assert request_for(tmp_path, modes=["rapid", "rapid", "blitz"]).modes == ["rapid", "blitz"]
    assert request_for(tmp_path, filename="custom.PGN").generated_filename == "custom.PGN"


@pytest.mark.parametrize("changes", [
    {"username": "../someone"},
    {"username": "someone?token=1"},
    {"modes": []},
    {"server": "chess.com", "modes": ["atomic"]},
    {"since": "2024-02-30"},
    {"since": "2025", "until": "2024"},
    {"until": "9999"},
    {"filename": "../games.pgn"},
    {"filename": "subfolder\\games.pgn"},
    {"filename": "CON.pgn"},
    {"filename": "com1.backup.pgn"},
    {"filename": "games.pgn:alternate"},
    {"filename": "games.txt"},
    {"output_dir": "relative-folder"},
])
def test_invalid_or_unsafe_download_inputs_are_rejected(tmp_path, changes):
    with pytest.raises(ValidationError):
        request_for(tmp_path, **changes)


def test_output_settings_require_an_absolute_path(tmp_path):
    assert OutputSettings(output_dir=f" {tmp_path} ").output_dir == str(tmp_path)
    with pytest.raises(ValidationError):
        OutputSettings(output_dir="relative-folder")


def test_lichess_preserves_pgn_bytes_filters_and_accurate_final_count(tmp_path, monkeypatch):
    pgn = b'[Event "One"]\n[White "Example"]\n\n1. e4 e5 1/2-1/2\n\n[Event "Two"]\n\n1. d4 d5 *\n\n'
    response = Response(chunks=[pgn[:16], b"", pgn[16:53], pgn[53:]])
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return response

    monkeypatch.setattr(downloader.requests, "get", get)
    original_requests = downloader.lichess.requests
    original_progress = downloader.lichess.tqdm
    request = request_for(tmp_path, color="white", since="2024-01-01", until="2024-01-31", modes=["rapid"])
    path = tmp_path / "download.part"
    events = run(request, path)
    assert path.read_bytes() == pgn
    assert events[-1] == {"type": "done", "games": 2, "bytes": len(pgn)}
    assert all(event["games"] == 0 for event in events[:-2])
    url, arguments = calls[0]
    assert url == "https://lichess.org/api/games/user/Example"
    assert arguments["timeout"] == (15, 60)
    assert arguments["allow_redirects"] is False
    assert arguments["stream"] is True
    assert arguments["headers"] == {
        "User-Agent": "PgnDownloaderGUI/1.0 (backend: https://github.com/Simske/pgn-downloader)",
        "Accept": "application/x-chess-pgn",
    }
    since, until = request.parsed_dates()
    assert arguments["params"] == {
        "sort": "dateAsc", "color": "white", "perfType": "rapid",
        "since": int(since.timestamp() * 1000), "until": int(until.timestamp() * 1000),
    }
    assert response.closed
    assert downloader.lichess.requests is original_requests
    assert downloader.lichess.tqdm is original_progress
    assert "open" not in downloader.lichess.__dict__


def test_chess_com_reuses_filters_accepts_username_case_and_writes_utf8(tmp_path, monkeypatch):
    pgn = '[Event "Café ♞"]\n\n1. e4 e5 *'
    base = {
        "pgn": pgn, "rules": "chess", "time_class": "daily",
        "white": {"username": "eXaMpLe"}, "black": {"username": "Opponent"},
        "end_time": int(datetime(2024, 1, 15, tzinfo=timezone.utc).timestamp()),
    }
    games = [
        base,
        {**base, "rules": "chess960"},
        {**base, "time_class": "bullet"},
        {**base, "white": {"username": "Other"}},
        {**base, "end_time": int(datetime(2023, 1, 15, tzinfo=timezone.utc).timestamp())},
        {key: value for key, value in base.items() if key != "pgn"},
    ]
    urls = ["https://api.chess.com/pub/player/example/games/2023/12", "https://api.chess.com/pub/player/example/games/2024/01"]
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return Response(data={"archives": urls} if url.endswith("archives") else {"games": games})

    monkeypatch.setattr(downloader.requests, "get", get)
    original_filter = downloader.chess_com.filter_game
    path = tmp_path / "download.part"
    events = run(request_for(tmp_path, server="chess.com", color="white", since="2024-01", until="2024-01", modes=["correspondence"]), path)
    expected = (pgn + "\n\n").encode("utf-8")
    assert path.read_bytes() == expected
    assert events[-1] == {"type": "done", "games": 1, "bytes": len(expected)}
    assert len(calls) == 2 and calls[-1][0] == urls[1]
    assert all(arguments["timeout"] == (15, 60) and arguments["allow_redirects"] is False for _, arguments in calls)
    assert all(arguments["stream"] is True for _, arguments in calls)
    assert all(arguments["headers"] == {
        "User-Agent": "PgnDownloaderGUI/1.0 (backend: https://github.com/Simske/pgn-downloader)",
        "Accept": "application/json",
    } for _, arguments in calls)
    assert base["white"]["username"] == "eXaMpLe"
    assert downloader.chess_com.filter_game is original_filter


@pytest.mark.parametrize("status,expected", [
    (404, "not found"), (403, "denied access"),
    (503, "temporarily unavailable"), (302, "redirected"),
])
def test_http_errors_are_friendly_and_do_not_create_output(tmp_path, monkeypatch, status, expected):
    response = Response(status=status)
    monkeypatch.setattr(downloader.requests, "get", lambda *args, **kwargs: response)
    path = tmp_path / "download.part"
    result = run(request_for(tmp_path, server="chess.com"), path)[-1]
    assert result["type"] == "error" and expected in result["message"]
    assert not path.exists()
    assert response.closed


def test_connection_timeout_has_clear_message(tmp_path, monkeypatch):
    def get(*args, **kwargs):
        raise requests.Timeout("unhelpful internal timeout")

    monkeypatch.setattr(downloader.requests, "get", get)
    result = run(request_for(tmp_path), tmp_path / "download.part")[-1]
    assert result["type"] == "error"
    assert "too long to respond" in result["message"]


def test_safe_redirect_canonicalizes_username_and_replaces_query(monkeypatch):
    redirected = Response(status=301, headers={"Location": "/pub/player/hikaru/games/archives?canonical=1"})
    completed = Response(data={"archives": []})
    responses = iter([redirected, completed])
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return next(responses)

    monkeypatch.setattr(downloader.requests, "get", get)
    result = downloader._Requests().get(
        "https://api.chess.com/pub/player/Hikaru/games/archives", params={"old": "query"},
    ).json()
    assert result == {"archives": []}
    assert [url for url, _ in calls] == [
        "https://api.chess.com/pub/player/Hikaru/games/archives",
        "https://api.chess.com/pub/player/hikaru/games/archives?canonical=1",
    ]
    assert calls[0][1]["params"] == {"old": "query"}
    assert "params" not in calls[1][1]
    assert all(arguments["timeout"] == (15, 60) and arguments["allow_redirects"] is False for _, arguments in calls)
    assert all(arguments["headers"]["User-Agent"].startswith("PgnDownloaderGUI/1.0 (")
               and arguments["headers"]["Accept"] == "application/json" for _, arguments in calls)
    assert redirected.closed and completed.closed


@pytest.mark.parametrize("location", [
    "https://untrusted.invalid/games",
    "//untrusted.invalid/games",
    "http://api.chess.com/pub/player/hikaru/games/archives",
    "https://user:password@api.chess.com/pub/player/hikaru/games/archives",
    "https://api.chess.com:444/pub/player/hikaru/games/archives",
])
def test_redirect_target_is_validated_before_another_request(monkeypatch, location):
    response = Response(status=302, headers={"Location": location})
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        return response

    monkeypatch.setattr(downloader.requests, "get", get)
    with pytest.raises(downloader.DownloadError, match="unsupported download address"):
        downloader._Requests().get("https://api.chess.com/pub/player/Hikaru/games/archives")
    assert len(calls) == 1
    assert response.closed


@pytest.mark.parametrize("location", [None, "", "https://", "https://[", "/games\r\nInjected: header"])
def test_malformed_redirect_is_closed_and_rejected(monkeypatch, location):
    response = Response(status=302, headers={"Location": location})
    monkeypatch.setattr(downloader.requests, "get", lambda *args, **kwargs: response)
    with pytest.raises(downloader.DownloadError, match="without a valid destination"):
        downloader._Requests().get("https://api.chess.com/pub/player/Hikaru/games/archives")
    assert response.closed


def test_redirect_loop_stops_after_three_hops(monkeypatch):
    responses = []

    def get(url, **kwargs):
        assert kwargs["timeout"] == (15, 60)
        assert kwargs["allow_redirects"] is False
        response = Response(status=302, headers={"Location": "/pub/player/hikaru/games/archives"})
        responses.append(response)
        return response

    monkeypatch.setattr(downloader.requests, "get", get)
    with pytest.raises(downloader.DownloadError, match="too many times"):
        downloader._Requests().get("https://api.chess.com/pub/player/Hikaru/games/archives")
    assert len(responses) == 4  # Initial request plus at most three redirect hops.
    assert all(response.closed for response in responses)


@pytest.mark.parametrize("address", [
    "http://api.chess.com/pub/player/example/games/2024/01",
    "https://untrusted.invalid/pub/player/example/games/2024/01",
    "https://api.chess.com.evil.invalid/pub/player/example/games/2024/01",
    "https://user:password@api.chess.com/pub/player/example/games/2024/01",
])
def test_archive_urls_cannot_send_requests_to_unapproved_destinations(tmp_path, monkeypatch, address):
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        return Response(data={"archives": [address]})

    monkeypatch.setattr(downloader.requests, "get", get)
    result = run(request_for(tmp_path, server="chess.com"), tmp_path / "download.part")[-1]
    assert result["type"] == "error"
    assert "unsupported download address" in result["message"]
    assert len(calls) == 1


def test_existing_output_remains_unchanged(tmp_path, monkeypatch):
    path = tmp_path / "existing.part"
    path.write_bytes(b"Existing user data")
    monkeypatch.setattr(downloader.requests, "get", lambda *args, **kwargs: Response(chunks=[b"New PGN"]))
    result = run(request_for(tmp_path), path)[-1]
    assert result["type"] == "error"
    assert "No file was overwritten" in result["message"]
    assert path.read_bytes() == b"Existing user data"


CONCURRENCY_ERROR = "Please only run 1 request(s) at a time"


def limited_response(*, retry_after=None, message=CONCURRENCY_ERROR):
    return Response(status=429, body=json.dumps({"error": message}).encode("utf-8"),
                    headers={"Retry-After": retry_after} if retry_after is not None else {})


def test_raw_concurrency_response_has_precise_metadata_and_no_implicit_retry(monkeypatch):
    response = limited_response()
    clock = FakeClock()
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        return response

    monkeypatch.setattr(downloader.requests, "get", get)
    with pytest.raises(downloader.DownloadError, match="cannot clear the server quota") as raised:
        downloader._Requests(clock=clock.now, sleep=clock.sleep).get("https://lichess.org/api/games/user/Example")
    assert raised.value.details() == {
        "status_code": 429, "server_message": CONCURRENCY_ERROR,
        "reason": "concurrency", "retry_at": clock.current + 60,
    }
    assert len(calls) == 1 and not clock.sleeps
    assert response.closed


@pytest.mark.parametrize("body", [
    b"<html><script>untrusted()</script></html>",
    b'{"error":"<script>untrusted()</script>"}',
    b'{"error":12}', b'["not an error object"]', b"not JSON",
])
def test_rate_limit_message_never_displays_untrusted_html_or_nonstring_json(monkeypatch, body):
    response = Response(status=429, body=body)
    monkeypatch.setattr(downloader.requests, "get", lambda *args, **kwargs: response)
    with pytest.raises(downloader.DownloadError) as raised:
        downloader._Requests().get("https://lichess.org/api/games/user/Example")
    assert raised.value.reason == "rate_limit"
    assert raised.value.server_message == ""
    assert "untrusted" not in str(raised.value) and "<" not in str(raised.value)
    assert response.closed


def test_error_body_read_is_bounded(monkeypatch):
    reads = []

    def chunks():
        for index in range(10):
            reads.append(index)
            yield b"x" * 1024

    response = Response(status=429, chunks=chunks())
    monkeypatch.setattr(downloader.requests, "get", lambda *args, **kwargs: response)
    with pytest.raises(downloader.DownloadError):
        downloader._Requests().get("https://lichess.org/api/games/user/Example")
    assert reads == [0, 1, 2, 3]
    assert response.closed


def test_lichess_429_then_success_runs_provider_once_and_writes_once(tmp_path, monkeypatch, worker_clock):
    path = tmp_path / "download.part"
    pgn = b'[Event "Recovered"]\n\n1. e4 e5 *\n\n'
    limited = limited_response()
    successful = Response(chunks=[pgn])
    responses = iter([limited, successful])
    calls = []
    provider_calls = []
    original_download = downloader.lichess.download_pgn

    def get(url, **kwargs):
        calls.append(url)
        assert not path.exists()
        return next(responses)

    def provider(*args, **kwargs):
        provider_calls.append(1)
        return original_download(*args, **kwargs)

    def during_wait(seconds):
        assert seconds == 60 and limited.closed
        assert not path.exists()

    worker_clock.on_sleep = during_wait
    monkeypatch.setattr(downloader.requests, "get", get)
    monkeypatch.setattr(downloader.lichess, "download_pgn", provider)
    events = run(request_for(tmp_path), path)
    assert len(provider_calls) == 1 and len(calls) == 2
    assert path.read_bytes() == pgn
    assert events[-1] == {"type": "done", "games": 1, "bytes": len(pgn)}
    waiting = next(event for event in events if event["type"] == "waiting")
    assert waiting["retry_at"] == 1_700_000_060.0
    assert waiting["attempt"] == 1 and waiting["reason"] == "concurrency"
    assert waiting["status_code"] == 429 and waiting["server_message"] == CONCURRENCY_ERROR
    assert [event["type"] for event in events if event["type"] in ("waiting", "resumed")] == ["waiting", "resumed"]
    assert successful.closed


def test_chess_com_retries_archive_requests_without_duplicate_games(tmp_path, monkeypatch, worker_clock):
    archive_url = "https://api.chess.com/pub/player/Example/games/archives"
    january = "https://api.chess.com/pub/player/example/games/2024/01"
    february = "https://api.chess.com/pub/player/example/games/2024/02"
    pgn_one = '[Event "January"]\n\n1. e4 e5 *'
    pgn_two = '[Event "February"]\n\n1. d4 d5 *'

    def game(month, pgn):
        return {"pgn": pgn, "rules": "chess", "time_class": "rapid",
                "end_time": int(datetime(2024, month, 15, tzinfo=timezone.utc).timestamp())}

    replies = {
        archive_url: iter([limited_response(message="Too many requests"), Response(data={"archives": [january, february]})]),
        january: iter([Response(data={"games": [game(1, pgn_one)]})]),
        february: iter([limited_response(), Response(data={"games": [game(2, pgn_two)]})]),
    }
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        return next(replies[url])

    monkeypatch.setattr(downloader.requests, "get", get)
    path = tmp_path / "download.part"
    events = run(request_for(tmp_path, server="chess.com", since="2024-01", until="2024-02", modes=["rapid"]), path)
    assert calls == [archive_url, archive_url, january, february, february]
    expected = (pgn_one + "\n\n" + pgn_two + "\n\n").encode("utf-8")
    assert path.read_bytes() == expected
    assert events[-1] == {"type": "done", "games": 2, "bytes": len(expected)}
    assert worker_clock.sleeps == [60, 120]


@pytest.mark.parametrize("retry_after,expected", [
    (None, 60), ("0", 60), ("-10", 60), ("not a date", 60),
    ("1.5", 60), ("120", 120), ("600", 600),
])
def test_retry_after_seconds_or_malformed_header(monkeypatch, retry_after, expected):
    clock = FakeClock()
    events = []
    first = limited_response(retry_after=retry_after)
    responses = iter([first, Response(data={"ok": True})])
    monkeypatch.setattr(downloader.requests, "get", lambda *args, **kwargs: next(responses))
    proxy = downloader._Requests(events.append, clock=clock.now, sleep=clock.sleep)
    assert proxy.get("https://api.chess.com/pub/player/Example/games/archives").json() == {"ok": True}
    assert clock.sleeps == [expected]
    assert events[0]["retry_at"] == 1_700_000_000.0 + expected
    assert first.closed


@pytest.mark.parametrize("offset,expected", [(180, 180), (-180, 60)])
def test_retry_after_http_date(monkeypatch, offset, expected):
    clock = FakeClock()
    header = format_datetime(datetime.fromtimestamp(clock.current + offset, tz=timezone.utc), usegmt=True)
    responses = iter([limited_response(retry_after=header), Response(data={"ok": True})])
    monkeypatch.setattr(downloader.requests, "get", lambda *args, **kwargs: next(responses))
    proxy = downloader._Requests(lambda event: None, clock=clock.now, sleep=clock.sleep)
    proxy.get("https://lichess.org/api/games/user/Example").json()
    assert clock.sleeps == [expected]


def test_backoff_is_exponential_then_capped_and_attempts_are_finite(monkeypatch):
    clock = FakeClock()
    events = []
    responses = []

    def get(*args, **kwargs):
        response = limited_response()
        responses.append(response)
        return response

    monkeypatch.setattr(downloader.requests, "get", get)
    proxy = downloader._Requests(events.append, clock=clock.now, sleep=clock.sleep, max_attempts=5)
    with pytest.raises(downloader.DownloadError, match="Automatic retries stopped") as raised:
        proxy.get("https://lichess.org/api/games/user/Example")
    assert clock.sleeps == [60, 120, 240, 300]
    assert len(responses) == 5 and all(response.closed for response in responses)
    assert [event["attempt"] for event in events if event["type"] == "waiting"] == [1, 2, 3, 4]
    assert raised.value.retry_at == clock.current + 300


def test_wait_budget_stops_before_the_next_retry(monkeypatch):
    clock = FakeClock()
    responses = []

    def get(*args, **kwargs):
        response = limited_response()
        responses.append(response)
        return response

    monkeypatch.setattr(downloader.requests, "get", get)
    proxy = downloader._Requests(lambda event: None, clock=clock.now, sleep=clock.sleep, max_total_wait=100)
    with pytest.raises(downloader.DownloadError) as raised:
        proxy.get("https://lichess.org/api/games/user/Example")
    assert clock.sleeps == [60] and len(responses) == 2
    assert raised.value.retry_at == clock.current + 120
    assert all(response.closed for response in responses)


@pytest.mark.parametrize("as_date", [False, True])
def test_retry_after_beyond_budget_is_preserved_without_early_retry(monkeypatch, as_date):
    clock = FakeClock()
    retry_at = clock.current + 7200
    header = (format_datetime(datetime.fromtimestamp(retry_at, tz=timezone.utc), usegmt=True)
              if as_date else "7200")
    response = limited_response(retry_after=header)
    events = []
    monkeypatch.setattr(downloader.requests, "get", lambda *args, **kwargs: response)
    proxy = downloader._Requests(events.append, clock=clock.now, sleep=clock.sleep)
    with pytest.raises(downloader.DownloadError, match="cannot clear the server quota") as raised:
        proxy.get("https://lichess.org/api/games/user/Example")
    assert raised.value.retry_at == retry_at
    assert not clock.sleeps and not events and response.closed


def test_worker_error_preserves_server_cooldown_metadata(tmp_path, monkeypatch, worker_clock):
    response = limited_response(retry_after="7200")
    monkeypatch.setattr(downloader.requests, "get", lambda *args, **kwargs: response)
    path = tmp_path / "download.part"
    result = run(request_for(tmp_path), path)[-1]
    assert result["type"] == "error"
    assert result["reason"] == "concurrency"
    assert result["status_code"] == 429
    assert result["server_message"] == CONCURRENCY_ERROR
    assert result["retry_at"] == worker_clock.current + 7200
    assert not worker_clock.sleeps and not path.exists()


@pytest.mark.parametrize("status", [403, 404, 503])
def test_automatic_retry_does_not_retry_other_http_errors(monkeypatch, status):
    clock = FakeClock()
    events = []
    response = Response(status=status)
    calls = []

    def get(*args, **kwargs):
        calls.append(1)
        return response

    monkeypatch.setattr(downloader.requests, "get", get)
    with pytest.raises(downloader.DownloadError):
        downloader._Requests(events.append, clock=clock.now, sleep=clock.sleep).get("https://lichess.org/api/games/user/Example")
    assert len(calls) == 1 and not clock.sleeps and not events


def test_stream_failure_keeps_partial_and_never_restarts_request(tmp_path, monkeypatch, worker_clock):
    partial = b'[Event "Partial"]\n'

    def chunks():
        yield partial
        raise requests.ConnectionError("Fixture disconnected midway")

    response = Response(chunks=chunks())
    calls = []

    def get(*args, **kwargs):
        calls.append(1)
        return response

    monkeypatch.setattr(downloader.requests, "get", get)
    path = tmp_path / "download.part"
    events = run(request_for(tmp_path), path)
    assert events[-1]["type"] == "error"
    assert path.read_bytes() == partial
    assert len(calls) == 1 and not worker_clock.sleeps
    assert not any(event["type"] in ("waiting", "resumed") for event in events)
    assert response.closed


def test_chess_json_stream_failure_is_closed_and_not_retried(tmp_path, monkeypatch, worker_clock):
    response = Response()
    calls = []

    def broken_json():
        raise requests.ConnectionError("Fixture JSON stream interrupted")

    def get(*args, **kwargs):
        assert kwargs["stream"] is True
        calls.append(1)
        return response

    monkeypatch.setattr(response, "json", broken_json)
    monkeypatch.setattr(downloader.requests, "get", get)
    path = tmp_path / "download.part"
    result = run(request_for(tmp_path, server="chess.com"), path)[-1]
    assert result["type"] == "error" and "connect to the chess site" in result["message"]
    assert len(calls) == 1 and not worker_clock.sleeps
    assert response.closed and not path.exists()
