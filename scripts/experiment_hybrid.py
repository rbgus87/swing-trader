"""실험 7: 하이브리드 백테스트 — 돌파(v2.1) + 눌림목 합산.

4변형 비교:
  1) v2.1 단독 4슬롯  (돌파 + ATR밴드)
  2) 눌림목 단독 4슬롯 (레퍼런스 사양)
  3) 하이브리드 2+2   (돌파 2슬롯 + 눌림목 2슬롯 고정)
  4) 하이브리드 자유4 (TF/PB 라운드로빈, 공용 4슬롯)
"""
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, replace

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

import pandas as pd
from loguru import logger

from src.backtest.portfolio_backtester import (
    load_backtest_data,
    precompute_daily_signals,
    precompute_pullback_signals,
    PortfolioTradeResult,
    BREADTH_GATE_THRESHOLD,
)
from src.backtest.swing_backtester import CostModel
from src.strategy.trend_following_v0 import StrategyParams


TOTAL_COST_PCT = 0.0031


@dataclass
class HybridPosition:
    strategy: str           # 'TF' | 'PB'
    ticker: str
    name: str
    entry_date: str
    entry_price: float
    shares: int
    atr_at_entry: float
    stop_price: float
    tp1_price: float
    highest_since_entry: float
    hold_days: int = 0
    tp1_triggered: bool = False


def filter_atr_band(precomp, atr_min=0.025, atr_max=0.08):
    new_cands = {}
    for d, cands in precomp['candidates'].items():
        new_cands[d] = [c for c in cands
                        if atr_min <= c.get('atr_ratio', 0) <= atr_max]
    return {**precomp, 'candidates': new_cands}


