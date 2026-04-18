"""PF 0.97 Loss Decomposition — TF v1 진단.

포트폴리오 백테스트 실행 후 864건의 거래를 분해 분석.
"PF를 깎는 주범이 뭔지" 찾기.

Usage:
    python scripts/loss_decomposition.py
"""
import sys

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except (AttributeError, Exception):
    pass

import numpy as np
import pandas as pd
from collections import defaultdict
from loguru import logger

from src.backtest.portfolio_backtester import (
    run_portfolio_backtest,
    PortfolioTradeResult,
    PortfolioResult,
)
from src.backtest.swing_backtester import CostModel


def analyze(result: PortfolioResult):
    """전체 분해 분석."""
    trades = result.trades
    if not trades:
        print("거래 없음.")
        return

    # ── 0. 기본 확인 ──
    print("=" * 90)
    print("  LOSS DECOMPOSITION — TrendFollowing v1 (PF 0.97 진단)")
    print("=" * 90)
    print(f"  Period:  {result.period}")
    print(f"  Trades:  {result.total_trades}  |  WR: {result.win_rate:.1%}  |  PF: {result.profit_factor:.2f}")
    print(f"  CAGR:    {result.cagr_pct:.1%}  |  MDD: {result.max_drawdown_pct:.1%}")
    print(f"  Capital: {result.initial_capital:,.0f} → {result.final_capital:,.0f}")
    print()

    # ── 1. Payoff Ratio (핵심 지표) ──
    section("1. PAYOFF RATIO — 승패 구조")
    winners = [t for t in trades if t.pnl_amount > 0]
    losers = [t for t in trades if t.pnl_amount <= 0]

    avg_win_pct = np.mean([t.pnl_pct for t in winners]) if winners else 0
    avg_loss_pct = np.mean([t.pnl_pct for t in losers]) if losers else 0
    avg_win_amt = np.mean([t.pnl_amount for t in winners]) if winners else 0
    avg_loss_amt = np.mean([t.pnl_amount for t in losers]) if losers else 0
    payoff = abs(avg_win_pct / avg_loss_pct) if avg_loss_pct != 0 else float('inf')

    med_win_pct = np.median([t.pnl_pct for t in winners]) if winners else 0
    med_loss_pct = np.median([t.pnl_pct for t in losers]) if losers else 0

    print(f"  Wins:  {len(winners)}건  |  Avg: {avg_win_pct:+.2%} ({avg_win_amt:+,.0f}원)  |  Median: {med_win_pct:+.2%}")
    print(f"  Loss:  {len(losers)}건  |  Avg: {avg_loss_pct:+.2%} ({avg_loss_amt:+,.0f}원)  |  Median: {med_loss_pct:+.2%}")
    print(f"  Payoff Ratio:  {payoff:.2f}  (avg_win / avg_loss)")
    print(f"  → PF = WR × Payoff / (1 - WR) = {result.win_rate:.2f} × {payoff:.2f} / {1 - result.win_rate:.2f} = {result.win_rate * payoff / (1 - result.win_rate):.2f}")
    print()
    print(f"  Gross Profit: {sum(t.pnl_amount for t in winners):+,.0f}원")
    print(f"  Gross Loss:   {sum(t.pnl_amount for t in losers):+,.0f}원")
    print(f"  Net P&L:      {sum(t.pnl_amount for t in trades):+,.0f}원")
    print()

    # 수익 분포 (상위/하위 10건)
    sorted_by_pnl = sorted(trades, key=lambda t: t.pnl_pct)
    print(f"  최대 손실 5건:")
    for t in sorted_by_pnl[:5]:
        print(f"    {t.entry_date} {t.ticker} {t.name[:8]}: {t.pnl_pct:+.2%} ({t.pnl_amount:+,.0f}원) [{t.exit_reason}, {t.hold_days}d]")
    print(f"  최대 수익 5건:")
    for t in sorted_by_pnl[-5:]:
        print(f"    {t.entry_date} {t.ticker} {t.name[:8]}: {t.pnl_pct:+.2%} ({t.pnl_amount:+,.0f}원) [{t.exit_reason}, {t.hold_days}d]")

    # ── 2. 연도별 분해 ──
    section("2. 연도별 분해 — 어느 시기가 PF를 깎는가")
    year_groups = defaultdict(list)
    for t in trades:
        year = t.entry_date[:4]
        year_groups[year].append(t)

    print(f"  {'연도':<6} {'건수':>5} {'승률':>6} {'PF':>6} {'수익':>10} {'평균%':>7} {'Payoff':>7} {'판정'}")
    print(f"  {'-'*65}")

    for year in sorted(year_groups.keys()):
        yr_trades = year_groups[year]
        yr_w = [t for t in yr_trades if t.pnl_amount > 0]
        yr_l = [t for t in yr_trades if t.pnl_amount <= 0]
        yr_wr = len(yr_w) / len(yr_trades) if yr_trades else 0
        yr_gp = sum(t.pnl_amount for t in yr_w) if yr_w else 0
        yr_gl = abs(sum(t.pnl_amount for t in yr_l)) if yr_l else 0
        yr_pf = yr_gp / yr_gl if yr_gl > 0 else float('inf')
        yr_net = sum(t.pnl_amount for t in yr_trades)
        yr_avg = np.mean([t.pnl_pct for t in yr_trades]) if yr_trades else 0
        yr_avg_w = np.mean([t.pnl_pct for t in yr_w]) if yr_w else 0
        yr_avg_l = abs(np.mean([t.pnl_pct for t in yr_l])) if yr_l else 0
        yr_payoff = yr_avg_w / yr_avg_l if yr_avg_l > 0 else float('inf')

        pf_s = f"{yr_pf:>6.2f}" if yr_pf < 100 else f"{'inf':>6}"
        po_s = f"{yr_payoff:>7.2f}" if yr_payoff < 100 else f"{'inf':>7}"
        verdict = "OK" if yr_pf >= 1.2 else ("WARN" if yr_pf >= 1.0 else "FAIL")
        print(f"  {year:<6} {len(yr_trades):>5} {yr_wr:>5.1%} {pf_s} {yr_net:>+10,.0f} {yr_avg:>+6.2%} {po_s}  {verdict}")

    # ── 3. Exit Reason별 분해 ──
    section("3. EXIT REASON별 성과 — 어떤 청산이 수익을 깎는가")
    reason_groups = defaultdict(list)
    for t in trades:
        reason_groups[t.exit_reason].append(t)

    print(f"  {'Reason':<16} {'건수':>5} {'승률':>6} {'PF':>6} {'순손익':>10} {'평균%':>7} {'보유일':>5}")
    print(f"  {'-'*60}")

    for reason in ['STOP_LOSS', 'TAKE_PROFIT_1', 'TRAILING', 'TREND_EXIT', 'TIME_EXIT', 'FINAL_CLOSE']:
        rt = reason_groups.get(reason, [])
        if not rt:
            continue
        rw = [t for t in rt if t.pnl_amount > 0]
        rl = [t for t in rt if t.pnl_amount <= 0]
        r_wr = len(rw) / len(rt)
        r_gp = sum(t.pnl_amount for t in rw) if rw else 0
        r_gl = abs(sum(t.pnl_amount for t in rl)) if rl else 0
        r_pf = r_gp / r_gl if r_gl > 0 else float('inf')
        r_net = sum(t.pnl_amount for t in rt)
        r_avg = np.mean([t.pnl_pct for t in rt])
        r_hold = np.mean([t.hold_days for t in rt])
        pf_s = f"{r_pf:>6.2f}" if r_pf < 100 else f"{'inf':>6}"
        print(f"  {reason:<16} {len(rt):>5} {r_wr:>5.1%} {pf_s} {r_net:>+10,.0f} {r_avg:>+6.2%} {r_hold:>5.1f}")

    # ── 4. 비용 영향 분석 ──
    section("4. 비용 영향 — 거래 비용이 PF를 얼마나 깎는가")
    cost = CostModel()
    cost_pct = cost.total_cost_pct()

    trades_before_cost = [(t.pnl_pct + cost_pct) for t in trades]
    gp_bc = sum(p for p in trades_before_cost if p > 0)
    gl_bc = abs(sum(p for p in trades_before_cost if p <= 0))
    pf_bc = gp_bc / gl_bc if gl_bc > 0 else float('inf')
    wr_bc = sum(1 for p in trades_before_cost if p > 0) / len(trades_before_cost)

    total_cost_amount = cost_pct * sum(t.shares * t.entry_price for t in trades)

    print(f"  비용 모델:  매수수수료 {cost.buy_commission:.3%} + 매도수수료 {cost.sell_commission:.3%} + 거래세 {cost.sell_tax:.2%} + 슬리피지 {cost.slippage:.2%}")
    print(f"  왕복 비용:  {cost_pct:.2%}")
    print(f"  총 거래액:  ~{sum(t.shares * t.entry_price for t in trades):,.0f}원")
    print(f"  총 비용:    ~{total_cost_amount:,.0f}원")
    print()
    print(f"  {'':>20} {'비용 전':>10} {'비용 후':>10} {'차이':>8}")
    print(f"  {'PF':>20} {pf_bc:>10.2f} {result.profit_factor:>10.2f} {result.profit_factor - pf_bc:>+8.2f}")
    print(f"  {'WR':>20} {wr_bc:>9.1%} {result.win_rate:>9.1%} {result.win_rate - wr_bc:>+7.1%}")
    print()
    print(f"  → 비용이 PF를 {pf_bc - result.profit_factor:.2f} 깎음.")
    if pf_bc >= 1.2:
        print(f"  ★ 비용 전 PF {pf_bc:.2f} = 진입 엣지는 존재. 비용 최적화가 유효한 개선 경로.")
    elif pf_bc >= 1.0:
        print(f"  → 비용 전 PF {pf_bc:.2f} = 엣지 미약. 비용 줄여도 효과 제한적.")
    else:
        print(f"  → 비용 전 PF {pf_bc:.2f} = 전략 자체에 엣지 없음. 진입/청산 로직 재검토 필요.")

    # ── 5. 연속 손실 분석 ──
    section("5. 연속 손실 분석 — MDD의 원인")

    sorted_trades = sorted(trades, key=lambda t: t.entry_date)

    streak = 0
    streaks = []
    current_streak_loss = 0
    current_streak_start = ""

    for t in sorted_trades:
        if t.pnl_amount <= 0:
            if streak == 0:
                current_streak_start = t.entry_date
            streak += 1
            current_streak_loss += t.pnl_amount
        else:
            if streak > 0:
                streaks.append((current_streak_start, t.entry_date, streak, current_streak_loss))
            streak = 0
            current_streak_loss = 0

    if streak > 0:
        streaks.append((current_streak_start, sorted_trades[-1].entry_date, streak, current_streak_loss))

    streaks.sort(key=lambda s: s[2], reverse=True)

    print(f"  총 연속 손실 구간: {len(streaks)}회")
    print()
    print(f"  {'순위':>4} {'시작':>12} {'종료':>12} {'연속':>4} {'누적 손실':>12}")
    print(f"  {'-'*50}")
    for i, (start, end, length, cum_loss) in enumerate(streaks[:10]):
        print(f"  {i+1:>4} {start:>12} {end:>12} {length:>4}건 {cum_loss:>+12,.0f}원")

    streak_counts = defaultdict(int)
    for _, _, length, _ in streaks:
        streak_counts[length] += 1

    print()
    print(f"  연속 손실 길이 분포:")
    for length in sorted(streak_counts.keys()):
        print(f"    {length}연속: {streak_counts[length]}회")

    # ── 6. 보유 기간별 성과 ──
    section("6. 보유 기간별 성과 — 최적 보유 기간 탐색")
    hold_buckets = [
        ("1-2일", lambda t: t.hold_days <= 2),
        ("3-5일", lambda t: 3 <= t.hold_days <= 5),
        ("6-10일", lambda t: 6 <= t.hold_days <= 10),
        ("11-15일", lambda t: 11 <= t.hold_days <= 15),
        ("15일+", lambda t: t.hold_days > 15),
    ]

    print(f"  {'구간':<8} {'건수':>5} {'승률':>6} {'PF':>6} {'순손익':>10} {'평균%':>7}")
    print(f"  {'-'*50}")

    for label, cond in hold_buckets:
        bt = [t for t in trades if cond(t)]
        if not bt:
            continue
        bw = [t for t in bt if t.pnl_amount > 0]
        bl = [t for t in bt if t.pnl_amount <= 0]
        b_wr = len(bw) / len(bt)
        b_gp = sum(t.pnl_amount for t in bw) if bw else 0
        b_gl = abs(sum(t.pnl_amount for t in bl)) if bl else 0
        b_pf = b_gp / b_gl if b_gl > 0 else float('inf')
        b_net = sum(t.pnl_amount for t in bt)
        b_avg = np.mean([t.pnl_pct for t in bt])
        pf_s = f"{b_pf:>6.2f}" if b_pf < 100 else f"{'inf':>6}"
        print(f"  {label:<8} {len(bt):>5} {b_wr:>5.1%} {pf_s} {b_net:>+10,.0f} {b_avg:>+6.2%}")

    # ── 7. Partial(TP1) vs Full Exit 분석 ──
    section("7. TP1 분할 매도 효과 분석")
    partial = [t for t in trades if t.is_partial]
    full = [t for t in trades if not t.is_partial]

    print(f"  Partial (TP1):  {len(partial)}건")
    if partial:
        p_net = sum(t.pnl_amount for t in partial)
        p_avg = np.mean([t.pnl_pct for t in partial])
        print(f"    순손익: {p_net:+,.0f}원  |  평균: {p_avg:+.2%}")

    print(f"  Full Exit:      {len(full)}건")
    if full:
        f_w = [t for t in full if t.pnl_amount > 0]
        f_l = [t for t in full if t.pnl_amount <= 0]
        f_wr = len(f_w) / len(full)
        f_gp = sum(t.pnl_amount for t in f_w) if f_w else 0
        f_gl = abs(sum(t.pnl_amount for t in f_l)) if f_l else 0
        f_pf = f_gp / f_gl if f_gl > 0 else float('inf')
        f_net = sum(t.pnl_amount for t in full)
        f_avg = np.mean([t.pnl_pct for t in full])
        pf_s = f"{f_pf:.2f}" if f_pf < 100 else "inf"
        print(f"    WR: {f_wr:.1%}  |  PF: {pf_s}  |  순손익: {f_net:+,.0f}원  |  평균: {f_avg:+.2%}")

    # ── 8. 종목별 성과 (상위/하위) ──
    section("8. 종목별 성과 — 상위/하위 10")
    ticker_groups = defaultdict(list)
    for t in trades:
        ticker_groups[t.ticker].append(t)

    ticker_summary = []
    for ticker, tt in ticker_groups.items():
        tw = [t for t in tt if t.pnl_amount > 0]
        tl = [t for t in tt if t.pnl_amount <= 0]
        t_net = sum(t.pnl_amount for t in tt)
        t_wr = len(tw) / len(tt) if tt else 0
        t_gp = sum(t.pnl_amount for t in tw) if tw else 0
        t_gl = abs(sum(t.pnl_amount for t in tl)) if tl else 0
        t_pf = t_gp / t_gl if t_gl > 0 else float('inf')
        name = tt[0].name if tt else ticker
        ticker_summary.append((ticker, name, len(tt), t_wr, t_pf, t_net))

    ticker_summary.sort(key=lambda x: x[5], reverse=True)

    print(f"  {'종목':<16} {'건수':>5} {'승률':>6} {'PF':>6} {'순손익':>10}")
    print(f"  {'-'*50}")

    print(f"  --- 상위 10 ---")
    for ticker, name, cnt, wr, pf, net in ticker_summary[:10]:
        pf_s = f"{pf:>6.2f}" if pf < 100 else f"{'inf':>6}"
        print(f"  {name[:14]:<16} {cnt:>5} {wr:>5.1%} {pf_s} {net:>+10,.0f}")

    print(f"  --- 하위 10 ---")
    for ticker, name, cnt, wr, pf, net in ticker_summary[-10:]:
        pf_s = f"{pf:>6.2f}" if pf < 100 else f"{'inf':>6}"
        print(f"  {name[:14]:<16} {cnt:>5} {wr:>5.1%} {pf_s} {net:>+10,.0f}")

    # ── 9. 개선 레버 요약 ──
    section("9. 개선 레버 요약")

    cost_lever = pf_bc - result.profit_factor
    worst_year = None
    worst_year_pf = 999
    for year, yr_trades in year_groups.items():
        yr_w = [t for t in yr_trades if t.pnl_amount > 0]
        yr_l = [t for t in yr_trades if t.pnl_amount <= 0]
        yr_gp = sum(t.pnl_amount for t in yr_w) if yr_w else 0
        yr_gl = abs(sum(t.pnl_amount for t in yr_l)) if yr_l else 0
        yr_pf = yr_gp / yr_gl if yr_gl > 0 else float('inf')
        if yr_pf < worst_year_pf and len(yr_trades) >= 10:
            worst_year_pf = yr_pf
            worst_year = year

    worst_reason = None
    worst_reason_net = 0
    for reason, rt in reason_groups.items():
        r_net = sum(t.pnl_amount for t in rt)
        if r_net < worst_reason_net:
            worst_reason_net = r_net
            worst_reason = reason

    print(f"  [비용]      PF를 {cost_lever:.2f} 깎음 → 비용 전 PF {pf_bc:.2f}")
    if worst_year:
        print(f"  [최악 연도] {worst_year}년 PF {worst_year_pf:.2f} → 이 구간 방어가 MDD 핵심")
    if worst_reason:
        print(f"  [최악 청산] {worst_reason}: 순손익 {worst_reason_net:+,.0f}원")
    if streaks:
        print(f"  [연속손실]  최대 {streaks[0][2]}연속 (누적 {streaks[0][3]:+,.0f}원)")
    print()

    pnl_pcts = [t.pnl_pct for t in trades]
    big_loss = sum(1 for p in pnl_pcts if p < -0.05)
    small_loss = sum(1 for p in pnl_pcts if -0.05 <= p < 0)
    small_win = sum(1 for p in pnl_pcts if 0 <= p < 0.05)
    big_win = sum(1 for p in pnl_pcts if p >= 0.05)

    total = len(pnl_pcts)
    print(f"  수익 분포:")
    print(f"    큰 손실 (<-5%):  {big_loss:>4}건 ({big_loss/total:.1%})")
    print(f"    작은 손실 (0~-5%): {small_loss:>4}건 ({small_loss/total:.1%})")
    print(f"    작은 수익 (0~+5%): {small_win:>4}건 ({small_win/total:.1%})")
    print(f"    큰 수익 (>+5%):  {big_win:>4}건 ({big_win/total:.1%})")

    print()
    print("=" * 90)
    print("  이 결과를 Archi에게 그대로 공유해주세요.")
    print("=" * 90)


def section(title: str):
    print()
    print(f"  -- {title} --")
    print()


def main():
    logger.info("Loss Decomposition 시작...")
    logger.info("포트폴리오 백테스트 실행 중 (12년, 소요 2~5분)...")

    result = run_portfolio_backtest(
        initial_capital=5_000_000,
        max_positions=4,
    )

    analyze(result)


if __name__ == "__main__":
    main()
