"""상세 백테스트 리포트 생성."""
import sys
sys.stdout.reconfigure(encoding="utf-8")

import numpy as np
import pandas as pd
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

PARAMS = {
    "regime_strategy": {
        "trending": ["golden_cross", "macd_pullback", "volume_breakout"],
        "sideways": "bb_bounce",
    },
    "adx_threshold": 15,
    "macd_fast": 12, "macd_slow": 26, "macd_signal": 9,
    "rsi_period": 14,
    "rsi_entry_min": 35, "rsi_entry_max": 65,
    "bb_period": 20, "bb_std": 2.0,
    "bb_touch_pct": 0.15, "rsi_oversold": 45,
    "rsi_pullback": 45, "screening_lookback": 5,
    "volume_multiplier": 1.0,
    "vol_breakout_multiplier": 1.5, "vol_lookback": 20,
    "target_return": 0.12, "max_hold_days": 10,
    "stop_atr_mult": 1.5, "max_stop_pct": 0.07,
    "trailing_atr_mult": 2.0, "trailing_activate_pct": 0.10,
    "partial_sell_enabled": True, "partial_target_pct": 0.5, "partial_sell_ratio": 0.5,
}

engine = BacktestEngine(initial_capital=INITIAL_CAPITAL)
engine.preload_data(CODES, START_DATE, END_DATE)

result = engine.run_portfolio(
    codes=CODES, start_date=START_DATE, end_date=END_DATE,
    params=PARAMS, strategy_name="adaptive",
    max_positions=MAX_POSITIONS, use_market_filter=True,
)

trades = list(engine._last_trades)
equity = engine._last_equity

# ── 거래 분석 ──
RET_KEY = "return"  # trade dict의 수익률 키
wins = [t for t in trades if t.get(RET_KEY, 0) > 0]
losses = [t for t in trades if t.get(RET_KEY, 0) <= 0]
win_pnls = [t[RET_KEY] * 100 for t in wins]  # % 변환
loss_pnls = [t[RET_KEY] * 100 for t in losses]

best_trade = max(trades, key=lambda t: t.get(RET_KEY, 0)) if trades else {}
worst_trade = min(trades, key=lambda t: t.get(RET_KEY, 0)) if trades else {}

# 연속 승/패
streaks_w, streaks_l = [], []
cur_w, cur_l = 0, 0
for t in trades:
    if t.get(RET_KEY, 0) * 100 > 0:
        cur_w += 1
        if cur_l > 0:
            streaks_l.append(cur_l)
        cur_l = 0
    else:
        cur_l += 1
        if cur_w > 0:
            streaks_w.append(cur_w)
        cur_w = 0
if cur_w > 0:
    streaks_w.append(cur_w)
if cur_l > 0:
    streaks_l.append(cur_l)
max_win_streak = max(streaks_w) if streaks_w else 0
max_loss_streak = max(streaks_l) if streaks_l else 0

# 보유일 분포
hold_days = [t.get("hold_days", 0) for t in trades]

# 비용 계산
total_amount = sum(t.get("entry_price", 0) * t.get("shares", 0) for t in trades)
total_commission = total_amount * 0.00015 * 2
total_tax = total_amount * 0.0015
total_slippage = total_amount * 0.001 * 2
total_cost = total_commission + total_tax + total_slippage

# 월별 수익률
monthly_rets = []
month_labels = []
if equity is not None and len(equity) > 0:
    eq_series = pd.Series(equity)
    total_days = len(eq_series)
    days_per_month = total_days / 26
    start_y, start_m = 2023, 1
    for i in range(26):
        s = int(i * days_per_month)
        e = int((i + 1) * days_per_month)
        e = min(e, total_days - 1)
        if s >= total_days or e >= total_days:
            break
        start_val = eq_series.iloc[s]
        end_val = eq_series.iloc[e]
        ret = (end_val - start_val) / start_val * 100 if start_val > 0 else 0.0
        m = (start_m + i - 1) % 12 + 1
        y = start_y + (start_m + i - 1) // 12
        monthly_rets.append(ret)
        month_labels.append(f"{y}-{m:02d}")

# 종목별 성과
code_stats = {}
for t in trades:
    c = t.get("code", "?")
    if c not in code_stats:
        code_stats[c] = {"wins": 0, "losses": 0, "total_pnl": 0.0, "trades": 0}
    code_stats[c]["trades"] += 1
    code_stats[c]["total_pnl"] += t.get(RET_KEY, 0) * 100
    if t.get(RET_KEY, 0) * 100 > 0:
        code_stats[c]["wins"] += 1
    else:
        code_stats[c]["losses"] += 1

