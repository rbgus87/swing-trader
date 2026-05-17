"""v2.7 + 유휴 현금 ETF 평균회귀 통합 백테스트.

v2.7 주식 포트폴리오의 유휴 현금을 KOSPI 지수 평균회귀(IBS / BB)에
자동 배치하는 통합 시뮬레이터.

원칙:
  - v2.7이 항상 최우선: 주식 진입 시 현금 부족 → ETF 강제 청산
  - ETF는 유휴 현금 전액 투입 (최소 100만원)
  - ETF 비용 0.03% 왕복 (거래세 면제)

실행:
    python experiments/experiment_integrated_etf.py
"""
from __future__ import annotations

import math
import os
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from loguru import logger
logger.remove()
logger.add(sys.stderr, level="WARNING")

import yaml
from src.backtest.portfolio_backtester import (
    Position, PortfolioTradeResult,
    load_backtest_data, precompute_daily_signals,
    BREADTH_GATE_THRESHOLD,
)
from src.strategy.exit_evaluator import ExitContext, ExitParams, evaluate_exit
from src.models import ExitReason
from src.utils.cost_model import CostModel
from src.utils.tick_size import adjust_price
from src.utils.slippage_model import SlippageParams
from src.strategy.sector_constraint import SectorConstraint
from src.strategy.dynamic_hold import DynamicHoldParams
from src.strategy.scaling import ScalingParams, compute_first_entry_qty
from src.strategy.trend_following_v2 import StrategyParams
from src.strategy.ranking import RankingWeights


# ─── v2.7 파라미터 (config.yaml) ─────────────────────────────────────────────
with open(os.path.join(ROOT, "config.yaml"), encoding="utf-8") as _f:
    _cfg = yaml.safe_load(_f)
_tf  = _cfg["trend_following"]
_trd = _cfg["trading"]
_rsk = _cfg["risk"]

INITIAL_CAPITAL   = int(_trd["initial_capital"])        # 10,000,000
MAX_POSITIONS     = int(_trd["max_positions"])          # 5
MIN_AMOUNT        = int(_rsk["min_position_amount"])    # 1,000,000
BREADTH_GATE      = 0.40

