"""주봉 MTF + MA5 필터 백테스트 — v2.7 기준 진입 차단 효과 검증.

일봉 데이터에서 주봉 캔들을 생성하고, 주봉 추세 필터 + MA5 진입 조건을
v2.7 기준선에 추가하여 WR/PF/MDD 개선 효과를 검증한다.

필터 변형 9개:
  [0] CURRENT_V27       : 기준선 (v2.7 그대로)
  [1] MA5_ALIGN         : close > MA5 > MA20 (5단계 정배열 포함)
  [2] MA5_ABOVE_MA20    : MA5 > MA20 (기존 3중 + MA5 조건 추가)
  [3] WMA10_40          : 주봉 MA10 > MA40
  [4] WMA10_20_40       : 주봉 MA10 > MA20 > MA40 (엄격)
  [5] CLOSE_ABOVE_WMA40 : 종가 > 주봉 MA40 (주봉 MA200 상방)
  [6] MA5_ALIGN+WMA10_40: [1] + [3]
  [7] MA5>20+WMA10_40   : [2] + [3]
  [8] MA5>20+>WMA40     : [2] + [5]

look-ahead 방지:
  주봉 캔들은 금요일 확정 후 다음 주 월요일부터 사용.
  구현: 주봉 index를 +1일(토요일)로 이동 후 일봉 날짜로 forward-fill.

실행:
    python experiments/experiment_weekly_mtf.py
결과:
    experiments/results_weekly_mtf.txt
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

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


# ─────────────────────────────────────────────────────────────────────────────
# v2.7 설정 (config.yaml에서 로드)
# ─────────────────────────────────────────────────────────────────────────────
with open(ROOT / "config.yaml", encoding="utf-8") as _f:
    _cfg = yaml.safe_load(_f)
_tf  = _cfg["trend_following"]
_trd = _cfg["trading"]
_rsk = _cfg["risk"]

CAPITAL  = 10_000_000
MAX_POS  = int(_trd["max_positions"])          # 5
MIN_AMT  = int(_rsk["min_position_amount"])    # 1,000,000
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
COST = CostModel()
_sm = _tf.get("slippage_model", {})
SLIP = SlippageParams(
    enabled=bool(_sm.get("enabled", True)),
    base_slippage=float(_sm.get("base_slippage", 0.0003)),
    impact_coefficient=float(_sm.get("impact_coefficient", 0.1)),
    max_slippage=float(_sm.get("max_slippage", 0.02)),
)
SECTOR  = SectorConstraint(enabled=False)
DYNHOLD = DynamicHoldParams(enabled=False)
SCALING = ScalingParams(enabled=False)


# ─────────────────────────────────────────────────────────────────────────────
# 주봉 유틸
# ─────────────────────────────────────────────────────────────────────────────

def daily_to_weekly(daily_df: pd.DataFrame) -> pd.DataFrame:
    """일봉 DataFrame → 주봉 DataFrame (금요일 기준).

    Args:
        daily_df: 'date' 컬럼(pd.Timestamp) + OHLCV 컬럼 포함 DataFrame.

    Returns:
        주봉 DataFrame (DatetimeIndex, 금요일 날짜).
    """
    dfi = daily_df.set_index("date")[["open", "high", "low", "close", "volume"]]
    weekly = dfi.resample("W-FRI").agg({
        "open":   "first",
        "high":   "max",
        "low":    "min",
        "close":  "last",
        "volume": "sum",
    }).dropna()
    return weekly


def compute_weekly_indicators(weekly_df: pd.DataFrame) -> pd.DataFrame:
    """주봉 MA 지표 계산."""
    df = weekly_df.copy()
    df["wma10"] = df["close"].rolling(10).mean()   # 10주선 (~50일)
    df["wma20"] = df["close"].rolling(20).mean()   # 20주선 (~100일)
    df["wma40"] = df["close"].rolling(40).mean()   # 40주선 (~200일)
    df["weekly_trend_up"]    = df["wma10"] > df["wma40"]
    df["weekly_ma_align"]    = (df["wma10"] > df["wma20"]) & (df["wma20"] > df["wma40"])
    df["close_above_wma40"]  = df["close"]  > df["wma40"]
    return df


def build_weekly_filter_map(ticker_data: dict) -> dict:
    """종목별 일별 주봉 추세 지표 맵 구성.

    look-ahead 방지: 금요일 종가 확정 후 다음 주 월요일부터 사용.
    구현: 주봉 index를 +1일(토요일)로 이동 후 일봉 날짜로 forward-fill.

    Returns:
        {ticker: {date_str: {
            'trend_up':          bool | None,  # WMA10 > WMA40
            'ma_align':          bool | None,  # WMA10 > WMA20 > WMA40
            'close_above_wma40': bool | None,  # close > WMA40
        }}}
    """
    cols = ["weekly_trend_up", "weekly_ma_align", "close_above_wma40"]
    weekly_map: dict = {}

    for ticker, df in ticker_data.items():
        if len(df) < 60 or "close" not in df.columns:
            weekly_map[ticker] = {}
            continue
        try:
            weekly    = daily_to_weekly(df)
            weekly_ind = compute_weekly_indicators(weekly)
            wf = weekly_ind[cols].copy()

            # look-ahead 방지: 금요일(index) → 다음 날(토요일)로 이동
            # ffill 시 금요일 당일은 직전 주봉 값 참조하게 됨
            wf.index = wf.index + pd.Timedelta(days=1)

            # 일봉 날짜 목록 (DatetimeIndex)
            daily_dates = df.set_index("date").index

            # 주봉 + 일봉 날짜 합쳐서 ffill → 일봉 날짜만 추출
            merged = (
                wf
                .reindex(wf.index.union(daily_dates))
                .sort_index()
                .ffill()
                .reindex(daily_dates)
            )

            # NaN → None 변환 후 date_str 키 dict 구성
            ticker_map: dict[str, dict] = {}
            for ts in merged.index:
                row = merged.loc[ts]
                ds  = str(ts.date())
                ticker_map[ds] = {
                    "trend_up":          (bool(row["weekly_trend_up"])   if pd.notna(row["weekly_trend_up"])    else None),
                    "ma_align":          (bool(row["weekly_ma_align"])   if pd.notna(row["weekly_ma_align"])    else None),
                    "close_above_wma40": (bool(row["close_above_wma40"]) if pd.notna(row["close_above_wma40"]) else None),
                }
            weekly_map[ticker] = ticker_map
        except Exception:
            weekly_map[ticker] = {}

    return weekly_map


# ─────────────────────────────────────────────────────────────────────────────
# 후처리 필터 적용
# ─────────────────────────────────────────────────────────────────────────────

def apply_extra_filters(
    base_precomp: dict,
    ticker_data: dict,
    ticker_date_idx: dict,
    weekly_map: dict,
    *,
    ma5_full_align: bool = False,     # close > MA5 AND MA5 > MA20 (엄격)
    ma5_above_ma20: bool = False,     # MA5 > MA20 만 (pullback 허용)
    weekly_trend_up: bool = False,    # 주봉 WMA10 > WMA40
    weekly_ma_align: bool = False,    # 주봉 WMA10 > WMA20 > WMA40
    close_above_wma40: bool = False,  # 종가 > 주봉 WMA40
) -> tuple[dict, list[dict]]:
    """base_precomp의 candidates에 추가 필터를 적용한다.

    기존 precompute_daily_signals 결과를 건드리지 않고 후처리로 진입 후보를 줄임.
    백테스터의 breadth/universe 등 나머지 구조는 그대로 유지.

    Args:
        base_precomp:    precompute_daily_signals() 결과.
        ticker_data:     preloaded['ticker_data'].
        ticker_date_idx: preloaded['ticker_date_idx']. {ticker: {pd.Timestamp: row_idx}}
        weekly_map:      build_weekly_filter_map() 결과.
        ma5_full_align:  close > MA5 AND MA5 > MA20 (5단계 정배열).
        ma5_above_ma20:  MA5 > MA20 만 체크.
        weekly_trend_up: 주봉 WMA10 > WMA40.
        weekly_ma_align: 주봉 정배열 (WMA10 > WMA20 > WMA40).
        close_above_wma40: 종가 > 주봉 WMA40.

    Returns:
        (filtered_precomp, blocked)
        filtered_precomp: candidates가 교체된 새 dict.
        blocked: [{'ticker', 'date', 'reason'}] — 차단된 candidate 목록.
    """
    need_ma5    = ma5_full_align or ma5_above_ma20
    need_weekly = weekly_trend_up or weekly_ma_align or close_above_wma40

    new_candidates: dict[str, list] = {}
    blocked: list[dict] = []

    for date_str, cands in base_precomp["candidates"].items():
        ts       = pd.Timestamp(date_str)
        new_cands: list[dict] = []

        for c in cands:
            ticker = c["ticker"]
            reason = None

            # ── MA5 필터 ──────────────────────────────────────────────────
            if need_ma5 and reason is None:
                idx_map = ticker_date_idx.get(ticker, {})
                row_idx = idx_map.get(ts)
                if row_idx is not None:
                    row    = ticker_data[ticker].iloc[row_idx]
                    ma5_v  = row.get("ma5")
                    ma20_v = row.get("ma20")
                    if pd.notna(ma5_v) and pd.notna(ma20_v):
                        m5, m20 = float(ma5_v), float(ma20_v)
                        if ma5_full_align:
                            # close > MA5 > MA20 (5단계 정배열)
                            if not (c["close"] > m5 and m5 > m20):
                                reason = "MA5_ALIGN"
                        elif ma5_above_ma20:
                            # MA5 > MA20 만 체크 (pullback 허용)
                            if m5 <= m20:
                                reason = "MA5>MA20"

            # ── 주봉 필터 ─────────────────────────────────────────────────
            if need_weekly and reason is None:
                wf = weekly_map.get(ticker, {}).get(date_str, {})
                if weekly_trend_up and wf.get("trend_up") is False:
                    reason = "WMA10≤WMA40"
                elif weekly_ma_align and wf.get("ma_align") is False:
                    reason = "WMA_ALIGN"
                elif close_above_wma40 and wf.get("close_above_wma40") is False:
                    reason = "CLOSE≤WMA40"

            if reason:
                blocked.append({"ticker": ticker, "date": date_str, "reason": reason})
            else:
                new_cands.append(c)

        new_candidates[date_str] = new_cands

    filtered_precomp = dict(base_precomp)
    filtered_precomp["candidates"] = new_candidates
    return filtered_precomp, blocked


# ─────────────────────────────────────────────────────────────────────────────
# 백테스트 + 분석 헬퍼
# ─────────────────────────────────────────────────────────────────────────────

def _run_bt(preloaded: dict, precomp: dict) -> object:
    return run_portfolio_backtest(
        initial_capital=CAPITAL,
        max_positions=MAX_POS,
        params=BASE_PARAMS,
        cost=COST,
        min_position_amount=MIN_AMT,
        preloaded_data=preloaded,
        precomputed=precomp,
        sizing_mode="equity",
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


def _blocked_trade_stats(baseline_result, variant_result) -> dict:
    """baseline에는 있지만 variant에는 없는 포지션의 PnL 분포.

    portfolio 동태 차이(새 포지션 진입) 때문에 정확한 인과 분석은 불가하나,
    '차단된 거래의 평균 질(수익/손실 비율)'을 근사한다.
    """
    base_pnl = _pos_pnl(baseline_result.trades)
    var_pnl  = _pos_pnl(variant_result.trades)

    blocked_pnls = [v for k, v in base_pnl.items() if k not in var_pnl]
    n = len(blocked_pnls)
    if n == 0:
        return {"n_blocked": 0, "win_rate": 0.0, "loss_rate": 0.0,
                "avg_pnl": 0.0, "total_pnl": 0.0}

    wins = sum(1 for p in blocked_pnls if p > 0)
    return {
        "n_blocked":  n,
        "win_rate":   wins / n,
        "loss_rate":  (n - wins) / n,
        "avg_pnl":    sum(blocked_pnls) / n,
        "total_pnl":  sum(blocked_pnls),
    }


# ─────────────────────────────────────────────────────────────────────────────
# 메인
# ─────────────────────────────────────────────────────────────────────────────

# 변형 정의: (label, ma5_full_align, ma5_above_ma20, weekly_trend_up, weekly_ma_align, close_above_wma40)
VARIANTS = [
    ("[0] CURRENT_V27",       False, False, False, False, False),
    ("[1] MA5_ALIGN",         True,  False, False, False, False),
    ("[2] MA5_ABOVE_MA20",    False, True,  False, False, False),
    ("[3] WMA10_40",          False, False, True,  False, False),
    ("[4] WMA10_20_40",       False, False, False, True,  False),
    ("[5] >WMA40",            False, False, False, False, True),
    ("[6] MA5+WMA10_40",      True,  False, True,  False, False),
    ("[7] MA5>20+WMA10_40",   False, True,  True,  False, False),
    ("[8] MA5>20+>WMA40",     False, True,  False, False, True),
]


def main():
    out_path = ROOT / "experiments" / "results_weekly_mtf.txt"
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
    print(f"  완료: {time.time() - t0:.1f}s  ({len(preloaded['ticker_data'])} 종목)")

    # ── 2. 기본 precompute (1회) ────────────────────────────────────────────
    print("[2/4] 신호 사전 계산...")
    t0 = time.time()
    base_precomp = precompute_daily_signals(
        preloaded["trading_dates"],
        preloaded["ticker_data"],
        preloaded["ticker_date_idx"],
        set(preloaded["initial_universe"]),
        params=BASE_PARAMS,
        kospi_ret_map=preloaded.get("kospi_ret_map"),
        kosdaq_ret_map=preloaded.get("kosdaq_ret_map"),
        ticker_market=preloaded.get("ticker_market"),
        weights=V27_WEIGHTS,
    )
    total_cands = sum(len(v) for v in base_precomp["candidates"].values())
    print(f"  완료: {time.time() - t0:.1f}s  (총 후보 {total_cands:,}건)")

    # ── 3. 주봉 필터 맵 구성 ──────────────────────────────────────────────
    print("[3/4] 주봉 지표 계산 (일봉 → 주봉 변환)...")
    t0 = time.time()
    weekly_map = build_weekly_filter_map(preloaded["ticker_data"])
    print(f"  완료: {time.time() - t0:.1f}s  ({len(weekly_map)} 종목 처리)")

    ticker_data     = preloaded["ticker_data"]
    ticker_date_idx = preloaded["ticker_date_idx"]

    # ── 4. 9개 변형 실행 ──────────────────────────────────────────────────
    print("[4/4] 백테스트 실행 (9개 변형)...")
    t0 = time.time()
    results: list[dict] = []

    for label, ma5_full, ma5_20, wt_up, wt_align, close_wma40 in VARIANTS:
        t1 = time.time()
        filtered_precomp, blocked_cands = apply_extra_filters(
            base_precomp, ticker_data, ticker_date_idx, weekly_map,
            ma5_full_align=ma5_full,
            ma5_above_ma20=ma5_20,
            weekly_trend_up=wt_up,
            weekly_ma_align=wt_align,
            close_above_wma40=close_wma40,
        )
        r = _run_bt(preloaded, filtered_precomp)
        elapsed = time.time() - t1
        results.append({
            "label":   label,
            "result":  r,
            "blocked": blocked_cands,
            "elapsed": elapsed,
        })
        cm = r.cagr_pct / r.max_drawdown_pct if r.max_drawdown_pct > 0 else 0
        print(f"  {label:<24} | 건수 {r.total_trades:4d} | "
              f"PF {r.profit_factor:.2f} | CAGR {r.cagr_pct*100:+.1f}% | "
              f"MDD -{r.max_drawdown_pct*100:.1f}% | {elapsed:.1f}s")

    print(f"  총 소요: {time.time() - t0:.1f}s")

    # ── 5. 보고서 작성 ────────────────────────────────────────────────────
    baseline = results[0]["result"]

    log()
    log(SEP)
    log("📋 주봉 MTF + MA5 필터 백테스트 (v2.7, 5종목, 2014~2026)")
    log(SEP)
    log()

    # ── 결과 비교 테이블 ───────────────────────────────────────────────────
    log("■ 결과 비교")
    hdr = f"{'변형':<25} {'건수':>5} {'WR':>7} {'PF':>6} {'CAGR':>7} {'MDD':>8} {'CAGR/MDD':>9}"
    log(hdr)
    log(SEP2)
    for item in results:
        r  = item["result"]
        lb = item["label"]
        cm = r.cagr_pct / r.max_drawdown_pct if r.max_drawdown_pct > 0 else 0
        log(
            f"{lb:<25} {r.total_trades:5d} {r.win_rate*100:6.1f}% "
            f"{r.profit_factor:6.2f} {r.cagr_pct*100:+6.1f}% "
            f"{-r.max_drawdown_pct*100:+7.1f}% {cm:9.2f}"
        )
    log()

    # ── MA5 필터 분석 ──────────────────────────────────────────────────────
    log("■ MA5 필터 분석")
    for idx in [1, 2]:
        item  = results[idx]
        label = item["label"]
        blocked_cands = item["blocked"]
        stats = _blocked_trade_stats(baseline, item["result"])
        reason_counts: dict[str, int] = {}
        for b in blocked_cands:
            reason_counts[b["reason"]] = reason_counts.get(b["reason"], 0) + 1
        log(f"  {label} 필터가 차단한 후보: {len(blocked_cands)}건")
        for reason, cnt in reason_counts.items():
            log(f"    차단 사유 [{reason}]: {cnt}건")
        log(f"    baseline 거래 중 차단된 포지션: {stats['n_blocked']}건")
        if stats["n_blocked"] > 0:
            log(f"    - 차단 포지션 손실 비율: {stats['loss_rate']*100:.1f}% (높으면 좋은 필터)")
            log(f"    - 차단 포지션 수익 비율: {stats['win_rate']*100:.1f}% (높으면 눌림목 진입 손실)")
            log(f"    - 차단 포지션 평균 PnL: {stats['avg_pnl']:,.0f}원")
            log(f"    - 차단 포지션 합산 PnL: {stats['total_pnl']:,.0f}원")
        log()

    # ── 주봉 필터 분석 ─────────────────────────────────────────────────────
    log("■ 주봉 필터 분석")
    for idx in [3, 4, 5]:
        item  = results[idx]
        label = item["label"]
        blocked_cands = item["blocked"]
        stats = _blocked_trade_stats(baseline, item["result"])
        log(f"  {label} 필터가 차단한 후보: {len(blocked_cands)}건")
        log(f"    baseline 거래 중 차단된 포지션: {stats['n_blocked']}건")
        if stats["n_blocked"] > 0:
            log(f"    - 차단 포지션 손실 비율: {stats['loss_rate']*100:.1f}%")
            log(f"    - 차단 포지션 평균 PnL: {stats['avg_pnl']:,.0f}원")
        log()

    # ── 핵심 질문 ──────────────────────────────────────────────────────────
    log("■ 핵심 질문")
    r0 = results[0]["result"]
    r1 = results[1]["result"]
    r2 = results[2]["result"]

    log("  Q1: MA5 필터가 WR을 개선하는가, 좋은 눌림목을 차단하는가?")
    d1 = (r1.win_rate - r0.win_rate) * 100
    d2 = (r2.win_rate - r0.win_rate) * 100
    log(f"      baseline WR {r0.win_rate*100:.1f}% → "
        f"[1] {r1.win_rate*100:.1f}% ({d1:+.1f}%p) | "
        f"[2] {r2.win_rate*100:.1f}% ({d2:+.1f}%p)")
    a1 = "WR 개선 (역추세 차단 효과)" if d1 > 0.5 else ("WR 저하 (눌림목 차단 부작용)" if d1 < -0.5 else "WR 변화 미미")
    a2 = "WR 개선 (역추세 차단 효과)" if d2 > 0.5 else ("WR 저하 (눌림목 차단 부작용)" if d2 < -0.5 else "WR 변화 미미")
    log(f"      → [1] {a1} | [2] {a2}")
    log()

    log("  Q2: 주봉 필터가 MDD를 줄이는가?")
    for idx in [3, 4, 5]:
        ri = results[idx]["result"]
        delta = ri.max_drawdown_pct - r0.max_drawdown_pct
        ans = "✅ 개선" if delta < -0.01 else ("❌ 악화" if delta > 0.01 else "변화 없음")
        log(f"      {VARIANTS[idx][0]}: MDD -{r0.max_drawdown_pct*100:.1f}% → "
            f"-{ri.max_drawdown_pct*100:.1f}% ({delta*100:+.1f}%p, {ans})")
    log()

    log("  Q3: MA5 + 주봉 결합이 단독보다 나은가?")
    cm0 = r0.cagr_pct / r0.max_drawdown_pct if r0.max_drawdown_pct > 0 else 0
    for idx in [6, 7, 8]:
        ri = results[idx]["result"]
        cm = ri.cagr_pct / ri.max_drawdown_pct if ri.max_drawdown_pct > 0 else 0
        ans = "✅ 개선" if cm > cm0 * 1.02 else ("❌ 저하" if cm < cm0 * 0.98 else "변화 없음")
        log(f"      {VARIANTS[idx][0]}: CAGR/MDD {cm0:.2f} → {cm:.2f} ({ans})")
    log()

    log("  Q4: 최적 조합의 CAGR/MDD는?")
    best_idx = max(
        range(len(results)),
        key=lambda i: results[i]["result"].cagr_pct / results[i]["result"].max_drawdown_pct
                      if results[i]["result"].max_drawdown_pct > 0 else 0
    )
    br = results[best_idx]["result"]
    bcm = br.cagr_pct / br.max_drawdown_pct if br.max_drawdown_pct > 0 else 0
    log(f"      최적: {VARIANTS[best_idx][0]}")
    log(f"      CAGR {br.cagr_pct*100:+.1f}% / MDD -{br.max_drawdown_pct*100:.1f}% "
        f"= {bcm:.2f} (baseline {cm0:.2f} 대비 {(bcm/cm0-1)*100:+.1f}%)")
    log()

    log(SEP)
    log(f"총 소요 시간: {time.time() - t_total:.1f}초")

    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n결과 저장: {out_path}")


if __name__ == "__main__":
    main()
