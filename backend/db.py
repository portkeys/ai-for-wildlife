"""Database layer — Postgres (Neon) in production, SQLite locally.

If ``DATABASE_URL`` is set we use Postgres via a psycopg connection pool: durable
and shared across all Cloud Run instances, so classified videos persist across
idle/restart/redeploy and every viewer sees the same data. Otherwise we fall back
to a local SQLite file so ``./run.sh`` needs zero setup.

The backend is decided lazily (on first ``db()`` call), not at import, so a
``DATABASE_URL`` placed in ``.env`` is honored once ``load_dotenv()`` has run.

Callers keep writing sqlite-style ``conn.execute(sql, params)`` with ``?``
placeholders; for Postgres they're translated to ``%s`` at execution time. The
schema uses column types that BOTH engines accept.
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "app.db"

# Types here are chosen so the same DDL runs on Postgres AND SQLite (SQLite maps
# BIGINT/DOUBLE PRECISION via type affinity). ON CONFLICT upserts work on both.
SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS videos (
        id TEXT PRIMARY KEY,
        original_name TEXT,
        stored_name TEXT,
        duration DOUBLE PRECISION, width INTEGER, height INTEGER, size_bytes BIGINT,
        status TEXT DEFAULT 'uploaded',
        created_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS analyses (
        id TEXT PRIMARY KEY,
        video_id TEXT,
        model TEXT,
        status TEXT,
        species_common TEXT, species_scientific TEXT,
        behavior TEXT, behavior_detail TEXT,
        count INTEGER, confidence DOUBLE PRECISION, notes TEXT,
        prompt_tokens INTEGER, completion_tokens INTEGER, total_tokens INTEGER,
        cost_usd DOUBLE PRECISION, latency_ms INTEGER,
        error TEXT, raw_text TEXT,
        created_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS reviews (
        video_id TEXT PRIMARY KEY,
        decision TEXT,
        final_species_common TEXT, final_species_scientific TEXT,
        final_behavior TEXT, notes TEXT,
        reviewer TEXT, updated_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY, value TEXT
    )
    """,
]

_POOL = None


def using_postgres() -> bool:
    """Decided at call time (not import) so a DATABASE_URL from .env is honored."""
    return bool(os.environ.get("DATABASE_URL"))


def _get_pool():
    """Create the psycopg connection pool on first use."""
    global _POOL
    if _POOL is None:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        # Our own small pool so the app's many short queries reuse connections.
        # prepare_threshold=None disables server-side prepared statements so this
        # works with Neon's pooled (PgBouncer) endpoint as well as a direct one.
        #
        # Neon auto-suspends idle compute (~5 min on the free tier) and drops the
        # TCP/SSL connection, so a pooled connection can be silently dead by the
        # next request ("SSL connection has been closed unexpectedly"). Two guards:
        #   - check=check_connection validates each connection on checkout and
        #     transparently replaces a dead one, so callers never see a stale conn.
        #   - max_idle/max_lifetime recycle connections before Neon's idle window,
        #     so the check rarely has to reconnect on the hot path.
        _POOL = ConnectionPool(
            os.environ["DATABASE_URL"], min_size=1, max_size=10,
            kwargs={"row_factory": dict_row, "prepare_threshold": None},
            check=ConnectionPool.check_connection,
            max_idle=120, max_lifetime=240, open=True,
        )
    return _POOL


class _Conn:
    """Adapts either backend to ``conn.execute(sql, params) -> cursor`` with
    ``fetchone()/fetchall()`` returning dict-like rows."""

    def __init__(self, raw, pg: bool):
        self._raw = raw
        self._pg = pg

    def execute(self, sql, params=()):
        if self._pg:
            cur = self._raw.cursor()
            cur.execute(sql.replace("?", "%s"), params)
            return cur
        return self._raw.execute(sql, params)


@contextmanager
def db():
    if using_postgres():
        # The pool's context manager commits on success, rolls back on error.
        with _get_pool().connection() as conn:
            yield _Conn(conn, pg=True)
    else:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(DB_PATH, timeout=15)
        conn.row_factory = sqlite3.Row
        # Wait (don't error) if the file is briefly locked under concurrent analysis.
        conn.execute("PRAGMA busy_timeout=15000")
        try:
            yield _Conn(conn, pg=False)
            conn.commit()
        finally:
            conn.close()


def init_db():
    with db() as conn:
        for stmt in SCHEMA:
            conn.execute(stmt)
