"""
swing-trader data pipeline.

Data access layer for Phase 1+.
Replaces legacy data/ module incrementally:
- Phase 1: new collectors (krx_client, fdr_client)
- Phase 1 late: absorb data/column_mapper.py
- Phase 3: replace data/provider.py + krx_api.py
"""
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DB_PATH = PROJECT_ROOT / "swing.db"
SCHEMA_PATH = PROJECT_ROOT / "db" / "schema.sql"

__all__ = ["PROJECT_ROOT", "DB_PATH", "SCHEMA_PATH"]
