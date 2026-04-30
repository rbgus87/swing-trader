"""v2.5 전환 — 페이퍼 포지션 clean reset.

v2.4 (500만원 + cash×25%) 환경에서 진입한 포지션이 v2.5 (1000만원 + equity 균등)
파라미터와 맞지 않음. positions / trades / daily_performance 초기화.
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.data_pipeline import TRADE_DB_PATH


def main():
    db = str(TRADE_DB_PATH)
    print(f"TRADE_DB: {db}")

    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row

    print("\n--- before ---")
    for tbl in ('positions', 'trades', 'daily_performance'):
        n = con.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
        print(f"  {tbl}: {n}")

    print("\n--- delete ---")
    con.execute("DELETE FROM positions")
    con.execute("DELETE FROM trades")
    con.execute("DELETE FROM daily_performance")
    con.commit()

    print("\n--- after ---")
    for tbl in ('positions', 'trades', 'daily_performance'):
        n = con.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
        print(f"  {tbl}: {n}")

    con.close()
    print("\n✅ v2.5 reset 완료. 1000만원 + equity 균등 + TP2로 clean start.")


if __name__ == "__main__":
    main()
