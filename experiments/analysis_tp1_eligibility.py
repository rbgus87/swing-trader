"""분석: 5M equity+TP2 백테스트에서 TP1 발동 가능 여부별 성과 분리.

TP1 발동 조건: int(initial_shares * 0.3) >= 1 → initial_shares >= 4
미만이면 TP1이 silent skip됨 (cascade에서 partial_shares=0으로 skip).

그룹별 비교 (position 단위, 한 ticker+entry_date를 1 position으로 집계):
  - TP1 가능 (shares ≥ 4): 정상 TP1/TP2 발동
  - TP1 불가 (shares < 4): TP1 미발동, 트레일링/SL/시간만 작동

추가: 슬롯 1~4번째별 TP1 불가 비율 (alloc_tracker로 매핑).
"""
import sys
import time
from collections import defaultdict
from dataclasses import replace

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

from loguru import logger

from src.backtest.portfolio_backtester import (
    load_backtest_data,
    precompute_daily_signals,
    run_portfolio_backtest,
)
from src.strategy.trend_following_v2 import StrategyParams


TOTAL_COST_PCT = 0.0031
TP1_RATIO = 0.30
MIN_SHARES_FOR_TP1 = 4   # int(4 * 0.3) = 1, int(3 * 0.3) = 0


def fmt_pf(pf):
    return f"{pf:.2f}" if pf != float('inf') else 'inf'


def compute_pf(pnls):
    gp = sum(p for p in pnls if p > 0)
    gl = abs(sum(p for p in pnls if p <= 0))
    return gp / gl if gl > 0 else float('inf')