def run_hybrid_backtest(
    preloaded_data,
    tf_precomp, tf_params,
    pb_precomp, pb_params,
    mode='split22',   # 'tf_only' | 'pb_only' | 'split22' | 'free'
    initial_capital=5_000_000,
    max_positions=4,
    tf_max=2, pb_max=2,
    min_position_amount=300_000,
    cost=None,
):
    if cost is None:
        cost = CostModel()
    total_cost_pct = cost.total_cost_pct()

    trading_dates = preloaded_data['trading_dates']
    ticker_data = preloaded_data['ticker_data']
    ticker_date_idx = preloaded_data['ticker_date_idx']
    ticker_names = preloaded_data['ticker_names']

    cash = initial_capital
    positions = []
    trades = []
    equity_curve = []

    # breadth/universe는 tf_precomp에서 가져옴 (둘 다 동일함)
    def get_day_row(ticker, ts):
        idx_map = ticker_date_idx.get(ticker)
        if not idx_map:
            return None, None
        i = idx_map.get(ts)
        if i is None:
            return None, None
        return ticker_data[ticker].iloc[i], i

    for day_idx, date_str in enumerate(trading_dates):
        ts = pd.Timestamp(date_str)

        # ── 1. 청산 ──
        closed = []
        for pos in positions[:]:
            day, curr_i = get_day_row(pos.ticker, ts)
            if day is None:
                continue

            p = tf_params if pos.strategy == 'TF' else pb_params

            pos.hold_days += 1
            pos.highest_since_entry = max(pos.highest_since_entry, day['high'])
            trailing_stop = pos.highest_since_entry - pos.atr_at_entry * p.trailing_atr

            exit_price = None
            exit_reason = None

            # SL
            if day['low'] <= pos.stop_price:
                exit_price = pos.stop_price
                exit_reason = 'STOP_LOSS'
            # TP1
            elif (p.take_profit_atr > 0
                  and not pos.tp1_triggered
                  and day['high'] >= pos.tp1_price):
                partial_shares = int(pos.shares * p.tp1_sell_ratio)
                if partial_shares > 0:
                    pnl_pct = (pos.tp1_price / pos.entry_price - 1) - total_cost_pct
                    pnl_amount = (partial_shares * pos.entry_price
                                  * (pos.tp1_price / pos.entry_price - 1)
                                  - partial_shares * pos.entry_price * total_cost_pct)
                    trades.append((pos.strategy, PortfolioTradeResult(
                        ticker=pos.ticker,
                        name=ticker_names.get(pos.ticker, pos.ticker),
                        entry_date=pos.entry_date,
                        entry_price=pos.entry_price,
                        exit_date=date_str,
                        exit_price=pos.tp1_price,
                        exit_reason='TAKE_PROFIT_1',
                        hold_days=pos.hold_days,
                        shares=partial_shares,
                        pnl_amount=pnl_amount,
                        pnl_pct=pnl_pct,
                        is_partial=True,
                    )))
                    cash += partial_shares * pos.tp1_price
                    pos.shares -= partial_shares
                    pos.tp1_triggered = True
                continue
            # Trail
            elif day['low'] <= trailing_stop:
                exit_price = trailing_stop
                exit_reason = 'TRAILING'
            # Trend exit
            elif pos.hold_days > 1 and curr_i is not None and curr_i > 0:
                prev = ticker_data[pos.ticker].iloc[curr_i - 1]
                if (pd.notna(prev.get('ma5')) and pd.notna(prev.get('ma20'))
                        and pd.notna(day.get('ma5')) and pd.notna(day.get('ma20'))):
                    if prev['ma5'] >= prev['ma20'] and day['ma5'] < day['ma20']:
                        exit_price = day['close']
                        exit_reason = 'TREND_EXIT'
            # Time exit
            if exit_price is None and pos.hold_days >= p.max_hold_days:
                exit_price = day['close']
                exit_reason = 'TIME_EXIT'

            if exit_price is not None:
                pnl_pct = (exit_price / pos.entry_price - 1) - total_cost_pct
                pnl_amount = (pos.shares * pos.entry_price * (exit_price / pos.entry_price - 1)
                              - pos.shares * pos.entry_price * total_cost_pct)
                trades.append((pos.strategy, PortfolioTradeResult(
                    ticker=pos.ticker,
                    name=ticker_names.get(pos.ticker, pos.ticker),
                    entry_date=pos.entry_date,
                    entry_price=pos.entry_price,
                    exit_date=date_str,
                    exit_price=exit_price,
                    exit_reason=exit_reason,
                    hold_days=pos.hold_days,
                    shares=pos.shares,
                    pnl_amount=pnl_amount,
                    pnl_pct=pnl_pct,
                    is_partial=pos.tp1_triggered,
                )))
                cash += pos.shares * exit_price
                closed.append(pos)

        for pos in closed:
            positions.remove(pos)

        # ── 2. Breadth 게이트 ──
        breadth = tf_precomp['breadth'].get(date_str, 0.5)
        gate_open = breadth >= BREADTH_GATE_THRESHOLD

        if not gate_open:
            portfolio_value = cash
            for pos in positions:
                day, _ = get_day_row(pos.ticker, ts)
                if day is not None:
                    portfolio_value += pos.shares * day['close']
                else:
                    portfolio_value += pos.shares * pos.entry_price
            equity_curve.append((date_str, portfolio_value))
            continue

        # ── 3. 진입 슬롯 계산 ──
        held_set = {p.ticker for p in positions}
        tf_count = sum(1 for p in positions if p.strategy == 'TF')
        pb_count = sum(1 for p in positions if p.strategy == 'PB')
        total_free = max_positions - len(positions)
        if total_free <= 0:
            portfolio_value = cash
            for pos in positions:
                day, _ = get_day_row(pos.ticker, ts)
                if day is not None:
                    portfolio_value += pos.shares * day['close']
                else:
                    portfolio_value += pos.shares * pos.entry_price
            equity_curve.append((date_str, portfolio_value))
            continue

        tf_avail = [c for c in tf_precomp['candidates'].get(date_str, []) if c['ticker'] not in held_set]
        pb_avail = [c for c in pb_precomp['candidates'].get(date_str, []) if c['ticker'] not in held_set]

        picks = []  # list of (strategy, cand)
        picked_tickers = set()

        if mode == 'tf_only':
            for c in tf_avail[:total_free]:
                picks.append(('TF', c))
                picked_tickers.add(c['ticker'])
        elif mode == 'pb_only':
            for c in pb_avail[:total_free]:
                picks.append(('PB', c))
                picked_tickers.add(c['ticker'])
        elif mode == 'split22':
            tf_slots = max(0, tf_max - tf_count)
            pb_slots = max(0, pb_max - pb_count)
            # 돌파 먼저 채움
            for c in tf_avail:
                if len(picks) >= total_free or tf_slots <= 0:
                    break
                if c['ticker'] in picked_tickers:
                    continue
                picks.append(('TF', c))
                picked_tickers.add(c['ticker'])
                tf_slots -= 1
            for c in pb_avail:
                if len(picks) >= total_free or pb_slots <= 0:
                    break
                if c['ticker'] in picked_tickers:
                    continue
                picks.append(('PB', c))
                picked_tickers.add(c['ticker'])
                pb_slots -= 1
        elif mode == 'free':
            # 라운드 로빈 TF→PB
            ti = pi = 0
            turn = 'TF'
            while len(picks) < total_free:
                advanced = False
                if turn == 'TF':
                    while ti < len(tf_avail):
                        c = tf_avail[ti]
                        ti += 1
                        if c['ticker'] not in picked_tickers:
                            picks.append(('TF', c))
                            picked_tickers.add(c['ticker'])
                            advanced = True
                            break
                else:
                    while pi < len(pb_avail):
                        c = pb_avail[pi]
                        pi += 1
                        if c['ticker'] not in picked_tickers:
                            picks.append(('PB', c))
                            picked_tickers.add(c['ticker'])
                            advanced = True
                            break
                turn = 'PB' if turn == 'TF' else 'TF'
                if not advanced:
                    if ti >= len(tf_avail) and pi >= len(pb_avail):
                        break

        # ── 4. 진입 처리 ──
        for strategy, cand in picks:
            next_day_idx = day_idx + 1
            if next_day_idx >= len(trading_dates):
                break
            next_date = trading_dates[next_day_idx]
            next_ts = pd.Timestamp(next_date)
            idx_map = ticker_date_idx.get(cand['ticker'], {})
            ni = idx_map.get(next_ts)
            if ni is None:
                continue
            nr = ticker_data[cand['ticker']].iloc[ni]
            entry_price = nr['open']
            if entry_price <= 0:
                continue

            if len(positions) >= max_positions:
                break

            max_alloc_pct = 1.0 / max_positions
            alloc = min(cash * max_alloc_pct, cash)
            if alloc < min_position_amount:
                continue
            shares = int(alloc / entry_price)
            if shares <= 0:
                continue
            actual_cost = shares * entry_price
            cash -= actual_cost

            p = tf_params if strategy == 'TF' else pb_params
            positions.append(HybridPosition(
                strategy=strategy,
                ticker=cand['ticker'],
                name=ticker_names.get(cand['ticker'], cand['ticker']),
                entry_date=next_date,
                entry_price=entry_price,
                shares=shares,
                atr_at_entry=cand['atr'],
                stop_price=entry_price - cand['atr'] * p.stop_loss_atr,
                tp1_price=entry_price + cand['atr'] * p.take_profit_atr,
                highest_since_entry=entry_price,
            ))

        # ── 5. equity 기록 ──
        portfolio_value = cash
        for pos in positions:
            day, _ = get_day_row(pos.ticker, ts)
            if day is not None:
                portfolio_value += pos.shares * day['close']
            else:
                portfolio_value += pos.shares * pos.entry_price
        equity_curve.append((date_str, portfolio_value))

    # 최종 강제 청산
    last_date = trading_dates[-1]
    last_ts = pd.Timestamp(last_date)
    for pos in positions:
        idx_map = ticker_date_idx.get(pos.ticker, {})
        li = idx_map.get(last_ts)
        if li is not None:
            exit_price = ticker_data[pos.ticker].iloc[li]['close']
            pnl_pct = (exit_price / pos.entry_price - 1) - total_cost_pct
            pnl_amount = (pos.shares * pos.entry_price * (exit_price / pos.entry_price - 1)
                          - pos.shares * pos.entry_price * total_cost_pct)
            trades.append((pos.strategy, PortfolioTradeResult(
                ticker=pos.ticker,
                name=ticker_names.get(pos.ticker, pos.ticker),
                entry_date=pos.entry_date,
                entry_price=pos.entry_price,
                exit_date=last_date,
                exit_price=exit_price,
                exit_reason='FINAL_CLOSE',
                hold_days=pos.hold_days,
                shares=pos.shares,
                pnl_amount=pnl_amount,
                pnl_pct=pnl_pct,
            )))

    return equity_curve, trades


