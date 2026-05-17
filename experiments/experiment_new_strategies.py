"""신규 전략 알파 스크리닝 실험.

5개 전략 가설을 경량 미니-백테스터로 독립 검증한다.
- H1: 우량주 과매도 반등 (Quality Mean Reversion)
- H2: VCP 변동성 수축 돌파 (Volatility Contraction Pattern)
- H3: 갭업 모멘텀 (Gap-Up Momentum)
- H4: SKIP (기관/외인 수급 — DB에 해당 컬럼 없음)
- H5: 52주 신고가 근접 돌파 (52-Week High Breakout)

판정 기준: PF >= 1.10 AND trades >= 100 → "알파 존재"
합산 검증: 조건 통과 전략 + v2.7 결합 포트폴리오 (별도 슬롯)

제약:
- v2.7 파라미터/로직 변경 금지
- 전략 파라미터 IS/OOS 없이 최적화 금지 (기본값으로만 검증)
- 비용 0.30% 미만 설정 금지
- PF < 1.0 전략에 대해 "파라미터 조정하면 될 것" 판단 금지
"""
from __future__ import annotations

import sys
import io
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Windows cp949 환경에서 한글 출력 깨짐 방지
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
elif hasattr(sys.stdout, 'buffer'):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

import math
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd
from loguru import logger

from src.backtest.portfolio_backtester import (
    load_backtest_data,
    precompute_daily_signals,
    run_portfolio_backtest,
    PortfolioResult,
)
from src.strategy.trend_following_v2 import StrategyParams
from src.strategy.ranking import RankingWeights
from src.utils.tick_size import adjust_price


# ============================================================================
# 공통 상수
# ============================================================================

CAPITAL = 10_000_000
MAX_POSITIONS = 5          # 미니 백테스터 단독 슬롯 수
MIN_AMOUNT = 300_000
COST_PCT = 0.003           # 고정 비용 0.30% (왕복)
ALPHA_PF = 1.10            # 알파 판정 PF 임계값
ALPHA_TRADES = 100         # 알파 판정 최소 거래 수
SEP = "=" * 70


# ============================================================================
# 지표 추가 (DB에 없는 것만 계산)
# ============================================================================

def add_strategy_indicators(ticker_data: dict) -> None:
    """H1~H5에 필요한 보조 지표를 ticker_data 각 DataFrame에 인플레이스 추가.

    계산 항목:
      ma50, ma150         — VCP(H2) Minervini 조건
      rsi14               — H1 과매도 판정
      high_52w            — H5 52주 신고가 근접
      atr_ma60            — H2 변동성 수축 비율 (현재 ATR / 60일 평균 ATR)
      prev_close          — H3 갭업 비율 기준
      ret_5               — H3 사전 조정 확인
      ret_60              — H1, H5 모멘텀 필터
      high_max_20         — H2/H5 근접 고점
      vol_ma20            — H3 거래량 급증 비교
    """
    for df in ticker_data.values():
        c = df['close']
        h = df['high']

        df['ma50'] = c.rolling(50).mean()
        df['ma150'] = c.rolling(150).mean()

        # RSI 14
        delta = c.diff()
        gain = delta.clip(lower=0)
        loss = (-delta).clip(lower=0)
        avg_gain = gain.ewm(com=13, adjust=False).mean()
        avg_loss = loss.ewm(com=13, adjust=False).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        df['rsi14'] = 100 - (100 / (1 + rs))

        # 52주(252영업일) 고가
        df['high_52w'] = h.rolling(252).max()

        # ATR 60일 이동평균 → 변동성 수축 비율
        if 'atr' in df.columns:
            df['atr_ma60'] = df['atr'].rolling(60).mean()
        else:
            df['atr_ma60'] = np.nan

        # 갭업 기준
        df['prev_close'] = c.shift(1)

        # 단기·중기 수익률
        df['ret_5'] = c.pct_change(5)
        df['ret_60'] = c.pct_change(60)

        # 20일 고가 최대
        df['high_max_20'] = h.rolling(20).max()

        # 거래량 20일 평균
        df['vol_ma20'] = df['volume'].rolling(20).mean()


# ============================================================================
# 신호 사전 계산 (각 전략별 후보 리스트)
# ============================================================================

def _safe_row(df: pd.DataFrame, i: int) -> pd.Series | None:
    """인덱스 범위 체크 후 행 반환."""
    if i < 0 or i >= len(df):
        return None
    return df.iloc[i]