V27_PARAMS = StrategyParams(
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
V27_COST = CostModel()
_sm = _tf.get("slippage_model", {})
V27_SLIP = SlippageParams(
    enabled=bool(_sm.get("enabled", True)),
    base_slippage=float(_sm.get("base_slippage", 0.0003)),
    impact_coefficient=float(_sm.get("impact_coefficient", 0.1)),
    max_slippage=float(_sm.get("max_slippage", 0.02)),
)
V27_SECTOR  = SectorConstraint(enabled=False)
V27_DYNHOLD = DynamicHoldParams(enabled=False)
V27_SCALING = ScalingParams(enabled=False)

# ─── ETF 상수 ────────────────────────────────────────────────────────────────
DB_PATH = os.path.join(ROOT, "swing_data.db")
ETF_COST = 0.0003         # 왕복 0.03%
ETF_MIN_ALLOC = 1_000_000 # 최소 진입액 100만원
SEP = "=" * 70


# ─── KOSPI ETF 지표 계산 ─────────────────────────────────────────────────────
def load_kospi_signals() -> dict[str, dict]:
    """KOSPI 일봉 로드 + IBS/BB/MA200 지표 계산. {date_str: row_dict} 반환."""
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql(
        "SELECT date, open, high, low, close FROM index_daily "
        "WHERE index_code='KOSPI' ORDER BY date",
        conn,
    )
    conn.close()

    sma20 = df["close"].rolling(20).mean()
    std20 = df["close"].rolling(20).std(ddof=1)
    df["bb_lower"] = sma20 - 2 * std20
    df["bb_sma20"] = sma20
    df["ma200"] = df["close"].rolling(200).mean()

    rng = df["high"] - df["low"]
    df["ibs"] = (df["close"] - df["low"]) / rng.where(rng > 0, np.nan)

    result: dict[str, dict] = {}
    for _, row in df.iterrows():
        result[str(row["date"])] = {
            "open":     float(row["open"]),
            "high":     float(row["high"]),
            "low":      float(row["low"]),
            "close":    float(row["close"]),
            "bb_lower": float(row["bb_lower"]) if not pd.isna(row["bb_lower"]) else None,
            "bb_sma20": float(row["bb_sma20"]) if not pd.isna(row["bb_sma20"]) else None,
            "ma200":    float(row["ma200"])    if not pd.isna(row["ma200"])    else None,
            "ibs":      float(row["ibs"])      if not pd.isna(row["ibs"])      else None,
        }
    return result


# ─── ETF 시그널 ──────────────────────────────────────────────────────────────
def check_etf_entry(
    kr: dict,          # 당일 KOSPI row
    strategy: str,     # 'ibs', 'bb', 'ibs_bb'
    use_ma200: bool,
) -> Optional[str]:
    """ETF 진입 시그널 체크. 발동 시 signal 문자열 반환, 아니면 None."""
    if use_ma200:
        ma200 = kr.get("ma200")
        if ma200 is not None and kr["close"] < ma200:
            return None

    if strategy in ("ibs", "ibs_bb", "ibs_ma200"):
        ibs = kr.get("ibs")
        if ibs is not None and ibs < 0.1:
            return "ibs"

    if strategy in ("bb", "ibs_bb"):
        lb = kr.get("bb_lower")
        if lb is not None and kr["close"] < lb:
            return "bb"

    return None


def check_etf_exit(kr: dict, signal: str) -> bool:
    """ETF 청산 시그널 체크."""
    if signal == "ibs":
        ibs = kr.get("ibs")
        return ibs is not None and ibs > 0.8
    if signal == "bb":
        sma20 = kr.get("bb_sma20")
        return sma20 is not None and kr["close"] > sma20
    return True  # 알 수 없는 시그널 → 즉시 청산


# ─── ETF 포지션 ──────────────────────────────────────────────────────────────
@dataclass
class EtfPosition:
    entry_date: str
    entry_kospi: float    # 진입 시 KOSPI 종가
    alloc: float          # 투자 원금 (원)
    signal: str           # 'ibs' or 'bb'
    hold_days: int = 0


@dataclass
class EtfTrade:
    entry_date: str
    exit_date: str
    entry_kospi: float
    exit_kospi: float
    alloc: float
    gross_ret: float
    net_ret: float
    pnl: float
    hold_days: int
    signal: str
    forced: bool = False  # v2.7 진입을 위한 강제 청산


# ─── 통합 백테스트 ──────────────────────────────────────────────────────────
def run_integrated(
    preloaded: dict,
    precomputed: dict,
    kospi_sigs: dict[str, dict],
    etf_strategy: str,       # 'none', 'ibs', 'bb', 'ibs_bb'
    use_ma200: bool = False,
    initial_capital: float = INITIAL_CAPITAL,
    max_positions: int = MAX_POSITIONS,
    min_position_amount: float = MIN_AMOUNT,
    params: StrategyParams = V27_PARAMS,
    cost: CostModel = V27_COST,
    slip: SlippageParams = V27_SLIP,
    etf_cost: float = ETF_COST,
    etf_min_alloc: float = ETF_MIN_ALLOC,
) -> dict:
    """v2.7 + ETF 통합 시뮬레이터.

    v2.7 로직은 portfolio_backtester.run_portfolio_backtest와 동일.
    ETF 오버레이를 세 지점에 삽입:
      (A) 주식 청산 후 → ETF 자연 청산
      (B) 주식 진입 직전 → 현금 부족 시 ETF 강제 청산
      (C) 주식 진입 후 → 유휴 현금으로 ETF 진입
    """
    trading_dates: list[str] = preloaded["trading_dates"]
    ticker_data = preloaded["ticker_data"]
    ticker_date_idx = preloaded["ticker_date_idx"]
    ticker_names = preloaded["ticker_names"]
    ticker_market_map: dict = preloaded.get("ticker_market", {})

    exit_params = ExitParams(
        max_hold_days=params.max_hold_days,
        trailing_atr_mult=params.trailing_atr,
        early_exit_enabled=False,
        early_exit_hold_days=10,
        early_exit_return_min=-0.02,
        trend_exit_enabled=True,
        dynamic_hold=V27_DYNHOLD,
    )

    cash = initial_capital
    positions: list[Position] = []
    stock_trades: list[PortfolioTradeResult] = []
    etf_trades: list[EtfTrade] = []
    equity_curve: list[tuple] = []
    etf_pos: Optional[EtfPosition] = None
    forced_exits = 0
    v27_blocked_by_etf_exit = 0   # 강제청산 후에도 현금 부족으로 v2.7 진입 불가한 케이스

    # 미사용이지만 서명 일관성 유지
    total_days_in_position = 0    # stock OR etf 포지션 보유일

    def get_day_row(ticker: str, ts: pd.Timestamp):
        idx_map = ticker_date_idx.get(ticker)
        if not idx_map:
            return None, None
        i = idx_map.get(ts)
        if i is None:
            return None, None
        return ticker_data[ticker].iloc[i], i

    def etf_exit_pnl(
        etf: EtfPosition, exit_kospi: float, forced: bool, date_str: str
    ) -> float:
        """ETF 청산 처리 → cash 증가분 반환."""
        nonlocal etf_pos
        gross_ret = exit_kospi / etf.entry_kospi - 1
        net_ret = gross_ret - etf_cost
        pnl = etf.alloc * net_ret
        recovered = etf.alloc + pnl
        etf_trades.append(EtfTrade(
            entry_date=etf.entry_date,
            exit_date=date_str,
            entry_kospi=etf.entry_kospi,
            exit_kospi=exit_kospi,
            alloc=etf.alloc,
            gross_ret=gross_ret,
            net_ret=net_ret,
            pnl=pnl,
            hold_days=etf.hold_days,
            signal=etf.signal,
            forced=forced,
        ))
        etf_pos = None
        return recovered

    last_kospi_close: float = 0.0   # kr=None 시 fallback용 마지막 KOSPI 종가

    for day_idx, date_str in enumerate(trading_dates):
        ts = pd.Timestamp(date_str)
        kr = kospi_sigs.get(date_str)  # KOSPI 당일 데이터
        if kr is not None:
            last_kospi_close = kr["close"]
        # kr=None(공휴일 등 KOSPI 미수록 날)이면 직전 KOSPI 종가로 ETF 가치 유지
        effective_kospi_close = kr["close"] if kr is not None else last_kospi_close

        # ETF hold_days 갱신
        if etf_pos is not None:
            etf_pos.hold_days += 1

        # ── (A) ETF 자연 청산 ─────────────────────────────────────────────
        if etf_pos is not None and etf_pos.hold_days >= 1 and kr is not None:
            if check_etf_exit(kr, etf_pos.signal):
                cash += etf_exit_pnl(etf_pos, effective_kospi_close, forced=False, date_str=date_str)

        # ── 주식 청산 체크 ────────────────────────────────────────────────
        closed: list[Position] = []
        for pos in positions[:]:
            day, curr_i = get_day_row(pos.ticker, ts)
            if day is None:
                continue

            pos.hold_days += 1
            pos.highest_since_entry = max(pos.highest_since_entry, float(day["high"]))

            prev_ma5 = prev_ma20 = None
            if pos.hold_days > 1 and curr_i is not None and curr_i > 0:
                pr = ticker_data[pos.ticker].iloc[curr_i - 1]
                if pd.notna(pr.get("ma5")) and pd.notna(pr.get("ma20")):
                    prev_ma5 = float(pr["ma5"])
                    prev_ma20 = float(pr["ma20"])

            curr_ma5_raw  = day.get("ma5")
            curr_ma20_raw = day.get("ma20")
            curr_ma5  = float(curr_ma5_raw)  if pd.notna(curr_ma5_raw)  else None
            curr_ma20 = float(curr_ma20_raw) if pd.notna(curr_ma20_raw) else None

            tp2_price = (
                adjust_price(pos.entry_price + pos.atr_at_entry * params.tp2_atr, "up")
                if params.tp2_atr > 0 else 0
            )

            ctx = ExitContext(
                entry_price=pos.entry_price,
                day_low=float(day["low"]),
                day_high=float(day["high"]),
                stop_price=pos.stop_price,
                initial_stop_price=pos.stop_price,
                target_price=pos.tp1_price,
                tp2_price=tp2_price,
                high_since_entry=pos.highest_since_entry,
                atr_at_entry=pos.atr_at_entry,
                partial_sold=pos.tp1_triggered,
                partial_sold_2=pos.tp2_triggered,
                hold_days=pos.hold_days,
                current_return=float(day["close"]) / pos.entry_price - 1,
                prev_ma5=prev_ma5,
                prev_ma20=prev_ma20,
                curr_ma5=curr_ma5,
                curr_ma20=curr_ma20,
                current_adx=float(day.get("adx", 25.0) or 25.0),
                entry_adx=pos.entry_adx,
            )
            reason = evaluate_exit(ctx, exit_params)

            # TP1 분할 매도
            if reason == ExitReason.PARTIAL_TARGET:
                partial_shares = int(pos.shares * params.tp1_sell_ratio)
                if partial_shares > 0:
                    _avg_tv = float(day.get("avg_trading_value_20", 1e10) or 1e10)
                    _mkt = ticker_market_map.get(pos.ticker, "KOSPI")
                    tc = cost.total_cost_pct_dynamic(_mkt, partial_shares * pos.tp1_price, _avg_tv, slip)
                    pnl_pct = (pos.tp1_price / pos.entry_price - 1) - tc
                    pnl_amt = (partial_shares * pos.entry_price * (pos.tp1_price / pos.entry_price - 1)
                               - partial_shares * pos.entry_price * tc)
                    stock_trades.append(PortfolioTradeResult(
                        ticker=pos.ticker, name=ticker_names.get(pos.ticker, pos.ticker),
                        entry_date=pos.entry_date, entry_price=pos.entry_price,
                        exit_date=date_str, exit_price=pos.tp1_price,
                        exit_reason="TAKE_PROFIT_1",
                        hold_days=pos.hold_days, shares=partial_shares,
                        pnl_amount=pnl_amt, pnl_pct=pnl_pct,
                        is_partial=True, initial_shares=pos.initial_shares,
                    ))
                    cash += partial_shares * pos.tp1_price
                    pos.shares -= partial_shares
                    pos.tp1_triggered = True
                continue

            # TP2 분할 매도
            if reason == ExitReason.PARTIAL_TARGET_2:
                partial_shares = min(int(pos.initial_shares * params.tp2_sell_ratio), pos.shares)
                if partial_shares > 0:
                    _avg_tv = float(day.get("avg_trading_value_20", 1e10) or 1e10)
                    _mkt = ticker_market_map.get(pos.ticker, "KOSPI")
                    tc = cost.total_cost_pct_dynamic(_mkt, partial_shares * tp2_price, _avg_tv, slip)
                    pnl_pct = (tp2_price / pos.entry_price - 1) - tc
                    pnl_amt = (partial_shares * pos.entry_price * (tp2_price / pos.entry_price - 1)
                               - partial_shares * pos.entry_price * tc)
                    stock_trades.append(PortfolioTradeResult(
                        ticker=pos.ticker, name=ticker_names.get(pos.ticker, pos.ticker),
                        entry_date=pos.entry_date, entry_price=pos.entry_price,
                        exit_date=date_str, exit_price=tp2_price,
                        exit_reason="TAKE_PROFIT_2",
                        hold_days=pos.hold_days, shares=partial_shares,
                        pnl_amount=pnl_amt, pnl_pct=pnl_pct,
                        is_partial=True, initial_shares=pos.initial_shares,
                    ))
                    cash += partial_shares * tp2_price
                    pos.shares -= partial_shares
                    pos.tp2_triggered = True
                continue

            if reason is None:
                continue

            # 전량 청산
            if reason == ExitReason.STOP_LOSS:
                exit_price = pos.stop_price
                exit_reason_str = "STOP_LOSS"
            elif reason == ExitReason.TRAILING_STOP:
                exit_price = adjust_price(
                    pos.highest_since_entry - pos.atr_at_entry * params.trailing_atr, "up"
                )
                exit_reason_str = "TRAILING"
            elif reason == ExitReason.TREND_EXIT:
                exit_price = float(day["close"])
                exit_reason_str = "TREND_EXIT"
            elif reason == ExitReason.MAX_HOLD:
                exit_price = float(day["close"])
                exit_reason_str = "TIME_EXIT"
            else:
                exit_price = float(day["close"])
                exit_reason_str = reason.value

            _avg_tv = float(day.get("avg_trading_value_20", 1e10) or 1e10)
            _mkt = ticker_market_map.get(pos.ticker, "KOSPI")
            tc = cost.total_cost_pct_dynamic(_mkt, pos.shares * exit_price, _avg_tv, slip)
            pnl_pct = (exit_price / pos.entry_price - 1) - tc
            pnl_amt = (pos.shares * pos.entry_price * (exit_price / pos.entry_price - 1)
                       - pos.shares * pos.entry_price * tc)
            stock_trades.append(PortfolioTradeResult(
                ticker=pos.ticker, name=ticker_names.get(pos.ticker, pos.ticker),
                entry_date=pos.entry_date, entry_price=pos.entry_price,
                exit_date=date_str, exit_price=exit_price,
                exit_reason=exit_reason_str,
                hold_days=pos.hold_days, shares=pos.shares,
                pnl_amount=pnl_amt, pnl_pct=pnl_pct,
                is_partial=pos.tp1_triggered, initial_shares=pos.initial_shares,
            ))
            cash += pos.shares * exit_price
            closed.append(pos)

        for pos in closed:
            positions.remove(pos)

        # ── 시장 국면 게이트 ─────────────────────────────────────────────
        breadth = precomputed["breadth"].get(date_str, 0.5)
        regime_ok = precomputed.get("index_above_ma200", {}).get(date_str, True)
        gate_open = (breadth >= BREADTH_GATE) and regime_ok

        # ── 주식 신규 진입 ───────────────────────────────────────────────
        if gate_open:
            open_slots = max_positions - len(positions)
            held_set = {p.ticker for p in positions}
            candidates = [
                c for c in precomputed["candidates"].get(date_str, [])
                if c["ticker"] not in held_set
            ]

            for cand in candidates[:open_slots]:
                # ── 익일 시가 진입 준비 ──
                nxt_idx = day_idx + 1
                if nxt_idx >= len(trading_dates):
                    break
                nxt_date = trading_dates[nxt_idx]
                nxt_ts = pd.Timestamp(nxt_date)
                ni = ticker_date_idx.get(cand["ticker"], {}).get(nxt_ts)
                if ni is None:
                    continue
                nxt_row = ticker_data[cand["ticker"]].iloc[ni]
                entry_price = float(nxt_row["open"])
                if entry_price <= 0:
                    continue

                if max_positions - len(positions) <= 0:
                    break

                # equity 기반 alloc (ETF 포함)
                today_equity = cash
                for _p in positions:
                    _d, _ = get_day_row(_p.ticker, ts)
                    today_equity += (_p.shares * float(_d["close"]) if _d is not None
                                     else _p.shares * _p.entry_price)
                if etf_pos is not None and effective_kospi_close > 0:
                    today_equity += etf_pos.alloc * (effective_kospi_close / etf_pos.entry_kospi)

                alloc = today_equity / max_positions
                if alloc < min_position_amount:
                    continue

                # ── (B) 현금 부족 → ETF 강제 청산 ────────────────────────
                if cash < alloc and etf_pos is not None and effective_kospi_close > 0:
                    recovered = etf_exit_pnl(
                        etf_pos, effective_kospi_close, forced=True, date_str=date_str
                    )
                    cash += recovered
                    forced_exits += 1

                if cash < alloc:
                    continue  # 강제청산 후에도 부족 → 건너뜀

                shares = compute_first_entry_qty(int(alloc), int(entry_price), V27_SCALING)
                if shares <= 0:
                    continue
                actual_cost = shares * entry_price
                if actual_cost < min_position_amount or actual_cost > cash:
                    continue

                cash -= actual_cost
                positions.append(Position(
                    ticker=cand["ticker"],
                    name=ticker_names.get(cand["ticker"], cand["ticker"]),
                    entry_date=nxt_date,
                    entry_price=entry_price,
                    shares=shares,
                    initial_shares=shares,
                    allocated_capital=actual_cost,
                    atr_at_entry=cand["atr"],
                    stop_price=adjust_price(entry_price - cand["atr"] * params.stop_loss_atr, "down"),
                    tp1_price=adjust_price(entry_price + cand["atr"] * params.take_profit_atr, "up"),
                    highest_since_entry=entry_price,
                    entry_adx=float(cand.get("adx", 25.0) or 25.0),
                ))

        # ── (C) 유휴 현금 → ETF 진입 (당일 종가, kr 있는 날만) ──────────
        if (etf_strategy != "none" and etf_pos is None
                and kr is not None and cash >= etf_min_alloc):
            sig = check_etf_entry(kr, etf_strategy, use_ma200)
            if sig is not None:
                etf_pos = EtfPosition(
                    entry_date=date_str,
                    entry_kospi=effective_kospi_close,
                    alloc=cash,           # 유휴 현금 전액
                    signal=sig,
                    hold_days=0,
                )
                cash = 0.0

        # ── 일별 포트폴리오 가치 ─────────────────────────────────────────
        portfolio_value = cash
        for pos in positions:
            day, _ = get_day_row(pos.ticker, ts)
            portfolio_value += (pos.shares * float(day["close"]) if day is not None
                                else pos.shares * pos.entry_price)
        if etf_pos is not None and effective_kospi_close > 0:
            portfolio_value += etf_pos.alloc * (effective_kospi_close / etf_pos.entry_kospi)

        equity_curve.append((date_str, portfolio_value))

        # 자본활용 통계 (포지션 or ETF 보유일)
        if positions or etf_pos is not None:
            total_days_in_position += 1

    # ── 미청산 포지션 강제 청산 ──────────────────────────────────────────
    last_date = trading_dates[-1]
    last_ts = pd.Timestamp(last_date)
    for pos in positions:
        idx_map = ticker_date_idx.get(pos.ticker, {})
        li = idx_map.get(last_ts)
        if li is not None:
            last_row = ticker_data[pos.ticker].iloc[li]
            exit_price = float(last_row["close"])
            _avg_tv = float(last_row.get("avg_trading_value_20", 1e10) or 1e10)
            _mkt = ticker_market_map.get(pos.ticker, "KOSPI")
            tc = cost.total_cost_pct_dynamic(_mkt, pos.shares * exit_price, _avg_tv, slip)
            pnl_pct = (exit_price / pos.entry_price - 1) - tc
            pnl_amt = (pos.shares * pos.entry_price * (exit_price / pos.entry_price - 1)
                       - pos.shares * pos.entry_price * tc)
            stock_trades.append(PortfolioTradeResult(
                ticker=pos.ticker, name=ticker_names.get(pos.ticker, pos.ticker),
                entry_date=pos.entry_date, entry_price=pos.entry_price,
                exit_date=last_date, exit_price=exit_price,
                exit_reason="FINAL_CLOSE",
                hold_days=pos.hold_days, shares=pos.shares,
                pnl_amount=pnl_amt, pnl_pct=pnl_pct,
                is_partial=pos.tp1_triggered, initial_shares=pos.initial_shares,
            ))

    # 미청산 ETF 강제 청산
    if etf_pos is not None:
        kr_last = kospi_sigs.get(last_date)
        if kr_last:
            cash += etf_exit_pnl(etf_pos, kr_last["close"], forced=True, date_str=last_date)

    return {
        "stock_trades": stock_trades,
        "etf_trades": etf_trades,
        "equity_curve": equity_curve,
        "forced_exits": forced_exits,
        "total_invested_days": total_days_in_position,
        "total_days": len(trading_dates),
    }


# ─── 지표 계산 ───────────────────────────────────────────────────────────────
def compute_metrics(raw: dict, initial_capital: float) -> dict:
    stock_trades: list[PortfolioTradeResult] = raw["stock_trades"]
    etf_trades:   list[EtfTrade]             = raw["etf_trades"]
    equity_curve                             = raw["equity_curve"]
    forced_exits                             = raw["forced_exits"]

    # 주식 메트릭 (portfolio_backtester와 동일: is_partial 포함 전체 거래로 PF/WR 계산)
    s_wins   = [t for t in stock_trades if t.pnl_amount > 0]
    s_losses = [t for t in stock_trades if t.pnl_amount <= 0]
    s_gp = sum(t.pnl_amount for t in s_wins)   if s_wins   else 0.0
    s_gl = abs(sum(t.pnl_amount for t in s_losses)) if s_losses else 0.0
    s_pf = s_gp / s_gl if s_gl > 0 else (math.inf if s_gp > 0 else 0.0)
    s_wr = len(s_wins) / len(stock_trades) if stock_trades else 0.0

    # ETF 메트릭
    etf_wins  = [t for t in etf_trades if t.net_ret > 0]
    etf_loss  = [t for t in etf_trades if t.net_ret <= 0]
    etf_gp = sum(t.pnl for t in etf_wins)      if etf_wins  else 0.0
    etf_gl = abs(sum(t.pnl for t in etf_loss)) if etf_loss  else 0.0
    etf_pf = etf_gp / etf_gl if etf_gl > 0 else (math.inf if etf_gp > 0 else 0.0)
    etf_wr = len(etf_wins) / len(etf_trades)    if etf_trades else 0.0
    etf_net_pnl = sum(t.pnl for t in etf_trades)
    etf_avg_hold = float(np.mean([t.hold_days for t in etf_trades])) if etf_trades else 0.0
    etf_forced = forced_exits
    etf_natural = len(etf_trades) - forced_exits

    # 전체 포트폴리오 메트릭 (equity_curve 기반)
    total_days = len(equity_curve)
    years = total_days / 245
    final_eq = equity_curve[-1][1] if equity_curve else initial_capital
    cagr = (final_eq / initial_capital) ** (1 / years) - 1 if years > 0 else 0.0

    peak = initial_capital
    mdd = 0.0
    for _, eq in equity_curve:
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak if peak > 0 else 0.0
        if dd > mdd:
            mdd = dd

    utilization = raw["total_invested_days"] / raw["total_days"] if raw["total_days"] > 0 else 0.0

    return dict(
        # 주식
        stock_trades=len(stock_trades),
        stock_wr=s_wr,
        stock_pf=s_pf,
        # ETF
        etf_trades=len(etf_trades),
        etf_wr=etf_wr,
        etf_pf=etf_pf,
        etf_net_pnl=etf_net_pnl,
        etf_avg_hold=etf_avg_hold,
        etf_natural=etf_natural,
        etf_forced=etf_forced,
        # 전체
        cagr=cagr,
        mdd=mdd,
        utilization=utilization,
        final_equity=final_eq,
        pnl=final_eq - initial_capital,
    )


# ─── 실험 변형 ───────────────────────────────────────────────────────────────
VARIANTS = [
    # (label, etf_strategy, use_ma200)
    ("[0] V27_ONLY",       "none",    False),
    ("[1] V27+IBS",        "ibs",     False),
    ("[2] V27+BB",         "bb",      False),
    ("[3] V27+IBS+BB",     "ibs_bb",  False),
    ("[4] V27+IBS_MA200",  "ibs",     True),
]


# ─── 보고서 ──────────────────────────────────────────────────────────────────
def _pf(v: float) -> str:
    return "∞" if math.isinf(v) else f"{v:.2f}"


def print_report(results: dict[str, dict]) -> None:
    base = results["[0] V27_ONLY"]

    print()
    print(SEP)
    print("  유휴 현금 ETF 통합 백테스트 (v2.7 + ETF, 10M, 2014~2026)")
    print(SEP)
    print()

    # ── 결과 비교표 ──────────────────────────────────────────────────────
    print("■ 결과 비교")
    hdr = (
        f"  {'변형':<22} {'주식건':>5} {'주식PF':>6} {'CAGR':>7} {'MDD':>7}"
        f" {'활용':>5} {'ETF건':>5} {'ETF수익':>9} {'강제':>4}"
    )
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))

    for label, m in results.items():
        etf_pnl_str = f"{m['etf_net_pnl']/10000:+.0f}만" if m["etf_trades"] > 0 else "  -  "
        forced_str = str(m["etf_forced"]) if m["etf_trades"] > 0 else " -"
        print(
            f"  {label:<22}"
            f" {m['stock_trades']:>5}"
            f" {_pf(m['stock_pf']):>6}"
            f" {m['cagr']*100:>+6.1f}%"
            f" {m['mdd']*100:>6.1f}%"
            f" {m['utilization']*100:>4.1f}%"
            f" {m['etf_trades']:>5}"
            f" {etf_pnl_str:>9}"
            f" {forced_str:>4}"
        )

    print()

    # ── 핵심 질문 ─────────────────────────────────────────────────────────
    print("■ 핵심 질문 분석")

    base_cagr = base["cagr"]
    base_mdd  = base["mdd"]

    # Q1: CAGR 개선?
    print("  Q1 ETF 추가 시 CAGR 개선?")
    for label, m in results.items():
        if "[0]" in label:
            continue
        diff = (m["cagr"] - base_cagr) * 100
        mark = "✅" if diff > 0 else ("⚠️" if diff > -0.5 else "❌")
        print(f"     {label}: {diff:+.2f}%p  {mark}")

    print()

    # Q2: v2.7 주식 PF 영향?
    print("  Q2 ETF 강제 청산이 v2.7 주식 PF에 영향?")
    print(f"     [0] 기준 주식PF: {_pf(base['stock_pf'])}")
    for label, m in results.items():
        if "[0]" in label:
            continue
        pf_diff = m["stock_pf"] - base["stock_pf"]
        fc = m["etf_forced"]
        print(f"     {label}: 주식PF {_pf(m['stock_pf'])} (diff {pf_diff:+.2f}), 강제청산 {fc}건")

    print()

    # Q3: 자본활용 개선?
    print("  Q3 자본활용 개선?")
    print(f"     [0] 기준: {base['utilization']*100:.1f}%")
    for label, m in results.items():
        if "[0]" in label:
            continue
        diff = (m["utilization"] - base["utilization"]) * 100
        print(f"     {label}: {m['utilization']*100:.1f}% ({diff:+.1f}%p)")

    print()

    # Q4: MDD 악화?
    print("  Q4 MDD 악화 여부?")
    print(f"     [0] 기준: -{base_mdd*100:.1f}%")
    for label, m in results.items():
        if "[0]" in label:
            continue
        diff = (m["mdd"] - base_mdd) * 100
        mark = "✅" if diff <= 0 else ("⚠️" if diff < 2 else "❌")
        print(f"     {label}: -{m['mdd']*100:.1f}% (diff {diff:+.1f}%p) {mark}")

    print()

    # ── ETF 상세 ─────────────────────────────────────────────────────────
    print("■ ETF 상세")
    for label, m in results.items():
        if "[0]" in label or m["etf_trades"] == 0:
            continue
        print(
            f"  {label}: "
            f"총 {m['etf_trades']}건 (자연 {m['etf_natural']}, 강제 {m['etf_forced']}) | "
            f"WR {m['etf_wr']*100:.1f}% | PF {_pf(m['etf_pf'])} | "
            f"평균보유 {m['etf_avg_hold']:.1f}일 | "
            f"순손익 {m['etf_net_pnl']/10000:+.1f}만"
        )

    print()

    # ── 판정 ─────────────────────────────────────────────────────────────
    print("■ 판정")
    best_label = max(
        [(l, m) for l, m in results.items() if "[0]" not in l],
        key=lambda x: x[1]["cagr"],
        default=(None, None),
    )
    if best_label[0]:
        lbl, bm = best_label
        cagr_diff = (bm["cagr"] - base_cagr) * 100
        mdd_diff  = (bm["mdd"] - base_mdd) * 100
        if cagr_diff > 0:
            print(f"  최우수: {lbl}")
            print(f"    CAGR {base_cagr*100:.1f}% → {bm['cagr']*100:.1f}% ({cagr_diff:+.1f}%p ✅)")
            print(f"    MDD  {base_mdd*100:.1f}% → {bm['mdd']*100:.1f}% ({mdd_diff:+.1f}%p)")
            print(f"    활용 {base['utilization']*100:.1f}% → {bm['utilization']*100:.1f}%")
        else:
            print(f"  모든 변형에서 CAGR 개선 없음 (최고: {lbl}, diff {cagr_diff:+.1f}%p)")
            print(f"  → ETF 유휴 현금 활용은 현재 v2.7 대비 추가 이점 제한적")

    print()
    print(SEP)


