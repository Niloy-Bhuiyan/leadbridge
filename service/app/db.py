"""SQLite persistence: the lead store and the run log.

Two rules, both taken from ResearchOS:

  Nothing is fabricated. A step that did not run is absent, not zero.
  Failures are kept. A failed run is never deleted, because a pipeline you
  cannot audit after the fact is a pipeline you cannot trust.

SQLite rather than Postgres because the run log is append-only, single
writer, and local to the service. Introducing a second database server for
this would be cost without benefit.
"""

import json
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from .models import NormalizedLead

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    fingerprint   TEXT PRIMARY KEY,
    email         TEXT NOT NULL,
    dedupe_key    TEXT NOT NULL,
    payload       TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    seen_count    INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_leads_email ON leads(email);
CREATE INDEX IF NOT EXISTS idx_leads_key   ON leads(dedupe_key);

CREATE TABLE IF NOT EXISTS runs (
    run_id       TEXT PRIMARY KEY,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    status       TEXT NOT NULL,
    fingerprint  TEXT,
    degraded     INTEGER NOT NULL DEFAULT 0,
    summary      TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at DESC);

CREATE TABLE IF NOT EXISTS run_steps (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL,
    step        TEXT NOT NULL,
    status      TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    duration_ms INTEGER,
    detail      TEXT,
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);
CREATE INDEX IF NOT EXISTS idx_steps_run ON run_steps(run_id);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Store:
    """Thread-safe SQLite wrapper.

    FastAPI runs sync endpoints in a threadpool, so connections cannot be
    shared across threads. A lock plus check_same_thread=False is the
    honest, boring solution at this write volume; a pool would be premature.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def _cursor(self) -> Iterator[sqlite3.Cursor]:
        with self._lock:
            cur = self._conn.cursor()
            try:
                yield cur
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            finally:
                cur.close()

    # ------------------------------------------------------------- leads
    def known_leads(self, email: str, dedupe_key: str) -> list[NormalizedLead]:
        """Only the rows that could possibly match, not the whole table.

        Loading every lead to dedupe one would work at demo scale and fall
        over at real scale, so the query is indexed from the start.
        """
        with self._cursor() as cur:
            cur.execute(
                "SELECT payload FROM leads WHERE email = ? OR dedupe_key = ?",
                (email, dedupe_key),
            )
            rows = cur.fetchall()
        return [NormalizedLead.model_validate_json(r["payload"]) for r in rows]

    def record_lead(self, lead: NormalizedLead) -> bool:
        """Insert, or bump the counter if already present.

        Returns True when the lead is new. The UPSERT is what makes a
        retried webhook safe at the storage layer, independent of whatever
        the dedupe layer decided.
        """
        now = _now()
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO leads
                    (fingerprint, email, dedupe_key, payload,
                     first_seen_at, last_seen_at, seen_count)
                VALUES (?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(fingerprint) DO UPDATE SET
                    last_seen_at = excluded.last_seen_at,
                    seen_count   = leads.seen_count + 1
                """,
                (
                    lead.fingerprint,
                    lead.email,
                    lead.dedupe_key,
                    lead.model_dump_json(),
                    now,
                    now,
                ),
            )
            cur.execute(
                "SELECT seen_count FROM leads WHERE fingerprint = ?",
                (lead.fingerprint,),
            )
            row = cur.fetchone()
        return bool(row and row["seen_count"] == 1)

    # -------------------------------------------------------------- runs
    def start_run(self, fingerprint: str | None = None) -> str:
        run_id = uuid.uuid4().hex[:16]
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO runs (run_id, started_at, status, fingerprint) "
                "VALUES (?, ?, 'running', ?)",
                (run_id, _now(), fingerprint),
            )
        return run_id

    def finish_run(
        self,
        run_id: str,
        status: str,
        degraded: bool = False,
        summary: str | None = None,
        fingerprint: str | None = None,
    ) -> None:
        with self._cursor() as cur:
            cur.execute(
                """
                UPDATE runs
                   SET finished_at = ?, status = ?, degraded = ?,
                       summary = ?, fingerprint = COALESCE(?, fingerprint)
                 WHERE run_id = ?
                """,
                (_now(), status, int(degraded), summary, fingerprint, run_id),
            )

    def log_step(
        self,
        run_id: str,
        step: str,
        status: str,
        duration_ms: int | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO run_steps (run_id, step, status, started_at, "
                "duration_ms, detail) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    step,
                    status,
                    _now(),
                    duration_ms,
                    json.dumps(detail) if detail is not None else None,
                ),
            )

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,))
            run = cur.fetchone()
            if run is None:
                return None
            cur.execute(
                "SELECT step, status, started_at, duration_ms, detail "
                "FROM run_steps WHERE run_id = ? ORDER BY id",
                (run_id,),
            )
            steps = cur.fetchall()
        return {
            **dict(run),
            "degraded": bool(run["degraded"]),
            "steps": [
                {
                    **dict(s),
                    "detail": json.loads(s["detail"]) if s["detail"] else None,
                }
                for s in steps
            ],
        }

    def recent_runs(self, limit: int = 25) -> list[dict[str, Any]]:
        with self._cursor() as cur:
            cur.execute(
                "SELECT run_id, started_at, finished_at, status, fingerprint, "
                "degraded, summary FROM runs ORDER BY started_at DESC LIMIT ?",
                (limit,),
            )
            rows = cur.fetchall()
        return [{**dict(r), "degraded": bool(r["degraded"])} for r in rows]

    def stats(self) -> dict[str, Any]:
        with self._cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM leads")
            leads = cur.fetchone()["n"]
            cur.execute(
                "SELECT status, COUNT(*) AS n FROM runs GROUP BY status"
            )
            by_status = {r["status"]: r["n"] for r in cur.fetchall()}
            cur.execute("SELECT COUNT(*) AS n FROM runs WHERE degraded = 1")
            degraded = cur.fetchone()["n"]
        return {
            "leads": leads,
            "runs_by_status": by_status,
            "degraded_runs": degraded,
        }
