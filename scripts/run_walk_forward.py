"""Walk-Forward 검증 실행 + IS/OOS 비교 분석.

각 구간에서:
1. Train(IS) 기간으로 최적 파라미터 탐색
2. Test(OOS) 기간으로 미래 성과 측정
3. IS vs OOS 성과 열화율 분석 → 오버피팅 감지

Usage:
    python scripts/run_walk_forward.py
    python scripts/run_walk_forward.py --strategy adaptive --train 24 --test 3
"""

import argparse
import os
import sys
import time
import warnings
from datetime import datetime

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
sys.stdout.reconfigure(encoding="utf-8")
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ".")

from src.backtest.engine import BacktestEngine, BacktestResult
from src.backtest.optimizer import ParameterOptimizer
from src.backtest.report import BacktestReporter

# watchlist 20종목
CODES = [
    "005930", "000660", "005380", "000270", "068270",
    "035420", "035720", "105560", "055550", "066570",
    "006400", "003670", "012330", "028260", "096770",
    "003550", "034730", "032830", "030200", "017670",
]

# 최적화 대상 파라미터 그리드 (소규모)
WF_GRID = {
    "rsi_min": [35, 40],
    "rsi_max": [65, 70],
    "volume_multiplier": [1.0, 1.5],
    "stop_atr_mult": [2.0, 2.5],
    "target_return": [0.06, 0.08],
    "max_hold_days": [10, 15],
    "trailing_activate_pct": [0.05, 0.07],
    "max_stop_pct": [0.07, 0.10],
}


def run_walk_forward(
    codes: list[str],
    start_date: str,
    end_date: str,
    strategy_name: str,
    train_months: int,
    test_months: int,
    step_months: int,
    capital: int,
) -> list[dict]:
    """Walk-Forward 실행 + IS/OOS 결과 수집."""
    engine = BacktestEngine(initial_capital=capital)
    optimizer = ParameterOptimizer(engine)

    windows = optimizer._generate_walk_forward_windows(
        start_date, end_date, train_months, test_months, step_months,
    )

    if not windows:
        print("Walk-Forward 윈도우 생성 실패 (기간 부족)")
        return []

    print(f"\nWalk-Forward 검증: {len(windows)}개 구간")
    print(f"  Train: {train_months}개월, Test: {test_months}개월, Step: {step_months}개월")
    print(f"  전략: {strategy_name}, 자본금: {capital:,}원, 종목: {len(codes)}개")
    print(f"  기간: {start_date} ~ {end_date}")
    print()

    results = []

    for i, (tr_start, tr_end, te_start, te_end) in enumerate(windows):
        print(f"  구간 {i + 1}/{len(windows)}: Train {tr_start}~{tr_end} → Test {te_start}~{te_end}")
        t0 = time.time()

        try:
            # IS: 그리드 서치 (필터 완화)
            is_results = optimizer.run_grid_search(
                codes, tr_start, tr_end, WF_GRID,
            )

            if is_results.empty:
                print(f"    ⚠ Train 결과 없음, 기본 파라미터 사용")
                best_params = {}
            else:
                best_row = is_results.iloc[0]
                best_params = {
                    k: best_row[k] for k in WF_GRID.keys() if k in best_row.index
                }

            # IS 최적 성과 기록
            is_result = engine.run(
                codes, tr_start, tr_end, best_params, strategy_name,
            )

            # OOS: 미래 성과 측정
            oos_result = engine.run(
                codes, te_start, te_end, best_params, strategy_name,
            )

            elapsed = time.time() - t0
            print(
                f"    IS: 수익률 {is_result.total_return:+.2f}%, "
                f"Sharpe {is_result.sharpe_ratio:.2f} | "
                f"OOS: 수익률 {oos_result.total_return:+.2f}%, "
                f"Sharpe {oos_result.sharpe_ratio:.2f} | "
                f"{elapsed:.0f}초"
            )

            results.append({
                "window": i + 1,
                "train_start": tr_start,
                "train_end": tr_end,
                "test_start": te_start,
                "test_end": te_end,
                "params": best_params,
                "is_return": is_result.total_return,
                "is_sharpe": is_result.sharpe_ratio,
                "is_mdd": is_result.max_drawdown,
                "is_win_rate": is_result.win_rate,
                "is_trades": is_result.trade_count,
                "oos_return": oos_result.total_return,
                "oos_sharpe": oos_result.sharpe_ratio,
                "oos_mdd": oos_result.max_drawdown,
                "oos_win_rate": oos_result.win_rate,
                "oos_trades": oos_result.trade_count,
            })

        except Exception as e:
            print(f"    ✗ 실패: {e}")

    return results


