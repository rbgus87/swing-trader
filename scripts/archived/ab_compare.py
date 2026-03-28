"""A/B/C 비교 백테스트 — adaptive config 확장 효과 검증.

A (Baseline): trending=[golden_cross, macd_pullback], sideways=bb_bounce
B (Full Ext): trending=[golden_cross, macd_pullback, macd_rsi, breakout], sideways=[bb_bounce, stoch_reversal]
C (SW Only):  trending=[golden_cross, macd_pullback], sideways=[bb_bounce, stoch_reversal]

Usage:
    python -m scripts.ab_compare
"""

import sys
sys.stdout.reconfigure(encoding="utf-8")

from loguru import logger
from src.backtest.engine import BacktestEngine

# 대형주 20종목 (유동성 충분)
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

# 공통 파라미터 (현재 config.yaml 기준)
BASE_PARAMS = {
    "target_return": 0.06,
    "stop_atr_mult": 2.5,
    "trailing_atr_mult": 2.0,
    "trailing_activate_pct": 0.06,
    "max_hold_days": 10,
    "max_stop_pct": 0.07,
    "partial_sell_enabled": True,
    "partial_target_pct": 0.5,
    "partial_sell_ratio": 0.5,
    "adx_threshold": 15,
    "rsi_entry_min": 35,
    "rsi_entry_max": 65,
    "volume_multiplier": 1.0,
    "bb_touch_pct": 0.15,
    "rsi_oversold": 45,
    "rsi_pullback": 45,
    "screening_lookback": 5,
}

# A: 현재 config
SCENARIO_A = {
    **BASE_PARAMS,
    "regime_strategy": {
        "trending": ["golden_cross", "macd_pullback"],
        "sideways": "bb_bounce",
    },
}

# B: 전략 확장 (trending 4개 + sideways 2개)
SCENARIO_B = {
    **BASE_PARAMS,
    "regime_strategy": {
        "trending": ["golden_cross", "macd_pullback", "macd_rsi", "breakout"],
        "sideways": ["bb_bounce", "stoch_reversal"],
    },
}

# C: sideways만 확장 (trending 유지 + sideways에 stoch_reversal 추가)
SCENARIO_C = {
    **BASE_PARAMS,
    "regime_strategy": {
        "trending": ["golden_cross", "macd_pullback"],
        "sideways": ["bb_bounce", "stoch_reversal"],
    },
}


def run_scenario(name: str, params: dict, engine: BacktestEngine) -> dict:
    """시나리오 실행 후 결과 반환."""
    print(f"\n{'='*60}")
    print(f"  시나리오 {name}")
    print(f"{'='*60}")

    result = engine.run_portfolio(
        codes=CODES,
        start_date=START_DATE,
        end_date=END_DATE,
        params=params,
        strategy_name="adaptive",
        max_positions=MAX_POSITIONS,
        use_market_filter=True,
    )

    return {
        "name": name,
        "result": result,
        "trades": list(engine._last_trades),
        "equity": engine._last_equity,
    }


def print_comparison(*scenarios):
    """다중 시나리오 비교 결과 출력."""
    print(f"\n\n{'='*90}")
    print(f"  시나리오 비교 결과 ({START_DATE} ~ {END_DATE}, {len(CODES)}종목)")
    print(f"{'='*90}")

    names = [s["name"] for s in scenarios]
    results = [s["result"] for s in scenarios]

    # 헤더
    col_width = 18
    header = f"{'지표':<20}" + "".join(f"{n:>{col_width}}" for n in names)
    print(header)
    print("-" * len(header))

    metric_defs = [
        ("총 수익률 (%)", "total_return"),
        ("연환산 수익률 (%)", "annual_return"),
        ("MDD (%)", "max_drawdown"),
        ("Sharpe", "sharpe_ratio"),
        ("Sortino", "sortino_ratio"),
        ("승률 (%)", "win_rate"),
        ("손익비", "profit_factor"),
        ("평균 거래수익 (%)", "avg_trade_return"),
        ("거래 횟수", "trade_count"),
        ("평균 보유일", "avg_hold_days"),
    ]

    for label, attr in metric_defs:
        vals = [getattr(r, attr) for r in results]
        parts = []
        for v in vals:
            if isinstance(v, int):
                parts.append(f"{v:>{col_width}d}")
            else:
                s = f"{v:.2f}" if v != float("inf") else "inf"
                parts.append(f"{s:>{col_width}}")
        print(f"{label:<20}" + "".join(parts))

    print("-" * len(header))

    # 월간 매매 횟수
    months = 26
    print(f"\n월간 매매 횟수 (추정):")
    for s in scenarios:
        monthly = s["result"].trade_count / months
        print(f"  {s['name']}: {monthly:.1f}회/월")
    print(f"  목표: 8~12회/월")

    # 베스트 Sharpe
    best = max(scenarios, key=lambda s: s["result"].sharpe_ratio)
    print(f"\n최고 Sharpe: {best['name']} ({best['result'].sharpe_ratio:.2f})")
    print(f"{'='*90}\n")


def main():
    engine = BacktestEngine(initial_capital=INITIAL_CAPITAL)

    # 데이터 프리로드
    print("데이터 프리로드 중...")
    engine.preload_data(CODES, START_DATE, END_DATE)

    # A: 현재 config
    result_a = run_scenario("A (GC+MP/BB)", SCENARIO_A, engine)

    # C: sideways만 확장
    result_c = run_scenario("C (GC+MP/BB+SR)", SCENARIO_C, engine)

    # 비교
    print_comparison(result_a, result_c)


if __name__ == "__main__":
    main()
