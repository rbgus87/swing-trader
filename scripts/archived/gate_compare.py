"""게이트/스크리닝 완화 A/B 테스트.

A (Baseline): 현재 config 그대로
D: ADX 임계값 완화 (15→10) — 더 많은 구간을 trending으로 분류
E: 전략 파라미터 완화 (RSI 범위 확대 + 거래량 배수 완화)
F: 포트폴리오 필터 완화 (거래량/모멘텀 필터 완화)
G: D+E+F 종합 완화

Usage:
    python -m scripts.gate_compare
"""

import sys
sys.stdout.reconfigure(encoding="utf-8")

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

# A: 현재 config (baseline)
SCENARIO_A = {
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
    "regime_strategy": {
        "trending": ["golden_cross", "macd_pullback"],
        "sideways": "bb_bounce",
    },
}

# D: ADX 임계값 완화 (15→10)
# 효과: 더 많은 날을 "trending"으로 분류 → trending 전략 활성 기간 증가
SCENARIO_D = {**SCENARIO_A, "adx_threshold": 10}

# E: 전략 파라미터 완화 (RSI 범위 + 거래량 배수)
# 효과: 개별 전략의 진입 조건이 느슨해짐
SCENARIO_E = {
    **SCENARIO_A,
    "rsi_entry_min": 30,       # 35→30
    "rsi_entry_max": 70,       # 65→70
    "volume_multiplier": 0.8,  # 1.0→0.8
    "rsi_oversold": 50,        # 45→50 (BB 진입 확대)
    "bb_touch_pct": 0.20,      # 0.15→0.20 (BB 범위 확대)
}

# F: 포트폴리오 필터 완화 (모멘텀/거래량 필터)
# 효과: run_portfolio 내부 필터가 덜 걸러냄
SCENARIO_F = {
    **SCENARIO_A,
    "volume_min_ratio": 0.5,   # 0.8→0.5 (거래량 하한 완화)
    "momentum_floor": -0.25,   # -0.15→-0.25 (더 깊은 조정도 허용)
}

# G: D+E+F 종합 완화
SCENARIO_G = {
    **SCENARIO_A,
    "adx_threshold": 10,
    "rsi_entry_min": 30,
    "rsi_entry_max": 70,
    "volume_multiplier": 0.8,
    "rsi_oversold": 50,
    "bb_touch_pct": 0.20,
    "volume_min_ratio": 0.5,
    "momentum_floor": -0.25,
}


def run_scenario(name: str, params: dict, engine: BacktestEngine) -> dict:
    print(f"\n{'='*60}")
    print(f"  {name}")
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

    return {"name": name, "result": result}


def print_comparison(*scenarios):
    print(f"\n\n{'='*100}")
    print(f"  게이트/스크리닝 완화 비교 ({START_DATE} ~ {END_DATE}, {len(CODES)}종목)")
    print(f"{'='*100}")

    names = [s["name"] for s in scenarios]
    results = [s["result"] for s in scenarios]

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

    months = 26
    print(f"\n월간 매매 횟수 (추정):")
    for s in scenarios:
        monthly = s["result"].trade_count / months
        print(f"  {s['name']}: {monthly:.1f}회/월")
    print(f"  목표: 8~12회/월")

    # 최고 Sharpe + 최다 거래
    best_sharpe = max(scenarios, key=lambda s: s["result"].sharpe_ratio)
    most_trades = max(scenarios, key=lambda s: s["result"].trade_count)
    print(f"\n최고 Sharpe: {best_sharpe['name']} ({best_sharpe['result'].sharpe_ratio:.2f})")
    print(f"최다 거래:   {most_trades['name']} ({most_trades['result'].trade_count}건)")

    # 수익률 유지 + 거래 증가 조합 찾기
    baseline = scenarios[0]["result"]
    print(f"\n수익률 유지(>= A의 80%) + 거래 증가 조합:")
    threshold = baseline.total_return * 0.8
    for s in scenarios[1:]:
        r = s["result"]
        if r.total_return >= threshold and r.trade_count > baseline.trade_count:
            print(f"  {s['name']}: 수익률 {r.total_return:.2f}%, 거래 {r.trade_count}건 (Sharpe {r.sharpe_ratio:.2f})")

    print(f"{'='*100}\n")


def main():
    engine = BacktestEngine(initial_capital=INITIAL_CAPITAL)

    print("데이터 프리로드 중...")
    engine.preload_data(CODES, START_DATE, END_DATE)

    result_a = run_scenario("A (현재)", SCENARIO_A, engine)
    result_d = run_scenario("D (ADX 10)", SCENARIO_D, engine)
    result_e = run_scenario("E (RSI+Vol)", SCENARIO_E, engine)
    result_f = run_scenario("F (필터완화)", SCENARIO_F, engine)
    result_g = run_scenario("G (종합완화)", SCENARIO_G, engine)

    print_comparison(result_a, result_d, result_e, result_f, result_g)


if __name__ == "__main__":
    main()