def analyze_results(results: list[dict]) -> None:
    """IS vs OOS 비교 분석."""
    if not results:
        print("\n분석할 결과 없음")
        return

    df = pd.DataFrame(results)

    print(f"\n{'='*90}")
    print(f"  Walk-Forward IS vs OOS 비교 분석")
    print(f"{'='*90}")

    # 구간별 상세
    print(f"\n{'구간':>4} {'Train 기간':>22} {'Test 기간':>22} "
          f"{'IS수익':>8} {'OOS수익':>8} {'IS샤프':>7} {'OOS샤프':>7} "
          f"{'IS승률':>7} {'OOS승률':>7}")
    print("-" * 90)

    for _, r in df.iterrows():
        print(
            f"{r['window']:>4} "
            f"{r['train_start']}~{r['train_end']} "
            f"{r['test_start']}~{r['test_end']} "
            f"{r['is_return']:>+7.2f}% {r['oos_return']:>+7.2f}% "
            f"{r['is_sharpe']:>7.2f} {r['oos_sharpe']:>7.2f} "
            f"{r['is_win_rate']:>6.1f}% {r['oos_win_rate']:>6.1f}%"
        )

    # 종합 통계
    print(f"\n{'='*60}")
    print(f"  종합 통계")
    print(f"{'='*60}")

    metrics = [
        ("수익률 (%)", "is_return", "oos_return"),
        ("Sharpe", "is_sharpe", "oos_sharpe"),
        ("MDD (%)", "is_mdd", "oos_mdd"),
        ("승률 (%)", "is_win_rate", "oos_win_rate"),
        ("거래 수", "is_trades", "oos_trades"),
    ]

    print(f"{'지표':>14} {'IS 평균':>10} {'OOS 평균':>10} {'열화율':>10} {'판정':>8}")
    print("-" * 56)

    overfitting_score = 0
    for name, is_col, oos_col in metrics:
        is_mean = df[is_col].mean()
        oos_mean = df[oos_col].mean()

        if abs(is_mean) > 0.01:
            degradation = (1 - oos_mean / is_mean) * 100
        else:
            degradation = 0

        # 판정 기준
        if name in ("수익률 (%)", "Sharpe"):
            if degradation > 50:
                verdict = "오버핏"
                overfitting_score += 2
            elif degradation > 30:
                verdict = "주의"
                overfitting_score += 1
            else:
                verdict = "양호"
        elif name == "MDD (%)":
            # MDD는 OOS가 IS보다 작으면(덜 빠지면) 좋음
            if oos_mean < is_mean:
                verdict = "양호"
            else:
                verdict = "주의"
                overfitting_score += 1
        else:
            verdict = "-"

        print(f"{name:>14} {is_mean:>10.2f} {oos_mean:>10.2f} {degradation:>+9.1f}% {verdict:>8}")

    # OOS 수익 일관성
    oos_positive = (df["oos_return"] > 0).sum()
    oos_total = len(df)
    consistency = oos_positive / oos_total * 100

    print(f"\n  OOS 수익 양(+) 구간: {oos_positive}/{oos_total} ({consistency:.0f}%)")
    print(f"  OOS 평균 수익률: {df['oos_return'].mean():+.2f}%")
    print(f"  OOS 수익률 표준편차: {df['oos_return'].std():.2f}%")

    # 최종 판정
    print(f"\n  {'='*40}")
    if overfitting_score == 0:
        print(f"  최종 판정: 견고 (Robust) — 파라미터 신뢰 가능")
    elif overfitting_score <= 2:
        print(f"  최종 판정: 주의 — 일부 지표 열화, 파라미터 범위 축소 권장")
    else:
        print(f"  최종 판정: 오버피팅 — 파라미터 재검토 필요")
    print(f"  {'='*40}\n")


