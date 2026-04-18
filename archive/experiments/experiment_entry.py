"""실험 4: 진입 방식 3종 비교 — v2 청산 구조 위에서.

고정 청산: SL 2.0 / Trail 4.0 / Hold 20 / TP1 2.0(50%)
비교:
  C: 60일 신고가 돌파 (현행, baseline)
  A: 돌파 후 MA20±ATR×0.5 리테스트 (5일 이내)
  B: 골든크로스 (MA5↑MA20) + MA20>MA60 + ADX20
"""
import sys
import time

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
    load_backtest_data,
    precompute_daily_signals,
)
from src.backtest.swing_backtester import CostModel
from src.strategy.trend_following_v0 import StrategyParams


BASE_PARAMS = StrategyParams(
    stop_loss_atr=2.0,
    take_profit_atr=2.0,
    tp1_sell_ratio=0.5,
    trailing_atr=4.0,
    max_hold_days=20,
)

RETEST_WINDOW = 5
RETEST_TOLERANCE = 0.5  # |close - MA20| <= ATR * 0.5


def precompute_retest(base_pre, ticker_data, ticker_date_idx):
    """돌파 감지(base의 candidates) + 리테스트 탐색 → 진입일 재배치."""
    retest_by_date = defaultdict(list)

    for date_str, cands in base_pre['candidates'].items():
        breakout_ts = pd.Timestamp(date_str)
        for cand in cands:
            ticker = cand['ticker']
            idx_map = ticker_date_idx.get(ticker, {})
            bi = idx_map.get(breakout_ts)
            if bi is None:
                continue
            df_t = ticker_data[ticker]
            breakout_low = df_t.iloc[bi]['low']

            for delta in range(1, RETEST_WINDOW + 1):
                fi = bi + delta
                if fi >= len(df_t):
                    break
                row = df_t.iloc[fi]
                if row['close'] < breakout_low:
                    break
                ma20 = row.get('ma20')
                atr = row.get('atr')
                if pd.isna(ma20) or pd.isna(atr) or atr <= 0:
                    continue
                if abs(row['close'] - ma20) <= atr * RETEST_TOLERANCE:
                    retest_date_str = row['date'].strftime('%Y-%m-%d')
                    retest_by_date[retest_date_str].append({
                        'ticker': ticker,
                        'score': cand['score'],
                        'close': float(row['close']),
                        'atr': float(atr),
                    })
                    break

    for k in retest_by_date:
        retest_by_date[k].sort(key=lambda x: x['score'], reverse=True)

    return {
        'breadth': base_pre['breadth'],
        'candidates': dict(retest_by_date),
        'universe_at': base_pre['universe_at'],
        'universe_refresh_count': base_pre['universe_refresh_count'],
    }


def precompute_golden_cross(
    trading_dates, ticker_data, ticker_date_idx, universe_at, breadth, params
):
    """MA5↑MA20 골든크로스 + MA20>MA60 + ADX + 거래대금."""
    gc_by_date = defaultdict(list)

    for date_str in trading_dates:
        ts = pd.Timestamp(date_str)
        universe = universe_at.get(date_str, set())
        for ticker in universe:
            idx_map = ticker_date_idx.get(ticker, {})
            curr_i = idx_map.get(ts)
            if curr_i is None or curr_i == 0:
                continue
            day = ticker_data[ticker].iloc[curr_i]
            prev = ticker_data[ticker].iloc[curr_i - 1]

            if (pd.isna(day.get('ma5')) or pd.isna(day.get('ma20'))
                    or pd.isna(day.get('ma60')) or pd.isna(day.get('adx'))
                    or pd.isna(day.get('atr'))):
                continue
            if pd.isna(prev.get('ma5')) or pd.isna(prev.get('ma20')):
                continue
            if day['atr'] <= 0:
                continue

            cross = prev['ma5'] <= prev['ma20'] and day['ma5'] > day['ma20']
            trend = day['ma20'] > day['ma60']
            trending = day['adx'] >= params.adx_threshold
            liquid = day.get('avg_trading_value_20', 0) >= params.min_trading_value

            if cross and trend and trending and liquid:
                gc_by_date[date_str].append({
                    'ticker': ticker,
                    'score': float(day['adx']),
                    'close': float(day['close']),
                    'atr': float(day['atr']),
                })

    for k in gc_by_date:
        gc_by_date[k].sort(key=lambda x: x['score'], reverse=True)

    return {
        'breadth': breadth,
        'candidates': dict(gc_by_date),
        'universe_at': universe_at,
        'universe_refresh_count': len(set(range(len(trading_dates)))),  # dummy
    }


