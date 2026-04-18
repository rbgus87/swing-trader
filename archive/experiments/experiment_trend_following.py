"""실험 9: 상태 기반 추세추종 — 돌파와 다른 진입 철학.

5개 변형:
  1) v2.1 돌파 baseline (비교 기준)
  2) 추세A: 추세추종 + 레퍼런스 MA 청산
  3) 추세B: 추세추종 + v2.1 ATR 청산
  4) 추세A 완화: 추세추종(상대강도 제외) + MA 청산
  5) 추세B 완화: 추세추종(상대강도 제외) + ATR 청산
"""
import sys
import time
from collections import defaultdict
from dataclasses import dataclass

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

import pandas as pd
from loguru import logger

from src.data_pipeline.db import get_connection
from src.backtest.portfolio_backtester import (
    BREADTH_GATE_THRESHOLD,
    UNIVERSE_REFRESH_DAYS,
    PortfolioTradeResult,
    build_universe,
    load_backtest_data,
    precompute_daily_signals,
    run_portfolio_backtest,
)
from src.backtest.swing_backtester import CostModel
from src.strategy.trend_following_v0 import StrategyParams


TOTAL_COST_PCT = 0.0031
ATR_BAND_MIN = 0.025
ATR_BAND_MAX = 0.08
MA60_DIST_MIN = 0.05
MA60_DIST_MAX = 0.20
REL_STRENGTH_MIN = 0.05