def precompute_h1_signals(
    trading_dates: list,
    ticker_data: dict,
    ticker_date_idx: dict,
    universe_at: dict,
) -> dict:
    """H1: 우량주 과매도 반등 (Quality Mean Reversion).

    조건 (모두 AND, 오늘 종가 기준):
      - ret_60 > 0          (60일 모멘텀 양수 — 추세 살아있음)
      - close / ma20 < 0.95 (MA20 대비 -5% 이상 이격)
      - rsi14 < 35          (과매도)
      - close > open        (당일 양봉 — 반등 시작)

    점수: 0.95 - (close/ma20) → 이격도 클수록 우선순위 높음
    진입: 익일 시가
    청산: close >= ma20 (회복) OR close <= entry * 0.94 (손절) OR 7일 만기
    """
    candidates: dict[str, list] = {}

    for i, date_str in enumerate(trading_dates):
        univ = universe_at.get(date_str, set())
        if not univ:
            continue

        day_cands = []
        ts = pd.Timestamp(date_str)

        for ticker in univ:
            df = ticker_data.get(ticker)
            if df is None:
                continue
            ci = ticker_date_idx[ticker].get(ts)
            if ci is None or ci < 70:  # ret_60 최소 필요
                continue

            row = df.iloc[ci]
            if pd.isna(row.get('ma20')) or pd.isna(row.get('rsi14')) or pd.isna(row.get('ret_60')):
                continue
            if row['ma20'] <= 0:
                continue

            close = row['close']
            ma20 = row['ma20']
            disp = close / ma20
            ret60 = row['ret_60']
            rsi = row['rsi14']
            open_p = row['open']

            if ret60 > 0 and disp < 0.95 and rsi < 35 and close > open_p:
                score = 0.95 - disp
                day_cands.append({
                    'ticker': ticker,
                    'score': score,
                    'close': close,
                    'atr': row.get('atr', close * 0.03),
                    'ma20': ma20,
                })

        if day_cands:
            day_cands.sort(key=lambda x: -x['score'])
            candidates[date_str] = day_cands

    return candidates


def precompute_h2_signals(
    trading_dates: list,
    ticker_data: dict,
    ticker_date_idx: dict,
    universe_at: dict,
) -> dict:
    """H2: VCP 변동성 수축 돌파 (Minervini).

    조건 (모두 AND):
      - ma50 > ma150 > (ma200 있으면 ma200 체크 생략, 없을 경우 건너뜀)
        → 실제로는 ma50 > ma150 (장기 추세) 만 사용
      - atr / atr_ma60 < 0.70  (현재 변동성이 60일 평균의 70% 미만 = 수축)
      - high_max_20 내에서 돌파: close > high_max_20 (전일까지의 20일 고가)
        (오늘 종가가 전일 기준 20일 고점을 돌파)
      - volume >= vol_ma20 * 1.5  (거래량 확인)

    점수: 수축 비율 역수 (atr_ratio 낮을수록 우선)
    진입: 익일 시가
    청산: ATR×1.5 손절 / ATR×3.0 트레일링 / 20일 만기
    """
    candidates: dict[str, list] = {}

    for i, date_str in enumerate(trading_dates):
        univ = universe_at.get(date_str, set())
        if not univ:
            continue

        day_cands = []
        ts = pd.Timestamp(date_str)

        for ticker in univ:
            df = ticker_data.get(ticker)
            if df is None:
                continue
            ci = ticker_date_idx[ticker].get(ts)
            if ci is None or ci < 160:  # ma150 최소 필요
                continue

            row = df.iloc[ci]
            prev_row = df.iloc[ci - 1] if ci >= 1 else None

            needed = ['ma50', 'ma150', 'atr', 'atr_ma60', 'high_max_20', 'vol_ma20']
            if any(pd.isna(row.get(c)) for c in needed):
                continue
            if prev_row is None or pd.isna(prev_row.get('high_max_20')):
                continue

            ma50 = row['ma50']
            ma150 = row['ma150']
            atr = row['atr']
            atr_ma60 = row['atr_ma60']
            close = row['close']
            volume = row['volume']
            vol_ma20 = row['vol_ma20']
            prev_high_max20 = prev_row['high_max_20']  # 전일 기준 20일 고가

            if atr_ma60 <= 0 or vol_ma20 <= 0:
                continue

            atr_ratio = atr / atr_ma60

            if (
                ma50 > ma150
                and atr_ratio < 0.70
                and close > prev_high_max20
                and volume >= vol_ma20 * 1.5
            ):
                score = 1.0 / atr_ratio  # 수축 강할수록 높은 점수
                day_cands.append({
                    'ticker': ticker,
                    'score': score,
                    'close': close,
                    'atr': atr,
                })

        if day_cands:
            day_cands.sort(key=lambda x: -x['score'])
            candidates[date_str] = day_cands

    return candidates


