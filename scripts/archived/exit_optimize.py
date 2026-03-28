"""청산 파라미터 그리드 서치 — 수익률/손실 최적화.

4개 축을 조합하여 최적 청산 파라미터를 찾습니다:
- target_return: 목표 수익률
- stop_atr_mult: ATR 손절 배수
- trailing_activate_pct: 트레일링 활성화 수익률
- max_hold_days: 최대 보유 기간

Usage:
    python -m scripts.exit_optimize
"""

import sys
sys.stdout.reconfigure(encoding="utf-8")

import itertools
from loguru import logger
from src.backtest.engine import BacktestEngine

CODES = [
    "005930", "000660", "005380", "000270", "068270",
    "035420", "035720", "105560", "055550", "066570",
    "006400", "003670", "012330", "028260", "096770",
    "003550", "034730", "032830", "030200", "017670",
]

START_DATE = "20230101"
END_DATE = "20250314"
INITIAL_CAPITAL = 3_000_000
MAX_POSITIONS = 7

# 고정 파라미터 (현재 config)
FIXED = {
    "adx_threshold": 15,
    "rsi_entry_min": 35,
    "rsi_entry_max": 65,
    "volume_multiplier": 1.0,
    "bb_touch_pct": 0.15,
    "rsi_oversold": 45,
    "rsi_pullback": 45,
    "screening_lookback": 5,
    "partial_sell_enabled": True,
    "partial_target_pct": 0.5,
    "partial_sell_ratio": 0.5,
    "trailing_atr_mult": 2.0,
    "max_stop_pct": 0.07,
    "regime_strategy": {
        "trending": ["golden_cross", "macd_pullback"],
        "sideways": "bb_bounce",
    },
}

# 그리드 서치 축
GRID = {
    "target_return": [0.04, 0.06, 0.08, 0.10, 0.12],
    "stop_atr_mult": [1.5, 2.0, 2.5, 3.0],
    "trailing_activate_pct": [0.03, 0.05, 0.07, 0.10],
    "max_hold_days": [7, 10, 15, 20],
}


def main():
    engine = BacktestEngine(initial_capital=INITIAL_CAPITAL)

    print("데이터 프리로드 중...")
    engine.preload_data(CODES, START_DATE, END_DATE)

    # 프리컴퓨팅 (1회만)
    print("포트폴리오 컨텍스트 프리컴퓨팅...")
    context = engine.prepare_portfolio_context(
        CODES, START_DATE, END_DATE,
        strategy_name="adaptive",
        use_market_filter=True,
    )

    # 그리드 생성
    keys = list(GRID.keys())
    values = list(GRID.values())
    combos = list(itertools.product(*values))
    total = len(combos)
    print(f"\n총 {total}개 조합 테스트 시작...\n")

    results = []
    for i, combo in enumerate(combos):
        params = dict(FIXED)
        for k, v in zip(keys, combo):
            params[k] = v

        result = engine.run_portfolio(
            codes=CODES,
            start_date=START_DATE,
            end_date=END_DATE,
            params=params,
            strategy_name="adaptive",
            max_positions=MAX_POSITIONS,
            use_market_filter=True,
            _context=context,
        )

        combo_dict = dict(zip(keys, combo))
        results.append({
            "params": combo_dict,
            "result": result,
        })

        if (i + 1) % 40 == 0 or (i + 1) == total:
            print(f"  진행: {i+1}/{total} ({(i+1)/total*100:.0f}%)")

    # 정렬: Sharpe 기준
    results.sort(key=lambda x: x["result"].sharpe_ratio, reverse=True)

    # Top 15 출력
    print(f"\n\n{'='*130}")
    print(f"  청산 파라미터 그리드 서치 Top 15 (Sharpe 기준)")
    print(f"{'='*130}")

    header = (
        f"{'#':>3} {'target':>7} {'stop':>6} {'trail':>6} {'hold':>5} "
        f"{'| 수익률':>9} {'연환산':>8} {'MDD':>8} {'Sharpe':>8} "
        f"{'승률':>7} {'손익비':>7} {'평균수익':>8} {'거래수':>6} {'보유일':>6}"
    )
    print(header)
    print("-" * len(header))

    for i, item in enumerate(results[:15]):
        p = item["params"]
        r = item["result"]
        pf_str = f"{r.profit_factor:.2f}" if r.profit_factor != float("inf") else "inf"
        print(
            f"{i+1:>3} {p['target_return']:>7.2f} {p['stop_atr_mult']:>6.1f} "
            f"{p['trailing_activate_pct']:>6.2f} {p['max_hold_days']:>5} "
            f"| {r.total_return:>8.2f}% {r.annual_return:>7.2f}% "
            f"{r.max_drawdown:>7.2f}% {r.sharpe_ratio:>8.2f} "
            f"{r.win_rate:>6.2f}% {pf_str:>7} {r.avg_trade_return:>7.2f}% "
            f"{r.trade_count:>6} {r.avg_hold_days:>5.1f}"
        )

    print("-" * len(header))

    # 현재 config와 비교
    current = None
    for item in results:
        p = item["params"]
        if (p["target_return"] == 0.06 and p["stop_atr_mult"] == 2.5
            and p["trailing_activate_pct"] == 0.07 and p["max_hold_days"] == 10):
            current = item
            break
    # 0.06 / 2.5 / 0.06 / 10 (현재 config에 가장 가까운 조합)
    if current is None:
        for item in results:
            p = item["params"]
            if (p["target_return"] == 0.06 and p["stop_atr_mult"] == 2.5
                and p["max_hold_days"] == 10):
                current = item
                break

    if current:
        cr = current["result"]
        cp = current["params"]
        rank = results.index(current) + 1
        print(f"\n현재 config 근사치 (순위 #{rank}/{total}):")
        print(f"  target={cp['target_return']}, stop={cp['stop_atr_mult']}, "
              f"trail={cp['trailing_activate_pct']}, hold={cp['max_hold_days']}")
        print(f"  수익률={cr.total_return:.2f}%, Sharpe={cr.sharpe_ratio:.2f}, "
              f"승률={cr.win_rate:.2f}%, 거래={cr.trade_count}건")

    best = results[0]
    br = best["result"]
    bp = best["params"]
    print(f"\n최적 조합 (#1):")
    print(f"  target={bp['target_return']}, stop={bp['stop_atr_mult']}, "
          f"trail={bp['trailing_activate_pct']}, hold={bp['max_hold_days']}")
    print(f"  수익률={br.total_return:.2f}%, Sharpe={br.sharpe_ratio:.2f}, "
          f"승률={br.win_rate:.2f}%, 거래={br.trade_count}건")

    months = 26
    print(f"\n월간 추정:")
    print(f"  현재: 월 {cr.total_return/months:.2f}%, {cr.trade_count/months:.1f}회/월" if current else "")
    print(f"  최적: 월 {br.total_return/months:.2f}%, {br.trade_count/months:.1f}회/월")
    print(f"{'='*130}\n")


if __name__ == "__main__":
    main()
