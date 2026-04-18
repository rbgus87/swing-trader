"""실험 8: v2.1 잔여 필터 + 시간손절 강화.

돌파 6변형 + 눌림목 4변형 = 10개.
"""
import sys
import time
from collections import defaultdict
from dataclasses import replace
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

from loguru import logger

from src.data_pipeline.db import get_connection
from src.backtest.portfolio_backtester import (
    RiskParams,
    load_backtest_data,
    precompute_daily_signals,
    precompute_pullback_signals,
    run_portfolio_backtest,
)
from src.strategy.trend_following_v0 import StrategyParams


TOTAL_COST_PCT = 0.0031
PRICE_MIN = 1000.0
PRICE_MAX = 50000.0
DAYS_LISTED_MIN = 180
ATR_BAND_MIN = 0.025
ATR_BAND_MAX = 0.08
PB_DEPTH_MIN = -0.12
PB_DEPTH_MAX = -0.05


def load_listed_dates():
    out = {}
    with get_connection() as conn:
        for r in conn.execute("SELECT ticker, listed_date, first_candle_date FROM stocks").fetchall():
            d = r['listed_date'] or r['first_candle_date']
            if d:
                out[r['ticker']] = d
    return out


def filter_precomp(precomp, *,
                   price_min=None, price_max=None,
                   atr_min=None, atr_max=None,
                   listed_dates=None, days_listed_min=None,
                   pb_depth_min=None, pb_depth_max=None):
    """candidate 후처리 필터. 차단 카운트 반환."""
    stats = defaultdict(int)
    new_cands = {}
    for date_str, cands in precomp['candidates'].items():
        filtered = []
        for c in cands:
            # 주가 범위
            if price_min is not None and c['close'] < price_min:
                stats['price_low'] += 1
                continue
            if price_max is not None and c['close'] > price_max:
                stats['price_high'] += 1
                continue
            # ATR 밴드 (v2.1 baseline 적용 시)
            if atr_min is not None and c.get('atr_ratio', 0) < atr_min:
                stats['atr_low'] += 1
                continue
            if atr_max is not None and c.get('atr_ratio', 0) > atr_max:
                stats['atr_high'] += 1
                continue
            # 상장기간
            if listed_dates is not None and days_listed_min is not None:
                ld = listed_dates.get(c['ticker'])
                if ld is None:
                    stats['listed_unknown'] += 1
                    continue
                try:
                    ld_dt = datetime.strptime(ld, '%Y-%m-%d').date()
                    cur_dt = datetime.strptime(date_str, '%Y-%m-%d').date()
                    days = (cur_dt - ld_dt).days
                    if days < days_listed_min:
                        stats['listed_short'] += 1
                        continue
                except (ValueError, TypeError):
                    pass
            # 눌림목 조정폭
            if pb_depth_min is not None and pb_depth_max is not None:
                pd_val = c.get('pullback_depth', 0.0)
                if not (pb_depth_min <= pd_val <= pb_depth_max):
                    stats['pb_depth'] += 1
                    continue
            filtered.append(c)
        new_cands[date_str] = filtered
    return {**precomp, 'candidates': new_cands}, dict(stats)


def compute_payoff(trades):
    w = [t.pnl_pct for t in trades if t.pnl_amount > 0]
    l = [t.pnl_pct for t in trades if t.pnl_amount <= 0]
    if not w or not l:
        return float('nan')
    return (sum(w) / len(w)) / abs(sum(l) / len(l))


def pf_gross(trades):
    gp = gl = 0.0
    for t in trades:
        g = t.pnl_amount + t.shares * t.entry_price * TOTAL_COST_PCT
        if g > 0:
            gp += g
        else:
            gl += abs(g)
    return gp / gl if gl > 0 else float('inf')


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


def format_pf(pf):
    return f"{pf:.2f}" if pf != float('inf') else 'inf'


