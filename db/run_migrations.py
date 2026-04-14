"""Apply pending SQL migrations from db/migrations/ to swing.db.

Tracks applied versions in schema_migrations table. Idempotent.

Migration filename convention: NNN_description.sql where NNN is a zero-padded
version number used as the unique key in schema_migrations.

Usage:
    python db/run_migrations.py
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from loguru import logger

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.data_pipeline import DB_PATH

MIGRATIONS_DIR = _PROJECT_ROOT / "db" / "migrations"


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY,
            applied_at DATETIME NOT NULL
        )
        """
    )


def _version_from(path: Path) -> str:
    return path.stem.split("_", 1)[0]


def main() -> int:
    if not DB_PATH.exists():
        logger.error(f"DB not found: {DB_PATH}. Run db/init_db.py first.")
        return 1
    if not MIGRATIONS_DIR.exists():
        logger.warning(f"No migrations dir: {MIGRATIONS_DIR}")
        return 0

    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not files:
        logger.info("No migration files found.")
        return 0

    conn = sqlite3.connect(DB_PATH)
    try:
        _ensure_table(conn)
        applied = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}

        pending = [(p, _version_from(p)) for p in files if _version_from(p) not in applied]
        if not pending:
            logger.info(f"All {len(files)} migration(s) already applied.")
            return 0

        logger.info(f"Pending migrations: {len(pending)} of {len(files)}")
        for path, version in pending:
            logger.info(f"Applying {path.name} (version={version})")
            sql = path.read_text(encoding="utf-8")
            try:
                conn.executescript(sql)
                conn.execute(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                    (version, datetime.now().isoformat(timespec="seconds")),
                )
                conn.commit()
                logger.success(f"  ✓ {version} applied")
            except sqlite3.Error as exc:
                conn.rollback()
                logger.error(f"  ✗ {version} failed: {exc}")
                return 1
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