def generate_html_report(results: list[dict], output_path: str) -> str:
    """Walk-Forward 결과 HTML 리포트 생성."""
    if not results:
        return ""

    df = pd.DataFrame(results)

    # 구간별 테이블
    rows = []
    for _, r in df.iterrows():
        is_css = "positive" if r["is_return"] > 0 else "negative"
        oos_css = "positive" if r["oos_return"] > 0 else "negative"
        rows.append(
            f'<tr><td>{int(r["window"])}</td>'
            f'<td>{r["train_start"]}~{r["train_end"]}</td>'
            f'<td>{r["test_start"]}~{r["test_end"]}</td>'
            f'<td class="{is_css}">{r["is_return"]:+.2f}%</td>'
            f'<td class="{oos_css}">{r["oos_return"]:+.2f}%</td>'
            f'<td>{r["is_sharpe"]:.2f}</td>'
            f'<td>{r["oos_sharpe"]:.2f}</td>'
            f'<td>{r["is_win_rate"]:.1f}%</td>'
            f'<td>{r["oos_win_rate"]:.1f}%</td></tr>'
        )

    # 차트 생성
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import base64
    from io import BytesIO

    plt.rcParams["font.family"] = "Malgun Gothic"
    plt.rcParams["axes.unicode_minus"] = False

    # IS vs OOS 수익률 비교 차트
    fig, axes = plt.subplots(1, 2, figsize=(14, 5))

    x = range(1, len(df) + 1)
    axes[0].bar([i - 0.2 for i in x], df["is_return"], 0.35, label="IS (Train)", color="#2196F3", alpha=0.8)
    axes[0].bar([i + 0.2 for i in x], df["oos_return"], 0.35, label="OOS (Test)", color="#FF9800", alpha=0.8)
    axes[0].axhline(y=0, color="gray", linewidth=0.5)
    axes[0].set_title("구간별 수익률: IS vs OOS")
    axes[0].set_xlabel("구간")
    axes[0].set_ylabel("수익률 (%)")
    axes[0].legend()
    axes[0].grid(True, alpha=0.3)

    axes[1].bar([i - 0.2 for i in x], df["is_sharpe"], 0.35, label="IS Sharpe", color="#2196F3", alpha=0.8)
    axes[1].bar([i + 0.2 for i in x], df["oos_sharpe"], 0.35, label="OOS Sharpe", color="#FF9800", alpha=0.8)
    axes[1].axhline(y=0, color="gray", linewidth=0.5)
    axes[1].set_title("구간별 Sharpe: IS vs OOS")
    axes[1].set_xlabel("구간")
    axes[1].set_ylabel("Sharpe Ratio")
    axes[1].legend()
    axes[1].grid(True, alpha=0.3)

    fig.tight_layout()
    buf = BytesIO()
    fig.savefig(buf, format="png", dpi=100, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    chart_b64 = base64.b64encode(buf.read()).decode("utf-8")

    # 종합 통계
    is_return_avg = df["is_return"].mean()
    oos_return_avg = df["oos_return"].mean()
    is_sharpe_avg = df["is_sharpe"].mean()
    oos_sharpe_avg = df["oos_sharpe"].mean()
    oos_positive = (df["oos_return"] > 0).sum()
    consistency = oos_positive / len(df) * 100

    ret_degradation = (1 - oos_return_avg / is_return_avg) * 100 if abs(is_return_avg) > 0.01 else 0

    html = f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<title>Walk-Forward 검증 리포트</title>
<style>
body {{ font-family: 'Malgun Gothic', sans-serif; margin: 40px; background: #f5f5f5; }}
h1 {{ color: #333; border-bottom: 2px solid #FF9800; padding-bottom: 10px; }}
h2 {{ color: #555; margin-top: 30px; }}
table {{ border-collapse: collapse; width: 100%; max-width: 1100px; margin: 10px 0; }}
th, td {{ border: 1px solid #ddd; padding: 8px 12px; text-align: center; }}
th {{ background: #FF9800; color: white; }}
tr:nth-child(even) {{ background: #f9f9f9; }}
.positive {{ color: #4CAF50; font-weight: bold; }}
.negative {{ color: #f44336; font-weight: bold; }}
.summary {{ background: #fff; padding: 20px; border: 1px solid #ddd; border-radius: 8px; margin: 20px 0; }}
.summary h3 {{ margin-top: 0; }}
img {{ border: 1px solid #ddd; border-radius: 4px; margin: 10px 0; }}
.footer {{ margin-top: 30px; color: #999; font-size: 12px; }}
</style>
</head>
<body>
<h1>Walk-Forward 검증 리포트</h1>
<p>생성 시각: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}</p>

<div class="summary">
<h3>종합 요약</h3>
<table style="max-width: 500px;">
<tr><th>지표</th><th>IS (Train)</th><th>OOS (Test)</th></tr>
<tr><td>평균 수익률</td><td>{is_return_avg:+.2f}%</td><td class="{'positive' if oos_return_avg > 0 else 'negative'}">{oos_return_avg:+.2f}%</td></tr>
<tr><td>평균 Sharpe</td><td>{is_sharpe_avg:.2f}</td><td>{oos_sharpe_avg:.2f}</td></tr>
<tr><td>수익률 열화</td><td colspan="2">{ret_degradation:+.1f}%</td></tr>
<tr><td>OOS 수익 일관성</td><td colspan="2">{oos_positive}/{len(df)} ({consistency:.0f}%)</td></tr>
</table>
</div>

<h2>IS vs OOS 비교 차트</h2>
<img src="data:image/png;base64,{chart_b64}" style="max-width:100%; height:auto;">

<h2>구간별 상세</h2>
<table>
<tr><th>#</th><th>Train 기간</th><th>Test 기간</th>
<th>IS 수익</th><th>OOS 수익</th><th>IS Sharpe</th><th>OOS Sharpe</th>
<th>IS 승률</th><th>OOS 승률</th></tr>
{"".join(rows)}
</table>

<div class="footer">
swing-trader Walk-Forward 검증 엔진
</div>
</body>
</html>"""

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"HTML 리포트: {output_path}")
    return output_path


def main():
    parser = argparse.ArgumentParser(description="Walk-Forward 검증")
    parser.add_argument("--strategy", default="golden_cross", help="전략 (기본: golden_cross)")
    parser.add_argument("--start", default="20190101", help="시작일 (YYYYMMDD)")
    parser.add_argument("--end", default="20250314", help="종료일 (YYYYMMDD)")
    parser.add_argument("--train", type=int, default=24, help="Train 개월 수 (기본: 24)")
    parser.add_argument("--test", type=int, default=3, help="Test 개월 수 (기본: 3)")
    parser.add_argument("--step", type=int, default=3, help="Step 개월 수 (기본: 3)")
    parser.add_argument("--capital", type=int, default=3_000_000, help="자본금 (기본: 3,000,000)")
    args = parser.parse_args()

    t0 = time.time()
    results = run_walk_forward(
        CODES, args.start, args.end, args.strategy,
        args.train, args.test, args.step, args.capital,
    )
    elapsed = time.time() - t0

    analyze_results(results)

    if results:
        report_path = generate_html_report(
            results,
            f"reports/walk_forward_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html",
        )

    print(f"총 소요 시간: {elapsed:.0f}초")


if __name__ == "__main__":
    main()
