"""스코어러 통합 백테스트 — 이진 필터 vs 스코어 기반 진입.

가설: "완화 이진 필터 + 스코어 품질 판단"이 "엄격한 이진 필터"보다 PF/WR/CAGR에서 개선되는지 검증.

변형 8개:
  [0] V27_BINARY      : 현행 v2.7 (기준선)
  [1] SCORE_65        : 스코어 ≥ 65 (이진 필터 없음)
  [2] SCORE_60        : 스코어 ≥ 60
  [3] SCORE_70        : 스코어 ≥ 70
  [4] RELAX+65 ⭐     : ADX≥15 + MA20>MA60 + MACD>0 + 스코어 ≥ 65
  [5] RELAX+60        : ADX≥15 + MA20>MA60 + MACD>0 + 스코어 ≥ 60
  [6] RELAX+70        : ADX≥15 + MA20>MA60 + MACD>0 + 스코어 ≥ 70
  [7] V27+RANK        : v2.7 이진 필터 + 스코어로 랭킹만 교체

스코어 계산: Technical(60%) + Momentum(40%)
  Technical: 추세·RSI·MACD·거래량·BB·주봉 추세
  Momentum : 가격 모멘텀·상대강도·가속도

성능 최적화: 종목별 지표 시계열 전체 1회 계산 후 일별 값 읽기 (O(n) per ticker)

실행:
    python experiments/experiment_scorer_entry.py
결과:
    experiments/results_scorer_entry.txt
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from loguru import logger
logger.remove()
logger.add(sys.stderr, level="WARNING")

import yaml
from src.backtest.portfolio_backtester import (
    load_backtest_data,
    precompute_daily_signals,
    run_portfolio_backtest,
)
from src.strategy.trend_following_v2 import StrategyParams
from src.utils.cost_model import CostModel
from src.utils.slippage_model import SlippageParams
from src.strategy.ranking import RankingWeights
from src.strategy.sector_constraint import SectorConstraint
from src.strategy.dynamic_hold import DynamicHoldParams
from src.strategy.scaling import ScalingParams
from src.data_pipeline.db import get_connection

# ── 스코어러 import ────────────────────────────────────────────────────────────
from src.strategy.scorers import normalize_score, weighted_average
from src.strategy.scorers.indicators import sma, rsi, macd, adx, bollinger_bands


# ─────────────────────────────────────────────────────────────────────────────
# v2.7 설정
# ─────────────────────────────────────────────────────────────────────────────
with open(ROOT / "config.yaml", encoding="utf-8") as _f:
    _cfg = yaml.safe_load(_f)
_tf  = _cfg["trend_following"]
_trd = _cfg["trading"]
_rsk = _cfg["risk"]

CAPITAL  = 10_000_000
MAX_POS  = int(_trd["max_positions"])
MIN_AMT  = int(_rsk["min_position_amount"])
BREADTH  = 0.40

BASE_PARAMS = StrategyParams(
    adx_threshold=float(_tf["adx_threshold"]),
    relative_strength_threshold=float(_tf["relative_strength_threshold"]),
    stop_loss_atr=float(_tf["stop_loss_atr"]),
    take_profit_atr=float(_tf["take_profit_atr"]),
    trailing_atr=float(_tf["trailing_atr"]),
    max_hold_days=int(_tf["max_hold_days"]),
    tp1_sell_ratio=float(_tf["tp1_sell_ratio"]),
    tp2_atr=float(_tf["tp2_atr"]),
    tp2_sell_ratio=float(_tf["tp2_sell_ratio"]),
    ma60_position_min=float(_tf["ma60_position_min"]),
    ma60_position_max=float(_tf["ma60_position_max"]),
    atr_price_min=float(_tf["atr_price_min"]),
    atr_price_max=float(_tf["atr_price_max"]),
    min_trading_value=float(_tf["min_trading_value"]),
)
_rw = _tf.get("ranking_weights", {})
V27_WEIGHTS = RankingWeights(
    rs=float(_rw.get("rs", 0.50)),
    momentum_atr=float(_rw.get("momentum_atr", 0.20)),
    adx=float(_rw.get("adx", 0.15)),
    liquidity=float(_rw.get("liquidity", 0.10)),
    ma_alignment=float(_rw.get("ma_alignment", 0.05)),
)
COST   = CostModel()
_sm    = _tf.get("slippage_model", {})
SLIP   = SlippageParams(
    enabled=bool(_sm.get("enabled", True)),
    base_slippage=float(_sm.get("base_slippage", 0.0003)),
    impact_coefficient=float(_sm.get("impact_coefficient", 0.1)),
    max_slippage=float(_sm.get("max_slippage", 0.02)),
)
SECTOR  = SectorConstraint(enabled=False)
DYNHOLD = DynamicHoldParams(enabled=False)
SCALING = ScalingParams(enabled=False)


# ─────────────────────────────────────────────────────────────────────────────
# KOSPI 가격 로드
# ─────────────────────────────────────────────────────────────────────────────

def _load_kospi_prices() -> dict:
    """index_daily에서 KOSPI 종가 시계열 로드. {date_str: float}"""
    with get_connection() as conn:
        cursor = conn.execute(
            "SELECT date, close FROM index_daily WHERE index_code = 'KOSPI' ORDER BY date"
        )
        return {row["date"]: float(row["close"]) for row in cursor.fetchall()}


# ─────────────────────────────────────────────────────────────────────────────
# 스코어 사전 계산 (고속: 종목별 1회 계산)
# ─────────────────────────────────────────────────────────────────────────────

def precompute_all_scores(ticker_data: dict, kospi_price_map: dict) -> dict:
    """전체 종목·전체 날짜 스코어 사전 계산.

    종목별로 지표 시계열을 1회만 계산하고 일별 값을 읽는 방식으로 속도 최적화.
    Technical(60%) + Momentum(40%) 조합. 스코어러 소스 로직과 동일.

    Returns:
        {ticker: {date_str: composite_score (0~100)}}
    """
    all_scores: dict[str, dict[str, float]] = {}
    tickers = sorted(ticker_data.keys())
    n_total = len(tickers)

    for t_idx, ticker in enumerate(tickers):
        if t_idx % 30 == 0:
            print(f"    {t_idx:3d}/{n_total} 종목 스코어 계산 중...", end='\r', flush=True)

        df = ticker_data[ticker]
        n  = len(df)
        if n < 60:
            continue

        close  = df['close'].astype(float)
        high   = df['high'].astype(float)
        low    = df['low'].astype(float)
        volume = df['volume'].astype(float)

        # ── 일봉 지표 전체 계산 (1회) ──────────────────────────────────────
        ma5_s          = sma(close, 5)
        ma20_s         = sma(close, 20)
        ma60_s         = sma(close, 60)
        rsi_s          = rsi(close, 14)
        _, _, macd_hist_s = macd(close)
        adx_s          = adx(high, low, close, 14)
        bb_upper_s, _, bb_lower_s = bollinger_bands(close, 20)

        # numpy 배열로 변환 (인덱스 접근 고속화)
        close_a = close.values
        vol_a   = volume.values
        ma5_a   = ma5_s.values
        ma20_a  = ma20_s.values
        ma60_a  = ma60_s.values
        rsi_a   = rsi_s.values
        mh_a    = macd_hist_s.values
        adx_a   = adx_s.values
        bbu_a   = bb_upper_s.values
        bbl_a   = bb_lower_s.values

        # ── 주봉 지표 전체 계산 (1회) ──────────────────────────────────────
        dates      = df['date'].tolist()
        date_index = pd.to_datetime(dates)
        temp_close = pd.Series(close_a, index=date_index)
        wkly_close = temp_close.resample('W-FRI').last().dropna()
        w_ma5_a    = sma(wkly_close, 5).values
        w_ma10_a   = sma(wkly_close, 10).values
        wkly_vals  = wkly_close.values
        wkly_dates = wkly_close.index.values  # datetime64[ns]

        # 일별 날짜 → 가장 최근 완성 주봉 인덱스 (searchsorted, O(log n))
        daily64 = date_index.values
        wi_arr  = (
            np.searchsorted(wkly_dates, daily64 + np.timedelta64(1, 'D'), side='left') - 1
        )

        # KOSPI 가격 배열 (daily 날짜 기준 정렬)
        kospi_a = np.array(
            [kospi_price_map.get(d.strftime('%Y-%m-%d'), np.nan) for d in date_index],
            dtype=float,
        )

        ticker_scores: dict[str, float] = {}

        for i in range(60, n):
            d_ts     = date_index[i]
            date_str = d_ts.strftime('%Y-%m-%d')

            # ─── Technical: _score_trend ────────────────────────────────
            m5, m20, m60 = ma5_a[i], ma20_a[i], ma60_a[i]
            adx_v = adx_a[i]
            if np.isnan(m5) or np.isnan(m20) or np.isnan(m60):
                trend_sc = 50.0
            else:
                bullish  = int(m5 > m20) + int(m20 > m60) + int(m5 > m60)
                trend_sc = (0.0, 33.0, 67.0, 100.0)[bullish]
                if not np.isnan(adx_v):
                    if adx_v >= 25 and bullish >= 2:
                        trend_sc = min(100.0, trend_sc + 10.0)
                    elif adx_v < 20 and bullish <= 1:
                        trend_sc = max(0.0, trend_sc - 10.0)

            # ─── Technical: _score_rsi ──────────────────────────────────
            rv = rsi_a[i]
            if np.isnan(rv):
                rsi_sc = 50.0
            elif rv <= 30:
                rsi_sc = normalize_score(rv, 0.0, 30.0) * 0.3 + 70.0
            elif rv >= 70:
                rsi_sc = normalize_score(100.0 - rv, 0.0, 30.0) * 0.3
            else:
                rsi_sc = max(0.0, 100.0 - abs(rv - 55.0) * 2.5)

            # ─── Technical: _score_macd ─────────────────────────────────
            curr_mh = mh_a[i]
            prev_mh = mh_a[i - 1] if i > 0 else curr_mh
            if np.isnan(curr_mh) or np.isnan(prev_mh):
                macd_sc = 50.0
            elif curr_mh > 0 and curr_mh > prev_mh:   macd_sc = 80.0
            elif curr_mh > 0 and curr_mh <= prev_mh:  macd_sc = 60.0
            elif curr_mh > 0 and prev_mh <= 0:        macd_sc = 90.0
            elif curr_mh <= 0 and prev_mh > 0:        macd_sc = 15.0
            elif curr_mh < 0 and curr_mh > prev_mh:   macd_sc = 40.0
            else:                                       macd_sc = 20.0

            # ─── Technical: _score_volume ───────────────────────────────
            v5_avg  = float(np.mean(vol_a[max(0, i - 4):i + 1]))
            v20_avg = float(np.mean(vol_a[max(0, i - 19):i + 1]))
            price_up = close_a[i] > close_a[max(0, i - 3)]
            if v20_avg == 0:
                vol_sc = 50.0
            else:
                vr = v5_avg / v20_avg
                if price_up:
                    vol_sc = (90.0 if vr >= 1.5 else 75.0 if vr >= 1.2
                              else 60.0 if vr >= 0.8 else 45.0)
                else:
                    vol_sc = (15.0 if vr >= 1.5 else 25.0 if vr >= 1.2
                              else 40.0 if vr >= 0.8 else 55.0)

            # ─── Technical: _score_bollinger ────────────────────────────
            ub_v = bbu_a[i]
            lb_v = bbl_a[i]
            c_v  = close_a[i]
            if np.isnan(ub_v) or np.isnan(lb_v) or (ub_v - lb_v) == 0:
                bb_sc = 50.0
            else:
                pct_b = (c_v - lb_v) / (ub_v - lb_v)
                if pct_b <= 0.2:
                    bb_sc = 80.0 + (0.2 - pct_b) * 50.0
                elif pct_b >= 0.8:
                    bb_sc = max(0.0, 30.0 - (pct_b - 0.8) * 100.0)
                elif 0.4 <= pct_b <= 0.6:
                    bb_sc = 65.0
                elif pct_b < 0.4:
                    bb_sc = normalize_score(pct_b, 0.2, 0.4) * 25.0 + 55.0
                else:
                    bb_sc = max(30.0, 65.0 - (pct_b - 0.6) * 175.0)

            # ─── Technical: _score_weekly_trend ─────────────────────────
            wi = int(wi_arr[i])
            if wi < 9 or wi >= len(w_ma5_a):
                wkly_sc = 50.0
            else:
                wc5  = w_ma5_a[wi]
                wc10 = w_ma10_a[wi]
                cwc  = wkly_vals[wi]
                if np.isnan(wc5) or np.isnan(wc10):
                    wkly_sc = 50.0
                elif cwc > wc5 > wc10:  wkly_sc = 85.0
                elif cwc > wc5:          wkly_sc = 65.0
                elif wc5 > wc10:         wkly_sc = 55.0
                elif cwc < wc5 < wc10:  wkly_sc = 20.0
                else:                    wkly_sc = 40.0

            tech_sc = weighted_average(
                [trend_sc, rsi_sc, macd_sc, vol_sc, bb_sc, wkly_sc],
                [0.25,     0.15,   0.20,    0.10,   0.15,  0.15],
            )

            # ─── Momentum: _score_price_momentum ────────────────────────
            def _pct(pos: int, days: int):
                if pos < days: return None
                s_, e_ = close_a[pos - days], close_a[pos]
                return (e_ - s_) / s_ * 100.0 if s_ != 0 else None

            parts, ws = [], []
            r20 = _pct(i, 20)
            if r20 is not None: parts.append(normalize_score(r20, -20., 30.)); ws.append(0.40)
            r60 = _pct(i, 60)
            if r60 is not None: parts.append(normalize_score(r60, -25., 40.)); ws.append(0.35)
            r120 = _pct(i, 120)
            if r120 is not None: parts.append(normalize_score(r120, -30., 50.)); ws.append(0.25)
            pm_sc = weighted_average(parts, ws) if parts else 50.0

            # ─── Momentum: _score_relative_strength ─────────────────────
            if r60 is not None:
                k_slice = kospi_a[max(0, i - 60):i + 1]
                k_valid = k_slice[~np.isnan(k_slice)]
                if len(k_valid) > 60:
                    k60_s, k60_e = k_valid[-61], k_valid[-1]
                    k_r60 = (k60_e - k60_s) / k60_s * 100.0 if k60_s != 0 else None
                    rs_sc = (normalize_score(r60 - k_r60, -20., 20.) if k_r60 is not None
                             else normalize_score(r60, -25., 40.))
                else:
                    rs_sc = normalize_score(r60, -25., 40.)
            else:
                rs_sc = 50.0

            # ─── Momentum: _score_acceleration ──────────────────────────
            if i >= 40:
                rs_i = close_a[i - 20] if i >= 20 else close_a[0]
                re_v = close_a[i]
                ps_i = close_a[i - 40]
                pe_v = close_a[i - 20] if i >= 20 else close_a[0]
                if rs_i != 0 and ps_i != 0:
                    accel_sc = normalize_score(
                        (re_v - rs_i) / rs_i * 100.0 - (pe_v - ps_i) / ps_i * 100.0,
                        -15., 15.,
                    )
                else:
                    accel_sc = 50.0
            else:
                accel_sc = 50.0

            mom_sc = weighted_average([pm_sc, rs_sc, accel_sc], [0.40, 0.35, 0.25])
            ticker_scores[date_str] = weighted_average([tech_sc, mom_sc], [0.60, 0.40])

        all_scores[ticker] = ticker_scores

    print(f"    {n_total}/{n_total} 종목 완료                ")
    return all_scores


# ─────────────────────────────────────────────────────────────────────────────
# 변형별 precomp 구성
# ─────────────────────────────────────────────────────────────────────────────

def _build_scored_precomp(
    precomp_base: dict,
    all_scores: dict,
    ticker_data: dict,
    ticker_date_idx: dict,
    kospi_ret_map: dict,
    kosdaq_ret_map: dict,
    ticker_market: dict,
    params: StrategyParams,
    mode: str,           # 'v27_score_rank' | 'score_only' | 'relaxed_binary'
    threshold: float = 65.0,
    relax_adx: float = 15.0,
) -> dict:
    """score 기반 precomp dict 구성."""
    ticker_industry = precomp_base.get('ticker_industry', {})
    min_tv = int(params.min_trading_value)

    if mode == 'v27_score_rank':
        # v2.7 이진 필터 통과 후보를 스코어로 재정렬
        new_cands = {}
        for date_str, cands in precomp_base['candidates'].items():
            updated = []
            for c in cands:
                s = all_scores.get(c['ticker'], {}).get(date_str)
                c2 = dict(c)
                c2['score'] = s if s is not None else c['score']
                updated.append(c2)
            updated.sort(key=lambda x: x['score'], reverse=True)
            new_cands[date_str] = updated
        r = dict(precomp_base)
        r['candidates'] = new_cands
        return r

    # score_only / relaxed_binary: universe에서 직접 필터 + 스코어링
    universe_by_date = precomp_base['universe_at']
    new_cands = {}

    for date_str, universe in universe_by_date.items():
        ts   = pd.Timestamp(date_str)
        cands = []

        for ticker in universe:
            idx_map = ticker_date_idx.get(ticker, {})
            row_idx = idx_map.get(ts)
            if row_idx is None:
                continue

            row     = ticker_data[ticker].iloc[row_idx]
            close_v = row.get('close', 0)
            if pd.isna(close_v) or float(close_v) <= 0:
                continue

            # 최소 거래대금 (공통 필터)
            tv = row.get('avg_trading_value_20', 0)
            if pd.isna(tv) or float(tv) < min_tv:
                continue

            # 완화 이진 필터 (relaxed_binary만 적용)
            if mode == 'relaxed_binary':
                adx_v = row.get('adx', 0)
                if pd.isna(adx_v) or float(adx_v) < relax_adx:
                    continue
                ma20_v = row.get('ma20', 0)
                ma60_v = row.get('ma60', 0)
                if pd.isna(ma20_v) or pd.isna(ma60_v) or float(ma20_v) <= float(ma60_v):
                    continue
                mh = row.get('macd_hist', 0)
                if pd.isna(mh) or float(mh) <= 0:
                    continue

            # 스코어 임계값
            s = all_scores.get(ticker, {}).get(date_str)
            if s is None or s < threshold:
                continue

            # 후보 dict 구성
            c_v   = float(close_v)
            atr_v = float(row.get('atr', 0)) if not pd.isna(row.get('atr', np.nan)) else 0.0
            atr_ratio = atr_v / c_v if c_v > 0 else 0.0

            mkt       = ticker_market.get(ticker, 'KOSPI')
            bench_ret = kosdaq_ret_map.get(ts) if mkt == 'KOSDAQ' else kospi_ret_map.get(ts)
            stk_ret   = float(row.get('stock_ret_n', 0)) if not pd.isna(row.get('stock_ret_n', np.nan)) else 0.0
            rs_val    = (stk_ret - float(bench_ret)
                         if bench_ret is not None and not pd.isna(bench_ret) else 0.0)
            ma60_dist = row.get('ma60_dist', 0)

            cands.append({
                'ticker':              ticker,
                'code':                ticker,
                'score':               s,
                'close':               c_v,
                'atr':                 atr_v,
                'atr_ratio':           atr_ratio,
                'ma60_dist':           float(ma60_dist) if not pd.isna(ma60_dist) else 0.0,
                'adx':                 float(row.get('adx', 0)) if not pd.isna(row.get('adx', np.nan)) else 0.0,
                'rs':                  rs_val,
                'stock_ret_n':         stk_ret,
                'avg_trading_value_20': float(tv),
                'industry':            ticker_industry.get(ticker, 'UNKNOWN'),
            })

        cands.sort(key=lambda x: x['score'], reverse=True)
        new_cands[date_str] = cands

    r = dict(precomp_base)
    r['candidates'] = new_cands
    return r


# ─────────────────────────────────────────────────────────────────────────────
# 백테스트 헬퍼
# ─────────────────────────────────────────────────────────────────────────────

def _run_bt(preloaded: dict, precomp: dict):
    return run_portfolio_backtest(
        initial_capital=CAPITAL,
        max_positions=MAX_POS,
        params=BASE_PARAMS,
        cost=COST,
        min_position_amount=MIN_AMT,
        preloaded_data=preloaded,
        precomputed=precomp,
        sizing_mode='equity',
        breadth_gate_threshold=BREADTH,
        regime_gate_enabled=True,
        sector_constraint=SECTOR,
        dynamic_hold=DYNHOLD,
        scaling=SCALING,
        slippage_params=SLIP,
    )


def _pos_pnl(trades: list) -> dict:
    """(ticker, entry_date) → 포지션 총 pnl_amount."""
    pnl: dict = {}
    for t in trades:
        k = (t.ticker, t.entry_date)
        pnl[k] = pnl.get(k, 0.0) + t.pnl_amount
    return pnl


# ─────────────────────────────────────────────────────────────────────────────
# 메인
# ─────────────────────────────────────────────────────────────────────────────

def main():
    out_path = ROOT / "experiments" / "results_scorer_entry.txt"
    SEP  = "═" * 70
    SEP2 = "─" * 70

    lines: list[str] = []

    def log(msg: str = "") -> None:
        print(msg)
        lines.append(msg)

    t_total = time.time()

    # ── 1. 데이터 로드 ─────────────────────────────────────────────────────
    print("[1/4] 데이터 로드...")
    t0 = time.time()
    preloaded = load_backtest_data(BASE_PARAMS)
    ticker_data     = preloaded['ticker_data']
    ticker_date_idx = preloaded['ticker_date_idx']
    kospi_ret_map   = preloaded.get('kospi_ret_map', {})
    kosdaq_ret_map  = preloaded.get('kosdaq_ret_map', {})
    ticker_market   = preloaded.get('ticker_market', {})
    print(f"  완료: {time.time() - t0:.1f}s  ({len(ticker_data)} 종목)")

    # ── 2. v2.7 기준 precompute ────────────────────────────────────────────
    print("[2/4] v2.7 신호 사전 계산...")
    t0 = time.time()
    precomp_base = precompute_daily_signals(
        preloaded["trading_dates"],
        ticker_data,
        ticker_date_idx,
        set(preloaded["initial_universe"]),
        params=BASE_PARAMS,
        kospi_ret_map=kospi_ret_map,
        kosdaq_ret_map=kosdaq_ret_map,
        ticker_market=ticker_market,
        weights=V27_WEIGHTS,
    )
    total_cands = sum(len(v) for v in precomp_base["candidates"].values())
    print(f"  완료: {time.time() - t0:.1f}s  (총 후보 {total_cands:,}건)")

    # ── 3. 스코어 사전 계산 ────────────────────────────────────────────────
    print("[3/4] 스코어 사전 계산 (Technical+Momentum)...")
    t0 = time.time()
    kospi_price_map = _load_kospi_prices()
    all_scores = precompute_all_scores(ticker_data, kospi_price_map)
    n_scored = sum(len(v) for v in all_scores.values())
    print(f"  완료: {time.time() - t0:.1f}s  ({n_scored:,} (종목,날짜) 쌍)")

    # ── 4. 8개 변형 구성 + 백테스트 ──────────────────────────────────────
    print("[4/4] 백테스트 실행 (8개 변형)...")
    t0 = time.time()

    VARIANTS = [
        ("[0] V27_BINARY",   None,              0.0 ),
        ("[1] SCORE_65",     'score_only',      65.0),
        ("[2] SCORE_60",     'score_only',      60.0),
        ("[3] SCORE_70",     'score_only',      70.0),
        ("[4] RELAX+65",     'relaxed_binary',  65.0),
        ("[5] RELAX+60",     'relaxed_binary',  60.0),
        ("[6] RELAX+70",     'relaxed_binary',  70.0),
        ("[7] V27+RANK",     'v27_score_rank',  0.0 ),
    ]

    results: list[dict] = []
    for label, mode, threshold in VARIANTS:
        t1 = time.time()
        if mode is None:
            precomp = precomp_base
        else:
            precomp = _build_scored_precomp(
                precomp_base, all_scores, ticker_data, ticker_date_idx,
                kospi_ret_map, kosdaq_ret_map, ticker_market,
                BASE_PARAMS, mode, threshold,
            )
        r = _run_bt(preloaded, precomp)
        elapsed = time.time() - t1
        results.append({'label': label, 'result': r, 'mode': mode,
                        'threshold': threshold, 'elapsed': elapsed})
        util = r.avg_positions / MAX_POS * 100
        cm   = r.cagr_pct / r.max_drawdown_pct if r.max_drawdown_pct > 0 else 0
        print(f"  {label:<16} | 건수 {r.total_trades:4d} | "
              f"PF {r.profit_factor:.2f} | CAGR {r.cagr_pct*100:+.1f}% | "
              f"MDD -{r.max_drawdown_pct*100:.1f}% | 활용 {util:.0f}% | {elapsed:.1f}s")

    print(f"  총 소요: {time.time() - t0:.1f}s")

    # ═══════════════════════════════════════════════════════════════════════
    # 보고서 작성
    # ═══════════════════════════════════════════════════════════════════════
    log()
    log(SEP)
    log("📋 스코어러 통합 백테스트 (10M/5종목, 2014~2026)")
    log(SEP)
    log()

    # ── 결과 비교 테이블 ────────────────────────────────────────────────────
    log("■ 결과 비교")
    hdr = (f"{'변형':<18} {'건수':>5} {'WR':>7} {'PF':>6} {'CAGR':>7} "
           f"{'MDD':>8} {'활용':>6} {'CAGR/MDD':>9}")
    log(hdr)
    log(SEP2)
    for item in results:
        r  = item['result']
        lb = item['label']
        util = r.avg_positions / MAX_POS * 100
        cm   = r.cagr_pct / r.max_drawdown_pct if r.max_drawdown_pct > 0 else 0
        star = " ⭐" if item['mode'] == 'relaxed_binary' and item['threshold'] == 65.0 else ""
        log(
            f"{lb:<18}{star:<3} {r.total_trades:5d} {r.win_rate*100:6.1f}% "
            f"{r.profit_factor:6.2f} {r.cagr_pct*100:+6.1f}% "
            f"{-r.max_drawdown_pct*100:+7.1f}% {util:5.0f}% {cm:9.2f}"
        )
    log()

    # ── 스코어 분포 분석 ─────────────────────────────────────────────────────
    log("■ 스코어 분포 분석 (v2.7 이진 필터 통과 vs 탈락)")
    pass_scores: list[float] = []
    fail_scores: list[float] = []
    sample_dates = list(precomp_base['candidates'].keys())
    step = max(1, len(sample_dates) // 500)  # 500일치 샘플링
    for date_str in sample_dates[::step]:
        pass_set = {c['ticker'] for c in precomp_base['candidates'].get(date_str, [])}
        universe = precomp_base['universe_at'].get(date_str, set())
        for ticker in universe:
            s = all_scores.get(ticker, {}).get(date_str)
            if s is None:
                continue
            if ticker in pass_set:
                pass_scores.append(s)
            else:
                fail_scores.append(s)

    avg_pass = sum(pass_scores) / len(pass_scores) if pass_scores else 0
    avg_fail = sum(fail_scores) / len(fail_scores) if fail_scores else 0
    log(f"  v2.7 이진 필터 통과 종목 평균 스코어: {avg_pass:.1f}점  (샘플 {len(pass_scores):,}건)")
    log(f"  v2.7 이진 필터 탈락 종목 평균 스코어: {avg_fail:.1f}점  (샘플 {len(fail_scores):,}건)")
    diff = avg_pass - avg_fail
    if abs(diff) >= 5:
        log(f"  → 차이 {diff:+.1f}점: 스코어러가 이진 필터와 방향 정렬됨 (필터가 좋은 종목을 고름)")
    elif abs(diff) >= 2:
        log(f"  → 차이 {diff:+.1f}점: 약한 정렬 (스코어러가 일부 다른 관점 제공)")
    else:
        log(f"  → 차이 {diff:+.1f}점: 미미 — 스코어러가 이진 필터와 독립적 관점 제공")
    log()

    # ── 추가 분석: 스코어만의 진입 vs 이진 필터만의 진입 ───────────────────
    log("■ 추가 분석: 스코어만의 진입 vs 이진 필터만의 진입")
    v27_pnl    = _pos_pnl(results[0]['result'].trades)
    relax65_pnl = _pos_pnl(results[4]['result'].trades)

    # v2.7 이진 필터 통과 거래 중 스코어 < 65
    v27_low_sc, v27_hi_sc = [], []
    for (ticker, entry_date), pnl in v27_pnl.items():
        s = all_scores.get(ticker, {}).get(entry_date)
        if s is None:
            continue
        (v27_hi_sc if s >= 65 else v27_low_sc).append(pnl)

    log(f"  이진 필터 통과 + 스코어 ≥ 65: {len(v27_hi_sc):3d}건 "
        f"→ 평균 PnL {sum(v27_hi_sc)/len(v27_hi_sc):+,.0f}원  "
        f"WR {sum(1 for x in v27_hi_sc if x>0)/len(v27_hi_sc)*100:.1f}%"
        if v27_hi_sc else "  이진 필터 통과 + 스코어 ≥ 65: 0건")
    log(f"  이진 필터 통과 + 스코어 < 65: {len(v27_low_sc):3d}건 "
        f"→ 평균 PnL {sum(v27_low_sc)/len(v27_low_sc):+,.0f}원  "
        f"WR {sum(1 for x in v27_low_sc if x>0)/len(v27_low_sc)*100:.1f}%"
        if v27_low_sc else "  이진 필터 통과 + 스코어 < 65: 0건")

    # RELAX+65 에서 v2.7에는 없는 신규 진입
    v27_pos_set = set(v27_pnl.keys())
    new_trades  = {k: v for k, v in relax65_pnl.items() if k not in v27_pos_set}
    if new_trades:
        nv = list(new_trades.values())
        log(f"  RELAX+65 신규 진입 (v2.7 미진입): {len(nv):3d}건 "
            f"→ 평균 PnL {sum(nv)/len(nv):+,.0f}원  "
            f"WR {sum(1 for x in nv if x>0)/len(nv)*100:.1f}%")
    else:
        log("  RELAX+65 신규 진입: 0건 (모두 v2.7과 겹침)")
    log("  → 스코어 < 65 이진 통과 거래의 평균 PnL이 낮으면 스코어 필터가 유효")
    log()

    # ── 핵심 질문 ────────────────────────────────────────────────────────────
    log("■ 핵심 질문")
    r0 = results[0]['result']
    r1 = results[1]['result']
    r4 = results[4]['result']
    r7 = results[7]['result']

    log("  Q1: 스코어 ≥ 65가 이진 필터보다 나은 선별을 하는가?")
    d_wr = (r1.win_rate - r0.win_rate) * 100
    d_pf = r1.profit_factor - r0.profit_factor
    log(f"      WR {r0.win_rate*100:.1f}% → {r1.win_rate*100:.1f}% ({d_wr:+.1f}%p), "
        f"PF {r0.profit_factor:.2f} → {r1.profit_factor:.2f} ({d_pf:+.2f})")
    log(f"      → {'스코어 단독이 이진 필터보다 낫다' if d_pf >= 0 else '이진 필터가 더 좋은 선별'}")
    log()

    log("  Q2: 완화 이진 + 스코어가 건수를 늘리면서 PF를 유지하는가?")
    d_cnt = r4.total_trades - r0.total_trades
    d_pf4 = r4.profit_factor - r0.profit_factor
    log(f"      건수: {r0.total_trades} → {r4.total_trades} ({d_cnt:+d}), "
        f"PF: {r0.profit_factor:.2f} → {r4.profit_factor:.2f} ({d_pf4:+.2f})")
    log(f"      → {'건수 증가 + PF 유지/개선' if d_cnt > 0 and d_pf4 >= -0.05 else 'PF 하락 또는 건수 미증가'}")
    log()

    log("  Q3: 자본활용이 40%에서 개선되는가?")
    log(f"      [0] V27_BINARY: {r0.avg_positions/MAX_POS*100:.0f}%  "
        f"[4] RELAX+65: {r4.avg_positions/MAX_POS*100:.0f}%  "
        f"[1] SCORE_65: {r1.avg_positions/MAX_POS*100:.0f}%")
    best_util = max(results, key=lambda x: x['result'].avg_positions)
    log(f"      최고 활용: {best_util['label']} "
        f"({best_util['result'].avg_positions/MAX_POS*100:.0f}%)")
    log()

    log("  Q4: 가장 높은 CAGR/MDD는 어떤 변형인가?")
    best_cm = max(results, key=lambda x: (
        x['result'].cagr_pct / x['result'].max_drawdown_pct
        if x['result'].max_drawdown_pct > 0 else 0
    ))
    r_best = best_cm['result']
    cm_best = r_best.cagr_pct / r_best.max_drawdown_pct
    cm_base = r0.cagr_pct / r0.max_drawdown_pct
    log(f"      최적: {best_cm['label']}")
    log(f"      CAGR {r_best.cagr_pct*100:+.1f}% / MDD {r_best.max_drawdown_pct*100:.1f}% "
        f"= {cm_best:.2f} (baseline {cm_base:.2f} 대비 {cm_best-cm_base:+.2f})")
    log()

    log(SEP)
    elapsed_total = time.time() - t_total
    log(f"총 소요 시간: {elapsed_total:.1f}초")

    # ── 파일 저장 ─────────────────────────────────────────────────────────────
    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n결과 저장: {out_path}")


if __name__ == "__main__":
    main()