# 청산 사유별 분류
exit_reasons = {}
for t in trades:
    reason = t.get("exit_reason", "unknown")
    if reason not in exit_reasons:
        exit_reasons[reason] = {"count": 0, "total_pnl": 0.0, "wins": 0}
    exit_reasons[reason]["count"] += 1
    exit_reasons[reason]["total_pnl"] += t.get(RET_KEY, 0) * 100
    if t.get(RET_KEY, 0) * 100 > 0:
        exit_reasons[reason]["wins"] += 1


# ════════════════════ 출력 ════════════════════

print("=" * 70)
print("  SWING TRADER 백테스트 상세 리포트")
print("=" * 70)
print(f"  기간: {START_DATE} ~ {END_DATE} (약 26개월)")
print(f"  전략: adaptive (GC + MP + VB / BB)")
print(f"  유니버스: 대형주 {len(CODES)}종목")
print(f"  자본금: {INITIAL_CAPITAL:,}원 | 최대 포지션: {MAX_POSITIONS}")
print("=" * 70)

# ── 1. 핵심 성과 ──
final = INITIAL_CAPITAL * (1 + result.total_return / 100)
monthly_avg = result.total_return / 26
calmar = result.annual_return / abs(result.max_drawdown) if result.max_drawdown != 0 else 0

print(f"\n  1. 핵심 성과 지표")
print(f"  {'─'*66}")
rows = [
    ("초기 자본", f"{INITIAL_CAPITAL:>14,}원"),
    ("최종 자산", f"{final:>14,.0f}원"),
    ("순 수익금", f"{final - INITIAL_CAPITAL:>+14,.0f}원"),
    ("총 수익률", f"{result.total_return:>+13.2f}%"),
    ("연환산 수익률 (CAGR)", f"{result.annual_return:>+13.2f}%"),
    ("월평균 수익률", f"{monthly_avg:>+13.2f}%"),
    ("MDD (최대 낙폭)", f"{result.max_drawdown:>13.2f}%"),
    ("Sharpe Ratio", f"{result.sharpe_ratio:>13.2f}"),
    ("Sortino Ratio", f"{result.sortino_ratio:>13.2f}"),
    ("Calmar Ratio", f"{calmar:>13.2f}"),
]
for label, val in rows:
    dots = "." * (28 - len(label.encode("utf-8")) + len(label))
    print(f"  {label}{dots} {val}")

# ── 2. 거래 통계 ──
print(f"\n  2. 거래 통계")
print(f"  {'─'*66}")
rows2 = [
    ("총 거래 횟수", f"{result.trade_count:>13d}건"),
    ("월간 평균 거래", f"{result.trade_count/26:>13.1f}건"),
    ("승리 거래", f"{len(wins):>13d}건"),
    ("패배 거래", f"{len(losses):>13d}건"),
    ("승률", f"{result.win_rate:>12.2f}%"),
    ("손익비 (Profit Factor)", f"{result.profit_factor:>13.2f}"),
    ("평균 거래 수익률", f"{result.avg_trade_return:>+12.2f}%"),
]
if win_pnls:
    rows2.append(("평균 승리 수익률", f"{np.mean(win_pnls):>+12.2f}%"))
if loss_pnls:
    rows2.append(("평균 패배 수익률", f"{np.mean(loss_pnls):>+12.2f}%"))
rows2 += [
    ("최고 수익 거래", f"{best_trade.get(RET_KEY,0)*100:>+12.2f}% ({best_trade.get("code","?")})"),
    ("최대 손실 거래", f"{worst_trade.get(RET_KEY,0)*100:>+12.2f}% ({worst_trade.get("code","?")})"),
    ("최대 연속 승리", f"{max_win_streak:>13d}건"),
    ("최대 연속 패배", f"{max_loss_streak:>13d}건"),
    ("평균 보유일", f"{result.avg_hold_days:>13.1f}일"),
]
if hold_days:
    rows2.append(("최소/최대 보유일", f"{min(hold_days):>6d} / {max(hold_days):>3d}일"))
for label, val in rows2:
    dots = "." * (28 - len(label.encode("utf-8")) + len(label))
    print(f"  {label}{dots} {val}")

# ── 3. 비용 분석 ──
print(f"\n  3. 비용 분석 (추정)")
print(f"  {'─'*66}")
rows3 = [
    ("총 매매금액", f"{total_amount:>14,.0f}원"),
    ("수수료 (0.015% x 2)", f"{total_commission:>14,.0f}원"),
    ("거래세 (0.2%)", f"{total_tax:>14,.0f}원"),
    ("슬리피지 (0.1% x 2)", f"{total_slippage:>14,.0f}원"),
    ("총 비용 합계", f"{total_cost:>14,.0f}원"),
]
if total_amount > 0:
    rows3.append(("비용 / 매매금액", f"{total_cost/total_amount*100:>13.3f}%"))