# ─── 메인 ─────────────────────────────────────────────────────────────────────
def main() -> None:
    print(SEP)
    print("  유휴 현금 ETF 통합 백테스트")
    print(f"  자본: {INITIAL_CAPITAL:,}원  |  max_pos: {MAX_POSITIONS}  |  ETF비용: {ETF_COST*100:.2f}%")
    print(SEP)

    t0 = time.time()
    print("\n[1/4] v2.7 데이터 로드...")
    preloaded = load_backtest_data(V27_PARAMS)
    print(f"  완료 {time.time()-t0:.1f}s")

    t0 = time.time()
    print("[2/4] v2.7 시그널 사전 계산...")
    precomputed = precompute_daily_signals(
        preloaded["trading_dates"],
        preloaded["ticker_data"],
        preloaded["ticker_date_idx"],
        set(preloaded["initial_universe"]),
        params=V27_PARAMS,
        kospi_ret_map=preloaded.get("kospi_ret_map"),
        kosdaq_ret_map=preloaded.get("kosdaq_ret_map"),
        ticker_market=preloaded.get("ticker_market"),
        weights=V27_WEIGHTS,
    )
    print(f"  완료 {time.time()-t0:.1f}s")

    t0 = time.time()
    print("[3/4] KOSPI ETF 시그널 준비...")
    kospi_sigs = load_kospi_signals()
    print(f"  KOSPI: {len(kospi_sigs)}일, 완료 {time.time()-t0:.1f}s")

    print("[4/4] 5개 변형 백테스트 실행...")
    all_results: dict[str, dict] = {}
    for label, etf_strat, use_ma200 in VARIANTS:
        t0 = time.time()
        print(f"  {label} ...", end="", flush=True)
        raw = run_integrated(
            preloaded=preloaded,
            precomputed=precomputed,
            kospi_sigs=kospi_sigs,
            etf_strategy=etf_strat,
            use_ma200=use_ma200,
        )
        m = compute_metrics(raw, INITIAL_CAPITAL)
        all_results[label] = m
        print(f" 완료 {time.time()-t0:.1f}s  (주식 {m['stock_trades']}건, ETF {m['etf_trades']}건)")

    print_report(all_results)


if __name__ == "__main__":
    main()
