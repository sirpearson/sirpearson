"""Accesso a PostgreSQL."""
from __future__ import annotations

import time
from importlib import resources

import psycopg
from psycopg.rows import dict_row


def connect(url: str, retries: int = 10, wait_s: float = 3.0) -> psycopg.Connection:
    """Connessione con qualche tentativo (all'avvio il container del database può essere lento)."""
    last = None
    for _ in range(retries):
        try:
            return psycopg.connect(url, row_factory=dict_row, autocommit=False)
        except psycopg.OperationalError as exc:
            last = exc
            time.sleep(wait_s)
    raise last  # type: ignore[misc]


def apply_schema(conn: psycopg.Connection) -> None:
    for name in ("schema.sql", "metrics.sql"):
        sql = resources.files("bnbmonitor.sql").joinpath(name).read_text(encoding="utf-8")
        conn.execute(sql)
    conn.commit()