for label, val in rows3:
    dots = "." * (28 - len(label.encode("utf-8")) + len(label))
    print(f"  {label}{dots} {val}")

# ── 4. 월별 수익률 ──
print(f"\n  4. 월별 수익률")
print(f"  {'─'*66}")
if monthly_rets:
    for row_start in range(0, len(month_labels), 6):
        row_end = min(row_start + 6, len(month_labels))
        labels = month_labels[row_start:row_end]
        rets = monthly_rets[row_start:row_end]
        print("  " + "".join(f"{l:>12}" for l in labels))
        line = "  "
        for r in rets:
            sign = "+" if r >= 0 else ""
            line += f"{sign}{r:>10.2f}% "
        print(line)
        print()

    pos_months = sum(1 for r in monthly_rets if r > 0)
    neg_months = sum(1 for r in monthly_rets if r <= 0)
    total_m = pos_months + neg_months
    print(f"  수익 월: {pos_months}개월 | 손실 월: {neg_months}개월 | 수익월 비율: {pos_months/total_m*100:.0f}%")
    print(f"  최고 월수익: {max(monthly_rets):+.2f}% | 최저 월수익: {min(monthly_rets):+.2f}%")
    print(f"  월평균 수익: {np.mean(monthly_rets):+.2f}% | 월수익 표준편차: {np.std(monthly_rets):.2f}%")

# ── 5. 청산 사유별 분석 ──
print(f"\n  5. 청산 사유별 분석")
print(f"  {'─'*66}")
print(f"  {'사유':<20s} {'건수':>6s} {'비율':>8s} {'승률':>8s} {'평균P&L':>10s}")
print(f"  {'─'*52}")
for reason, stats in sorted(exit_reasons.items(), key=lambda x: -x[1]["count"]):
    cnt = stats["count"]
    pct = cnt / result.trade_count * 100
    wr = stats["wins"] / cnt * 100 if cnt > 0 else 0
    avg_pnl = stats["total_pnl"] / cnt if cnt > 0 else 0
    print(f"  {reason:<20s} {cnt:>6d} {pct:>7.1f}% {wr:>7.1f}% {avg_pnl:>+9.2f}%")

# ── 6. 종목별 성과 ──
print(f"\n  6. 종목별 성과 (누적 수익률 기준)")
print(f"  {'─'*66}")
sorted_codes = sorted(code_stats.items(), key=lambda x: -x[1]["total_pnl"])

print(f"\n  [상위 10종목]")
print(f"  {'종목':>8s} {'거래':>5s} {'승':>4s} {'패':>4s} {'승률':>7s} {'누적P&L':>10s}")
for code, s in sorted_codes[:10]:
    wr = s["wins"] / s["trades"] * 100 if s["trades"] > 0 else 0
    print(f"  {code:>8s} {s['trades']:>5d} {s['wins']:>4d} {s['losses']:>4d} {wr:>6.1f}% {s['total_pnl']:>+9.2f}%")

print(f"\n  [하위 5종목]")
for code, s in sorted_codes[-5:]:
    wr = s["wins"] / s["trades"] * 100 if s["trades"] > 0 else 0
    print(f"  {code:>8s} {s['trades']:>5d} {s['wins']:>4d} {s['losses']:>4d} {wr:>6.1f}% {s['total_pnl']:>+9.2f}%")

# ── 7. 목표 대비 평가 ──
print(f"\n  7. 성과 목표 대비 평가")
print(f"  {'─'*66}")
targets = [
    ("승률", result.win_rate, ">=50%", result.win_rate >= 50),
    ("손익비", result.profit_factor, ">=1.5", result.profit_factor >= 1.5),
    ("월 수익률", monthly_avg, "1.5~3.0%", 1.5 <= monthly_avg <= 3.0),
    ("Sharpe", result.sharpe_ratio, "1.0~1.3", result.sharpe_ratio >= 1.0),
    ("월 매매", result.trade_count / 26, "8~12회", 8 <= result.trade_count / 26 <= 12),
    ("MDD", abs(result.max_drawdown), "<=20%", abs(result.max_drawdown) <= 20),
]
for name, actual, target_str, passed in targets:
    mark = "O" if passed else "X"
    unit = "%" if name in ("승률", "월 수익률", "MDD") else ("회" if name == "월 매매" else "")
    print(f"  [{mark}] {name:<12s} {actual:>8.2f}{unit}  (목표: {target_str})")

print(f"\n{'='*70}")
print(f"  리포트 생성 완료")
print(f"{'='*70}")
