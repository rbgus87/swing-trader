"""스크리닝 필터 깔때기 진단 — 각 필터의 종목 차단률 분석.

최근 60거래일에 대해:
  - 각 필터 개별 통과율 (독립, AND 아님)
  - 누적 AND 통과율 (precompute 순서 그대로)
  - ADX / RS 분포 → 완화 효과 예측
  - 완화 시나리오 시뮬레이션

Usage:
    python experiments/experiment_filter_funnel.py
    python experiments/experiment_filter_funnel.py --days 90
"""
from __future__ import annotations

import argparse
import sys
import time
from collections import defaultdict
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from loguru import logger
logger.remove()
logger.add(sys.stderr, level="WARNING")

from src.backtest.portfolio_backtester import (
    BREADTH_GATE_THRESHOLD,
    build_universe,
    load_backtest_data,
    precompute_daily_signals,
)
from src.data_pipeline.db import get_connection
from src.strategy.dynamic_hold import DynamicHoldParams
from src.strategy.ranking import RankingWeights
from src.strategy.scaling import ScalingParams
from src.strategy.sector_constraint import SectorConstraint
from src.strategy.trend_following_v2 import StrategyParams
from src.utils.slippage_model import SlippageParams

# ─────────────────────────────────────────────────────────────────────────────
# 고정 설정 — v2.7
# ─────────────────────────────────────────────────────────────────────────────

BASE_PARAMS = StrategyParams(
    adx_threshold=25.0,
    relative_strength_threshold=0.08,
    stop_loss_atr=3.0,
    take_profit_atr=1.5,
    trailing_atr=3.0,
    tp1_sell_ratio=0.10,
    tp2_atr=4.0,
    tp2_sell_ratio=0.10,
)

WEIGHTS = RankingWeights(
    rs=0.50,
    momentum_atr=0.20,
    adx=0.15,
    liquidity=0.10,
    ma_alignment=0.05,
)

# 완화 시나리오 정의
SCENARIOS: dict[str, dict] = {
    "current":           {"adx": 25.0, "rs": 0.08, "skip_volume": False, "ma60_min": 0.05},
    "adx_20":            {"adx": 20.0, "rs": 0.08, "skip_volume": False, "ma60_min": 0.05},
    "rs_05":             {"adx": 25.0, "rs": 0.05, "skip_volume": False, "ma60_min": 0.05},
    "adx_20_rs_05":      {"adx": 20.0, "rs": 0.05, "skip_volume": False, "ma60_min": 0.05},
    "no_volume":         {"adx": 25.0, "rs": 0.08, "skip_volume": True,  "ma60_min": 0.05},
    "ma60_dist_relaxed": {"adx": 25.0, "rs": 0.08, "skip_volume": False, "ma60_min": 0.02},
}

SCENARIO_LABELS = {
    "current":           "현재 (ADX≥25, RS≥0.08)",
    "adx_20":            "ADX≥20 완화",
    "rs_05":             "RS≥0.05 완화",
    "adx_20_rs_05":      "ADX≥20 + RS≥0.05",
    "no_volume":         "거래량 필터 제거",
    "ma60_dist_relaxed": "MA60 이격도 완화 (2~20%)",
}


# ─────────────────────────────────────────────────────────────────────────────
# 필터 체크
# ─────────────────────────────────="────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────

def _safe(val) -> float | None:
    if val is None:
        return None
    try:
        f = float(val)
        return None if pd.isna(f) else f
    except (TypeError, ValueError):
        return None