def extract(result, label):
    trades = result.trades
    cost_pct = CostModel().total_cost_pct()
    winners = [t for t in trades if t.pnl_amount > 0]
    losers = [t for t in trades if t.pnl_amount <= 0]
    avg_w = np.mean([t.pnl_pct for t in winners]) if winners else 0
    avg_l = np.mean([t.pnl_pct for t in losers]) if losers else 0
    payoff = abs(avg_w / avg_l) if avg_l != 0 else 0
    pnl_bc = [(t.pnl_pct + cost_pct) for t in trades]
    gp = sum(p for p in pnl_bc if p > 0)
    gl = abs(sum(p for p in pnl_bc if p <= 0))
    pf_bc = gp / gl if gl > 0 else float('inf')

    reasons = defaultdict(int)
    for t in trades:
        reasons[t.exit_reason] += 1

    hold_net = {
        '1-5d': [sum(t.pnl_amount for t in trades if t.hold_days <= 5),
                 sum(1 for t in trades if t.hold_days <= 5)],
        '6-10d': [sum(t.pnl_amount for t in trades if 6 <= t.hold_days <= 10),
                  sum(1 for t in trades if 6 <= t.hold_days <= 10)],
        '11-15d': [sum(t.pnl_amount for t in trades if 11 <= t.hold_days <= 15),
                   sum(1 for t in trades if 11 <= t.hold_days <= 15)],
        '16-25d': [sum(t.pnl_amount for t in trades if 16 <= t.hold_days <= 25),
                   sum(1 for t in trades if 16 <= t.hold_days <= 25)],
        '26d+': [sum(t.pnl_amount for t in trades if t.hold_days > 25),
                 sum(1 for t in trades if t.hold_days > 25)],
    }

    # 연도별 PF
    year_pf = {}
    year_groups = defaultdict(list)
    for t in trades:
        year_groups[t.entry_date[:4]].append(t)
    for yr, yt in year_groups.items():
        gp_y = sum(t.pnl_amount for t in yt if t.pnl_amount > 0)
        gl_y = abs(sum(t.pnl_amount for t in yt if t.pnl_amount <= 0))
        year_pf[yr] = gp_y / gl_y if gl_y > 0 else float('inf')

    return {
        'label': label,
        'trades': result.total_trades,
        'wr': result.win_rate,
        'pf': result.profit_factor,
        'pf_bc': pf_bc,
        'cagr': result.cagr_pct,
        'mdd': result.max_drawdown_pct,
        'net': sum(t.pnl_amount for t in trades),
        'payoff': payoff,
        'reasons': dict(reasons),
        'hold_net': hold_net,
        'year_pf': year_pf,
    }


def fmt_pf(x, w=5):
    if x == float('inf'):
        return f"{'inf':>{w}}"
    return f"{x:>{w}.2f}"


