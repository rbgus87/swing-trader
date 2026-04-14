"""Initialize swing.db by applying db/schema.sql.

Idempotent:
- If swing.db does not exist: create and apply schema.
- If it already exists: refuse (exit 1) unless --force.
- With --force: back up existing file to swing.db.bak.<timestamp>, then recreate.

Usage:
    python db/init_db.py
    python db/init_db.py --force
"""
import argparse
import sqlite3
import sys
from datetime import datetime

from loguru import logger

# Ensure project root on sys.path so `src.data_pipeline` imports work when
# running as a plain script (`python db/init_db.py`).
from pathlib import Path
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.data_pipeline import DB_PATH, SCHEMA_PATH


def main() -> int:
    parser = argparse.ArgumentParser(description="Initialize swing.db schema")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Back up existing DB and recreate from schema",
    )
    args = parser.parse_args()

    if DB_PATH.exists():
        if not args.force:
            logger.warning(
                f"{DB_PATH} already exists. Refusing to overwrite. "
                f"Use --force to back up and recreate."
            )
            return 1
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = DB_PATH.with_suffix(f".db.bak.{timestamp}")
        DB_PATH.rename(backup_path)
        logger.info(f"Backed up existing DB to {backup_path}")

    if not SCHEMA_PATH.exists():
        logger.error(f"Schema file not found: {SCHEMA_PATH}")
        return 1

    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")

    logger.info(f"Creating {DB_PATH}")
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.executescript(schema_sql)
        conn.commit()

        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        indexes = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
    finally:
        conn.close()

    logger.success(f"Created {len(tables)} tables: {tables}")
    logger.success(f"Created {len(indexes)} indexes: {indexes}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