def precompute_h3_signals(
    trading_dates: list,
    ticker_data: dict,
    ticker_date_idx: dict,
    universe_at: dict,
) -> dict:
    """H3: 갭업 모멘텀 (Gap-Up Momentum).

    조건 (모두 AND):
      - open / prev_close >= 1.02  (2% 이상 갭업 시가)
      - volume >= vol_ma20 * 2.0   (거래량 2배 이상)
      - ret_5 < 0.00               (최근 5일 횡보/하락 — 갭 전 조정)
      - close >= open              (갭업 봉에서 양봉 유지)

    점수: 갭 비율 (높을수록 우선)
    진입: 당일 조건 확인 → 익일 시가
    청산: 진입가 × 0.97 손절 / 진입가 × 1.06 목표 / 5일 만기
    """
    candidates: dict[str, list] = {}

    for i, date_str in enumerate(trading_dates):
        univ = universe_at.get(date_str, set())
        if not univ:
            continue

        day_cands = []
        ts = pd.Timestamp(date_str)

        for ticker in univ:
            df = ticker_data.get(ticker)
            if df is None:
                continue
            ci = ticker_date_idx[ticker].get(ts)
            if ci is None or ci < 25:
                continue

            row = df.iloc[ci]
            needed = ['prev_close', 'vol_ma20', 'ret_5']
            if any(pd.isna(row.get(c)) for c in needed):
                continue

            prev_close = row['prev_close']
            open_p = row['open']
            close = row['close']
            volume = row['volume']
            vol_ma20 = row['vol_ma20']
            ret_5 = row['ret_5']

            if prev_close <= 0 or vol_ma20 <= 0:
                continue

            gap_ratio = open_p / prev_close - 1.0

            if (
                gap_ratio >= 0.02
                and volume >= vol_ma20 * 2.0
                and ret_5 < 0.00
                and close >= open_p
            ):
                day_cands.append({
                    'ticker': ticker,
                    'score': gap_ratio,
                    'close': close,
                    'atr': row.get('atr', close * 0.03),
                })

        if day_cands:
            day_cands.sort(key=lambda x: -x['score'])
            candidates[date_str] = day_cands

    return candidates


def precompute_h5_signals(
    trading_dates: list,
    ticker_data: dict,
    ticker_date_idx: dict,
    universe_at: dict,
) -> dict:
    """H5: 52주 신고가 근접 돌파.

    조건 (모두 AND):
      - 0.95 × high_52w <= close   (52주 고가의 95% 이상)
      - close >= high_52w * 0.995  (52주 고가 근접 또는 돌파)
        → 즉: close ∈ [0.95×high_52w, high_52w × 1.005] 범위
        (0.995를 곱해 근접 포착, 상한선은 고점 갱신 여부 무관)
      - ma20 > ma60                (중단기 추세 정배열)
      - volume >= vol_ma20 * 1.3   (거래량 확인)
      - adx >= 20                  (추세 강도)

    점수: close / high_52w (52주 고가 대비 비율)
    진입: 익일 시가
    청산: ATR×2.5 손절 / ATR×3.5 트레일링 / 25일 만기
    """
    candidates: dict[str, list] = {}

    for i, date_str in enumerate(trading_dates):
        univ = universe_at.get(date_str, set())
        if not univ:
            continue

        day_cands = []
        ts = pd.Timestamp(date_str)

        for ticker in univ:
            df = ticker_data.get(ticker)
            if df is None:
                continue
            ci = ticker_date_idx[ticker].get(ts)
            if ci is None or ci < 260:  # high_52w(252일) + 여유
                continue

            row = df.iloc[ci]
            needed = ['high_52w', 'ma20', 'ma60', 'vol_ma20', 'adx']
            if any(pd.isna(row.get(c)) for c in needed):
                continue

            close = row['close']
            high_52w = row['high_52w']
            ma20 = row['ma20']
            ma60 = row['ma60']
            volume = row['volume']
            vol_ma20 = row['vol_ma20']
            adx = row['adx']

            if high_52w <= 0 or vol_ma20 <= 0:
                continue

            ratio = close / high_52w

            if (
                ratio >= 0.95
                and ratio >= 0.995      # 52주 고가의 99.5% 이상 = 진짜 근접
                and ma20 > ma60
                and volume >= vol_ma20 * 1.3
                and adx >= 20
            ):
                day_cands.append({
                    'ticker': ticker,
                    'score': ratio,
                    'close': close,
                    'atr': row.get('atr', close * 0.03),
                    'high_52w': high_52w,
                })

        if day_cands:
            day_cands.sort(key=lambda x: -x['score'])
            candidates[date_str] = day_cands

    return candidates


# ============================================================================
# 미니 포지션 + 백테스터
# ============================================================================

@dataclass
class MiniPos:
    ticker: str
    strategy: str
    entry_date: str
    entry_price: float
    shares: int
    stop_price: float       # 절대 손절가 (전략별 상이)
    target_price: float     # 목표가 (H1, H3) / 0 = 없음
    trailing_atr: float     # 트레일링용 ATR 배수 (H2, H5) / 0 = 없음
    atr_at_entry: float
    max_hold_days: int
    highest_since_entry: float = 0.0
    hold_days: int = 0
    allocated: float = 0.0


def _resolve_entry_price(df: pd.DataFrame, date_idx: dict, entry_date_str: str) -> float | None:
    """진입일 시가 반환. 없으면 None."""
    ts = pd.Timestamp(entry_date_str)
    idx = date_idx.get(ts)
    if idx is None:
        return None
    return float(df.iloc[idx]['open'])