def main():
    logger.info("=" * 60)
    logger.info("실험 4: 진입 방식 3종 비교 (C/A/B)")
    logger.info("=" * 60)

    t0 = time.time()
    cache = load_backtest_data(BASE_PARAMS)
    t_load = time.time() - t0
    logger.info(f"Data loaded in {t_load:.1f}s")

    # C: 현행 precompute (돌파)
    t0 = time.time()
    pre_C = precompute_daily_signals(
        cache['trading_dates'], cache['ticker_data'], cache['ticker_date_idx'],
        cache['initial_universe'], BASE_PARAMS,
    )
    t_C = time.time() - t0
    logger.info(f"Precompute C (breakout) in {t_C:.1f}s")

    # A: 리테스트 precompute
    t0 = time.time()
    pre_A = precompute_retest(pre_C, cache['ticker_data'], cache['ticker_date_idx'])
    t_A = time.time() - t0
    logger.info(f"Precompute A (retest) in {t_A:.1f}s")

    # B: 골든크로스 precompute
    t0 = time.time()
    pre_B = precompute_golden_cross(
        cache['trading_dates'], cache['ticker_data'], cache['ticker_date_idx'],
        pre_C['universe_at'], pre_C['breadth'], BASE_PARAMS,
    )
    t_B = time.time() - t0
    logger.info(f"Precompute B (golden cross) in {t_B:.1f}s")

    # 시뮬 3회
    results = []
    for label, pre in [('C: 돌파(현행)', pre_C), ('A: 리테스트', pre_A), ('B: 골든크로스', pre_B)]:
        t0 = time.time()
        result = run_portfolio_backtest(
            initial_capital=5_000_000, max_positions=4,
            params=BASE_PARAMS, preloaded_data=cache, precomputed=pre,
        )
        elapsed = time.time() - t0
        m = extract(result, label)
        results.append(m)
        logger.info(
            f"[{label}] trades={m['trades']} WR={m['wr']:.1%} "
            f"PF={m['pf']:.2f} CAGR={m['cagr']:+.1%} MDD={m['mdd']:.1%} "
            f"| {elapsed:.1f}s"
        )

    # ───── 리포트 ─────
    print("\n" + "=" * 110)
    print("  실험 4: 진입 방식 3종 비교 (SL 2.0/Trail 4.0/Hold 20/TP1 2.0/50%)")
    print("=" * 110)

    print("\n■ 성능")
    print(f"  Load: {t_load:.1f}s | Precompute C: {t_C:.1f}s | A: {t_A:.1f}s | B: {t_B:.1f}s")
    print(f"  총 소요: {t_load + t_C + t_A + t_B:.1f}초")

    print("\n■ 핵심 지표 비교")
    print(f"  {'진입':<18} {'건수':>5} {'승률':>6} {'PF':>5} {'PF(전)':>7} {'CAGR':>7} {'MDD':>7} {'순손익':>14} {'Payoff':>7}")
    print("  " + "-" * 95)
    for m in results:
        print(f"  {m['label']:<18} {m['trades']:>5} {m['wr']:>5.1%} "
              f"{fmt_pf(m['pf'])} {fmt_pf(m['pf_bc'], 7)} "
              f"{m['cagr']:>+6.1%} {m['mdd']:>6.1%} "
              f"{m['net']:>+14,.0f} {m['payoff']:>7.2f}")

    print("\n■ 보유 기간별 순손익 (건수)")
    print(f"  {'진입':<18} {'1-5d':>16} {'6-10d':>16} {'11-15d':>16} {'16-25d':>16} {'26d+':>16}")
    print("  " + "-" * 100)
    for m in results:
        h = m['hold_net']
        print(f"  {m['label']:<18} "
              f"{h['1-5d'][0]:>+10,.0f}({h['1-5d'][1]:>3}) "
              f"{h['6-10d'][0]:>+10,.0f}({h['6-10d'][1]:>3}) "
              f"{h['11-15d'][0]:>+10,.0f}({h['11-15d'][1]:>3}) "
              f"{h['16-25d'][0]:>+10,.0f}({h['16-25d'][1]:>3}) "
              f"{h['26d+'][0]:>+10,.0f}({h['26d+'][1]:>3})")

    print("\n■ Exit Reason 분포")
    print(f"  {'진입':<18} {'SL':>5} {'TP1':>5} {'TRAIL':>6} {'TREND':>6} {'TIME':>5} {'FINAL':>6}")
    print("  " + "-" * 60)
    for m in results:
        r = m['reasons']
        print(f"  {m['label']:<18} "
              f"{r.get('STOP_LOSS', 0):>5} {r.get('TAKE_PROFIT_1', 0):>5} "
              f"{r.get('TRAILING', 0):>6} {r.get('TREND_EXIT', 0):>6} "
              f"{r.get('TIME_EXIT', 0):>5} {r.get('FINAL_CLOSE', 0):>6}")

    # 연도별
    all_years = sorted(set(y for m in results for y in m['year_pf']))
    print("\n■ 연도별 PF 안정성")
    print(f"  {'연도':<6} {'C: 돌파':>10} {'A: 리테스트':>12} {'B: 골든':>10}")
    print("  " + "-" * 45)
    for yr in all_years:
        vals = [m['year_pf'].get(yr, 0) for m in results]
        print(f"  {yr:<6} {fmt_pf(vals[0], 10)} {fmt_pf(vals[1], 12)} {fmt_pf(vals[2], 10)}")

    # Best
    best = max(results, key=lambda m: m['pf'])
    baseline = results[0]
    print("\n" + "=" * 110)
    print(f"  Best 진입: {best['label']}")
    print(f"  PF: {baseline['pf']:.2f} (C: 돌파) → {best['pf']:.2f} ({best['label']})  [{best['pf']-baseline['pf']:+.2f}]")
    if best['pf'] > baseline['pf'] and best['pf'] >= 1.2:
        v = "✅ 개선 + PF≥1.2"
    elif best['pf'] > baseline['pf']:
        v = "✅ 개선 (PF<1.2)"
    else:
        v = "❌ 현행 유지"
    print(f"  판정: {v}")
    print("=" * 110)


if __name__ == '__main__':
    main()