def _check_row(
    day: pd.Series,
    bench_ret,
    adx_thresh: float = 25.0,
    rs_thresh: float = 0.08,
    skip_volume: bool = False,
    ma60_min: float = 0.05,
    ma60_max: float = 0.20,
    atr_min: float = 0.025,
    atr_max: float = 0.08,
    tv_min: float = 5e9,
) -> dict[str, bool] | None:
    """한 종목·한 날의 모든 필터를 체크.

    Returns:
        필터명 → bool 딕셔너리. 데이터 불충분 시 None.
    """
    close = _safe(day.get('close'))
    ma20  = _safe(day.get('ma20'))
    ma60  = _safe(day.get('ma60'))
    ma120 = _safe(day.get('ma120'))
    ma60_slope = _safe(day.get('ma60_slope'))
    ma60_dist  = _safe(day.get('ma60_dist'))
    macd_hist  = _safe(day.get('macd_hist'))
    vol5  = _safe(day.get('avg_volume_5'))
    vol20 = _safe(day.get('avg_volume_20'))
    adx   = _safe(day.get('adx'))
    atr   = _safe(day.get('atr'))
    tv    = _safe(day.get('avg_trading_value_20'))
    ret_n = _safe(day.get('stock_ret_n'))

    # 필수 컬럼 누락 시 건너뜀
    required = [close, ma20, ma60, ma120, ma60_slope, ma60_dist,
                macd_hist, vol5, vol20, adx, atr, tv, ret_n]
    if any(v is None for v in required):
        return None
    if atr <= 0 or close <= 0:
        return None

    atr_ratio = atr / close
    rs_val = (ret_n - float(bench_ret)) if bench_ret is not None else None

    return {
        'ma_align':   close > ma20 > ma60 > ma120,
        'ma60_slope': ma60_slope > 0,
        'ma60_dist':  ma60_min <= ma60_dist <= ma60_max,
        'macd':       macd_hist > 0,
        'volume':     skip_volume or (vol5 > vol20),
        'adx':        adx >= adx_thresh,
        'tv':         tv >= tv_min,
        'atr':        atr_min <= atr_ratio <= atr_max,
        'rs':         (rs_val is not None) and (rs_val >= rs_thresh),
        # 원시 값 (분포 분석용)
        '_adx_val':   adx,
        '_rs_val':    rs_val,
    }


# 누적 AND 순서 (precompute_daily_signals와 동일)
CUM_ORDER = ['ma_align', 'ma60_slope', 'ma60_dist', 'macd', 'volume', 'adx', 'tv', 'atr', 'rs']

CUM_LABELS = {
    'ma_align':   'MA 정배열 (close>MA20>MA60>MA120)',
    'ma60_slope': 'MA60 기울기 > 0',
    'ma60_dist':  'MA60 이격도 5~20%',
    'macd':       'MACD 히스토그램 > 0',
    'volume':     '거래량 5일 > 20일',
    'adx':        f'ADX ≥ {BASE_PARAMS.adx_threshold:.0f}',
    'tv':         '거래대금 20일 ≥ 50억',
    'atr':        'ATR/가격 2.5~8%',
    'rs':         f'상대강도 RS > {BASE_PARAMS.relative_strength_threshold:.2f}',
}


# ─────────────────────────────────────────────────────────────────────────────
# 메인 분석
# ─────────────────────────────────────────────────────────────────────────────

