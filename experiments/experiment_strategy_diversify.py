"""전략 다각화 타당성 검증 — 보완 전략 3종 백테스트

[변형]
  [0] V27_ONLY      : TF v2.7만 (기준선)
  [1] MR_ONLY       : 이격도 평균회귀만
  [2] BO_ONLY       : 20일 고가 돌파만
  [3] MP_ONLY       : 모멘텀 눌림목만
  [4] V27 + MR      : TF + 평균회귀 (TF 우선)
  [5] V27 + BO + MP : TF + 돌파 + 눌림목
  [6] V27 + ALL     : TF + 3개 전부

핵심 가설:
  - 이격도 MR → 추세추종이 쉬는 횡보/하락장에서 수익
  - 고가 돌파 BO → 추세 초입 포착 (v2.7은 확립된 추세만 진입)
  - 모멘텀 눌림목 MP → 추세 중 조정 후 재진입

실행:
    python experiments/experiment_strategy_diversify.py
"""
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from loguru import logger
logger.remove()
logger.add(sys.stderr, level="WARNING")

import pandas as pd

from src.backtest.portfolio_backtester import (
    load_backtest_data,
    precompute_daily_signals,
    _compute_portfolio_summary,
    PortfolioTradeResult,
    PortfolioResult,
)
from src.strategy.exit_evaluator import ExitContext, ExitParams, evaluate_exit
from src.models import ExitReason
from src.strategy.trend_following_v2 import StrategyParams
from src.utils.cost_model import CostModel
from src.utils.slippage_model import SlippageParams
from src.utils.tick_size import adjust_price
from src.strategy.ranking import RankingWeights


# ─────────────────────────────────────────────────────────────────────────────
# 공통 설정
# ─────────────────────────────────────────────────────────────────────────────
CAPITAL    = 10_000_000
MAX_POS    = 5
MIN_AMOUNT = 300_000

V27_PARAMS = StrategyParams(
    stop_loss_atr=3.0,
    take_profit_atr=1.5,
    trailing_atr=3.0,
    max_hold_days=20,
    adx_threshold=25.0,
    relative_strength_threshold=0.08,
    tp1_sell_ratio=0.10,
    tp2_atr=4.0,
    tp2_sell_ratio=0.10,
)
V27_WEIGHTS  = RankingWeights(rs=0.50, momentum_atr=0.20, adx=0.15, liquidity=0.10, ma_alignment=0.05)
V27_BREADTH  = 0.40
V27_REGIME   = True   # KOSPI MA200 게이트 사용

# 보완 전략 게이트 (breadth 기준만, regime 게이트 없음)
MR_BREADTH   = 0.0    # gate 없음
BO_BREADTH   = 0.30
MP_BREADTH   = 0.40

COST = CostModel()
SLIP = SlippageParams(
    enabled=True,
    base_slippage=0.0003,
    impact_coefficient=0.1,
    max_slippage=0.02,
)

TF_EXIT_PARAMS = ExitParams(
    max_hold_days=V27_PARAMS.max_hold_days,
    trailing_atr_mult=V27_PARAMS.trailing_atr,
    trend_exit_enabled=True,
    early_exit_enabled=False,
)


# ─────────────────────────────────────────────────────────────────────────────
# 추가 지표 계산 (MR/BO/MP용)
# ─────────────────────────────────────────────────────────────────────────────

def add_extra_indicators(ticker_data: dict) -> None:
    """20일 고점 롤링 최대, 60일 수익률을 각 DataFrame에 인플레이스 추가."""
    for df in ticker_data.values():
        df['high_max_20'] = df['high'].rolling(20).max()
        df['ret_60'] = df['close'].pct_change(60)


# ─────────────────────────────────────────────────────────────────────────────
# 보완 전략 시그널 사전 계산
# ─────────────────────────────────────────────────────────────────────────────

def precompute_mr_signals(trading_dates, ticker_data, ticker_date_idx, universe_at):
    """이격도 평균회귀: MA20 대비 -4%+ 괴리 + 당일 양봉."""
    candidates_by_date = {}
    for date_str in trading_dates:
        ts = pd.Timestamp(date_str)
        universe = universe_at.get(date_str, set())
        cands = []
        for ticker in universe:
            idx_map = ticker_date_idx.get(ticker)
            if not idx_map:
                continue
            curr_i = idx_map.get(ts)
            if curr_i is None or curr_i < 25:
                continue
            day = ticker_data[ticker].iloc[curr_i]
            ma20 = day.get('ma20')
            atr  = day.get('atr')
            if pd.isna(ma20) or float(ma20) <= 0 or pd.isna(atr) or float(atr) <= 0:
                continue
            disparity = float(day['close']) / float(ma20)
            if disparity >= 0.96:
                continue
            if float(day['close']) <= float(day['open']):  # 음봉
                continue
            score = 0.96 - disparity
            cands.append({
                'ticker': ticker,
                'close': float(day['close']),
                'atr':   float(atr),
                'ma20':  float(ma20),
                'avg_trading_value_20': float(day.get('avg_trading_value_20', 1e10) or 1e10),
                'score': score,
            })
        candidates_by_date[date_str] = sorted(cands, key=lambda x: x['score'], reverse=True)
    return candidates_by_date


