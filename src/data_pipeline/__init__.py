"""
swing-trader data pipeline.

Phase A: DB 역할 분리
  - swing_data.db  : 데이터 (일봉/시총/지수/stocks/이벤트)
  - swing_trade.db : 매매 (positions/trades/snapshot/signals/watchlist)

PyInstaller --onefile: __file__은 _MEIPASS 임시폴더 내부를 가리키므로
parents[2]를 쓰면 DB_PATH가 임시폴더로 잡혀 실제 DB를 찾지 못한다.
sys.executable의 부모(= exe가 놓인 폴더)를 루트로 사용한다.
"""
import sys
from pathlib import Path


def _resolve_project_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


PROJECT_ROOT = _resolve_project_root()
DATA_DB_PATH = PROJECT_ROOT / "swing_data.db"
TRADE_DB_PATH = PROJECT_ROOT / "swing_trade.db"

# 하위 호환: 기존 코드가 DB_PATH를 사용하면 데이터 DB로 리다이렉트
DB_PATH = DATA_DB_PATH

SCHEMA_PATH = PROJECT_ROOT / "db" / "schema.sql"

__all__ = [
    "PROJECT_ROOT",
    "DATA_DB_PATH",
    "TRADE_DB_PATH",
    "DB_PATH",
    "SCHEMA_PATH",
]
