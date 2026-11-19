from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator, Sequence

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


class Database:
    """Small connection-pool wrapper used by both HTTP handlers and collector."""

    def __init__(self, dsn: str, *, min_size: int = 1, max_size: int = 12) -> None:
        self.dsn = dsn
        self.pool = ConnectionPool(
            conninfo=dsn,
            min_size=min_size,
            max_size=max_size,
            open=False,
            # Mark controller/observer sessions so eBPF can avoid tracing its
            # own persistence traffic.  WorkloadRunner's measured sessions
            # use direct psycopg connections from the same DSN and therefore
            # remain observable as external PostgreSQL clients.
            kwargs={"row_factory": dict_row, "application_name": "keeninsight-api"},
        )

    def open(self) -> None:
        self.pool.open(wait=True)

    def close(self) -> None:
        self.pool.close()

    @contextmanager
    def connection(self) -> Iterator[Any]:
        with self.pool.connection() as conn:
            yield conn

    def fetch_all(self, statement: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        with self.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(statement, params)
                return list(cur.fetchall())

    def fetch_one(self, statement: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        with self.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(statement, params)
                return cur.fetchone()

    def execute(self, statement: str, params: Sequence[Any] = ()) -> int:
        with self.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(statement, params)
                return cur.rowcount

    @contextmanager
    def advisory_lock(self, key: int) -> Iterator[Any]:
        """Hold a session-level PostgreSQL advisory lock for a workflow.

        The lock connection is deliberately kept checked out while callers
        use the normal pool for their individual transactions.  This lets
        independent API/background threads coordinate multi-transaction
        workflows without sharing a psycopg connection.
        """
        with self.connection() as conn:
            conn.execute("SELECT pg_advisory_lock(%s)", (int(key),))
            try:
                yield conn
            finally:
                conn.execute("SELECT pg_advisory_unlock(%s)", (int(key),))
