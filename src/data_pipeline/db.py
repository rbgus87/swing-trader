"""Shared SQLite connection utility for data pipeline.

All DB access goes through get_connection(). Transactional semantics: commit on
clean exit, rollback on exception. Caller does not need to manage commit/rollback.
"""
import sqlite3
from contextlib import contextmanager
from typing import Iterator

from src.data_pipeline import DB_PATH


@contextmanager
def get_connection() -> Iterator[sqlite3.Connection]:
    """Context-managed SQLite connection.

    Usage:
        with get_connection() as conn:
            conn.execute(...)
            # auto-commit on success, rollback on exception

    Returns Connection with row_factory=Row for dict-like access.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