def load_kospi_ret20():
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT date, close FROM index_daily
            WHERE index_code = 'KOSPI' ORDER BY date
        """).fetchall()
    if not rows:
        return {}
    df = pd.DataFrame([dict(r) for r in rows])
    df['date'] = pd.to_datetime(df['date'])
    df['ret20'] = df['close'].pct_change(20)
    return dict(zip(df['date'], df['ret20']))


def augment_ticker_data(ticker_data):
    """ticker_data 각 DataFrame에 ma120/ma60_slope/ma60_dist/macd_hist/vol5/stock_ret20 추가."""
    for tk, df in ticker_data.items():
        df['ma120'] = df['close'].rolling(120).mean()
        df['ma60_slope'] = df['ma60'] - df['ma60'].shift(5)
        df['ma60_dist'] = df['close'] / df['ma60'] - 1.0
        ema12 = df['close'].ewm(span=12, adjust=False).mean()
        ema26 = df['close'].ewm(span=26, adjust=False).mean()
        macd = ema12 - ema26
        sig = macd.ewm(span=9, adjust=False).mean()
        df['macd_hist'] = macd - sig
        df['avg_volume_5'] = df['volume'].rolling(5).mean()
        df['stock_ret20'] = df['close'].pct_change(20)


def precompute_trend_signals(
    trading_dates, ticker_data, ticker_date_idx, initial_universe,
    params: StrategyParams, kospi_ret20_map: dict,
    use_relative_strength: bool = True,
    atr_band_min: float = ATR_BAND_MIN,
    atr_band_max: float = ATR_BAND_MAX,
):
    """상태 기반 추세추종 후보 사전 계산.

    조건: 완전 정배열(MA20>MA60>MA120 + close > MA20), MA60 기울기+,
          5% ≤ (close/MA60 - 1) ≤ 20%, MACD hist > 0,
          [옵션] 종목 20일 수익률 - KOSPI 20일 수익률 ≥ 5%,
          vol_5 > vol_20, ADX ≥ 20, 거래대금 50억+, ATR/close 2.5~8%
    """
    breadth_by_date = {}
    candidates_by_date = {}
    universe_by_date = {}

    universe = set(initial_universe)
    last_refresh = 0
    refresh_count = 1

    for day_idx, date_str in enumerate(trading_dates):
        ts = pd.Timestamp(date_str)
        if day_idx - last_refresh >= UNIVERSE_REFRESH_DAYS:
            with get_connection() as conn:
                universe = build_universe(date_str, conn)
            last_refresh = day_idx
            refresh_count += 1
        universe_by_date[date_str] = set(universe)

        # breadth
        above = 0
        total_b = 0
        for tk in universe:
            im = ticker_date_idx.get(tk, {})
            bi = im.get(ts)
            if bi is None or bi < 199:
                continue
            br = ticker_data[tk].iloc[bi]
            ma200_v = br.get('ma200')
            if pd.isna(ma200_v):
                continue
            total_b += 1
            if br['close'] > ma200_v:
                above += 1
        breadth_by_date[date_str] = above / total_b if total_b > 0 else 0.5

        kospi_ret = kospi_ret20_map.get(ts)

        cands = []
        for ticker in universe:
            im = ticker_date_idx.get(ticker)
            if not im:
                continue
            ci = im.get(ts)
            if ci is None or ci < 120:
                continue
            day = ticker_data[ticker].iloc[ci]

            # 필수값 NaN 체크
            req = ['ma20', 'ma60', 'ma120', 'ma60_slope', 'ma60_dist',
                   'macd_hist', 'adx', 'atr', 'avg_volume_5',
                   'avg_volume_20', 'avg_trading_value_20', 'stock_ret20']
            if any(pd.isna(day.get(k)) for k in req):
                continue
            if day['atr'] <= 0 or day['close'] <= 0:
                continue

            # 정배열 + MA120
            if not (day['close'] > day['ma20'] > day['ma60'] > day['ma120']):
                continue
            # MA60 기울기 (+)
            if day['ma60_slope'] <= 0:
                continue
            # MA60 대비 위치
            if not (MA60_DIST_MIN <= day['ma60_dist'] <= MA60_DIST_MAX):
                continue
            # MACD hist > 0
            if day['macd_hist'] <= 0:
                continue
            # 상대강도
            if use_relative_strength:
                if kospi_ret is None or pd.isna(kospi_ret):
                    continue
                if (day['stock_ret20'] - float(kospi_ret)) < REL_STRENGTH_MIN:
                    continue
            # 거래량 증가
            if day['avg_volume_5'] <= day['avg_volume_20']:
                continue
            # ADX
            if day['adx'] < params.adx_threshold:
                continue
            # 거래대금
            if day['avg_trading_value_20'] < params.min_trading_value:
                continue
            # ATR 밴드
            atr_ratio = day['atr'] / day['close']
            if not (atr_band_min <= atr_ratio <= atr_band_max):
                continue

            cands.append({
                'ticker': ticker,
                'score': float(day['adx']),
                'close': float(day['close']),
                'atr': float(day['atr']),
                'atr_ratio': float(atr_ratio),
                'ma60_dist': float(day['ma60_dist']),
            })
        cands.sort(key=lambda x: x['score'], reverse=True)
        candidates_by_date[date_str] = cands

    return {
        'breadth': breadth_by_date,
        'candidates': candidates_by_date,
        'universe_at': universe_by_date,
        'universe_refresh_count': refresh_count,
    }


@dataclass
class MAPos:
    ticker: str
    name: str
    entry_date: str
    entry_price: float
    shares: int
    atr_at_entry: float
    hold_days: int = 0
    ma20_partial_done: bool = False


def run_ma_exit_backtest(
    preloaded, precomputed, params,
    initial_capital=5_000_000, max_positions=4, min_position_amount=300_000, cost=None,
):
    """변형 A: MA 기반 청산 시뮬.

    강제: entry × 0.93 → 전량
    1차: close < MA20 → 50%
    2차: close ≤ MA60 × 1.02 → 전량
    시간: hold ≥ 10 & return < 3% → 전량
    최대: hold ≥ 30 → 전량
    """
    if cost is None:
        cost = CostModel()
    total_cost_pct = cost.total_cost_pct()

    trading_dates = preloaded['trading_dates']
    ticker_data = preloaded['ticker_data']
    ticker_date_idx = preloaded['ticker_date_idx']
    ticker_names = preloaded['ticker_names']

    cash = initial_capital
    positions = []
    trades = []
    equity_curve = []

    def gdr(tk, ts):
        im = ticker_date_idx.get(tk)
        if not im:
            return None, None
        i = im.get(ts)
        if i is None:
            return None, None
        return ticker_data[tk].iloc[i], i

    for day_idx, date_str in enumerate(trading_dates):
        ts = pd.Timestamp(date_str)

        # ── 청산 ──
        closed = []
        for pos in positions[:]:
            day, curr_i = gdr(pos.ticker, ts)
            if day is None:
                continue
            pos.hold_days += 1

            current_return = (day['close'] / pos.entry_price) - 1
            exit_price = None
            exit_reason = None

            # 1. 강제 -7%
            if day['low'] <= pos.entry_price * 0.93:
                exit_price = min(pos.entry_price * 0.93, day['high'])
                exit_reason = 'STOP_LOSS_7PCT'
            # 2. 1차: MA20 이탈 (미발동 시 50% 매도)
            elif (not pos.ma20_partial_done
                  and pd.notna(day.get('ma20'))
                  and day['close'] < day['ma20']):
                partial = pos.shares // 2
                if partial > 0:
                    pnl_pct = (day['close'] / pos.entry_price - 1) - total_cost_pct
                    pnl_amount = (partial * pos.entry_price * (day['close'] / pos.entry_price - 1)
                                  - partial * pos.entry_price * total_cost_pct)
                    trades.append(PortfolioTradeResult(
                        ticker=pos.ticker,
                        name=ticker_names.get(pos.ticker, pos.ticker),
                        entry_date=pos.entry_date,
                        entry_price=pos.entry_price,
                        exit_date=date_str,
                        exit_price=day['close'],
                        exit_reason='MA20_PARTIAL',
                        hold_days=pos.hold_days,
                        shares=partial,
                        pnl_amount=pnl_amount,
                        pnl_pct=pnl_pct,
                        is_partial=True,
                    ))
                    cash += partial * day['close']
                    pos.shares -= partial
                    pos.ma20_partial_done = True
                continue
            # 3. 2차: MA60 × 1.02 이탈
            elif (pd.notna(day.get('ma60'))
                  and day['close'] <= day['ma60'] * 1.02):
                exit_price = day['close']
                exit_reason = 'MA60_EXIT'
            # 4. 시간손절: 10일 +3% 미달
            elif pos.hold_days >= 10 and current_return < 0.03:
                exit_price = day['close']
                exit_reason = 'EARLY_TIME_EXIT'
            # 5. 최대 30일
            elif pos.hold_days >= 30:
                exit_price = day['close']
                exit_reason = 'TIME_EXIT'

            if exit_price is not None:
                pnl_pct = (exit_price / pos.entry_price - 1) - total_cost_pct
                pnl_amount = (pos.shares * pos.entry_price * (exit_price / pos.entry_price - 1)
                              - pos.shares * pos.entry_price * total_cost_pct)
                trades.append(PortfolioTradeResult(
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
                    is_partial=pos.ma20_partial_done,
                ))
                cash += pos.shares * exit_price
                closed.append(pos)
        for pos in closed:
            positions.remove(pos)

        # ── Breadth gate ──
        breadth = precomputed['breadth'].get(date_str, 0.5)
        gate_open = breadth >= BREADTH_GATE_THRESHOLD
        open_slots = max_positions - len(positions) if gate_open else 0

        if open_slots > 0:
            held = {p.ticker for p in positions}
            cands = [c for c in precomputed['candidates'].get(date_str, [])
                     if c['ticker'] not in held]
            for cand in cands[:open_slots]:
                ni = day_idx + 1
                if ni >= len(trading_dates):
                    break
                nd = trading_dates[ni]
                nts = pd.Timestamp(nd)
                im = ticker_date_idx.get(cand['ticker'], {})
                nii = im.get(nts)
                if nii is None:
                    continue
                nr = ticker_data[cand['ticker']].iloc[nii]
                ep = nr['open']
                if ep <= 0:
                    continue
                if len(positions) >= max_positions:
                    break
                alloc = min(cash * (1.0 / max_positions), cash)
                if alloc < min_position_amount:
                    continue
                sh = int(alloc / ep)
                if sh <= 0:
                    continue
                cash -= sh * ep
                positions.append(MAPos(
                    ticker=cand['ticker'],
                    name=ticker_names.get(cand['ticker'], cand['ticker']),
                    entry_date=nd,
                    entry_price=ep,
                    shares=sh,
                    atr_at_entry=cand['atr'],
                ))

        # ── equity ──
        pv = cash
        for pos in positions:
            day, _ = gdr(pos.ticker, ts)
            if day is not None:
                pv += pos.shares * day['close']
            else:
                pv += pos.shares * pos.entry_price
        equity_curve.append((date_str, pv))

    # 최종 청산
    last_date = trading_dates[-1]
    last_ts = pd.Timestamp(last_date)
    for pos in positions:
        im = ticker_date_idx.get(pos.ticker, {})
        li = im.get(last_ts)
        if li is not None:
            ep = ticker_data[pos.ticker].iloc[li]['close']
            pnl_pct = (ep / pos.entry_price - 1) - total_cost_pct
            pnl_amount = (pos.shares * pos.entry_price * (ep / pos.entry_price - 1)
                          - pos.shares * pos.entry_price * total_cost_pct)
            trades.append(PortfolioTradeResult(
                ticker=pos.ticker,
                name=ticker_names.get(pos.ticker, pos.ticker),
                entry_date=pos.entry_date,
                entry_price=pos.entry_price,
                exit_date=last_date,
                exit_price=ep,
                exit_reason='FINAL_CLOSE',
                hold_days=pos.hold_days,
                shares=pos.shares,
                pnl_amount=pnl_amount,
                pnl_pct=pnl_pct,
            ))
    return equity_curve, trades


def _summary(equity_curve, trades, initial_capital):
    final = equity_curve[-1][1] if equity_curve else initial_capital
    years = len(equity_curve) / 245
    cagr = (final / initial_capital) ** (1 / years) - 1 if years > 0 and final > 0 else 0
    peak = initial_capital
    mdd = 0
    for _, eq in equity_curve:
        peak = max(peak, eq)
        dd = (peak - eq) / peak if peak > 0 else 0
        mdd = max(mdd, dd)
    wins = [t for t in trades if t.pnl_amount > 0]
    los = [t for t in trades if t.pnl_amount <= 0]
    gp = sum(t.pnl_amount for t in wins) if wins else 0
    gl = abs(sum(t.pnl_amount for t in los)) if los else 0
    pf = gp / gl if gl > 0 else float('inf')
    return {
        'final': final, 'net': final - initial_capital,
        'cagr': cagr, 'mdd': mdd, 'pf': pf,
        'wr': len(wins) / len(trades) if trades else 0,
        'total': len(trades), 'trades': trades,
    }


def pf_gross(trades):
    gp = gl = 0.0
    for t in trades:
        g = t.pnl_amount + t.shares * t.entry_price * TOTAL_COST_PCT
        if g > 0: gp += g
        else: gl += abs(g)
    return gp / gl if gl > 0 else float('inf')


def payoff(trades):
    w = [t.pnl_pct for t in trades if t.pnl_amount > 0]
    l = [t.pnl_pct for t in trades if t.pnl_amount <= 0]
    if not w or not l: return float('nan')
    return (sum(w) / len(w)) / abs(sum(l) / len(l))


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


def exit_dist(trades):
    d = defaultdict(int)
    for t in trades:
        d[t.exit_reason] += 1
    return d


def yearly_pf(trades):
    by = defaultdict(lambda: {'gp': 0.0, 'gl': 0.0})
    for t in trades:
        yr = t.exit_date[:4]
        if t.pnl_amount > 0: by[yr]['gp'] += t.pnl_amount
        else: by[yr]['gl'] += abs(t.pnl_amount)
    return {y: (by[y]['gp'] / by[y]['gl'] if by[y]['gl'] > 0 else float('inf'))
            for y in sorted(by)}


def format_pf(pf):
    return f"{pf:.2f}" if pf != float('inf') else 'inf'


def filter_atr_band(precomp, lo=ATR_BAND_MIN, hi=ATR_BAND_MAX):
    new = {}
    for d, cs in precomp['candidates'].items():
        new[d] = [c for c in cs if lo <= c.get('atr_ratio', 0) <= hi]
    return {**precomp, 'candidates': new}


def run_all():
    t0 = time.time()
    params = StrategyParams()

    logger.info("데이터 로드 + 지표 확장")
    preloaded = load_backtest_data(params)
    augment_ticker_data(preloaded['ticker_data'])
    kospi_ret20 = load_kospi_ret20()
    logger.info(f"KOSPI ret20 entries: {len(kospi_ret20)}")

    logger.info("돌파 precompute + ATR 필터")
    precomp_tf_raw = precompute_daily_signals(
        preloaded['trading_dates'], preloaded['ticker_data'],
        preloaded['ticker_date_idx'], preloaded['initial_universe'], params,
    )
    precomp_v21 = filter_atr_band(precomp_tf_raw)

    logger.info("추세 precompute (with 상대강도)")
    precomp_trend = precompute_trend_signals(
        preloaded['trading_dates'], preloaded['ticker_data'],
        preloaded['ticker_date_idx'], preloaded['initial_universe'],
        params, kospi_ret20, use_relative_strength=True,
    )
    logger.info("추세 precompute (상대강도 제외)")
    precomp_trend_relaxed = precompute_trend_signals(
        preloaded['trading_dates'], preloaded['ticker_data'],
        preloaded['ticker_date_idx'], preloaded['initial_universe'],
        params, kospi_ret20, use_relative_strength=False,
    )

    t_prep = time.time() - t0
    logger.info(f"준비 완료 ({t_prep:.1f}s)")

    # 5개 변형 실행
    results = []

    # 1) v2.1 baseline
    logger.info("\n--- v2.1 돌파 baseline ---")
    t1 = time.time()
    r = run_portfolio_backtest(
        initial_capital=5_000_000, max_positions=4, params=params,
        preloaded_data=preloaded, precomputed=precomp_v21, risk=None,
    )
    s = {'final': r.final_capital, 'net': r.final_capital - r.initial_capital,
         'cagr': r.cagr_pct, 'mdd': r.max_drawdown_pct, 'pf': r.profit_factor,
         'wr': r.win_rate, 'total': r.total_trades, 'trades': r.trades}
    logger.info(f"PF={format_pf(s['pf'])}, MDD={s['mdd']:.1%}, net={s['net']:+,.0f} ({time.time()-t1:.1f}s)")
    results.append(('v2.1 돌파', s))

    # 2) 추세A: 추세 + MA청산
    logger.info("\n--- 추세A (MA청산) ---")
    t1 = time.time()
    eq, tr = run_ma_exit_backtest(preloaded, precomp_trend, params)
    s = _summary(eq, tr, 5_000_000)
    logger.info(f"PF={format_pf(s['pf'])}, MDD={s['mdd']:.1%}, net={s['net']:+,.0f}, trades={s['total']} ({time.time()-t1:.1f}s)")
    results.append(('추세A MA청산', s))

    # 3) 추세B: 추세 + ATR청산
    logger.info("\n--- 추세B (ATR청산) ---")
    t1 = time.time()
    r = run_portfolio_backtest(
        initial_capital=5_000_000, max_positions=4, params=params,
        preloaded_data=preloaded, precomputed=precomp_trend, risk=None,
    )
    s = {'final': r.final_capital, 'net': r.final_capital - r.initial_capital,
         'cagr': r.cagr_pct, 'mdd': r.max_drawdown_pct, 'pf': r.profit_factor,
         'wr': r.win_rate, 'total': r.total_trades, 'trades': r.trades}
    logger.info(f"PF={format_pf(s['pf'])}, MDD={s['mdd']:.1%}, net={s['net']:+,.0f} ({time.time()-t1:.1f}s)")
    results.append(('추세B ATR청산', s))

    # 4) 추세A 완화
    logger.info("\n--- 추세A 완화 (상대강도 제외) ---")
    t1 = time.time()
    eq, tr = run_ma_exit_backtest(preloaded, precomp_trend_relaxed, params)
    s = _summary(eq, tr, 5_000_000)
    logger.info(f"PF={format_pf(s['pf'])}, MDD={s['mdd']:.1%}, net={s['net']:+,.0f}, trades={s['total']} ({time.time()-t1:.1f}s)")
    results.append(('추세A 완화', s))

    # 5) 추세B 완화
    logger.info("\n--- 추세B 완화 ---")
    t1 = time.time()
    r = run_portfolio_backtest(
        initial_capital=5_000_000, max_positions=4, params=params,
        preloaded_data=preloaded, precomputed=precomp_trend_relaxed, risk=None,
    )
    s = {'final': r.final_capital, 'net': r.final_capital - r.initial_capital,
         'cagr': r.cagr_pct, 'mdd': r.max_drawdown_pct, 'pf': r.profit_factor,
         'wr': r.win_rate, 'total': r.total_trades, 'trades': r.trades}
    logger.info(f"PF={format_pf(s['pf'])}, MDD={s['mdd']:.1%}, net={s['net']:+,.0f} ({time.time()-t1:.1f}s)")
    results.append(('추세B 완화', s))

    total_time = time.time() - t0

    # ── 출력 ──
    print("\n" + "=" * 100)
    print("실험 9: 상태 기반 추세추종 결과")
    print("=" * 100)
    print(f"총 소요: {total_time:.1f}초 (prep {t_prep:.1f}s + sim {total_time-t_prep:.1f}s)")

    print("\n## 핵심 지표 비교")
    print(f"{'변형':<20} {'건수':>5} {'승률':>6} {'PF':>6} {'PF(전)':>7} {'CAGR':>7} {'MDD':>7} {'순손익':>13} {'Payoff':>7}")
    print("-" * 100)
    for name, s in results:
        po = payoff(s['trades'])
        pos = f"{po:.2f}" if po == po else 'n/a'
        print(
            f"{name:<20} {s['total']:>5} {s['wr']:>5.1%} "
            f"{format_pf(s['pf']):>6} {format_pf(pf_gross(s['trades'])):>7} "
            f"{s['cagr']:>6.1%} {s['mdd']:>6.1%} "
            f"{s['net']:>+12,.0f} {pos:>7}"
        )

    # Best
    baseline = results[0]
    cands = [(n, s) for n, s in results[1:]]
    best_name, best_s = max(cands, key=lambda x: (x[1]['pf'], x[1]['net']))

    print(f"\n## 보유 기간별 순손익 (Best: {best_name} vs v2.1)")
    bb = hold_buckets(baseline[1]['trades'])
    xb = hold_buckets(best_s['trades'])
    print(f"{'구간':<10} {'v2.1 PnL(건)':>22} {'Best PnL(건)':>22} {'변화':>15}")
    print("-" * 75)
    for k in ['1-5d', '6-10d', '11-15d', '16-25d', '26d+']:
        bp, bn = bb[k]
        xp, xn = xb[k]
        print(f"{k:<10} {bp:>+14,.0f} ({bn:>3}) {xp:>+14,.0f} ({xn:>3}) {xp - bp:>+14,.0f}")

    print("\n## Exit Reason 분포")
    # 통합 reason 집합
    all_reasons = set()
    dists = []
    for name, s in results:
        d = exit_dist(s['trades'])
        dists.append((name, d, s['total']))
        all_reasons |= set(d.keys())
    reasons_sorted = sorted(all_reasons)
    print(f"{'변형':<20} " + " ".join(f"{r:>15}" for r in reasons_sorted))
    print("-" * (20 + 16 * len(reasons_sorted)))
    for name, d, total in dists:
        cells = []
        for r in reasons_sorted:
            n = d.get(r, 0)
            cells.append(f"{n:>4}({n/total:>5.1%})" if total else "    -")
        print(f"{name:<20} " + " ".join(f"{c:>15}" for c in cells))

    # 연도별 PF
    all_years = set()
    yr_maps = []
    for name, s in results:
        ym = yearly_pf(s['trades'])
        yr_maps.append((name, ym))
        all_years |= set(ym)
    print("\n## 연도별 PF")
    print(f"{'연도':<6} " + "".join(f"{n:>18}" for n, _ in yr_maps))
    print("-" * (6 + 18 * len(yr_maps)))
    for yr in sorted(all_years):
        cells = []
        for _, ym in yr_maps:
            v = ym.get(yr, float('nan'))
            cells.append(format_pf(v) if v == v else '-')
        print(f"{yr:<6} " + "".join(f"{c:>18}" for c in cells))

    # 종목 중복도 (v2.1 vs 추세B 기본)
    v21_tickers = set(t.ticker for t in baseline[1]['trades'])
    trend_name = '추세B ATR청산'
    trend_s = dict(results)[trend_name]
    tr_tickers = set(t.ticker for t in trend_s['trades'])
    overlap = v21_tickers & tr_tickers
    v21_pairs = set((t.ticker, t.entry_date) for t in baseline[1]['trades'])
    tr_pairs = set((t.ticker, t.entry_date) for t in trend_s['trades'])
    po_pair = v21_pairs & tr_pairs
    print(f"\n## 종목 중복도 (v2.1 vs 추세B)")
    print(f"  v2.1 티커 {len(v21_tickers)} / 추세 티커 {len(tr_tickers)}")
    print(f"  티커 교집합 {len(overlap)} 중복률 {len(overlap)/min(len(v21_tickers),len(tr_tickers))*100 if v21_tickers and tr_tickers else 0:.1f}%")
    print(f"  (티커,진입일) 쌍 중복 {len(po_pair)} / min({len(v21_pairs)},{len(tr_pairs)}) = "
          f"{len(po_pair)/min(len(v21_pairs),len(tr_pairs))*100 if v21_pairs and tr_pairs else 0:.1f}%")

    # 신호 빈도
    trend_yearly_count = defaultdict(int)
    for t in trend_s['trades']:
        trend_yearly_count[t.entry_date[:4]] += 1
    avg_trend_per_year = sum(trend_yearly_count.values()) / len(trend_yearly_count) if trend_yearly_count else 0
    print(f"\n## 신호 빈도 (추세B)")
    print(f"  연간 평균 거래: {avg_trend_per_year:.1f}건 / {sum(trend_yearly_count.values())}건 / {len(trend_yearly_count)}년")

    # 상대강도 효과
    trend_B = dict(results)['추세B ATR청산']
    trend_B_relaxed = dict(results)['추세B 완화']
    print(f"\n## 상대강도 효과 (추세B: 포함 vs 제외)")
    print(f"{'항목':<10} {'상대강도 포함':>15} {'상대강도 제외':>15} {'차이':>10}")
    print(f"{'거래수':<10} {trend_B['total']:>15} {trend_B_relaxed['total']:>15} {trend_B_relaxed['total']-trend_B['total']:>+10}")
    print(f"{'PF':<10} {format_pf(trend_B['pf']):>15} {format_pf(trend_B_relaxed['pf']):>15} {trend_B_relaxed['pf']-trend_B['pf']:>+10.2f}")
    print(f"{'MDD':<10} {trend_B['mdd']:>15.1%} {trend_B_relaxed['mdd']:>15.1%} {(trend_B_relaxed['mdd']-trend_B['mdd'])*100:>+9.1f}%p")
    print(f"{'CAGR':<10} {trend_B['cagr']:>15.1%} {trend_B_relaxed['cagr']:>15.1%} {(trend_B_relaxed['cagr']-trend_B['cagr'])*100:>+9.1f}%p")

    # 판정
    pf_diff = best_s['pf'] - baseline[1]['pf']
    if pf_diff >= 0.03:
        v = "✅ 우위"
    elif abs(pf_diff) < 0.03:
        v = "⚠ 비슷"
    else:
        v = "❌ 열세"
    print(f"\n## 판정")
    print(f"  Best: {best_name}")
    print(f"  PF: {format_pf(baseline[1]['pf'])} → {format_pf(best_s['pf'])} ({pf_diff:+.2f})")
    print(f"  v2.1 대비: {v}")
    pair_pct = len(po_pair)/min(len(v21_pairs),len(tr_pairs))*100 if v21_pairs and tr_pairs else 0
    if pair_pct < 5:
        comp = "높음 (독립 알파)"
    elif pair_pct < 20:
        comp = "중간"
    else:
        comp = "낮음 (대체)"
    print(f"  보완 가능성: 쌍 중복률 {pair_pct:.1f}% → {comp}")


if __name__ == "__main__":
    run_all()