def run():
    t0 = time.time()
    base = StrategyParams()
    v25_params = replace(base, tp2_atr=4.0, tp2_sell_ratio=0.30)

    logger.info("데이터 로드")
    preloaded = load_backtest_data(base)

    logger.info("precompute")
    precomp = precompute_daily_signals(
        preloaded['trading_dates'], preloaded['ticker_data'],
        preloaded['ticker_date_idx'], preloaded['initial_universe'],
        base,
        kospi_ret_map=preloaded['kospi_ret_map'],
        kosdaq_ret_map=preloaded['kosdaq_ret_map'],
        ticker_market=preloaded['ticker_market'],
    )

    logger.info("백테스트 실행 (5M equity+TP2)")
    tracker = []
    r = run_portfolio_backtest(
        initial_capital=5_000_000, max_positions=4,
        params=v25_params, preloaded_data=preloaded, precomputed=precomp,
        risk=None, sizing_mode='equity', alloc_tracker=tracker,
    )
    logger.info(
        f"trades={r.total_trades}, WR={r.win_rate:.1%}, "
        f"PF={fmt_pf(r.profit_factor)}, CAGR={r.cagr_pct:.1%}, "
        f"MDD={r.max_drawdown_pct:.1%}, net={r.final_capital - r.initial_capital:+,.0f}"
    )

    # ── position 단위 집계 ──
    # key = (ticker, entry_date), value = {pnls: list, initial_shares: int}
    positions_agg = defaultdict(lambda: {'pnls': [], 'initial_shares': 0,
                                          'entry_price': 0.0,
                                          'has_tp1': False, 'has_tp2': False})
    for t in r.trades:
        key = (t.ticker, t.entry_date)
        positions_agg[key]['pnls'].append(t.pnl_amount)
        positions_agg[key]['initial_shares'] = max(
            positions_agg[key]['initial_shares'], t.initial_shares
        )
        positions_agg[key]['entry_price'] = float(t.entry_price)
        if t.exit_reason == 'TAKE_PROFIT_1':
            positions_agg[key]['has_tp1'] = True
        if t.exit_reason == 'TAKE_PROFIT_2':
            positions_agg[key]['has_tp2'] = True

    # ── 그룹 분류 ──
    group_able = []        # initial_shares >= 4
    group_unable = []      # initial_shares < 4
    for key, d in positions_agg.items():
        total_pnl = sum(d['pnls'])
        info = {
            'key': key,
            'pnl': total_pnl,
            'initial_shares': d['initial_shares'],
            'entry_price': d['entry_price'],
            'has_tp1': d['has_tp1'],
            'has_tp2': d['has_tp2'],
        }
        if d['initial_shares'] >= MIN_SHARES_FOR_TP1:
            group_able.append(info)
        else:
            group_unable.append(info)

    def stats(group):
        n = len(group)
        if n == 0:
            return (0, 0, 0.0, 0.0, 0.0, 0.0)
        wins = sum(1 for x in group if x['pnl'] > 0)
        wr = wins / n
        pnls = [x['pnl'] for x in group]
        pf = compute_pf(pnls)
        net = sum(pnls)
        avg = net / n
        return (n, wins, wr, pf, net, avg)

    # ── 슬롯별 TP1 불가 비율 (alloc_tracker 활용) ──
    # alloc_tracker에는 (date, ticker, order, shares) 정보가 있음
    # 동일 (date, ticker)의 entry → 슬롯 매핑
    # 단, alloc_tracker의 date는 진입 결정일(시그널일), positions의 entry_date는 익일.
    # 백테스터: alloc_tracker.date = 신호일, position.entry_date = 신호일+1 (next_date)
    # 매핑: tracker entry로부터 entry_date를 다음 거래일로 이동시킬 수 없으니 직접 비교용 dict
    # 간단 매핑: tracker의 (ticker, shares) 각 entry는 각각 1번 사용 → 순서대로 매칭
    # 더 정확하게: alloc_tracker는 시그널일 기준, position.entry_date = next_trading_date(signal_date)
    # 같은 ticker가 시그널일마다 1번씩 들어감. 동일 (ticker, shares)는 거의 unique한 시그널 매칭.

    # Slot 분포 — 각 슬롯에서 진입한 거래의 initial_shares 분포로 분석
    slot_breakdown = defaultdict(lambda: {'total': 0, 'unable': 0})
    for entry in tracker:
        slot = entry['order']
        slot_breakdown[slot]['total'] += 1
        if entry['shares'] < MIN_SHARES_FOR_TP1:
            slot_breakdown[slot]['unable'] += 1

    # ── 보고 ──
    print("\n" + "=" * 100)
    print("📋 TP1 발동 가능 여부별 성과 분석 (5M equity+TP2)")
    print("=" * 100)
    print(f"Period: {r.period} / Total trade records: {r.total_trades}")
    print(f"Total positions (unique entries): {len(positions_agg)}")
    print(f"  TP1 가능 (shares ≥ {MIN_SHARES_FOR_TP1}): {len(group_able)}")
    print(f"  TP1 불가 (shares < {MIN_SHARES_FOR_TP1}): {len(group_unable)}")
    print(f"총 소요: {time.time()-t0:.1f}s")

    print("\n■ TP1 발동 가능 여부별 성과 (position 단위)")
    print(f"{'그룹':<28} {'건수':>5} {'WR':>6} {'PF':>6} {'순손익':>14} {'평균손익':>12}")
    print("-" * 80)
    for name, group in [
        (f'TP1 가능 (≥{MIN_SHARES_FOR_TP1}주)', group_able),
        (f'TP1 불가 (<{MIN_SHARES_FOR_TP1}주)', group_unable),
    ]:
        n, w, wr, pf, net, avg = stats(group)
        print(
            f"{name:<28} {n:>5} {wr:>5.1%} {fmt_pf(pf):>6} "
            f"{net:>+13,.0f} {avg:>+11,.0f}"
        )

    # 추가 통계: TP1/TP2 발동 비율
    print("\n■ 발동 횟수")
    for name, group in [
        (f'TP1 가능 (≥{MIN_SHARES_FOR_TP1}주)', group_able),
        (f'TP1 불가 (<{MIN_SHARES_FOR_TP1}주)', group_unable),
    ]:
        if not group:
            continue
        tp1_n = sum(1 for x in group if x['has_tp1'])
        tp2_n = sum(1 for x in group if x['has_tp2'])
        print(
            f"  {name}: TP1 발동 {tp1_n}/{len(group)} ({tp1_n/len(group)*100:.1f}%), "
            f"TP2 발동 {tp2_n}/{len(group)} ({tp2_n/len(group)*100:.1f}%)"
        )

    # ── 슬롯별 TP1 불가 비율 ──
    print("\n■ 슬롯별 TP1 불가 비율 (alloc_tracker 기준)")
    print(f"{'슬롯':<6} {'전체':>6} {'TP1 불가':>10} {'비율':>8}")
    print("-" * 40)
    for slot in sorted(slot_breakdown.keys()):
        d = slot_breakdown[slot]
        ratio = d['unable'] / d['total'] * 100 if d['total'] else 0
        print(f"{slot:<6} {d['total']:>6} {d['unable']:>10} {ratio:>7.1f}%")

    # 슬롯별 평균 shares
    print("\n■ 슬롯별 평균 shares (alloc_tracker 기준)")
    print(f"{'슬롯':<6} {'평균':>7} {'최소':>5} {'최대':>5}")
    print("-" * 30)
    by_slot_shares = defaultdict(list)
    for entry in tracker:
        by_slot_shares[entry['order']].append(entry['shares'])
    for slot in sorted(by_slot_shares.keys()):
        ss = by_slot_shares[slot]
        print(f"{slot:<6} {sum(ss)/len(ss):>7.1f} {min(ss):>5} {max(ss):>5}")

    # 가격 분포 — TP1 불가 그룹의 entry_price
    if group_unable:
        prices = sorted([x['entry_price'] for x in group_unable])
        print(f"\n■ TP1 불가 그룹 entry_price 분포 (N={len(prices)})")
        print(f"  min: {prices[0]:>10,.0f}")
        print(f"  median: {prices[len(prices)//2]:>10,.0f}")
        print(f"  max: {prices[-1]:>10,.0f}")
        # 가격 buckets
        buckets = {'<200K':0, '200~500K':0, '500K~1M':0, '1M+':0}
        for p in prices:
            if p < 200_000: buckets['<200K'] += 1
            elif p < 500_000: buckets['200~500K'] += 1
            elif p < 1_000_000: buckets['500K~1M'] += 1
            else: buckets['1M+'] += 1
        for b, c in buckets.items():
            print(f"  {b:<12}: {c}")


if __name__ == "__main__":
    run()