def summary(equity_curve, trades, initial_capital):
    trades_only = [t for _, t in trades]
    final = equity_curve[-1][1] if equity_curve else initial_capital
    total_ret = (final / initial_capital) - 1
    years = len(equity_curve) / 245
    cagr = (final / initial_capital) ** (1 / years) - 1 if years > 0 and final > 0 else 0
    peak = initial_capital
    mdd = 0
    for _, eq in equity_curve:
        peak = max(peak, eq)
        dd = (peak - eq) / peak if peak > 0 else 0
        mdd = max(mdd, dd)
    winners = [t for t in trades_only if t.pnl_amount > 0]
    losers = [t for t in trades_only if t.pnl_amount <= 0]
    gp = sum(t.pnl_amount for t in winners) if winners else 0
    gl = abs(sum(t.pnl_amount for t in losers)) if losers else 0
    pf = gp / gl if gl > 0 else float('inf')
    return {
        'final': final,
        'net': final - initial_capital,
        'total_return': total_ret,
        'cagr': cagr,
        'mdd': mdd,
        'pf': pf,
        'wr': len(winners) / len(trades_only) if trades_only else 0,
        'total': len(trades_only),
        'trades': trades_only,
        'strat_trades': trades,
    }


def pf_gross(trades):
    gp = gl = 0.0
    for t in trades:
        g = t.pnl_amount + t.shares * t.entry_price * TOTAL_COST_PCT
        if g > 0:
            gp += g
        else:
            gl += abs(g)
    return gp / gl if gl > 0 else float('inf')