def precompute_bo_signals(trading_dates, ticker_data, ticker_date_idx, universe_at):
    """20일 고가 돌파 + 거래량 1.5x + ADX >= 20."""
    candidates_by_date = {}
    for date_str in trading_dates:
        ts = pd.Timestamp(date_str)
        universe = universe_at.get(date_str, set())
        cands = []
        for ticker in universe:
            idx_map = ticker_date_idx.get(ticker)
            if not idx_map:
                continue
            curr_i = idx_map.get(ts)
            if curr_i is None or curr_i < 30:
                continue
            df  = ticker_data[ticker]
            day = df.iloc[curr_i]
            atr        = day.get('atr')
            adx        = day.get('adx')
            avg_vol_20 = day.get('avg_volume_20')
            if any(pd.isna(x) or x is None for x in [atr, adx, avg_vol_20]):
                continue
            if float(atr) <= 0 or float(avg_vol_20) <= 0:
                continue
            # 전일 기준 20일 고점 (당일 제외 → look-ahead 방지)
            prev_high_max = df.iloc[curr_i - 1].get('high_max_20')
            if pd.isna(prev_high_max) or float(prev_high_max) <= 0:
                continue
            if float(day['close']) <= float(prev_high_max):
                continue
            if float(day['volume']) < float(avg_vol_20) * 1.5:
                continue
            if float(adx) < 20:
                continue
            score = float(day['close']) / float(prev_high_max) - 1
            cands.append({
                'ticker': ticker,
                'close': float(day['close']),
                'atr':   float(atr),
                'adx':   float(adx),
                'avg_trading_value_20': float(day.get('avg_trading_value_20', 1e10) or 1e10),
                'score': score,
            })
        candidates_by_date[date_str] = sorted(cands, key=lambda x: x['score'], reverse=True)
    return candidates_by_date


def precompute_mp_signals(trading_dates, ticker_data, ticker_date_idx, universe_at):
    """60일 모멘텀 양수 + 최근 3일 중 2일 음봉 + 당일 양봉."""
    candidates_by_date = {}
    for date_str in trading_dates:
        ts = pd.Timestamp(date_str)
        universe = universe_at.get(date_str, set())
        cands = []
        for ticker in universe:
            idx_map = ticker_date_idx.get(ticker)
            if not idx_map:
                continue
            curr_i = idx_map.get(ts)
            if curr_i is None or curr_i < 70:
                continue
            df  = ticker_data[ticker]
            day = df.iloc[curr_i]
            atr    = day.get('atr')
            ret_60 = day.get('ret_60')
            if pd.isna(atr) or float(atr) <= 0:
                continue
            if pd.isna(ret_60) or float(ret_60) <= 0:
                continue
            if float(day['close']) <= float(day['open']):
                continue
            recent_3   = [df.iloc[curr_i - 3], df.iloc[curr_i - 2], df.iloc[curr_i - 1]]
            down_days  = sum(1 for d in recent_3 if float(d['close']) < float(d['open']))
            if down_days < 2:
                continue
            score = float(ret_60)
            cands.append({
                'ticker': ticker,
                'close': float(day['close']),
                'atr':   float(atr),
                'avg_trading_value_20': float(day.get('avg_trading_value_20', 1e10) or 1e10),
                'score': score,
            })
        candidates_by_date[date_str] = sorted(cands, key=lambda x: x['score'], reverse=True)
    return candidates_by_date


# ─────────────────────────────────────────────────────────────────────────────
# 포지션 + 백테스터
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class DivPosition:
    ticker: str
    name: str
    strategy: str         # 'TF', 'MR', 'BO', 'MP'
    entry_date: str
    entry_price: float
    shares: int
    initial_shares: int
    atr_at_entry: float
    stop_price: float     # ATR 기반 SL (TF/BO/MP) or 0 (MR)
    tp1_price: float      # TF only; others = 0
    tp2_price: float      # TF only; others = 0
    highest_since_entry: float
    hold_days: int = 0
    tp1_triggered: bool = False
    tp2_triggered: bool = False
    entry_adx: float = 0.0
    allocated_capital: float = 0.0


