"""SQLite-backed durable state store (spec §39: SQLite WAL, backups, rollback-safe)."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any, Optional

from ..schemas.job import AuditEntry, Job
from ..schemas.profile import Profile


def _locked(fn):
    def wrapper(self, *a, **kw):
        with self._lock:
            return fn(self, *a, **kw)
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


class Store:
    """Thin persistence layer over SQLite (WAL mode). Profiles, jobs, audit."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        if str(self.db_path) != ":memory:" and not self.db_path.exists():
            # profile environments and job logs live here: not readable by other local users
            os.close(os.open(self.db_path, os.O_WRONLY | os.O_CREAT, 0o600))
        # FastAPI runs sync routes in a threadpool and jobs run on the event loop:
        # one shared connection, serialised by a lock (sqlite3 objects are not
        # thread-safe by default and raise ProgrammingError across threads).
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA synchronous=FULL;")
        self._migrate()

    def _migrate(self) -> None:
        with self.conn:
            self.conn.execute(
                """CREATE TABLE IF NOT EXISTS profiles(
                    name TEXT PRIMARY KEY, doc TEXT NOT NULL, updated TEXT)"""
            )
            self.conn.execute(
                """CREATE TABLE IF NOT EXISTS jobs(
                    job_id TEXT PRIMARY KEY, doc TEXT NOT NULL, updated TEXT)"""
            )
            self.conn.execute(
                """CREATE TABLE IF NOT EXISTS audit(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, doc TEXT NOT NULL)"""
            )
            self.conn.execute(
                """CREATE TABLE IF NOT EXISTS kv(
                    key TEXT PRIMARY KEY, value TEXT)"""
            )

    # ---- profiles ---------------------------------------------------------
    @_locked
    def save_profile(self, p: Profile) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO profiles(name, doc, updated) VALUES(?,?,?)",
                (p.name, p.model_dump_json(), p.updated_at),
            )

    @_locked
    def load_profile(self, name: str) -> Optional[Profile]:
        row = self.conn.execute("SELECT doc FROM profiles WHERE name=?", (name,)).fetchone()
        return Profile.model_validate_json(row["doc"]) if row else None

    @_locked
    def list_profiles(self) -> list[Profile]:
        rows = self.conn.execute("SELECT doc FROM profiles ORDER BY name").fetchall()
        return [Profile.model_validate_json(r["doc"]) for r in rows]

    @_locked
    def delete_profile(self, name: str) -> bool:
        with self.conn:
            cur = self.conn.execute("DELETE FROM profiles WHERE name=?", (name,))
        return cur.rowcount > 0

    # ---- jobs -------------------------------------------------------------
    @_locked
    def save_job(self, job: Job) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO jobs(job_id, doc, updated) VALUES(?,?,?)",
                (job.job_id, job.model_dump_json(), job.updated_at),
            )

    @_locked
    def load_job(self, job_id: str) -> Optional[Job]:
        row = self.conn.execute("SELECT doc FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        return Job.model_validate_json(row["doc"]) if row else None

    @_locked
    def list_jobs(self, limit: int = 50) -> list[Job]:
        rows = self.conn.execute(
            "SELECT doc FROM jobs ORDER BY updated DESC LIMIT ?", (limit,)
        ).fetchall()
        return [Job.model_validate_json(r["doc"]) for r in rows]

    # ---- audit ------------------------------------------------------------
    @_locked
    def append_audit(self, entry: AuditEntry) -> None:
        with self.conn:
            self.conn.execute("INSERT INTO audit(doc) VALUES(?)", (entry.model_dump_json(),))

    @_locked
    def audit_log(self, limit: int = 200) -> list[AuditEntry]:
        rows = self.conn.execute(
            "SELECT doc FROM audit ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [AuditEntry.model_validate_json(r["doc"]) for r in rows]

    # ---- generic kv -------------------------------------------------------
    @_locked
    def kv_set(self, key: str, value: Any) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO kv(key,value) VALUES(?,?)",
                (key, json.dumps(value)),
            )

    @_locked
    def kv_get(self, key: str, default: Any = None) -> Any:
        row = self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    # ---- backups (spec §39) ------------------------------------------------
    @_locked
    def backup(self, dest: str | Path) -> Path:
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        # sqlite3 backup API gives a consistent snapshot even with WAL.
        dst_conn = sqlite3.connect(str(dest))
        with dst_conn:
            self.conn.backup(dst_conn)
        dst_conn.close()
        return dest

    def close(self) -> None:
        self.conn.close()


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
