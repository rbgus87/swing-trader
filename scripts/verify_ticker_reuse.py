"""티커 재사용 검증: 단순 상호변경 vs 법인 교체 판정.

003620 (쌍용자동차 → KG모빌리티), 097230 (한진중공업 → HJ중공업)의
일봉 데이터 연속성을 분석하여 티커 재사용 유형을 판정한다.

DB 수정 없음. 검증 결과와 SQL 제안만 출력.

Usage:
    python scripts/verify_ticker_reuse.py
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from src.data_pipeline.db import get_connection  # noqa: E402

TICKER_REUSE_CASES = [
    {
        "ticker": "003620",
        "old_name": "쌍용자동차",
        "new_name": "KG모빌리티",
        "transition_period": ("2022-11-01", "2023-05-01"),
    },
    {
        "ticker": "097230",
        "old_name": "한진중공업",
        "new_name": "HJ중공업",
        "transition_period": ("2021-04-01", "2021-10-01"),
    },
]

# 판정 기준
PRICE_JUMP_RENAME_THRESHOLD = 0.20   # ±20% 이내 → 단순 상호변경
PRICE_JUMP_REPLACE_THRESHOLD = 0.30  # ±30% 초과 → 법인 교체
HALT_DAYS_THRESHOLD = 10             # 거래 중단 10일+ → 법인 교체 의심


def fetch_candles(conn: sqlite3.Connection, ticker: str,
                  start: str, end: str) -> list[dict]:
    rows = conn.execute(
        """SELECT date, open, high, low, close, volume
           FROM daily_candles
           WHERE ticker = ? AND date BETWEEN ? AND ?
           ORDER BY date""",
        (ticker, start, end),
    ).fetchall()
    return [dict(r) for r in rows]


def find_halt_periods(candles: list[dict]) -> list[tuple[str, str, int]]:
    """거래 중단 구간 식별 (open=0 AND volume=0)."""
    periods: list[tuple[str, str, int]] = []
    halt_start = None
    halt_count = 0
    for c in candles:
        is_halted = c["open"] == 0 and c["volume"] == 0
        if is_halted:
            if halt_start is None:
                halt_start = c["date"]
            halt_count += 1
        else:
            if halt_start is not None:
                periods.append((halt_start, prev_date, halt_count))
                halt_start = None
                halt_count = 0
        prev_date = c["date"]
    if halt_start is not None:
        periods.append((halt_start, prev_date, halt_count))
    return periods


def find_big_moves(candles: list[dict], threshold: float = 0.30) -> list[dict]:
    """일일 변동률 ±threshold 초과 이벤트."""
    moves = []
    trading = [c for c in candles if c["open"] > 0 and c["volume"] > 0]
    for i in range(1, len(trading)):
        prev_close = trading[i - 1]["close"]
        if prev_close == 0:
            continue
        change = (trading[i]["close"] - prev_close) / prev_close
        if abs(change) > threshold:
            moves.append({
                "date": trading[i]["date"],
                "prev_close": prev_close,
                "close": trading[i]["close"],
                "change_pct": change * 100,
            })
    return moves


def compute_avg_close(candles: list[dict], n: int = 5,
                      from_end: bool = False) -> float | None:
    """거래가 있는 봉 중 처음/마지막 n개의 평균 종가."""
    trading = [c for c in candles if c["open"] > 0 and c["volume"] > 0]
    if len(trading) < n:
        return None
    subset = trading[-n:] if from_end else trading[:n]
    return sum(c["close"] for c in subset) / n


def analyze_case(conn: sqlite3.Connection, case: dict) -> dict:
    ticker = case["ticker"]
    start, end = case["transition_period"]

    candles = fetch_candles(conn, ticker, start, end)
    halt_periods = find_halt_periods(candles)
    big_moves = find_big_moves(candles)

    total_halt_days = sum(p[2] for p in halt_periods)
    max_halt_days = max((p[2] for p in halt_periods), default=0)

    # 거래정지 전후 평균가 비교
    # 전환 전: 거래정지 직전 5일
    # 전환 후: 거래 재개 직후 5일
    trading = [c for c in candles if c["open"] > 0 and c["volume"] > 0]

    if halt_periods:
        halt_start_date = halt_periods[0][0]
        halt_end_date = halt_periods[-1][1]
        pre_halt = [c for c in trading if c["date"] < halt_start_date]
        post_halt = [c for c in trading if c["date"] > halt_end_date]
    else:
        pre_halt = trading[:len(trading) // 2]
        post_halt = trading[len(trading) // 2:]

    avg_before = (sum(c["close"] for c in pre_halt[-5:]) / min(5, len(pre_halt[-5:]))
                  if pre_halt else None)
    avg_after = (sum(c["close"] for c in post_halt[:5]) / min(5, len(post_halt[:5]))
                 if post_halt else None)

    if avg_before and avg_after and avg_before > 0:
        price_jump = (avg_after - avg_before) / avg_before
    else:
        price_jump = None

    # 판정
    if max_halt_days >= HALT_DAYS_THRESHOLD:
        if price_jump is not None and abs(price_jump) > PRICE_JUMP_REPLACE_THRESHOLD:
            verdict = "법인 교체"
        else:
            verdict = "거래정지 동반 상호변경 (장기 정지, 추가 확인 권장)"
    else:
        if price_jump is not None and abs(price_jump) <= PRICE_JUMP_RENAME_THRESHOLD:
            verdict = "단순 상호변경"
        elif price_jump is not None and abs(price_jump) > PRICE_JUMP_REPLACE_THRESHOLD:
            verdict = "법인 교체"
        else:
            verdict = "판정 보류 (데이터 부족)"

    return {
        "ticker": ticker,
        "old_name": case["old_name"],
        "new_name": case["new_name"],
        "total_candles": len(candles),
        "trading_candles": len(trading),
        "halt_periods": halt_periods,
        "total_halt_days": total_halt_days,
        "max_halt_days": max_halt_days,
        "avg_before": avg_before,
        "avg_after": avg_after,
        "price_jump": price_jump,
        "big_moves": big_moves,
        "verdict": verdict,
        "pre_halt_last5": pre_halt[-5:] if pre_halt else [],
        "post_halt_first5": post_halt[:5] if post_halt else [],
    }


def print_report(result: dict) -> None:
    print(f"\n=== {result['ticker']} {result['old_name']} → {result['new_name']} 검증 ===")
    print(f"전환 기간 일봉 수: {result['total_candles']}건 (거래 있음: {result['trading_candles']}건)")

    if result["halt_periods"]:
        print(f"거래 중단 구간:")
        for start, end, days in result["halt_periods"]:
            print(f"  {start} ~ {end} ({days}거래일)")
        print(f"총 거래 중단일: {result['total_halt_days']}일")
    else:
        print("거래 중단 구간: 없음")

    if result["pre_halt_last5"]:
        print(f"\n전환 전 마지막 5거래일:")
        for c in result["pre_halt_last5"]:
            print(f"  {c['date']}  close={c['close']:,.0f}  vol={c['volume']:,}")

    if result["post_halt_first5"]:
        print(f"\n전환 후 첫 5거래일:")
        for c in result["post_halt_first5"]:
            print(f"  {c['date']}  close={c['close']:,.0f}  vol={c['volume']:,}")

    if result["avg_before"] is not None:
        print(f"\n전 5일 평균 종가: {result['avg_before']:,.0f}")
    else:
        print(f"\n전 5일 평균 종가: (데이터 없음)")

    if result["avg_after"] is not None:
        print(f"후 5일 평균 종가: {result['avg_after']:,.0f}")
    else:
        print(f"후 5일 평균 종가: (데이터 없음)")

    if result["price_jump"] is not None:
        print(f"가격 점프율: {result['price_jump']:+.1%}")
    else:
        print(f"가격 점프율: (산출 불가)")

    if result["big_moves"]:
        print(f"\n일일 변동률 ±30% 초과 이벤트:")
        for m in result["big_moves"]:
            print(f"  {m['date']}  {m['prev_close']:,.0f} → {m['close']:,.0f}  ({m['change_pct']:+.1f}%)")
    else:
        print(f"\n일일 변동률 ±30% 초과 이벤트: 없음")

    print(f"\n판정: {result['verdict']}")


def print_sql_suggestion(result: dict) -> None:
    if "법인 교체" not in result["verdict"]:
        return
    halt = result["halt_periods"]
    reuse_date = halt[-1][1] if halt else "UNKNOWN"
    old_delist = halt[0][0] if halt else "UNKNOWN"
    print(f"\n-- [제안] 법인 교체 등록: {result['ticker']}")
    print(f"INSERT INTO ticker_reuse_events")
    print(f"(original_ticker, reuse_start_date, old_company_name, new_company_name,")
    print(f" old_delisted_date, notes, verified)")
    print(f"VALUES ('{result['ticker']}', '{reuse_date}', '{result['old_name']}', "
          f"'{result['new_name']}',")
    print(f" '{old_delist}', '일봉 검증 기반 자동 제안', 0);")


def main() -> int:
    results = []
    with get_connection() as conn:
        for case in TICKER_REUSE_CASES:
            result = analyze_case(conn, case)
            results.append(result)
            print_report(result)

    # 종합 판정
    rename_count = sum(1 for r in results if "상호변경" in r["verdict"])
    replace_count = sum(1 for r in results if "법인 교체" in r["verdict"])
    pending_count = len(results) - rename_count - replace_count

    print("\n" + "=" * 60)
    print("=== 종합 판정 ===")
    print(f"단순 상호변경: {rename_count}건 → ticker_reuse_events 0 유지")
    print(f"법인 교체:     {replace_count}건 → 수동 등록 필요 (아래 SQL 제안)")
    if pending_count:
        print(f"판정 보류:     {pending_count}건 → 추가 확인 필요")

    if replace_count:
        print("\n--- 법인 교체 SQL 제안 ---")
        for r in results:
            print_sql_suggestion(r)

    return 0


if __name__ == "__main__":
    sys.exit(main())