def _check_tf_exit(pos: DivPosition, day, prev_row):
    """TF 포지션 청산 체크. 반환: (reason_str, exit_price) or (None, None).

    'TP1'/'TP2'는 분할 매도를 의미 (전량 청산 아님).
    """
    prev_ma5 = prev_ma20 = curr_ma5 = curr_ma20 = None
    if prev_row is not None:
        pma5  = prev_row.get('ma5')
        pma20 = prev_row.get('ma20')
        if pd.notna(pma5) and pd.notna(pma20):
            prev_ma5, prev_ma20 = float(pma5), float(pma20)
    cma5  = day.get('ma5')
    cma20 = day.get('ma20')
    if pd.notna(cma5) and pd.notna(cma20):
        curr_ma5, curr_ma20 = float(cma5), float(cma20)

    tp2p = pos.tp2_price if pos.tp2_price > 0 else 0

    ctx = ExitContext(
        entry_price=pos.entry_price,
        day_low=float(day['low']),
        day_high=float(day['high']),
        stop_price=pos.stop_price,
        initial_stop_price=pos.stop_price,
        target_price=pos.tp1_price,
        tp2_price=tp2p,
        high_since_entry=pos.highest_since_entry,
        atr_at_entry=pos.atr_at_entry,
        partial_sold=pos.tp1_triggered,
        partial_sold_2=pos.tp2_triggered,
        hold_days=pos.hold_days,
        current_return=float(day['close']) / pos.entry_price - 1,
        prev_ma5=prev_ma5,
        prev_ma20=prev_ma20,
        curr_ma5=curr_ma5,
        curr_ma20=curr_ma20,
        current_adx=float(day.get('adx', 25.0) or 25.0),
        entry_adx=pos.entry_adx,
    )
    reason = evaluate_exit(ctx, TF_EXIT_PARAMS)
    if reason is None:
        return None, None

    if reason == ExitReason.PARTIAL_TARGET:
        return 'TP1', pos.tp1_price
    if reason == ExitReason.PARTIAL_TARGET_2:
        return 'TP2', pos.tp2_price
    if reason == ExitReason.STOP_LOSS:
        return 'STOP_LOSS', pos.stop_price
    if reason == ExitReason.TRAILING_STOP:
        trail_px = adjust_price(
            pos.highest_since_entry - pos.atr_at_entry * V27_PARAMS.trailing_atr, "up"
        )
        return 'TRAILING', float(trail_px)
    if reason == ExitReason.TREND_EXIT:
        return 'TREND_EXIT', float(day['close'])
    if reason == ExitReason.MAX_HOLD:
        return 'TIME_EXIT', float(day['close'])
    return reason.value, float(day['close'])


def _check_simple_exit(pos: DivPosition, day):
    """MR / BO / MP 포지션 청산 체크. 반환: (reason_str, exit_price) or (None, None)."""
    strategy = pos.strategy

    if strategy == 'MR':
        ma20_v = day.get('ma20')
        if pd.notna(ma20_v) and float(ma20_v) > 0:
            disp = float(day['close']) / float(ma20_v)
            if disp >= 1.0:
                return 'TARGET', float(day['close'])   # MA20 복귀
            if disp <= 0.88:
                return 'STOP_LOSS', float(day['close'])  # 추가 하락
        if pos.hold_days >= 7:
            return 'TIME_EXIT', float(day['close'])
        return None, None

    if strategy == 'BO':
        sl_price = pos.stop_price
        if float(day['low']) <= sl_price:
            return 'STOP_LOSS', sl_price
        trail_price = pos.highest_since_entry - pos.atr_at_entry * 3.0
        if float(day['close']) <= trail_price and pos.hold_days > 1:
            return 'TRAILING', float(day['close'])
        if pos.hold_days >= 15:
            return 'TIME_EXIT', float(day['close'])
        return None, None

    if strategy == 'MP':
        sl_price = pos.stop_price
        if float(day['low']) <= sl_price:
            return 'STOP_LOSS', sl_price
        ma20_v = day.get('ma20')
        if pd.notna(ma20_v) and float(ma20_v) > 0:
            if float(day['close']) >= float(ma20_v) * 1.10:
                return 'TARGET', float(day['close'])
        if pos.hold_days >= 10:
            return 'TIME_EXIT', float(day['close'])
        return None, None

    return None, None


