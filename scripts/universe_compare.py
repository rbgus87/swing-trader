"""종목풀 확대 + 보유 종목 수 매트릭스 테스트.

종목풀: 20종목 vs 50종목 vs 100종목
보유 수: 5개 vs 7개 vs 10개

Usage:
    python -m scripts.universe_compare
"""

import sys
sys.stdout.reconfigure(encoding="utf-8")

from loguru import logger
from src.backtest.engine import BacktestEngine

# 20종목 (현재)
CODES_20 = [
    "005930", "000660", "005380", "000270", "068270",
    "035420", "035720", "105560", "055550", "066570",
    "006400", "003670", "012330", "028260", "096770",
    "003550", "034730", "032830", "030200", "017670",
]

# 50종목 (대형주 + 중형주)
CODES_50 = CODES_20 + [
    "051910", "009150", "018260", "036570", "004020",  # LG화학, 삼성전기, 삼성SDS, 엔씨소프트, 현대제철
    "011170", "086790", "097950", "010130", "033780",  # 롯데케미칼, 하나금융, CJ제일제당, 고려아연, KT&G
    "015760", "001570", "005490", "034020", "003490",  # 한국전력, 금양, POSCO, 두산에너빌리티, 대한항공
    "010950", "271560", "009540", "024110", "011200",  # S-Oil, 오리온, 한국조선해양, 기업은행, HMM
    "000810", "005830", "326030", "011790", "016360",  # 삼성화재, DB손보, SK바이오팜, SKC, 삼성증권
    "047050", "267260", "000720", "259960", "180640",  # 포스코인터, HD현대일렉, 현대건설, 크래프톤, 한진칼
]

# 100종목 (대형 + 중형 + 소형 우량주)
CODES_100 = CODES_50 + [
    "003410", "006260", "039490", "004370", "011780",  # 쌍용C&E, LS, 키움증권, 농심, 금호석유
    "138040", "271940", "241560", "090430", "088350",  # 메리츠금융, 잡코리아, 두산밥캣, 아모레퍼시픽, 한화생명
    "383220", "361610", "377300", "003030", "004990",  # F&F, SK아이이테크, 카카오페이, 세아제강, 롯데지주
    "009830", "042660", "000100", "069500", "042700",  # 한화솔루션, 한화오션, 유한양행, KODEX200, 한미반도체
    "128940", "112610", "010120", "352820", "023530",  # 한미약품, CS Wind, LS일렉, 하이브, 롯데쇼핑
    "307950", "247540", "298050", "006800", "005940",  # 현대오토에버, 에코프로BM, 에스엘, 미래에셋증권, NH투자증권
    "021240", "078930", "004170", "010140", "036460",  # 코웨이, GS, 신세계, 삼성중공업, 한국가스공사
    "002790", "008770", "139480", "005070", "020150",  # 아모레G, 호텔신라, 이마트, 코스모신소재, 일진머티리얼즈
    "316140", "010620", "950210", "402340", "011070",  # 우리금융, 현대미포, 프레스티지바이오로직스, SK스퀘어, LG이노텍
    "017800", "030000", "014680", "018880", "009420",  # 현대엘리베이, 제일기획, 한솔케미칼, 한온시스템, 한올바이오
]

START_DATE = "20230101"
END_DATE = "20250314"
INITIAL_CAPITAL = 3_000_000

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
    "regime_strategy": {
        "trending": ["golden_cross", "macd_pullback"],
        "sideways": "bb_bounce",
    },
}

UNIVERSES = {
    "20종목": CODES_20,
    "50종목": CODES_50,
    "100종목": CODES_100,
}

MAX_POS_OPTIONS = [5, 7, 10]


def main():
    results = {}

    for uni_name, codes in UNIVERSES.items():
        engine = BacktestEngine(initial_capital=INITIAL_CAPITAL)
        print(f"\n{'#'*60}")
        print(f"  {uni_name} 데이터 프리로드 중...")
        print(f"{'#'*60}")
        engine.preload_data(codes, START_DATE, END_DATE)

        for max_pos in MAX_POS_OPTIONS:
            label = f"{uni_name}/pos{max_pos}"
            print(f"\n  --- {label} ---")

            result = engine.run_portfolio(
                codes=codes,
                start_date=START_DATE,
                end_date=END_DATE,
                params=BASE_PARAMS,
                strategy_name="adaptive",
                max_positions=max_pos,
                use_market_filter=True,
            )
            results[label] = result

    # 결과 출력
    print(f"\n\n{'='*120}")
    print(f"  종목풀 x 보유수 매트릭스 ({START_DATE} ~ {END_DATE})")
    print(f"{'='*120}")

    col_width = 16
    labels = list(results.keys())
    header = f"{'지표':<18}" + "".join(f"{l:>{col_width}}" for l in labels)
    print(header)
    print("-" * len(header))

    metric_defs = [
        ("총 수익률 (%)", "total_return"),
        ("연환산 (%)", "annual_return"),
        ("MDD (%)", "max_drawdown"),
        ("Sharpe", "sharpe_ratio"),
        ("승률 (%)", "win_rate"),
        ("손익비", "profit_factor"),
        ("평균수익 (%)", "avg_trade_return"),
        ("거래 횟수", "trade_count"),
        ("보유일", "avg_hold_days"),
    ]

    for label_kr, attr in metric_defs:
        parts = []
        for l in labels:
            v = getattr(results[l], attr)
            if isinstance(v, int):
                parts.append(f"{v:>{col_width}d}")
            else:
                s = f"{v:.2f}" if v != float("inf") else "inf"
                parts.append(f"{s:>{col_width}}")
        print(f"{label_kr:<18}" + "".join(parts))

    print("-" * len(header))

    # 월간 매매 + 월 수익률
    months = 26
    print(f"\n{'지표':<18}" + "".join(f"{l:>{col_width}}" for l in labels))
    print("-" * (18 + col_width * len(labels)))
    # 월 매매
    parts = []
    for l in labels:
        monthly = results[l].trade_count / months
        parts.append(f"{monthly:>{col_width - 2}.1f}회")
    print(f"{'월 매매':<18}" + "".join(parts))
    # 월 수익률
    parts = []
    for l in labels:
        monthly_ret = results[l].total_return / months
        parts.append(f"{monthly_ret:>{col_width - 2}.2f}%")
    print(f"{'월 수익률 (추정)':<18}" + "".join(parts))

    # 베스트 조합 찾기
    print(f"\n{'='*120}")
    best_return = max(results.items(), key=lambda x: x[1].total_return)
    best_sharpe = max(results.items(), key=lambda x: x[1].sharpe_ratio)
    best_trades = max(results.items(), key=lambda x: x[1].trade_count)
    print(f"  최고 수익률: {best_return[0]} ({best_return[1].total_return:.2f}%)")
    print(f"  최고 Sharpe: {best_sharpe[0]} ({best_sharpe[1].sharpe_ratio:.2f})")
    print(f"  최다 거래:   {best_trades[0]} ({best_trades[1].trade_count}건)")
    print(f"{'='*120}\n")


if __name__ == "__main__":
    main()