def run_funnel(preloaded: dict, precomputed: dict, n_days: int,
               current_universe: set | None = None) -> dict:
    """최근 n_days 거래일에 대해 필터 깔때기 집계.

    Args:
        current_universe: 분석에 사용할 Universe 종목 집합.
                          None 이면 precomputed['universe_at'] 에서 조회.

    Returns:
        {
          'daily': list[dict],    # 날짜별 집계
          'adx_vals': list[float], # ADX 필터 직전 단계의 ADX 원시값
          'rs_vals':  list[float], # RS 필터 직전 단계의 RS 원시값
          'scen_daily': dict[scen_name, list[int]],  # 시나리오별 날짜별 pass_all
        }
    """
    all_dates     = preloaded['trading_dates']
    ticker_data   = preloaded['ticker_data']
    ticker_date_idx = preloaded['ticker_date_idx']
    kospi_ret_map = preloaded.get('kospi_ret_map', {})
    kosdaq_ret_map = preloaded.get('kosdaq_ret_map', {})
    ticker_market  = preloaded.get('ticker_market', {})

    recent_dates = all_dates[-n_days:]
    universe_at  = precomputed['universe_at']
    breadth_map  = precomputed['breadth']
    index_ma200  = precomputed['index_above_ma200']

    daily: list[dict] = []
    adx_vals: list[float] = []   # ADX 직전 통과 종목의 ADX 값
    rs_vals:  list[float] = []   # RS 직전 통과 종목의 RS 값
    scen_daily: dict[str, list[int]] = {k: [] for k in SCENARIOS}

    for date_str in recent_dates:
        ts = pd.Timestamp(date_str)

        # 날짜별 Universe: current_universe 우선, 없으면 precomputed 조회
        if current_universe is not None:
            universe = current_universe
        else:
            universe = universe_at.get(date_str, set())
            if not universe:
                for d in reversed(recent_dates):
                    if d <= date_str and universe_at.get(d):
                        universe = universe_at[d]
                        break

        kospi_ret  = kospi_ret_map.get(ts)
        kosdaq_ret = kosdaq_ret_map.get(ts)

        breadth   = breadth_map.get(date_str, 0.0)
        index_ok  = index_ma200.get(date_str, True)
        gate_open = breadth >= BREADTH_GATE_THRESHOLD and index_ok

        # 누적 AND 카운터 (현재 파라미터)
        cum_counts: dict[str, int] = {k: 0 for k in CUM_ORDER}
        # 개별 독립 카운터
        ind_counts: dict[str, int] = {k: 0 for k in CUM_ORDER}
        # 시나리오 카운터
        scen_counts: dict[str, int] = {k: 0 for k in SCENARIOS}

        for ticker in universe:
            idx_map = ticker_date_idx.get(ticker)
            if not idx_map:
                continue
            i = idx_map.get(ts)
            if i is None or i < BASE_PARAMS.ma_long:
                continue

            day = ticker_data[ticker].iloc[i]

            mkt = ticker_market.get(ticker, 'KOSPI')
            bench_ret = kosdaq_ret if mkt == 'KOSDAQ' else kospi_ret

            # ── 현재 파라미터 필터 체크 ──────────────────────────────────────
            checks = _check_row(
                day, bench_ret,
                adx_thresh=BASE_PARAMS.adx_threshold,
                rs_thresh=BASE_PARAMS.relative_strength_threshold,
                ma60_min=BASE_PARAMS.ma60_position_min,
                ma60_max=BASE_PARAMS.ma60_position_max,
                atr_min=BASE_PARAMS.atr_price_min,
                atr_max=BASE_PARAMS.atr_price_max,
                tv_min=BASE_PARAMS.min_trading_value,
            )
            if checks is None:
                continue

            # 개별 통과 카운트
            for k in CUM_ORDER:
                if checks[k]:
                    ind_counts[k] += 1

            # 누적 AND 통과 카운트
            alive = True
            for k in CUM_ORDER:
                if not alive:
                    break
                if checks[k]:
                    cum_counts[k] += 1
                else:
                    alive = False

            # ADX / RS 분포 수집
            # - ADX 분포: tv 통과 직후, adx 직전 단계 생존한 종목
            pre_adx = (checks['ma_align'] and checks['ma60_slope'] and
                       checks['ma60_dist'] and checks['macd'] and
                       checks['volume'] and checks['tv'])
            if pre_adx and checks.get('_adx_val') is not None:
                adx_vals.append(checks['_adx_val'])

            # - RS 분포: adx 통과 직후, rs 직전 단계 생존한 종목
            pre_rs = pre_adx and checks['adx'] and checks['atr']
            if pre_rs and checks.get('_rs_val') is not None:
                rs_vals.append(checks['_rs_val'])

            # ── 시나리오 시뮬레이션 ──────────────────────────────────────────
            for scen_name, scen in SCENARIOS.items():
                sc = _check_row(
                    day, bench_ret,
                    adx_thresh=scen['adx'],
                    rs_thresh=scen['rs'],
                    skip_volume=scen['skip_volume'],
                    ma60_min=scen['ma60_min'],
                    ma60_max=BASE_PARAMS.ma60_position_max,
                    atr_min=BASE_PARAMS.atr_price_min,
                    atr_max=BASE_PARAMS.atr_price_max,
                    tv_min=BASE_PARAMS.min_trading_value,
                )
                if sc is not None and all(sc[k] for k in CUM_ORDER):
                    scen_counts[scen_name] += 1

        daily.append({
            'date':      date_str,
            'universe':  len(universe),
            'ind':       ind_counts,
            'cum':       cum_counts,
            'pass_all':  cum_counts['rs'],   # 마지막 누적 AND 값
            'breadth':   breadth,
            'index_ok':  index_ok,
            'gate_open': gate_open,
            'scen':      scen_counts,
        })

        for scen_name in SCENARIOS:
            scen_daily[scen_name].append(scen_counts[scen_name])

    return {
        'daily':      daily,
        'adx_vals':   adx_vals,
        'rs_vals':    rs_vals,
        'scen_daily': scen_daily,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 보고서
# ─────────────────────────────────────────────────────────────────────────────

def _pct(n: float, total: float) -> str:
    if total <= 0:
        return "  -"
    return f"{n / total * 100:5.1f}%"


def _adx_dist(vals: list[float]) -> dict[str, float]:
    if not vals:
        return {}
    n = len(vals)
    return {
        "< 15":    sum(1 for v in vals if v < 15) / n,
        "15~20":   sum(1 for v in vals if 15 <= v < 20) / n,
        "20~25":   sum(1 for v in vals if 20 <= v < 25) / n,
        "25~30":   sum(1 for v in vals if 25 <= v < 30) / n,
        "≥ 30":    sum(1 for v in vals if v >= 30) / n,
    }


def _rs_dist(vals: list[float]) -> dict[str, float]:
    if not vals:
        return {}
    n = len(vals)
    return {
        "< 0":       sum(1 for v in vals if v < 0) / n,
        "0~0.05":    sum(1 for v in vals if 0 <= v < 0.05) / n,
        "0.05~0.08": sum(1 for v in vals if 0.05 <= v < 0.08) / n,
        "≥ 0.08":    sum(1 for v in vals if v >= 0.08) / n,
    }


def print_report(result: dict, n_days: int, elapsed: float, log_fn) -> None:
    daily     = result['daily']
    adx_vals  = result['adx_vals']
    rs_vals   = result['rs_vals']
    scen_daily = result['scen_daily']

    if not daily:
        log_fn("결과 없음")
        return

    SEP  = "═" * 68
    SEP2 = "─" * 68

    log_fn(SEP)
    log_fn(f"  스크리닝 필터 깔때기 진단 (최근 {n_days}거래일)")
    log_fn(SEP)
    log_fn("")

    # ── 1. 필터별 평균 통과율 ────────────────────────────────────────────────
    n = len(daily)
    avg_univ     = sum(d['universe'] for d in daily) / n
    avg_pass_all = sum(d['pass_all'] for d in daily) / n

    # 개별 통과율 (Universe 대비)
    avg_ind: dict[str, float] = {}
    for k in CUM_ORDER:
        avg_ind[k] = sum(d['ind'][k] for d in daily) / n

    # 누적 AND 통과율
    avg_cum: dict[str, float] = {}
    for k in CUM_ORDER:
        avg_cum[k] = sum(d['cum'][k] for d in daily) / n

    log_fn("■ 필터별 평균 통과율 (Universe 대비, 독립)")
    log_fn(f"  Universe 평균:               {avg_univ:5.0f}종목  (100%)")
    log_fn(SEP2)
    for k in CUM_ORDER:
        label = CUM_LABELS[k]
        ind_n = avg_ind[k]
        log_fn(f"  {label:<36}  {ind_n:5.1f}종목  ({_pct(ind_n, avg_univ)})")
    log_fn(SEP2)
    log_fn(f"  [전체 AND 통과]              {avg_pass_all:5.1f}종목  ({_pct(avg_pass_all, avg_univ)})")
    log_fn("")

    # ── 2. 누적 AND 깔때기 ────────────────────────────────────────────────────
    log_fn("■ 누적 AND 깔때기 (필터 적용 순서대로)")
    log_fn(f"  Universe                     {avg_univ:5.0f}종목  (100%)")
    for k in CUM_ORDER:
        label = CUM_LABELS[k]
        cum_n = avg_cum[k]
        log_fn(f"  → {label:<34}  {cum_n:5.1f}종목  ({_pct(cum_n, avg_univ)})")
    log_fn("")

    # ── 3. 병목 필터 TOP 3 (차단률 = 100% - 개별통과율) ────────────────────────
    log_fn("■ 병목 필터 TOP 3 (개별 차단률 높은 순)")
    block_rates = {k: 1 - (avg_ind[k] / avg_univ) for k in CUM_ORDER if avg_univ > 0}
    top3 = sorted(block_rates.items(), key=lambda x: x[1], reverse=True)[:3]
    for rank, (k, rate) in enumerate(top3, 1):
        log_fn(f"  {rank}. {CUM_LABELS[k]:<36}  차단 {rate*100:.1f}%")
    log_fn("")

    # ── 4. 날짜별 pass_all 분포 ───────────────────────────────────────────────
    log_fn("■ 날짜별 전체 통과 종목 수 (게이트 무관)")
    cnt_0 = sum(1 for d in daily if d['pass_all'] == 0)
    cnt_1 = sum(1 for d in daily if d['pass_all'] == 1)
    cnt_23 = sum(1 for d in daily if 2 <= d['pass_all'] <= 3)
    cnt_4p = sum(1 for d in daily if d['pass_all'] >= 4)
    gate_open_days = sum(1 for d in daily if d['gate_open'])
    log_fn(f"  0종목 일수:  {cnt_0:3d}일 / {n}일 ({cnt_0/n*100:.0f}%)")
    log_fn(f"  1종목 일수:  {cnt_1:3d}일")
    log_fn(f"  2~3종목:     {cnt_23:3d}일")
    log_fn(f"  4+종목:      {cnt_4p:3d}일")
    log_fn(f"  국면 게이트 OPEN: {gate_open_days:3d}일 / {n}일 ({gate_open_days/n*100:.0f}%)")
    log_fn("")

    # ── 5. 최근 5거래일 상세 ───────────────────────────────────────────────────
    log_fn("■ 최근 5거래일 상세")
    for d in daily[-5:]:
        g = "O" if d['gate_open'] else "X"
        breadth_pct = d['breadth'] * 100
        log_fn(
            f"  {d['date']}  Univ={d['universe']:3d}  "
            + "  ".join(f"{k[:3]}={d['cum'][k]}" for k in CUM_ORDER)
            + f"  게이트={g}(BW={breadth_pct:.0f}%)"
        )
    log_fn("")

    # ── 6. ADX 분포 ───────────────────────────────────────────────────────────
    adx_dist = _adx_dist(adx_vals)
    log_fn(f"■ ADX 분포 (ADX 필터 직전 통과 종목, n={len(adx_vals)})")
    log_fn(f"  (이 구간 종목들이 ADX ≥ 25 통과 여부를 결정함)")
    for label, rate in adx_dist.items():
        bar = "█" * int(rate * 30)
        marker = "  ← ADX 20 완화 시 추가 획득" if label == "20~25" else ""
        log_fn(f"  ADX {label:>7}:  {rate*100:5.1f}%  {bar}{marker}")
    log_fn("")

    # ── 7. RS 분포 ────────────────────────────────────────────────────────────
    rs_dist = _rs_dist(rs_vals)
    log_fn(f"■ RS 분포 (RS 필터 직전 통과 종목, n={len(rs_vals)})")
    log_fn(f"  (이 구간 종목들이 RS ≥ 0.08 통과 여부를 결정함)")
    for label, rate in rs_dist.items():
        bar = "█" * int(rate * 30)
        marker = "  ← RS 0.05 완화 시 추가 획득" if label == "0.05~0.08" else ""
        log_fn(f"  RS {label:>10}:  {rate*100:5.1f}%  {bar}{marker}")
    log_fn("")

    # ── 8. 완화 시나리오 시뮬레이션 ──────────────────────────────────────────
    log_fn("■ 완화 시나리오 시뮬레이션 (게이트 무관, 일평균 통과 종목)")
    base_avg = sum(scen_daily['current']) / len(scen_daily['current']) if scen_daily['current'] else 0
    for scen_name, counts in scen_daily.items():
        if not counts:
            continue
        avg = sum(counts) / len(counts)
        diff = avg - base_avg
        diff_str = f"(+{diff:.1f})" if diff >= 0 else f"({diff:.1f})"
        label = SCENARIO_LABELS[scen_name]
        log_fn(f"  {label:<30}  평균 {avg:5.2f}종목/일  {diff_str}")
    log_fn("")

    log_fn(f"  소요 시간: {elapsed:.1f}s")
    log_fn(SEP)


# ─────────────────────────────────────────────────────────────────────────────
# 엔트리포인트
# ─────────────────────────────────────────────────────────────────────────────

def main(n_days: int = 60) -> None:
    out_path = ROOT / "experiments" / "results_filter_funnel.txt"
    output_lines: list[str] = []

    def log(msg: str = "") -> None:
        print(msg)
        output_lines.append(msg)

    t_total = time.time()

    # ── 1. 데이터 로드 ────────────────────────────────────────────────────────
    print(f"\n[1/3] 데이터 로드...")
    t0 = time.time()
    preloaded = load_backtest_data(BASE_PARAMS)
    all_dates: list[str] = preloaded['trading_dates']
    recent_dates = all_dates[-n_days:]
    print(f"  완료: {time.time() - t0:.1f}s  (전체 {len(all_dates)}거래일, 최근 {len(recent_dates)}일 분석)")

    # ── 2. 최신 Universe 쿼리 ────────────────────────────────────────────────
    # load_backtest_data 는 최초 거래일(2014-01-02)로 initial_universe를 빌드하는데,
    # 해당 시점 candle이 부족해 build_universe 가 0을 반환함.
    # market_cap_history 의 가장 최근 유효 날짜로 재쿼리한다.
    print(f"\n[2/4] 최신 Universe 쿼리...")
    t0 = time.time()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT MAX(date) as mx FROM market_cap_history WHERE date <= ?",
            (recent_dates[-1],),
        ).fetchone()
        mcap_date = row['mx'] if row and row['mx'] else recent_dates[-1]
        recent_universe = build_universe(mcap_date, conn)
    print(f"  완료: {time.time() - t0:.1f}s  (기준일 {mcap_date}, Universe {len(recent_universe)}종목)")

    # ── 3. precompute (최근 n_days) ───────────────────────────────────────────
    print(f"\n[3/4] 신호 사전계산 (최근 {n_days}거래일)...")
    t0 = time.time()

    precomputed = precompute_daily_signals(
        recent_dates,
        preloaded['ticker_data'],
        preloaded['ticker_date_idx'],
        recent_universe,           # ← 최신 universe 사용
        params=BASE_PARAMS,
        kospi_ret_map=preloaded.get('kospi_ret_map'),
        kosdaq_ret_map=preloaded.get('kosdaq_ret_map'),
        ticker_market=preloaded.get('ticker_market'),
        weights=WEIGHTS,
    )
    print(f"  완료: {time.time() - t0:.1f}s")

    # ── 4. 깔때기 분석 ────────────────────────────────────────────────────────
    print(f"\n[4/4] 필터 깔때기 분석...")
    t0 = time.time()
    result = run_funnel(preloaded, precomputed, n_days, recent_universe)
    print(f"  완료: {time.time() - t0:.1f}s")

    elapsed = time.time() - t_total
    print()
    print_report(result, n_days, elapsed, log)

    out_path.write_text("\n".join(output_lines), encoding="utf-8")
    print(f"\n결과 저장: {out_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="스크리닝 필터 깔때기 진단")
    parser.add_argument("--days", type=int, default=60, metavar="N",
                        help="분석 거래일 수 (기본 60)")
    args = parser.parse_args()
    main(n_days=args.days)