def run_diversified_backtest(
    active_strategies: list,   # e.g. ['TF', 'MP', 'BO', 'MR'] ordered by priority
    preloaded_data: dict,
    tf_precomputed: dict,      # from precompute_daily_signals
    mr_candidates: dict,       # {date_str: [...]} or {}
    bo_candidates: dict,
    mp_candidates: dict,
    initial_capital: float = CAPITAL,
    max_positions: int = MAX_POS,
    min_position_amount: float = MIN_AMOUNT,
    sizing_mode: str = 'equity',
) -> tuple:
    """다중 전략 포트폴리오 백테스트.

    Returns:
        (PortfolioResult, daily_stats dict)

    daily_stats: {date_str: {'TF': int, 'MR': int, 'BO': int, 'MP': int}}
        각 날짜에 해당 전략으로 보유 중인 포지션 수.
    """
    trading_dates      = preloaded_data['trading_dates']
    ticker_data        = preloaded_data['ticker_data']
    ticker_date_idx    = preloaded_data['ticker_date_idx']
    ticker_names       = preloaded_data['ticker_names']
    ticker_market_map  = preloaded_data.get('ticker_market', {})

    universe_at        = tf_precomputed['universe_at']
    breadth_by_date    = tf_precomputed['breadth']
    index_ma200_map    = tf_precomputed.get('index_above_ma200', {})
    tf_cands_by_date   = tf_precomputed['candidates']

    cash       = initial_capital
    positions  = []     # list[DivPosition]
    trades     = []     # list[PortfolioTradeResult]
    equity_curve  = []
    daily_stats   = {}  # {date_str: {strategy: count}}

    concurrent_sum = 0
    max_concurrent = 0

    def _get_row(ticker, ts):
        idx_map = ticker_date_idx.get(ticker)
        if not idx_map:
            return None, None
        i = idx_map.get(ts)
        if i is None:
            return None, None
        return ticker_data[ticker].iloc[i], i

    for day_idx, date_str in enumerate(trading_dates):
        ts = pd.Timestamp(date_str)
        day_stats = {'TF': 0, 'MR': 0, 'BO': 0, 'MP': 0}

        # ── 1. 청산 체크 ─────────────────────────────────────────────────────
        closed = []
        for pos in positions[:]:
            day, curr_i = _get_row(pos.ticker, ts)
            if day is None:
                continue

            pos.hold_days += 1
            pos.highest_since_entry = max(pos.highest_since_entry, float(day['high']))

            prev_row = ticker_data[pos.ticker].iloc[curr_i - 1] if curr_i > 0 else None

            mkt     = ticker_market_map.get(pos.ticker, 'KOSPI')
            avg_tv  = float(day.get('avg_trading_value_20', 1e10) or 1e10)

            if pos.strategy == 'TF':
                reason, exit_price = _check_tf_exit(pos, day, prev_row)
            else:
                reason, exit_price = _check_simple_exit(pos, day)

            if reason is None:
                continue

            # ── TP1 분할 매도 (TF only) ──────────────────────────────────────
            if reason == 'TP1':
                partial_shares = int(pos.initial_shares * V27_PARAMS.tp1_sell_ratio)
                partial_shares = min(partial_shares, pos.shares)
                if partial_shares > 0:
                    tc   = COST.total_cost_pct_dynamic(mkt, partial_shares * exit_price, avg_tv, SLIP)
                    pnl  = partial_shares * pos.entry_price * (exit_price / pos.entry_price - 1 - tc)
                    trades.append(PortfolioTradeResult(
                        ticker=pos.ticker,
                        name=pos.name,
                        entry_date=pos.entry_date,
                        entry_price=pos.entry_price,
                        exit_date=date_str,
                        exit_price=exit_price,
                        exit_reason='TAKE_PROFIT_1',
                        hold_days=pos.hold_days,
                        shares=partial_shares,
                        pnl_amount=pnl,
                        pnl_pct=(exit_price / pos.entry_price - 1) - tc,
                        is_partial=True,
                        initial_shares=pos.initial_shares,
                    ))
                    cash += partial_shares * exit_price
                    pos.shares -= partial_shares
                    pos.tp1_triggered = True
                continue

            # ── TP2 분할 매도 (TF only) ──────────────────────────────────────
            if reason == 'TP2':
                partial_shares = int(pos.initial_shares * V27_PARAMS.tp2_sell_ratio)
                partial_shares = min(partial_shares, pos.shares)
                if partial_shares > 0:
                    tc   = COST.total_cost_pct_dynamic(mkt, partial_shares * exit_price, avg_tv, SLIP)
                    pnl  = partial_shares * pos.entry_price * (exit_price / pos.entry_price - 1 - tc)
                    trades.append(PortfolioTradeResult(
                        ticker=pos.ticker,
                        name=pos.name,
                        entry_date=pos.entry_date,
                        entry_price=pos.entry_price,
                        exit_date=date_str,
                        exit_price=exit_price,
                        exit_reason='TAKE_PROFIT_2',
                        hold_days=pos.hold_days,
                        shares=partial_shares,
                        pnl_amount=pnl,
                        pnl_pct=(exit_price / pos.entry_price - 1) - tc,
                        is_partial=True,
                        initial_shares=pos.initial_shares,
                    ))
                    cash += partial_shares * exit_price
                    pos.shares -= partial_shares
                    pos.tp2_triggered = True
                continue

            # ── 전량 청산 ────────────────────────────────────────────────────
            tc        = COST.total_cost_pct_dynamic(mkt, pos.shares * exit_price, avg_tv, SLIP)
            pnl_pct   = (exit_price / pos.entry_price - 1) - tc
            pnl_amt   = pos.shares * pos.entry_price * (exit_price / pos.entry_price - 1) \
                        - pos.shares * pos.entry_price * tc
            trades.append(PortfolioTradeResult(
                ticker=pos.ticker,
                name=pos.name,
                entry_date=pos.entry_date,
                entry_price=pos.entry_price,
                exit_date=date_str,
                exit_price=exit_price,
                exit_reason=reason,
                hold_days=pos.hold_days,
                shares=pos.shares,
                pnl_amount=pnl_amt,
                pnl_pct=pnl_pct,
                is_partial=pos.tp1_triggered,
                initial_shares=pos.initial_shares,
            ))
            cash += pos.shares * exit_price
            closed.append(pos)

        for p in closed:
            positions.remove(p)

        # ── 2. 국면 게이트 (TF/MP 공용: breadth + regime) ────────────────────
        breadth = breadth_by_date.get(date_str, 0.5)
        regime_ok = index_ma200_map.get(date_str, True)

        def _gate_ok(strategy_name):
            if strategy_name == 'TF':
                return breadth >= V27_BREADTH and regime_ok
            elif strategy_name == 'MP':
                return breadth >= MP_BREADTH
            elif strategy_name == 'BO':
                return breadth >= BO_BREADTH
            elif strategy_name == 'MR':
                return True  # gate 없음
            return False

        # ── 3. 신규 진입 (전략 우선순위 순) ──────────────────────────────────
        open_slots = max_positions - len(positions)
        if open_slots > 0:
            held_set = {p.ticker for p in positions}

            for strategy_name in active_strategies:
                if open_slots <= 0:
                    break
                if not _gate_ok(strategy_name):
                    continue

                if strategy_name == 'TF':
                    cands = [c for c in tf_cands_by_date.get(date_str, [])
                             if c['ticker'] not in held_set]
                elif strategy_name == 'MR':
                    cands = [c for c in mr_candidates.get(date_str, [])
                             if c['ticker'] not in held_set]
                elif strategy_name == 'BO':
                    cands = [c for c in bo_candidates.get(date_str, [])
                             if c['ticker'] not in held_set]
                elif strategy_name == 'MP':
                    cands = [c for c in mp_candidates.get(date_str, [])
                             if c['ticker'] not in held_set]
                else:
                    cands = []

                for cand in cands[:open_slots]:
                    nxt_idx = day_idx + 1
                    if nxt_idx >= len(trading_dates):
                        break
                    nxt_date = trading_dates[nxt_idx]
                    nxt_ts   = pd.Timestamp(nxt_date)
                    nxt_row, _ = _get_row(cand['ticker'], nxt_ts)
                    if nxt_row is None:
                        continue
                    entry_price = float(nxt_row['open'])
                    if entry_price <= 0:
                        continue

                    if sizing_mode == 'equity':
                        today_equity = cash
                        for _p in positions:
                            _d, _ = _get_row(_p.ticker, ts)
                            today_equity += _p.shares * (float(_d['close']) if _d is not None else _p.entry_price)
                        alloc = min(today_equity / max_positions, cash)
                    else:
                        alloc = min(cash / max_positions, cash)

                    if alloc < min_position_amount:
                        continue

                    shares = int(alloc // entry_price)
                    if shares <= 0:
                        continue
                    actual_cost = shares * entry_price
                    if actual_cost > cash:
                        continue

                    atr = float(cand['atr'])

                    # 전략별 SL / TP 계산
                    if strategy_name == 'TF':
                        sl_price  = adjust_price(entry_price - atr * V27_PARAMS.stop_loss_atr, "down")
                        tp1_price = adjust_price(entry_price + atr * V27_PARAMS.take_profit_atr, "up")
                        tp2_price = (adjust_price(entry_price + atr * V27_PARAMS.tp2_atr, "up")
                                     if V27_PARAMS.tp2_atr > 0 else 0.0)
                    elif strategy_name in ('BO', 'MP'):
                        sl_price  = float(entry_price - atr * 2.0)
                        tp1_price = 0.0
                        tp2_price = 0.0
                    else:  # MR
                        sl_price  = 0.0
                        tp1_price = 0.0
                        tp2_price = 0.0

                    cash -= actual_cost
                    open_slots -= 1

                    positions.append(DivPosition(
                        ticker=cand['ticker'],
                        name=ticker_names.get(cand['ticker'], cand['ticker']),
                        strategy=strategy_name,
                        entry_date=nxt_date,
                        entry_price=entry_price,
                        shares=shares,
                        initial_shares=shares,
                        atr_at_entry=atr,
                        stop_price=float(sl_price),
                        tp1_price=float(tp1_price),
                        tp2_price=float(tp2_price),
                        highest_since_entry=entry_price,
                        entry_adx=float(cand.get('adx', 25.0) or 25.0),
                        allocated_capital=actual_cost,
                    ))
                    held_set.add(cand['ticker'])

        # ── 4. equity 기록 ────────────────────────────────────────────────────
        portfolio_value = cash
        for pos in positions:
            day, _ = _get_row(pos.ticker, ts)
            portfolio_value += pos.shares * (float(day['close']) if day is not None else pos.entry_price)
            day_stats[pos.strategy] += 1  # 보유 포지션 반영

        equity_curve.append((date_str, portfolio_value))
        daily_stats[date_str] = dict(day_stats)

        max_concurrent = max(max_concurrent, len(positions))
        concurrent_sum += len(positions)

        if day_idx % 500 == 0 and day_idx > 0:
            logger.info(f"Day {day_idx}/{len(trading_dates)}: equity={portfolio_value:,.0f}, "
                        f"positions={len(positions)}, breadth={breadth:.0%}")

    # ── 미청산 포지션 강제 청산 ────────────────────────────────────────────────
    last_date = trading_dates[-1]
    last_ts   = pd.Timestamp(last_date)
    for pos in positions:
        last_row, _ = _get_row(pos.ticker, last_ts)
        if last_row is not None:
            ep  = float(last_row['close'])
            mkt = ticker_market_map.get(pos.ticker, 'KOSPI')
            atv = float(last_row.get('avg_trading_value_20', 1e10) or 1e10)
            tc  = COST.total_cost_pct_dynamic(mkt, pos.shares * ep, atv, SLIP)
            pnl = pos.shares * pos.entry_price * (ep / pos.entry_price - 1) \
                  - pos.shares * pos.entry_price * tc
            trades.append(PortfolioTradeResult(
                ticker=pos.ticker,
                name=pos.name,
                entry_date=pos.entry_date,
                entry_price=pos.entry_price,
                exit_date=last_date,
                exit_price=ep,
                exit_reason='FINAL_CLOSE',
                hold_days=pos.hold_days,
                shares=pos.shares,
                pnl_amount=pnl,
                pnl_pct=(ep / pos.entry_price - 1) - tc,
                is_partial=pos.tp1_triggered,
                initial_shares=pos.initial_shares,
            ))

    avg_concurrent = concurrent_sum / len(trading_dates) if trading_dates else 0
    result = _compute_portfolio_summary(
        initial_capital, equity_curve, trades, trading_dates, avg_concurrent, max_concurrent, 1
    )
    return result, daily_stats


# ─────────────────────────────────────────────────────────────────────────────
# 활성일 + 상보성 분석
# ─────────────────────────────────────────────────────────────────────────────

def compute_complementarity(
    standalone_stats: dict,   # {'V27': daily_stats_v27, 'MR': daily_stats_mr, ...}
    trading_dates: list,
) -> dict:
    """전략별 활성일 + 페어별 겹침 비율 계산."""
    active = {}
    for name, ds in standalone_stats.items():
        active[name] = {d for d, s in ds.items() if s.get(name, 0) > 0}

    n_total = len(trading_dates)
    result  = {}

    for name, active_set in active.items():
        result[name] = {
            'active_days':  len(active_set),
            'active_pct':   len(active_set) / n_total * 100,
        }

    pairs = [('TF', 'MR'), ('TF', 'BO'), ('TF', 'MP')]
    for a, b in pairs:
        if a not in active or b not in active:
            continue
        overlap = len(active[a] & active[b])
        b_while_a_off = len(active[b] - active[a])
        key = f'{a}_X_{b}'
        result[key] = {
            'overlap_days': overlap,
            'overlap_pct':  overlap / max(len(active[a]), 1) * 100,
            f'{b}_while_{a}_off': b_while_a_off,
        }
    return result


# ─────────────────────────────────────────────────────────────────────────────
# 메인
# ─────────────────────────────────────────────────────────────────────────────

def main():
    out_path = ROOT / "experiments" / "results_strategy_diversify.txt"
    SEP  = "=" * 70

    lines = []

    def log(msg=""):
        print(msg)
        lines.append(msg)

    def fmt_pf(pf):
        return "inf" if pf == float('inf') else f"{pf:.2f}"

    t_total = time.time()

    # ── 1. 데이터 로드 ─────────────────────────────────────────────────────────
    print("\n[1/4] 데이터 로드...")
    t0 = time.time()
    preloaded = load_backtest_data(V27_PARAMS)
    print(f"  완료: {time.time() - t0:.1f}s")

    print("  추가 지표 계산 (high_max_20, ret_60)...")
    add_extra_indicators(preloaded['ticker_data'])

    # ── 2. 시그널 사전 계산 ────────────────────────────────────────────────────
    print("\n[2/4] 시그널 사전 계산...")
    t0 = time.time()

    print("  TF v2.7 precompute...")
    tf_precomp = precompute_daily_signals(
        preloaded['trading_dates'],
        preloaded['ticker_data'],
        preloaded['ticker_date_idx'],
        set(preloaded['initial_universe']),
        params=V27_PARAMS,
        kospi_ret_map=preloaded.get('kospi_ret_map'),
        kosdaq_ret_map=preloaded.get('kosdaq_ret_map'),
        ticker_market=preloaded.get('ticker_market'),
        weights=V27_WEIGHTS,
    )
    print(f"    완료: {time.time() - t0:.1f}s")

    universe_at = tf_precomp['universe_at']

    t1 = time.time()
    print("  MR/BO/MP precompute...")
    mr_cands = precompute_mr_signals(
        preloaded['trading_dates'], preloaded['ticker_data'],
        preloaded['ticker_date_idx'], universe_at,
    )
    bo_cands = precompute_bo_signals(
        preloaded['trading_dates'], preloaded['ticker_data'],
        preloaded['ticker_date_idx'], universe_at,
    )
    mp_cands = precompute_mp_signals(
        preloaded['trading_dates'], preloaded['ticker_data'],
        preloaded['ticker_date_idx'], universe_at,
    )
    print(f"    완료: {time.time() - t1:.1f}s")

    # 빈 candidates (미사용 전략 대체용)
    empty_cands = {d: [] for d in preloaded['trading_dates']}

    # ── 3. 7개 변형 실행 ────────────────────────────────────────────────────────
    print("\n[3/4] 변형 실행 (7개)...")
    VARIANTS = [
        {'label': '[0] V27_ONLY',      'strategies': ['TF']},
        {'label': '[1] MR_ONLY',       'strategies': ['MR']},
        {'label': '[2] BO_ONLY',       'strategies': ['BO']},
        {'label': '[3] MP_ONLY',       'strategies': ['MP']},
        {'label': '[4] V27 + MR',      'strategies': ['TF', 'MR']},
        {'label': '[5] V27 + BO + MP', 'strategies': ['TF', 'MP', 'BO']},
        {'label': '[6] V27 + ALL',     'strategies': ['TF', 'MP', 'BO', 'MR']},
    ]

    results = {}
    daily_stats_all = {}  # label → daily_stats

    for v in VARIANTS:
        label      = v['label']
        strategies = v['strategies']
        print(f"  {label}...")
        t0 = time.time()

        result, daily_stats = run_diversified_backtest(
            active_strategies=strategies,
            preloaded_data=preloaded,
            tf_precomputed=tf_precomp,
            mr_candidates=mr_cands  if 'MR' in strategies else empty_cands,
            bo_candidates=bo_cands  if 'BO' in strategies else empty_cands,
            mp_candidates=mp_cands  if 'MP' in strategies else empty_cands,
            initial_capital=CAPITAL,
            max_positions=MAX_POS,
        )
        results[label]          = result
        daily_stats_all[label]  = daily_stats
        pnl = result.final_capital - result.initial_capital
        print(f"    PF={fmt_pf(result.profit_factor)}  CAGR={result.cagr_pct*100:+.1f}%  "
              f"MDD=-{result.max_drawdown_pct*100:.1f}%  건수={result.total_trades}  "
              f"순손익={pnl:+,.0f}원  ({time.time()-t0:.1f}s)")

    # ── 4. 상보성 분석 ─────────────────────────────────────────────────────────
    print("\n[4/4] 상보성 분석...")
    standalone_map = {
        'TF': '[0] V27_ONLY',
        'MR': '[1] MR_ONLY',
        'BO': '[2] BO_ONLY',
        'MP': '[3] MP_ONLY',
    }

    # daily_stats에서 전략별 활성일을 뽑기 위해 strategy 이름 기준으로 재구성
    standalone_daily = {}
    for strat, label in standalone_map.items():
        ds = daily_stats_all.get(label, {})
        # 전략 이름 기준으로 재매핑 (키를 전략명으로 통일)
        standalone_daily[strat] = {d: {strat: s.get(strat, 0)} for d, s in ds.items()}

    comp = compute_complementarity(standalone_daily, preloaded['trading_dates'])

    # 자본 활용도 (avg positions / max_positions)
    utilization = {}
    for label, r in results.items():
        utilization[label] = r.avg_positions / MAX_POS * 100

    # ─────────────────────────────────────────────────────────────────────────
    # 보고서 작성
    # ─────────────────────────────────────────────────────────────────────────
    log(SEP)
    log("📋 전략 다각화 타당성 검증 (10M/5종목, 2014~2026)")
    log(f"   기간: {preloaded['trading_dates'][0]} ~ {preloaded['trading_dates'][-1]}")
    log(SEP)

    log("")
    log("■ 개별 전략 수익성 (독립 실행)")
    log("  전략               건수    WR      PF    CAGR     MDD    순손익")
    log("  " + "-" * 62)
    for label in ['[0] V27_ONLY', '[1] MR_ONLY', '[2] BO_ONLY', '[3] MP_ONLY']:
        r   = results[label]
        pnl = r.final_capital - r.initial_capital
        log(
            f"  {label:20s}  {r.total_trades:4d}  {r.win_rate*100:5.1f}%  "
            f"{fmt_pf(r.profit_factor):>5}  {r.cagr_pct*100:+5.1f}%  "
            f"-{r.max_drawdown_pct*100:4.1f}%  {pnl:+11,.0f}"
        )

    log("")
    log("■ 상보성 분석 — 전략별 활성일 (보유 포지션 1개+ 일수)")
    log(f"  총 거래일: {len(preloaded['trading_dates'])}일")
    log("")
    log(f"  {'전략':6s}  {'활성일':>6s}  {'활성%':>7s}")
    log("  " + "-" * 28)
    for strat in ['TF', 'MR', 'BO', 'MP']:
        if strat in comp:
            info = comp[strat]
            log(f"  {strat:6s}  {info['active_days']:6d}  {info['active_pct']:6.1f}%")

    log("")
    log("  ── 겹침 분석 (TF 비활성 구간에서 보완 전략 활성 여부) ──")
    log(f"  {'페어':12s}  {'겹침일':>6s}  {'겹침%':>7s}  {'TF OFF + 보완 ON':>18s}")
    log("  " + "-" * 52)
    for a, b in [('TF', 'MR'), ('TF', 'BO'), ('TF', 'MP')]:
        key = f'{a}_X_{b}'
        if key in comp:
            info = comp[key]
            log(
                f"  {a}↔{b:10s}  {info['overlap_days']:6d}  {info['overlap_pct']:6.1f}%  "
                f"{info.get(f'{b}_while_{a}_off', 0):>18d}일"
            )

    log("")
    log("■ 결합 포트폴리오 (전략 우선순위: TF > MP > BO > MR)")
    log(f"  {'변형':22s}  {'건수':>5}  {'WR':>6}  {'PF':>5}  {'CAGR':>6}  "
        f"{'MDD':>6}  {'활용%':>6}  {'순손익':>12}")
    log("  " + "-" * 80)

    for label in [v['label'] for v in VARIANTS]:
        r    = results[label]
        pnl  = r.final_capital - r.initial_capital
        util = utilization[label]
        log(
            f"  {label:22s}  {r.total_trades:5d}  {r.win_rate*100:5.1f}%  "
            f"{fmt_pf(r.profit_factor):>5}  {r.cagr_pct*100:+5.1f}%  "
            f"-{r.max_drawdown_pct*100:4.1f}%  {util:5.1f}%  {pnl:+12,.0f}"
        )

    log("")
    log("■ 기준선 대비 결합 효과")
    r_base = results['[0] V27_ONLY']
    log(f"  기준선 (V27_ONLY): PF {fmt_pf(r_base.profit_factor)}  "
        f"CAGR {r_base.cagr_pct*100:+.1f}%  MDD -{r_base.max_drawdown_pct*100:.1f}%  "
        f"활용 {utilization['[0] V27_ONLY']:.1f}%")
    log("")
    log(f"  {'변형':22s}  {'ΔCAGR':>7}  {'ΔMDD':>8}  {'Δ활용':>7}  {'ΔPF':>6}")
    log("  " + "-" * 56)
    for label in ['[4] V27 + MR', '[5] V27 + BO + MP', '[6] V27 + ALL']:
        r   = results[label]
        dcagr = (r.cagr_pct - r_base.cagr_pct) * 100
        dmdd  = (r_base.max_drawdown_pct - r.max_drawdown_pct) * 100  # 양수=개선
        dutil = utilization[label] - utilization['[0] V27_ONLY']
        dpf   = r.profit_factor - r_base.profit_factor
        log(
            f"  {label:22s}  {dcagr:+6.1f}%p  {dmdd:+7.1f}%p  "
            f"{dutil:+6.1f}%p  {dpf:+5.2f}"
        )

    log("")
    log("■ 판정")

    # 각 보완 전략의 단독 PF
    strat_labels = {
        'MR': '[1] MR_ONLY',
        'BO': '[2] BO_ONLY',
        'MP': '[3] MP_ONLY',
    }
    for sname, slabel in strat_labels.items():
        r   = results[slabel]
        pf  = r.profit_factor
        pf_s = fmt_pf(pf)
        verdict = "✅ 단독 수익" if pf > 1.0 else "❌ 단독 손실"
        log(f"  Q1 {sname} 단독 수익성:  PF {pf_s}  → {verdict}")

    log("")
    # 상보성
    for a, b in [('TF', 'MR'), ('TF', 'BO'), ('TF', 'MP')]:
        key = f'{a}_X_{b}'
        if key in comp:
            off_days = comp[key].get(f'{b}_while_{a}_off', 0)
            tf_days  = comp.get('TF', {}).get('active_days', 1)
            verdict  = "✅ 상보적" if off_days > tf_days * 0.3 else "⚠ 겹침 많음"
            log(f"  Q2 {b} 상보성: TF 비활성 구간 {b} 활성 {off_days}일  → {verdict}")

    log("")
    # Q3 활용도
    best_util_label = max(results.keys(), key=lambda k: utilization[k])
    log(f"  Q3 자본활용 최고: {best_util_label}  {utilization[best_util_label]:.1f}%  "
        f"(기준선 {utilization['[0] V27_ONLY']:.1f}%)")

    # Q4 CAGR/MDD
    def cagr_mdd(r):
        return r.cagr_pct / r.max_drawdown_pct if r.max_drawdown_pct > 0 else 0.0

    best_cm_label = max(
        ['[0] V27_ONLY', '[4] V27 + MR', '[5] V27 + BO + MP', '[6] V27 + ALL'],
        key=lambda k: cagr_mdd(results[k]),
    )
    log(f"  Q4 CAGR/MDD 최고: {best_cm_label}  {cagr_mdd(results[best_cm_label]):.2f}  "
        f"(기준선 {cagr_mdd(r_base):.2f})")

    log("")
    log(f"  총 실행 시간: {(time.time() - t_total) / 60:.1f}분")
    log(SEP)

    with open(out_path, "w", encoding="utf-8") as f:
        for line in lines:
            f.write(line + "\n")

    print(f"\n결과 저장: {out_path}")


if __name__ == "__main__":
    main()
