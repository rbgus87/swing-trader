"""
swing-trader data pipeline.

Data access layer for Phase 1+.
Replaces legacy data/ module incrementally:
- Phase 1: new collectors (krx_client, fdr_client)
- Phase 1 late: absorb data/column_mapper.py
- Phase 3: replace data/provider.py + krx_api.py
"""
import sys
from pathlib import Path


def _resolve_project_root() -> Path:
    """소스 실행 시 레포 루트 / PyInstaller EXE 시 실행 파일 디렉토리.

    PyInstaller --onefile: __file__은 _MEIPASS 임시폴더 내부를 가리키므로
    parents[2]를 쓰면 DB_PATH가 임시폴더로 잡혀 실제 DB를 찾지 못한다.
    sys.executable의 부모(= exe가 놓인 폴더)를 루트로 사용한다.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


PROJECT_ROOT = _resolve_project_root()
DB_PATH = PROJECT_ROOT / "swing.db"
SCHEMA_PATH = PROJECT_ROOT / "db" / "schema.sql"

__all__ = ["PROJECT_ROOT", "DB_PATH", "SCHEMA_PATH"]