def run_all():
    t0 = time.time()

    params_base = StrategyParams()
    logger.info("데이터 로드")
    preloaded = load_backtest_data(params_base)

    logger.info("돌파 precompute")
    precomp_tf_raw = precompute_daily_signals(
        preloaded['trading_dates'], preloaded['ticker_data'],
        preloaded['ticker_date_idx'], preloaded['initial_universe'], params_base,
    )
    logger.info("눌림목 precompute")
    precomp_pb_raw = precompute_pullback_signals(
        preloaded['trading_dates'], preloaded['ticker_data'],
        preloaded['ticker_date_idx'], preloaded['initial_universe'], params_base,
    )
    listed_dates = load_listed_dates()
    logger.info(f"listed_dates loaded for {len(listed_dates)} tickers")

    # v2.1 baseline = 돌파 + ATR 밴드
    precomp_v21_base, _ = filter_precomp(precomp_tf_raw, atr_min=ATR_BAND_MIN, atr_max=ATR_BAND_MAX)

    # TF 변형들
    precomp_tf_price, stats_tf_price = filter_precomp(
        precomp_v21_base, price_min=PRICE_MIN, price_max=PRICE_MAX)
    precomp_tf_listed, stats_tf_listed = filter_precomp(
        precomp_v21_base, listed_dates=listed_dates, days_listed_min=DAYS_LISTED_MIN)
    precomp_tf_pl, stats_tf_pl = filter_precomp(
        precomp_v21_base,
        price_min=PRICE_MIN, price_max=PRICE_MAX,
        listed_dates=listed_dates, days_listed_min=DAYS_LISTED_MIN,
    )

    # PB 변형들 — 이미 precompute_pullback 내부에 ATR 밴드 있음
    precomp_pb_depth, stats_pb_depth = filter_precomp(
        precomp_pb_raw, pb_depth_min=PB_DEPTH_MIN, pb_depth_max=PB_DEPTH_MAX)
    precomp_pb_pl_depth, stats_pb_pl_depth = filter_precomp(
        precomp_pb_raw,
        price_min=PRICE_MIN, price_max=PRICE_MAX,
        listed_dates=listed_dates, days_listed_min=DAYS_LISTED_MIN,
        pb_depth_min=PB_DEPTH_MIN, pb_depth_max=PB_DEPTH_MAX,
    )

    t_prep = time.time() - t0
    logger.info(f"준비 완료 ({t_prep:.1f}s)")

    # ── 파라미터 세트 ──
    tf_params = StrategyParams()    # v2.1
    pb_params = replace(params_base,
                        stop_loss_atr=1.5, take_profit_atr=2.0, trailing_atr=3.0,
                        max_hold_days=10, tp1_sell_ratio=0.3)

    # 조기 시간손절 — 돌파는 10일+2%, 눌림목은 5일+2%
    risk_tf_early = RiskParams(
        enable_sizing=False, enable_atr_sizing=False,
        enable_daily_loss=False, enable_ticker_cooldown=False,
        early_exit_enabled=True, early_exit_hold_days=10, early_exit_return_min=0.02,
    )
    # 눌림목 baseline: ATR 사이징 (실험 6 기준)
    risk_pb_base = RiskParams(
        enable_sizing=False, enable_atr_sizing=True,
        enable_daily_loss=False, enable_ticker_cooldown=False,
    )
    risk_pb_early = RiskParams(
        enable_sizing=False, enable_atr_sizing=True,
        enable_daily_loss=False, enable_ticker_cooldown=False,
        early_exit_enabled=True, early_exit_hold_days=5, early_exit_return_min=0.02,
    )

    # ── 변형 정의 ──
    tf_variants = [
        ('v2.1 baseline',        precomp_v21_base, tf_params, None),
        ('+주가범위',             precomp_tf_price, tf_params, None),
        ('+상장기간',             precomp_tf_listed, tf_params, None),
        ('+시간손절',             precomp_v21_base, tf_params, risk_tf_early),
        ('+주가+상장',            precomp_tf_pl, tf_params, None),
        ('+주가+상장+시간',         precomp_tf_pl, tf_params, risk_tf_early),
    ]

    pb_variants = [
        ('눌림목 baseline',        precomp_pb_raw, pb_params, risk_pb_base),
        ('+조정폭제한',            precomp_pb_depth, pb_params, risk_pb_base),
        ('+주가+상장+조정폭',        precomp_pb_pl_depth, pb_params, risk_pb_base),
        ('+전부',                precomp_pb_pl_depth, pb_params, risk_pb_early),
    ]

    tf_results = []
    pb_results = []

    for name, pcomp, p, risk in tf_variants:
        logger.info(f"\n--- TF: {name} ---")
        t1 = time.time()
        r = run_portfolio_backtest(
            initial_capital=5_000_000, max_positions=4,
            params=p, preloaded_data=preloaded, precomputed=pcomp, risk=risk,
        )
        net = r.final_capital - r.initial_capital
        pfg = pf_gross(r.trades)
        logger.info(
            f"[{name}] trades={r.total_trades}, WR={r.win_rate:.1%}, "
            f"PF={format_pf(r.profit_factor)}, CAGR={r.cagr_pct:.1%}, "
            f"MDD={r.max_drawdown_pct:.1%}, net={net:+,.0f} ({time.time()-t1:.1f}s)"
        )
        tf_results.append((name, r, net, pfg))

    for name, pcomp, p, risk in pb_variants:
        logger.info(f"\n--- PB: {name} ---")
        t1 = time.time()
        r = run_portfolio_backtest(
            initial_capital=5_000_000, max_positions=4,
            params=p, preloaded_data=preloaded, precomputed=pcomp, risk=risk,
        )
        net = r.final_capital - r.initial_capital
        pfg = pf_gross(r.trades)
        logger.info(
            f"[{name}] trades={r.total_trades}, WR={r.win_rate:.1%}, "
            f"PF={format_pf(r.profit_factor)}, CAGR={r.cagr_pct:.1%}, "
            f"MDD={r.max_drawdown_pct:.1%}, net={net:+,.0f} ({time.time()-t1:.1f}s)"
        )
        pb_results.append((name, r, net, pfg))

    total_time = time.time() - t0

    # ── 출력 ──
    print("\n" + "=" * 100)
    print("실험 8: 잔여 필터 + 시간손절 결과")
    print("=" * 100)
    print(f"총 소요: {total_time:.1f}초 (prep {t_prep:.1f}s + sim {total_time-t_prep:.1f}s)")

    def print_results(title, results):
        print(f"\n## {title}")
        print(f"{'변형':<22} {'건수':>5} {'승률':>6} {'PF':>6} {'PF(전)':>7} {'CAGR':>7} {'MDD':>7} {'순손익':>13} {'Payoff':>7}")
        print("-" * 100)
        for name, r, net, pfg in results:
            payoff = compute_payoff(r.trades)
            pay_s = f"{payoff:.2f}" if payoff == payoff else 'n/a'
            print(
                f"{name:<22} {r.total_trades:>5} {r.win_rate:>5.1%} "
                f"{format_pf(r.profit_factor):>6} {format_pf(pfg):>7} "
                f"{r.cagr_pct:>6.1%} {r.max_drawdown_pct:>6.1%} "
                f"{net:>+12,.0f} {pay_s:>7}"
            )

    print_results("돌파 v2.1 변형", tf_results)
    print_results("눌림목 변형", pb_results)

    # 필터 차단 통계
    print("\n## 필터 차단 통계")
    print(f"  주가 <{PRICE_MIN:.0f}원:          {stats_tf_pl.get('price_low', 0) + stats_pb_pl_depth.get('price_low', 0)} (TF/PB 합)")
    print(f"  주가 >{PRICE_MAX:.0f}원:        {stats_tf_pl.get('price_high', 0) + stats_pb_pl_depth.get('price_high', 0)}")
    print(f"  상장 <{DAYS_LISTED_MIN}일:         {stats_tf_pl.get('listed_short', 0) + stats_pb_pl_depth.get('listed_short', 0)}")
    print(f"  조정폭 범위 밖:         {stats_pb_depth.get('pb_depth', 0)}")
    print(f"  [돌파 단독]:")
    print(f"    price_low={stats_tf_price.get('price_low', 0)}, price_high={stats_tf_price.get('price_high', 0)}")
    print(f"    listed_short={stats_tf_listed.get('listed_short', 0)}")
    print(f"  [눌림목 단독]:")
    print(f"    pb_depth={stats_pb_depth.get('pb_depth', 0)}")

    # 러너 보존 (돌파만)
    print("\n## 러너 보존 (돌파)")
    print(f"{'변형':<22} {'26d+건':>8} {'26d+손익':>14} {'16-25d건':>10} {'16-25d손익':>15}")
    print("-" * 75)
    for name, r, *_ in tf_results:
        b = hold_buckets(r.trades)
        p26, n26 = b['26d+']
        p16, n16 = b['16-25d']
        print(f"{name:<22} {n26:>8} {p26:>+14,.0f} {n16:>10} {p16:>+15,.0f}")

    # Best 선정: PF 최고 (동률시 net)
    tf_best = max(tf_results[1:], key=lambda x: (x[1].profit_factor, x[2]))  # baseline 제외
    pb_best = max(pb_results[1:], key=lambda x: (x[1].profit_factor, x[2]))
    tf_baseline = tf_results[0]
    pb_baseline = pb_results[0]

    # 보유 기간별 (Best vs Baseline)
    def print_buckets(title, baseline, best):
        print(f"\n## 보유 기간별 — {title} (Best vs Baseline)")
        bb = hold_buckets(baseline[1].trades)
        xb = hold_buckets(best[1].trades)
        print(f"{'구간':<10} {'Baseline PnL(건)':>22} {'Best PnL(건)':>22} {'변화':>15}")
        print("-" * 75)
        for key in ['1-5d', '6-10d', '11-15d', '16-25d', '26d+']:
            bp, bn = bb[key]
            xp, xn = xb[key]
            print(f"{key:<10} {bp:>+14,.0f} ({bn:>3}) {xp:>+14,.0f} ({xn:>3}) {xp - bp:>+14,.0f}")

    print_buckets(f"돌파 ({tf_best[0]})", tf_baseline, tf_best)
    print_buckets(f"눌림목 ({pb_best[0]})", pb_baseline, pb_best)

    print("\n## Best vs Baseline 요약")
    print(f"{'항목':<10} {'v2.1':>12} {'돌파Best':>12} {'눌림목':>12} {'눌림목Best':>12}")
    print("-" * 70)
    print(f"{'PF':<10} {format_pf(tf_baseline[1].profit_factor):>12} {format_pf(tf_best[1].profit_factor):>12} "
          f"{format_pf(pb_baseline[1].profit_factor):>12} {format_pf(pb_best[1].profit_factor):>12}")
    print(f"{'CAGR':<10} {tf_baseline[1].cagr_pct:>12.1%} {tf_best[1].cagr_pct:>12.1%} "
          f"{pb_baseline[1].cagr_pct:>12.1%} {pb_best[1].cagr_pct:>12.1%}")
    print(f"{'MDD':<10} {tf_baseline[1].max_drawdown_pct:>12.1%} {tf_best[1].max_drawdown_pct:>12.1%} "
          f"{pb_baseline[1].max_drawdown_pct:>12.1%} {pb_best[1].max_drawdown_pct:>12.1%}")
    print(f"{'순손익':<10} {tf_baseline[2]:>+12,.0f} {tf_best[2]:>+12,.0f} "
          f"{pb_baseline[2]:>+12,.0f} {pb_best[2]:>+12,.0f}")

    print("\n## 판정")
    print(f"  돌파 Best: {tf_best[0]}")
    print(f"    PF: {format_pf(tf_baseline[1].profit_factor)} → {format_pf(tf_best[1].profit_factor)} "
          f"({tf_best[1].profit_factor - tf_baseline[1].profit_factor:+.2f})")
    print(f"    CAGR: {tf_baseline[1].cagr_pct:.1%} → {tf_best[1].cagr_pct:.1%}")
    print(f"    MDD: {tf_baseline[1].max_drawdown_pct:.1%} → {tf_best[1].max_drawdown_pct:.1%}")
    print(f"  눌림목 Best: {pb_best[0]}")
    print(f"    PF: {format_pf(pb_baseline[1].profit_factor)} → {format_pf(pb_best[1].profit_factor)} "
          f"({pb_best[1].profit_factor - pb_baseline[1].profit_factor:+.2f})")
    print(f"    CAGR: {pb_baseline[1].cagr_pct:.1%} → {pb_best[1].cagr_pct:.1%}")
    print(f"    MDD: {pb_baseline[1].max_drawdown_pct:.1%} → {pb_best[1].max_drawdown_pct:.1%}")

    tf_up = tf_best[1].profit_factor >= tf_baseline[1].profit_factor + 0.03
    pb_up = pb_best[1].profit_factor >= pb_baseline[1].profit_factor + 0.03
    if tf_up and pb_up:
        print("  ✅ 양쪽 개선")
    elif tf_up or pb_up:
        print(f"  ⚠ 일부 개선 (TF {'✓' if tf_up else '✗'}, PB {'✓' if pb_up else '✗'})")
    else:
        print("  ❌ 현행 유지")


if __name__ == "__main__":
    run_all()