def compute_payoff(trades):
    winners = [t.pnl_pct for t in trades if t.pnl_amount > 0]
    losers = [t.pnl_pct for t in trades if t.pnl_amount <= 0]
    if not winners or not losers:
        return float('nan')
    return (sum(winners) / len(winners)) / abs(sum(losers) / len(losers))


def hold_buckets(trades):
    b = {'1-5d': [0, 0], '6-10d': [0, 0], '11-15d': [0, 0], '16-25d': [0, 0], '26d+': [0, 0]}
    for t in trades:
        hd = t.hold_days
        if hd <= 5: k = '1-5d'
        elif hd <= 10: k = '6-10d'
        elif hd <= 15: k = '11-15d'
        elif hd <= 25: k = '16-25d'
        else: k = '26d+'
        b[k][0] += t.pnl_amount
        b[k][1] += 1
    return b


def yearly_pf(trades):
    by = defaultdict(lambda: {'gp': 0.0, 'gl': 0.0})
    for t in trades:
        yr = t.exit_date[:4]
        if t.pnl_amount > 0:
            by[yr]['gp'] += t.pnl_amount
        else:
            by[yr]['gl'] += abs(t.pnl_amount)
    return {y: (by[y]['gp'] / by[y]['gl'] if by[y]['gl'] > 0 else float('inf'))
            for y in sorted(by)}


