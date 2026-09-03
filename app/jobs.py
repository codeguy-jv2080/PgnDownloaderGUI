"""Serialized download jobs with killable workers and exclusive PGN publication."""
import multiprocessing
import os
import queue
import threading
import time
import uuid
from contextlib import nullcontext
from pathlib import Path

from .database import now
from .downloader import run_download


class JobConflict(ValueError):
    pass


class DownloadCancelled(Exception):
    pass


class JobManager:
    def __init__(self, database, data_dir: Path, worker=run_download):
        self.db = database
        self.data_dir = data_dir
        self.worker = worker
        self.context = multiprocessing.get_context("spawn")
        self.lock = threading.RLock()
        self.active_id = None
        self.cancel_event = threading.Event()
        self.thread = None
        self.closed = False

    def start(self, request):
        with self.lock:
            if self.closed:
                raise JobConflict("The app is shutting down.")
            if self.active_id:
                raise JobConflict("A download is already running. Wait for it or cancel it first.")
            directory = Path(request.output_dir).resolve()
            if str(directory).startswith("\\\\"):
                raise ValueError("Choose a local output folder, not a network share.")
            directory.mkdir(parents=True, exist_ok=True)
            if not directory.is_dir():
                raise ValueError("The output folder is not a directory.")
            filename = request.generated_filename
            output = directory / filename
            if output.exists():
                raise JobConflict(f"{filename} already exists. Choose a different filename; existing files are never overwritten.")
            job_id = uuid.uuid4().hex
            staging = self.data_dir / "partials" / f"{job_id}.pgn.part"
            staging.parent.mkdir(parents=True, exist_ok=True)
            payload = request.model_dump()
            payload["output_dir"] = str(directory)
            job = self.db.add(job_id, payload, filename, output, staging)
            self.db.save_settings(output_dir=str(directory), last_request=payload)
            self.active_id = job_id
            self.cancel_event.clear()
            self.thread = threading.Thread(target=self._run, args=(job_id, payload, staging, output), daemon=True)
            self.thread.start()
            return job

    def cancel(self, job_id):
        with self.lock:
            job = self.db.get(job_id)
            if job is None:
                raise KeyError(job_id)
            if self.active_id == job_id and job["status"] in ("queued", "running", "waiting"):
                self.cancel_event.set()
                return self.db.update(job_id, message="Cancelling download…")
            return job

    def clear_history(self):
        # Serialize with start/cancel so a newly created job cannot be removed.
        with self.lock:
            if self.closed:
                raise JobConflict("The app is shutting down.")
            if self.active_id:
                raise JobConflict("Wait for the current download to finish or cancel it before clearing history.")
            return self.db.clear_history()

    @staticmethod
    def publish(staging: Path, output: Path, cancel_event=None, commit_lock=None):
        """Publish a complete PGN atomically, without replacing an existing file."""
        temporary = output.parent / f".{output.name}.{uuid.uuid4().hex}.part"
        created = False
        try:
            with temporary.open("xb") as destination:
                created = True
                with staging.open("rb") as source:
                    while chunk := source.read(1024 * 1024):
                        if cancel_event is not None and cancel_event.is_set():
                            raise DownloadCancelled()
                        destination.write(chunk)
                destination.flush()
                os.fsync(destination.fileno())
            with commit_lock if commit_lock is not None else nullcontext():
                if cancel_event is not None and cancel_event.is_set():
                    raise DownloadCancelled()
                if os.name == "nt":
                    # Windows rename fails if the destination exists, unlike POSIX rename.
                    os.rename(temporary, output)
                else:
                    os.link(temporary, output)
        finally:
            if created:
                temporary.unlink(missing_ok=True)

    def _run(self, job_id, payload, staging, output):
        events = None
        process = None
        result = None
        try:
            # Cancelling/restarting a job or the app must not bypass a server cooldown.
            cooldown = self.db.cooldown(payload["server"])
            if cooldown and cooldown["retry_at"] > time.time():
                self.db.update(job_id, status="waiting", retry_at=cooldown["retry_at"],
                               message=cooldown["message"])
                while cooldown["retry_at"] > time.time():
                    if self.cancel_event.wait(min(1, cooldown["retry_at"] - time.time())):
                        raise DownloadCancelled()
            events = self.context.Queue()
            process = self.context.Process(target=self.worker, args=(payload, str(staging), events), daemon=True)
            self.db.update(job_id, status="running", retry_at=None, message="Connecting to the chess server…")
            with self.lock:
                if self.cancel_event.is_set():
                    raise DownloadCancelled()
                process.start()
            dead_at = None
            while True:
                if self.cancel_event.is_set():
                    # A 429 may already be queued when Cancel is clicked. Give the
                    # queue's feeder a bounded moment to deliver it before killing
                    # the worker, so restarting cannot skip that known cooldown.
                    drain_until = time.monotonic() + 0.15
                    while (remaining := drain_until - time.monotonic()) > 0:
                        try:
                            pending = events.get(timeout=remaining)
                        except queue.Empty:
                            break
                        if pending.get("type") in ("waiting", "error") and pending.get("retry_at"):
                            self.db.set_cooldown(payload["server"], pending["retry_at"], pending["message"])
                    if process.is_alive():
                        process.terminate()
                    process.join(timeout=5)
                    self.db.update(job_id, status="cancelled", finished_at=now(), retry_at=None,
                                   bytes=staging.stat().st_size if staging.exists() else 0,
                                   message="Cancelled. Any unfinished file is kept in the app data folder.")
                    return
                try:
                    event = events.get(timeout=0.15)
                except queue.Empty:
                    if not process.is_alive():
                        if dead_at is None:
                            dead_at = time.monotonic()
                        elif time.monotonic() - dead_at > 0.5:
                            raise RuntimeError(f"Download worker stopped unexpectedly (exit {process.exitcode}).")
                    continue
                kind = event.get("type")
                if kind == "progress":
                    self.db.update(job_id, **{key: event[key] for key in ("games", "bytes", "message") if key in event})
                elif kind == "waiting":
                    self.db.set_cooldown(payload["server"], event["retry_at"], event["message"])
                    self.db.update(job_id, status="waiting", retry_at=event["retry_at"],
                                   attempt=event["attempt"], message=event["message"], error=None)
                elif kind == "resumed":
                    self.db.update(job_id, status="running", retry_at=None, message=event["message"])
                elif kind == "error":
                    if event.get("retry_at"):
                        self.db.set_cooldown(payload["server"], event["retry_at"], event["message"])
                    raise RuntimeError(event.get("message") or "Download failed.")
                elif kind == "done":
                    result = event
                    self.db.clear_cooldown(payload["server"])
                    break
            process.join(timeout=5)
            self.db.update(job_id, message="Saving the completed PGN…")
            self.publish(staging, output, cancel_event=self.cancel_event, commit_lock=self.lock)
            self.db.update(job_id, status="completed", finished_at=now(), games=result["games"], retry_at=None,
                           bytes=output.stat().st_size, partial_path="",
                           message=f"Saved {result['games']:,} games." if result["games"] else "No games matched these filters. Saved an empty PGN.")
            try:
                staging.unlink(missing_ok=True)
            except OSError:
                pass  # The finished PGN and completed journal entry are already safe.
        except DownloadCancelled:
            self.db.update(job_id, status="cancelled", finished_at=now(), retry_at=None,
                           bytes=staging.stat().st_size if staging.exists() else 0,
                           message=("Cancelled before saving the final PGN. The unfinished file was retained."
                                    if staging.is_file() else "Cancelled before the download started."))
        except Exception as exc:
            message = str(exc)
            if isinstance(exc, FileExistsError):
                message = "The output filename now exists. It was not overwritten. Choose a different filename and try again."
            self.db.update(job_id, status="failed", finished_at=now(), error=message, message=message, retry_at=None,
                           bytes=staging.stat().st_size if staging.exists() else 0)
        finally:
            if process is not None and process.pid:
                if process.is_alive():
                    process.terminate()
                process.join(timeout=5)
            if events is not None:
                events.close()
            with self.lock:
                self.active_id = None

    def close(self):
        with self.lock:
            self.closed = True
            self.cancel_event.set()
        if self.thread:
            self.thread.join(timeout=12)
