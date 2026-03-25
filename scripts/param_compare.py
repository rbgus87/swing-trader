"""파라미터 완화 A/B 비교 백테스트.

A (현재): volume_multiplier=1.0, rsi_pullback=45, vol_breakout_multiplier=1.5
B (완화): volume_multiplier=0.8, rsi_pullback=55, vol_breakout_multiplier=1.2

Usage:
    python -m scripts.param_compare
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
END_DATE = "20260320"
INITIAL_CAPITAL = 3_000_000
MAX_POSITIONS = 8

# 현재 config.yaml 기준 공통 파라미터
BASE_PARAMS = {
    "target_return": 0.10,
    "stop_atr_mult": 1.5,
    "trailing_atr_mult": 2.0,
    "trailing_activate_pct": 0.10,
    "max_hold_days": 10,
    "max_stop_pct": 0.07,
    "partial_sell_enabled": True,
    "partial_target_pct": 0.5,
    "partial_sell_ratio": 0.5,
    "adx_threshold": 15,
    "rsi_entry_min": 35,
    "rsi_entry_max": 65,
    "screening_lookback": 5,
    "bb_touch_pct": 0.15,
    "rsi_oversold": 45,
    "regime_strategy": {
        "trending": ["golden_cross", "macd_pullback", "volume_breakout"],
        "sideways": "bb_bounce",
    },
}

# A: 현재 파라미터
SCENARIO_A = {
    **BASE_PARAMS,
    "volume_multiplier": 1.0,
    "rsi_pullback": 45,
    "vol_breakout_multiplier": 1.5,
}

# B: 완화 파라미터
SCENARIO_B = {
    **BASE_PARAMS,
    "volume_multiplier": 0.8,
    "rsi_pullback": 55,
    "vol_breakout_multiplier": 1.2,
}

# C: 거래량만 완화
SCENARIO_C = {
    **BASE_PARAMS,
    "volume_multiplier": 0.8,
    "rsi_pullback": 45,
    "vol_breakout_multiplier": 1.5,
}

# D: RSI pullback만 완화
SCENARIO_D = {
    **BASE_PARAMS,
    "volume_multiplier": 1.0,
    "rsi_pullback": 55,
    "vol_breakout_multiplier": 1.5,
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
        "trades": list(engine._last_trades) if hasattr(engine, '_last_trades') else [],
    }


def print_comparison(*scenarios):
    """다중 시나리오 비교 결과 출력."""
    print(f"\n\n{'='*100}")
    print(f"  파라미터 완화 비교 ({START_DATE} ~ {END_DATE}, {len(CODES)}종목, {MAX_POSITIONS}포지션)")
    print(f"{'='*100}")

    names = [s["name"] for s in scenarios]
    results = [s["result"] for s in scenarios]

    # 헤더
    col_width = 20
    header = f"{'지표':<22}" + "".join(f"{n:>{col_width}}" for n in names)
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
        print(f"{label:<22}" + "".join(parts))

    print("-" * len(header))

    # 월간 매매 횟수
    from datetime import datetime
    d1 = datetime.strptime(START_DATE, "%Y%m%d")
    d2 = datetime.strptime(END_DATE, "%Y%m%d")
    months = max(1, (d2 - d1).days / 30)
    print(f"\n월간 매매 횟수:")
    for s in scenarios:
        monthly = s["result"].trade_count / months
        print(f"  {s['name']}: {monthly:.1f}회/월")
    print(f"  목표: 8~12회/월")

    # 파라미터 차이 요약
    print(f"\n파라미터 비교:")
    print(f"  {'파라미터':<30} {'A (현재)':>12} {'B (전체완화)':>12} {'C (거래량만)':>12} {'D (RSI만)':>12}")
    print(f"  {'volume_multiplier':<30} {'1.0':>12} {'0.8':>12} {'0.8':>12} {'1.0':>12}")
    print(f"  {'rsi_pullback':<30} {'45':>12} {'55':>12} {'45':>12} {'55':>12}")
    print(f"  {'vol_breakout_multiplier':<30} {'1.5':>12} {'1.2':>12} {'1.5':>12} {'1.5':>12}")

    # 베스트 Sharpe
    best = max(scenarios, key=lambda s: s["result"].sharpe_ratio)
    print(f"\n최고 Sharpe: {best['name']} ({best['result'].sharpe_ratio:.2f})")
    print(f"{'='*100}\n")


def main():
    engine = BacktestEngine(initial_capital=INITIAL_CAPITAL)

    # 데이터 프리로드
    print("데이터 프리로드 중...")
    engine.preload_data(CODES, START_DATE, END_DATE)

    result_a = run_scenario("A (현재)", SCENARIO_A, engine)
    result_b = run_scenario("B (전체완화)", SCENARIO_B, engine)
    result_c = run_scenario("C (거래량만)", SCENARIO_C, engine)
    result_d = run_scenario("D (RSI만)", SCENARIO_D, engine)

    print_comparison(result_a, result_b, result_c, result_d)


if __name__ == "__main__":
    main()