def run_mini_backtest(
    strategy_id: str,
    candidates: dict,        # {date_str: [cand_dict]}
    trading_dates: list,
    ticker_data: dict,
    ticker_date_idx: dict,
    ticker_names: dict,
    max_positions: int = MAX_POSITIONS,
    initial_capital: float = CAPITAL,
    min_amount: float = MIN_AMOUNT,
    cost_pct: float = COST_PCT,
) -> dict:
    """경량 미니 백테스터.

    - 진입: 신호일 다음 날 시가 (candidates[date] → 익일 open)
    - 비용: 매매 왕복 cost_pct (진입 시 절반, 청산 시 절반 부과)
    - 사이징: equity / max_positions, cash 상한
    - 청산: 전략별 stop_price / target_price / trailing / max_hold_days
    """
    cash = initial_capital
    positions: list[MiniPos] = []
    equity_curve = []
    trades = []

    date_to_next = {}
    for i, d in enumerate(trading_dates[:-1]):
        date_to_next[d] = trading_dates[i + 1]

    for date_str in trading_dates:
        ts = pd.Timestamp(date_str)

        # ── 청산 처리 ────────────────────────────────────────────────
        remaining = []
        for pos in positions:
            df = ticker_data.get(pos.ticker)
            if df is None:
                remaining.append(pos)
                continue

            ci = ticker_date_idx[pos.ticker].get(ts)
            if ci is None:
                pos.hold_days += 1
                remaining.append(pos)
                continue

            row = df.iloc[ci]
            day_open = row['open']
            day_high = row['high']
            day_low = row['low']
            day_close = row['close']

            pos.hold_days += 1
            pos.highest_since_entry = max(pos.highest_since_entry, day_high)

            # 트레일링 스탑 갱신 (H2, H5)
            effective_stop = pos.stop_price
            if pos.trailing_atr > 0 and pos.atr_at_entry > 0:
                trail_candidate = adjust_price(
                    pos.highest_since_entry - pos.atr_at_entry * pos.trailing_atr,
                    direction='up',
                )
                effective_stop = max(pos.stop_price, trail_candidate)

            exit_price = None
            exit_reason = None

            # 1. 손절
            if day_low <= effective_stop:
                exit_price = effective_stop
                exit_reason = 'STOP'

            # 2. 목표가 (H1 MA20 회복, H3 고정 목표)
            elif pos.target_price > 0 and day_high >= pos.target_price:
                exit_price = pos.target_price
                exit_reason = 'TARGET'

            # 3. 최대 보유일
            elif pos.hold_days >= pos.max_hold_days:
                exit_price = day_close
                exit_reason = 'MAX_HOLD'

            if exit_price is not None:
                gross = exit_price * pos.shares
                fee = gross * (cost_pct / 2)
                net = gross - fee
                cash += net

                ep_net = pos.entry_price * (1 + cost_pct / 2)
                pnl = net - ep_net * pos.shares
                pnl_pct = (exit_price / pos.entry_price - 1) - cost_pct

                trades.append({
                    'ticker': pos.ticker,
                    'strategy': pos.strategy,
                    'entry_date': pos.entry_date,
                    'exit_date': date_str,
                    'entry_price': pos.entry_price,
                    'exit_price': exit_price,
                    'exit_reason': exit_reason,
                    'hold_days': pos.hold_days,
                    'pnl': pnl,
                    'pnl_pct': pnl_pct,
                })
            else:
                remaining.append(pos)

        positions = remaining

        # ── 진입 처리 ────────────────────────────────────────────────
        next_date = date_to_next.get(date_str)
        if next_date and len(positions) < max_positions:
            total_equity = cash + sum(
                (ticker_data[p.ticker].iloc[
                    ticker_date_idx[p.ticker].get(ts, -1)
                ]['close'] if ticker_date_idx[p.ticker].get(ts) is not None
                 else p.entry_price) * p.shares
                for p in positions
                if p.ticker in ticker_data
            )

            slots_free = max_positions - len(positions)
            alloc_per = total_equity / max_positions
            holding_tickers = {p.ticker for p in positions}

            day_cands = candidates.get(date_str, [])
            entered = 0
            for cand in day_cands:
                if entered >= slots_free:
                    break
                ticker = cand['ticker']
                if ticker in holding_tickers:
                    continue

                df = ticker_data.get(ticker)
                if df is None:
                    continue

                # 익일 시가
                next_ts = pd.Timestamp(next_date)
                ni = ticker_date_idx[ticker].get(next_ts)
                if ni is None:
                    continue

                entry_price = float(df.iloc[ni]['open'])
                if entry_price <= 0:
                    continue

                alloc = min(alloc_per, cash)
                if alloc < min_amount:
                    continue

                shares = int(alloc / (entry_price * (1 + cost_pct / 2)))
                if shares <= 0:
                    continue

                cost_paid = entry_price * shares * (cost_pct / 2)
                cash -= entry_price * shares + cost_paid

                atr = cand.get('atr', entry_price * 0.03)
                close = cand['close']

                # 전략별 청산 파라미터
                if strategy_id == 'H1':
                    stop_price = entry_price * 0.94
                    target_price = cand.get('ma20', entry_price * 1.05)  # MA20 회복
                    trailing_atr = 0.0
                    max_hold = 7

                elif strategy_id == 'H2':
                    stop_price = adjust_price(entry_price - atr * 1.5, direction='up')
                    target_price = 0.0
                    trailing_atr = 3.0
                    max_hold = 20

                elif strategy_id == 'H3':
                    stop_price = entry_price * 0.97
                    target_price = entry_price * 1.06
                    trailing_atr = 0.0
                    max_hold = 5

                elif strategy_id == 'H5':
                    stop_price = adjust_price(entry_price - atr * 2.5, direction='up')
                    target_price = 0.0
                    trailing_atr = 3.5
                    max_hold = 25

                else:
                    continue

                pos = MiniPos(
                    ticker=ticker,
                    strategy=strategy_id,
                    entry_date=next_date,
                    entry_price=entry_price,
                    shares=shares,
                    stop_price=stop_price,
                    target_price=target_price,
                    trailing_atr=trailing_atr,
                    atr_at_entry=atr,
                    max_hold_days=max_hold,
                    highest_since_entry=entry_price,
                    allocated=alloc,
                )
                positions.append(pos)
                holding_tickers.add(ticker)
                entered += 1

        # ── equity 기록 ──────────────────────────────────────────────
        port_value = 0.0
        for pos in positions:
            df = ticker_data.get(pos.ticker)
            if df is None:
                port_value += pos.entry_price * pos.shares
                continue
            ci = ticker_date_idx[pos.ticker].get(ts)
            if ci is None:
                port_value += pos.entry_price * pos.shares
            else:
                port_value += df.iloc[ci]['close'] * pos.shares
        equity_curve.append({'date': date_str, 'equity': cash + port_value})

    # 잔여 포지션 강제 청산
    last_date = trading_dates[-1] if trading_dates else '2026-12-31'
    last_ts = pd.Timestamp(last_date)
    for pos in positions:
        df = ticker_data.get(pos.ticker)
        if df is None:
            continue
        ci = ticker_date_idx[pos.ticker].get(last_ts)
        if ci is None:
            continue
        exit_price = float(df.iloc[ci]['close'])
        gross = exit_price * pos.shares
        fee = gross * (cost_pct / 2)
        net = gross - fee
        pnl = net - pos.entry_price * pos.shares * (1 + cost_pct / 2)
        trades.append({
            'ticker': pos.ticker,
            'strategy': strategy_id,
            'entry_date': pos.entry_date,
            'exit_date': last_date,
            'entry_price': pos.entry_price,
            'exit_price': exit_price,
            'exit_reason': 'FORCED_EXIT',
            'hold_days': pos.hold_days,
            'pnl': pnl,
            'pnl_pct': (exit_price / pos.entry_price - 1) - cost_pct,
        })

    return {
        'trades': trades,
        'equity_curve': equity_curve,
        'initial_capital': initial_capital,
    }


