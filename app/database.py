"""SQLite settings and job journal. PGN content remains in ordinary files."""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .paths import default_output_dir


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    VERSION = 2

    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "app.sqlite3"
        with self.connect() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > self.VERSION:
                raise RuntimeError("This data was created by a newer PGN Downloader. Please use that version.")
            if version < self.VERSION and self.path.stat().st_size:
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
                with sqlite3.connect(directory / f"app-before-v{self.VERSION}-{stamp}.sqlite3") as backup:
                    db.backup(backup)
            if version < self.VERSION:
                db.execute("BEGIN IMMEDIATE")
                if version == 0:
                    db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                    db.execute("""CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, created_at TEXT NOT NULL, finished_at TEXT,
                    status TEXT NOT NULL, request TEXT NOT NULL,
                    filename TEXT NOT NULL, output_path TEXT NOT NULL, partial_path TEXT NOT NULL,
                    games INTEGER NOT NULL DEFAULT 0, bytes INTEGER NOT NULL DEFAULT 0,
                    message TEXT NOT NULL DEFAULT '', error TEXT
                    )""")
                if version < 2:
                    db.execute("ALTER TABLE jobs ADD COLUMN retry_at REAL")
                    db.execute("ALTER TABLE jobs ADD COLUMN attempt INTEGER NOT NULL DEFAULT 0")
                    db.execute("""CREATE TABLE cooldowns (
                        server TEXT PRIMARY KEY, retry_at REAL NOT NULL, message TEXT NOT NULL
                    )""")
                db.execute("PRAGMA user_version = 2")
            db.execute("UPDATE jobs SET status='interrupted', finished_at=?, message=?, error=? "
                       "WHERE status IN ('queued','running','waiting')", (now(), "Interrupted when the app stopped.",
                       "The app stopped before this download completed. Any partial file was retained."))

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def settings(self) -> dict:
        with self.connect() as db:
            values = {r["key"]: json.loads(r["value"]) for r in db.execute("SELECT key,value FROM settings")}
        return {"output_dir": str(default_output_dir()), "last_request": None, **values,
                "theme": "dark" if values.get("theme") == "dark" else "light"}

    def save_settings(self, **values):
        with self.connect() as db:
            db.executemany("INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                           [(k, json.dumps(v)) for k, v in values.items()])
        return self.settings()

    @staticmethod
    def unpack(row):
        if row is None:
            return None
        result = dict(row)
        request = json.loads(result.pop("request"))
        # A planned staging path is not a partial file. Also handle files moved later.
        if result.get("partial_path"):
            try:
                if not Path(result["partial_path"]).is_file():
                    result["partial_path"] = ""
            except OSError:
                result["partial_path"] = ""
        return {**request, **result}

    def cooldown(self, server):
        with self.connect() as db:
            row = db.execute("SELECT retry_at,message FROM cooldowns WHERE server=?", (server,)).fetchone()
        return dict(row) if row else None

    def set_cooldown(self, server, retry_at, message):
        with self.connect() as db:
            db.execute("""INSERT INTO cooldowns(server,retry_at,message) VALUES (?,?,?)
                ON CONFLICT(server) DO UPDATE SET
                    message=CASE WHEN excluded.retry_at >= cooldowns.retry_at THEN excluded.message ELSE cooldowns.message END,
                    retry_at=MAX(cooldowns.retry_at,excluded.retry_at)""", (server, retry_at, message))

    def clear_cooldown(self, server):
        with self.connect() as db:
            db.execute("DELETE FROM cooldowns WHERE server=?", (server,))

    def jobs(self):
        with self.connect() as db:
            return [self.unpack(row) for row in db.execute("SELECT * FROM jobs ORDER BY created_at DESC")]

    def clear_history(self) -> int:
        """Remove journal entries only; exports, partials, settings and cooldowns remain."""
        with self.connect() as db:
            return db.execute("DELETE FROM jobs").rowcount

    def get(self, job_id):
        with self.connect() as db:
            return self.unpack(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def add(self, job_id, request, filename, output_path, partial_path):
        with self.connect() as db:
            db.execute("INSERT INTO jobs(id,created_at,status,request,filename,output_path,partial_path,message) "
                       "VALUES (?,?,?,?,?,?,?,?)", (job_id, now(), "queued", json.dumps(request), filename,
                       str(output_path), str(partial_path), "Waiting to download…"))
        return self.get(job_id)

    def update(self, job_id, **values):
        allowed = {"status", "finished_at", "games", "bytes", "message", "error", "partial_path", "retry_at", "attempt"}
        if not values or not set(values).issubset(allowed):
            raise ValueError("Invalid job update")
        with self.connect() as db:
            db.execute("UPDATE jobs SET " + ",".join(f"{key}=?" for key in values) + " WHERE id=?",
                       (*values.values(), job_id))
        return self.get(job_id)
