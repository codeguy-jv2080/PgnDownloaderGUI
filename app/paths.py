"""Application locations and a per-data-directory instance lock."""
import os
from pathlib import Path


def default_data_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local" / "share")) / "PgnDownloaderGUI"


def default_output_dir() -> Path:
    return Path.home() / "Downloads" / "PGN Downloads"


class InstanceLock:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "instance.lock"
        self.file = None

    def acquire(self):
        self.file = self.path.open("a+b")
        self.file.seek(0, 2)
        if self.file.tell() == 0:
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            self.file = None
            raise RuntimeError("PGN Downloader is already running with this data folder.") from exc

    def release(self):
        if self.file is not None:
            self.file.close()
            self.file = None