# ============================================================================
# 통계 집계
# ============================================================================

def compute_stats(result: dict) -> dict:
    """미니 백테스터 결과로부터 핵심 통계 계산."""
    trades = result['trades']
    equity_curve = result['equity_curve']
    initial_capital = result['initial_capital']

    if not trades:
        return {
            'trades': 0, 'win_rate': 0, 'profit_factor': 0,
            'cagr': 0, 'mdd': 0, 'avg_hold': 0, 'final_equity': initial_capital,
        }

    wins = [t for t in trades if t['pnl'] > 0]
    losses = [t for t in trades if t['pnl'] <= 0]

    gross_profit = sum(t['pnl'] for t in wins)
    gross_loss = abs(sum(t['pnl'] for t in losses))
    pf = gross_profit / gross_loss if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0)

    wr = len(wins) / len(trades)
    avg_hold = sum(t['hold_days'] for t in trades) / len(trades)

    # CAGR
    equities = [e['equity'] for e in equity_curve]
    final_eq = equities[-1] if equities else initial_capital
    n_years = len(equity_curve) / 252.0
    cagr = (final_eq / initial_capital) ** (1.0 / n_years) - 1 if n_years > 0 else 0.0

    # MDD
    peak = initial_capital
    max_dd = 0.0
    for eq in equities:
        if eq > peak:
            peak = eq
        dd = (eq - peak) / peak
        if dd < max_dd:
            max_dd = dd

    # 청산 사유 분포
    reason_dist = {}
    for t in trades:
        r = t['exit_reason']
        reason_dist[r] = reason_dist.get(r, 0) + 1

    return {
        'trades': len(trades),
        'win_rate': wr,
        'profit_factor': pf,
        'cagr': cagr,
        'mdd': max_dd,
        'avg_hold': avg_hold,
        'final_equity': final_eq,
        'reason_dist': reason_dist,
    }