def format_pf(pf):
    return f"{pf:.2f}" if pf != float('inf') else 'inf'


def run_all():
    t0 = time.time()

    params_base = StrategyParams()
    logger.info("데이터 로드")
    preloaded = load_backtest_data(params_base)

    logger.info("돌파 precompute + ATR밴드 필터")
    precomp_tf_raw = precompute_daily_signals(
        preloaded['trading_dates'], preloaded['ticker_data'],
        preloaded['ticker_date_idx'], preloaded['initial_universe'], params_base,
    )
    precomp_tf = filter_atr_band(precomp_tf_raw, 0.025, 0.08)

    logger.info("눌림목 precompute")
    precomp_pb = precompute_pullback_signals(
        preloaded['trading_dates'], preloaded['ticker_data'],
        preloaded['ticker_date_idx'], preloaded['initial_universe'], params_base,
    )
    t_prep = time.time() - t0
    logger.info(f"준비 완료 ({t_prep:.1f}s)")

    tf_params = StrategyParams()  # v2.1
    pb_params = replace(
        params_base,
        stop_loss_atr=1.5, take_profit_atr=2.0, trailing_atr=3.0,
        max_hold_days=10, tp1_sell_ratio=0.3,
    )

    variants = [
        ('v2.1 단독 4슬롯',    'tf_only', 4, 0),
        ('눌림목 단독 4슬롯',   'pb_only', 0, 4),
        ('하이브리드 2+2',      'split22', 2, 2),
        ('하이브리드 자유4',     'free',    4, 4),
    ]

    results = []
    for name, mode, tf_max, pb_max in variants:
        logger.info(f"\n--- {name} ---")
        t1 = time.time()
        eq, tr = run_hybrid_backtest(
            preloaded, precomp_tf, tf_params, precomp_pb, pb_params,
            mode=mode, tf_max=tf_max, pb_max=pb_max,
        )
        s = summary(eq, tr, 5_000_000)
        dt = time.time() - t1
        logger.info(
            f"[{name}] trades={s['total']}, WR={s['wr']:.1%}, "
            f"PF={format_pf(s['pf'])}, CAGR={s['cagr']:.1%}, "
            f"MDD={s['mdd']:.1%}, net={s['net']:+,.0f} ({dt:.1f}s)"
        )
        results.append((name, mode, s))

    total_time = time.time() - t0

    print("\n" + "=" * 100)
    print("실험 7: 하이브리드 백테스트 결과")
    print("=" * 100)
    print(f"총 소요: {total_time:.1f}초 (prep {t_prep:.1f}s + sim {total_time-t_prep:.1f}s)")

    print("\n## 핵심 지표 비교")
    print(f"{'변형':<22} {'건수':>5} {'승률':>6} {'PF':>6} {'PF(전)':>7} {'CAGR':>7} {'MDD':>7} {'순손익':>13} {'Payoff':>7}")
    print("-" * 100)
    for name, mode, s in results:
        pfg = pf_gross(s['trades'])
        payoff = compute_payoff(s['trades'])
        pay_s = f"{payoff:.2f}" if payoff == payoff else 'n/a'
        print(
            f"{name:<22} {s['total']:>5} {s['wr']:>5.1%} "
            f"{format_pf(s['pf']):>6} {format_pf(pfg):>7} "
            f"{s['cagr']:>6.1%} {s['mdd']:>6.1%} "
            f"{s['net']:>+12,.0f} {pay_s:>7}"
        )

    # 전략별 내역 (split22, free)
    for name, mode, s in results:
        if mode not in ('split22', 'free'):
            continue
        tf_trades = [t for strat, t in s['strat_trades'] if strat == 'TF']
        pb_trades = [t for strat, t in s['strat_trades'] if strat == 'PB']
        print(f"\n## 전략별 내역 — {name}")
        for lbl, ts in [('TF (돌파)', tf_trades), ('PB (눌림목)', pb_trades)]:
            if not ts:
                print(f"  {lbl:<14} (0건)")
                continue
            wins = [t for t in ts if t.pnl_amount > 0]
            gp = sum(t.pnl_amount for t in wins)
            gl = abs(sum(t.pnl_amount for t in ts if t.pnl_amount <= 0))
            pf = gp / gl if gl > 0 else float('inf')
            net = sum(t.pnl_amount for t in ts)
            wr = len(wins) / len(ts)
            print(f"  {lbl:<14} {len(ts):>4}건  WR {wr:>5.1%}  PF {format_pf(pf):>6}  순손익 {net:>+12,.0f}")

    # 연도별 PF
    all_years = set()
    yrs = []
    for name, _, s in results:
        ym = yearly_pf(s['trades'])
        yrs.append((name, ym))
        all_years |= set(ym)
    print("\n## 연도별 PF")
    print(f"{'연도':<6} " + "".join(f"{n:>22}" for n, _ in yrs))
    print("-" * (6 + 22 * len(yrs)))
    for yr in sorted(all_years):
        cells = []
        for _, ym in yrs:
            v = ym.get(yr, float('nan'))
            cells.append(format_pf(v) if v == v else '-')
        print(f"{yr:<6} " + "".join(f"{c:>22}" for c in cells))

    # Best 선정: PF 최고 (동률시 net)
    best = max(results, key=lambda x: (x[2]['pf'], x[2]['net']))
    best_name, best_mode, best_s = best
    baseline = results[0]  # v2.1 단독 4슬롯
    base_s = baseline[2]

    print(f"\n## 보유 기간별 (Best: {best_name} vs v2.1 단독)")
    bb = hold_buckets(base_s['trades'])
    xb = hold_buckets(best_s['trades'])
    print(f"{'구간':<10} {'v2.1 PnL(건)':>22} {'Best PnL(건)':>22} {'변화':>15}")
    print("-" * 75)
    for key in ['1-5d', '6-10d', '11-15d', '16-25d', '26d+']:
        bp, bn = bb[key]
        xp, xn = xb[key]
        delta = xp - bp
        print(f"{key:<10} {bp:>+14,.0f} ({bn:>3}) {xp:>+14,.0f} ({xn:>3}) {delta:>+14,.0f}")

    print(f"\n## 판정")
    print(f"  Best: {best_name}")
    print(f"  PF: {format_pf(best_s['pf'])}")
    print(f"  v2.1 단독 대비 PF: {format_pf(base_s['pf'])} → {format_pf(best_s['pf'])} ({best_s['pf']-base_s['pf']:+.2f})")
    print(f"  v2.1 단독 대비 CAGR: {base_s['cagr']:.1%} → {best_s['cagr']:.1%}")
    print(f"  v2.1 단독 대비 MDD: {base_s['mdd']:.1%} → {best_s['mdd']:.1%}")
    print(f"  v2.1 단독 대비 순손익: {base_s['net']:+,} → {best_s['net']:+,}")

    if best_s['pf'] > base_s['pf'] + 0.03:
        v = "✅ 우위"
    elif abs(best_s['pf'] - base_s['pf']) < 0.03:
        v = "⚠ 비슷"
    else:
        v = "❌ 열세"
    print(f"  v2.1 단독 대비: {v}")

    if best_name == 'v2.1 단독 4슬롯':
        rec = "단독 (v2.1)"
    elif '2+2' in best_name:
        rec = "하이브리드 (2+2)"
    elif '자유' in best_name:
        rec = "하이브리드 (자유)"
    else:
        rec = best_name
    print(f"  최종 권고: {rec}")


if __name__ == "__main__":
    run_all()