# ============================================================================
# 결합 포트폴리오 (v2.7 + 통과 전략)
# ============================================================================

def run_combined_backtest(
    winner_strategy_id: str,
    winner_candidates: dict,
    preloaded_data: dict,
    tf_precomputed: dict,
) -> tuple:
    """v2.7 단독 기준선 + 통과 전략 (2슬롯) 각각 실행 후 반환.

    슬롯 독립 운용 — TF 5슬롯 / 보완 전략 2슬롯.
    결합 equity는 호출자에서 단순 합산(자본 공유 없음).
    """
    logger.info(f"결합 백테스트: v2.7 + {winner_strategy_id}")

    # v2.7 단독 기준선
    tf_params = StrategyParams(
        stop_loss_atr=3.0, take_profit_atr=1.5, trailing_atr=3.0,
        adx_threshold=25.0, relative_strength_threshold=0.08,
        tp1_sell_ratio=0.10, tp2_atr=4.0, tp2_sell_ratio=0.10,
        max_hold_days=20,
    )

    tf_only = run_portfolio_backtest(
        preloaded_data=preloaded_data,
        precomputed=tf_precomputed,
        params=tf_params,
        max_positions=5,
        initial_capital=CAPITAL,
        min_position_amount=MIN_AMOUNT,
        sizing_mode='equity',
        regime_gate_enabled=True,
        slippage_params=None,   # None → 동적 슬리피지 기본값 (v2.7 동일)
    )

    # 보완 전략 단독 (2슬롯, 같은 자본)
    compl_result = run_mini_backtest(
        strategy_id=winner_strategy_id,
        candidates=winner_candidates,
        trading_dates=preloaded_data['trading_dates'],
        ticker_data=preloaded_data['ticker_data'],
        ticker_date_idx=preloaded_data['ticker_date_idx'],
        ticker_names=preloaded_data['ticker_names'],
        max_positions=2,
        initial_capital=CAPITAL,
        min_amount=MIN_AMOUNT,
        cost_pct=COST_PCT,
    )

    return tf_only, compl_result


# ============================================================================
# 보고서 출력
# ============================================================================

def _pf_str(pf: float) -> str:
    if pf >= 999:
        return "∞"
    return f"{pf:.2f}"


def print_strategy_report(strategy_id: str, label: str, stats: dict) -> str:
    """단일 전략 결과 한 줄 보고서."""
    t = stats['trades']
    wr = stats['win_rate'] * 100
    pf = _pf_str(stats['profit_factor'])
    cagr = stats['cagr'] * 100
    mdd = stats['mdd'] * 100
    ah = stats['avg_hold']

    alpha = stats['profit_factor'] >= ALPHA_PF and t >= ALPHA_TRADES
    verdict = "[PASS] 알파 존재" if alpha else "[FAIL] 알파 없음"

    line = (
        f"{strategy_id} {label:<30} | "
        f"거래 {t:4d}건 | WR {wr:5.1f}% | PF {pf:>6} | "
        f"CAGR {cagr:+6.1f}% | MDD {mdd:6.1f}% | "
        f"평균보유 {ah:4.1f}일 | {verdict}"
    )
    return line


def print_reason_dist(reason_dist: dict, total: int) -> list[str]:
    lines = []
    for r, cnt in sorted(reason_dist.items(), key=lambda x: -x[1]):
        lines.append(f"    {r:<20}: {cnt:4d}건 ({cnt/total*100:5.1f}%)")
    return lines


# ============================================================================
# 메인 실행
# ============================================================================

def main():
    print(SEP)
    print("신규 전략 알파 스크리닝 실험")
    print("기간: 2014-01-02 ~ 2026-05-15 (12년+)")
    print(f"비용: 고정 {COST_PCT*100:.2f}% (왕복)")
    print(f"판정: PF >= {ALPHA_PF} AND 거래 >= {ALPHA_TRADES}건 → 알파 존재")
    print("H4 (기관/외인 수급): DB에 inst_net/foreign_net 컬럼 없음 → SKIP")
    print(SEP)

    # ── 1. 데이터 로드 ──────────────────────────────────────────────
    print("\n[1/4] 데이터 로드 중...")
    tf_params = StrategyParams(
        stop_loss_atr=3.0, take_profit_atr=1.5, trailing_atr=3.0,
        adx_threshold=25.0, relative_strength_threshold=0.08,
        tp1_sell_ratio=0.10, tp2_atr=4.0, tp2_sell_ratio=0.10,
        max_hold_days=20,
    )
    preloaded = load_backtest_data(params=tf_params)

    trading_dates = preloaded['trading_dates']
    ticker_data = preloaded['ticker_data']
    ticker_date_idx = preloaded['ticker_date_idx']
    ticker_names = preloaded['ticker_names']

    print(f"    거래일: {len(trading_dates)}일 ({trading_dates[0]} ~ {trading_dates[-1]})")
    print(f"    종목 수: {len(ticker_data)}")

    # ── 2. 보조 지표 추가 ───────────────────────────────────────────
    print("\n[2/4] 보조 지표 계산 중 (ma50, ma150, rsi14, high_52w, atr_ma60 등)...")
    add_strategy_indicators(ticker_data)
    print("    완료")

    # ── 3. v2.7 신호 사전 계산 (universe_at 추출용) ─────────────────
    print("\n[3/4] v2.7 신호 사전 계산 (universe_at 추출)...")
    tf_weights = RankingWeights(
        rs=0.50, momentum_atr=0.20, adx=0.15, liquidity=0.10, ma_alignment=0.05,
    )
    tf_precomputed = precompute_daily_signals(
        trading_dates=trading_dates,
        ticker_data=ticker_data,
        ticker_date_idx=ticker_date_idx,
        initial_universe=preloaded['initial_universe'],
        params=tf_params,
        kospi_ret_map=preloaded['kospi_ret_map'],
        kosdaq_ret_map=preloaded['kosdaq_ret_map'],
        ticker_market=preloaded['ticker_market'],
        weights=tf_weights,
    )
    universe_at = tf_precomputed['universe_at']
    print(f"    Universe 스냅샷: {len(universe_at)}일분")

    # ── 4. 전략별 신호 계산 ──────────────────────────────────────────
    print("\n[4/4] 전략별 신호 계산 중...")

    print("    H1 우량주 과매도 반등...")
    h1_cands = precompute_h1_signals(trading_dates, ticker_data, ticker_date_idx, universe_at)
    print(f"    H1 신호일: {len(h1_cands)}일")

    print("    H2 VCP 변동성 수축 돌파...")
    h2_cands = precompute_h2_signals(trading_dates, ticker_data, ticker_date_idx, universe_at)
    print(f"    H2 신호일: {len(h2_cands)}일")

    print("    H3 갭업 모멘텀...")
    h3_cands = precompute_h3_signals(trading_dates, ticker_data, ticker_date_idx, universe_at)
    print(f"    H3 신호일: {len(h3_cands)}일")

    print("    H5 52주 신고가 근접 돌파...")
    h5_cands = precompute_h5_signals(trading_dates, ticker_data, ticker_date_idx, universe_at)
    print(f"    H5 신호일: {len(h5_cands)}일")

    # ── 5. 미니 백테스트 실행 ────────────────────────────────────────
    print(f"\n{SEP}")
    print("전략별 백테스트 (미니 백테스터, 비용 0.30% 고정)")
    print(SEP)

    strategies = [
        ('H1', '우량주 과매도 반등',    h1_cands),
        ('H2', 'VCP 변동성 수축 돌파',  h2_cands),
        ('H3', '갭업 모멘텀',           h3_cands),
        ('H5', '52주 신고가 근접 돌파', h5_cands),
    ]

    all_stats = {}
    winners = []

    for sid, label, cands in strategies:
        print(f"\n  {sid}: {label}")
        result = run_mini_backtest(
            strategy_id=sid,
            candidates=cands,
            trading_dates=trading_dates,
            ticker_data=ticker_data,
            ticker_date_idx=ticker_date_idx,
            ticker_names=ticker_names,
            max_positions=MAX_POSITIONS,
            initial_capital=CAPITAL,
            min_amount=MIN_AMOUNT,
            cost_pct=COST_PCT,
        )
        stats = compute_stats(result)
        all_stats[sid] = (label, stats, result)

        line = print_strategy_report(sid, label, stats)
        print(f"  {line}")

        if stats['trades'] > 0:
            reason_lines = print_reason_dist(stats.get('reason_dist', {}), stats['trades'])
            for rl in reason_lines:
                print(rl)

        if stats['profit_factor'] >= ALPHA_PF and stats['trades'] >= ALPHA_TRADES:
            winners.append((sid, label, cands, stats))

    # ── 6. 결과 요약 ─────────────────────────────────────────────────
    print(f"\n{SEP}")
    print("전략 요약 테이블")
    print(SEP)
    print(f"{'전략':<5} {'이름':<25} {'거래':>6} {'WR':>7} {'PF':>6} {'CAGR':>8} {'MDD':>7} {'보유':>6} {'판정'}")
    print("-" * 85)

    for sid, label, stats in [(s, l, st) for s, (l, st, _) in all_stats.items()]:
        t = stats['trades']
        wr = f"{stats['win_rate']*100:.1f}%"
        pf = _pf_str(stats['profit_factor'])
        cagr = f"{stats['cagr']*100:+.1f}%"
        mdd = f"{stats['mdd']*100:.1f}%"
        ah = f"{stats['avg_hold']:.1f}d"
        alpha = stats['profit_factor'] >= ALPHA_PF and t >= ALPHA_TRADES
        verdict = "PASS" if alpha else "FAIL"
        print(f"{sid:<5} {label:<25} {t:>6} {wr:>7} {pf:>6} {cagr:>8} {mdd:>7} {ah:>6} {verdict}")

    print("-" * 85)
    print(f"판정 기준: PF >= {ALPHA_PF} AND 거래 >= {ALPHA_TRADES}건")

    # ── 7. 합산 검증 (통과 전략이 있을 때만) ─────────────────────────
    if winners:
        print(f"\n{SEP}")
        print(f"통과 전략 발견: {[w[0] for w in winners]}")
        print("v2.7 + 보완 전략 결합 검증")
        print(SEP)

        for sid, label, cands, w_stats in winners:
            print(f"\n  [{sid}] v2.7 + {label} 결합")

            tf_result, compl_result = run_combined_backtest(
                winner_strategy_id=sid,
                winner_candidates=cands,
                preloaded_data=preloaded,
                tf_precomputed=tf_precomputed,
            )

            compl_stats = compute_stats(compl_result)

            # TF 결과
            tf_r = tf_result
            print(f"  v2.7 단독:  거래 {tf_r.total_trades:4d}건 | WR {tf_r.win_rate*100:.1f}% | "
                  f"PF {tf_r.profit_factor:.2f} | CAGR {tf_r.cagr_pct:+.1f}% | MDD {tf_r.max_drawdown_pct:.1f}%")

            print(f"  {sid} 보완:   거래 {compl_stats['trades']:4d}건 | WR {compl_stats['win_rate']*100:.1f}% | "
                  f"PF {_pf_str(compl_stats['profit_factor'])} | CAGR {compl_stats['cagr']*100:+.1f}% | "
                  f"MDD {compl_stats['mdd']*100:.1f}%")

            # 결합 equity (단순 합산 — 슬롯 독립)
            # tf_r.equity_curve: list of (date_str, value) tuples
            tf_eq = {e[0]: e[1] for e in tf_r.equity_curve}
            compl_eq = {e['date']: e['equity'] for e in compl_result['equity_curve']}

            combined_equity = []
            for d in trading_dates:
                tf_v = tf_eq.get(d, CAPITAL)
                co_v = compl_eq.get(d, CAPITAL)
                combined_equity.append({'date': d, 'equity': tf_v + co_v - CAPITAL})

            if combined_equity:
                c_eqs = [e['equity'] for e in combined_equity]
                c_final = c_eqs[-1]
                n_years = len(c_eqs) / 252.0
                c_cagr = (c_final / CAPITAL) ** (1.0 / n_years) - 1 if n_years > 0 else 0
                c_peak = CAPITAL
                c_mdd = 0.0
                for eq in c_eqs:
                    if eq > c_peak:
                        c_peak = eq
                    dd = (eq - c_peak) / c_peak
                    if dd < c_mdd:
                        c_mdd = dd

                print(f"  결합 추정:  자본 활용↑ | CAGR {c_cagr*100:+.1f}% | MDD {c_mdd*100:.1f}%")
                tf_score = tf_r.cagr_pct / abs(tf_r.max_drawdown_pct) if tf_r.max_drawdown_pct != 0 else 0
                c_score = (c_cagr * 100) / abs(c_mdd * 100) if c_mdd != 0 else 0
                direction = "개선" if c_score > tf_score else "악화"
                print(f"  CAGR/MDD:   v2.7 단독 {tf_score:.2f} -> 결합 {c_score:.2f} ({direction})")

    else:
        print(f"\n{SEP}")
        print("통과 전략 없음 — 결합 검증 생략")
        print("결론: 4개 전략 가설 모두 단독 알파 미확인")
        print("권고: 진입 필터 완화 또는 청산 로직 재설계가 필요하나,")
        print("      현재 기본값 검증 기준에서는 채택 불가.")
        print(SEP)

    # ── 8. 종합 결론 ─────────────────────────────────────────────────
    print(f"\n{SEP}")
    print("종합 결론")
    print(SEP)

    passing = [sid for sid, (label, stats, _) in all_stats.items()
               if stats['profit_factor'] >= ALPHA_PF and stats['trades'] >= ALPHA_TRADES]
    marginal = [sid for sid, (label, stats, _) in all_stats.items()
                if 1.0 <= stats['profit_factor'] < ALPHA_PF and stats['trades'] >= 50]
    failing = [sid for sid, (label, stats, _) in all_stats.items()
               if stats['profit_factor'] < 1.0 or stats['trades'] < 50]

    print(f"  알파 확인 (PF≥{ALPHA_PF}, 거래≥{ALPHA_TRADES}): {passing if passing else '없음'}")
    print(f"  경계선 (PF 1.0~{ALPHA_PF}):                      {marginal if marginal else '없음'}")
    print(f"  채택 불가 (PF<1.0 또는 거래<50):                  {failing if failing else '없음'}")

    if not passing:
        print("\n  → 현재 Universe + 비용 구조에서 검증된 독립 알파 없음.")
        print("    Phase 5 전략 개발은 실전 데이터 축적 후 재시도 권장.")

    print(SEP)


if __name__ == '__main__':
    main()
