"""스윙 백테스트 엔진 — 단일 종목 버전.

Phase 2 Step 1a. 단일 종목에 대해 TrendFollowing v0 전략 신호를 시뮬레이션.
포트폴리오(동시 보유 제한)는 Step 1b에서 구현.
"""
from dataclasses import dataclass, field
from typing import Optional
import pandas as pd
import numpy as np
from loguru import logger


@dataclass
class TradeResult:
    """개별 거래 결과."""
    ticker: str
    entry_date: str
    entry_price: float
    exit_date: str
    exit_price: float
    exit_reason: str
    hold_days: int
    pnl_pct: float
    pnl_amount: float
    atr_at_entry: float


@dataclass
class CostModel:
    """거래 비용."""
    buy_commission: float = 0.00015
    sell_commission: float = 0.00015
    sell_tax: float = 0.0018
    slippage: float = 0.0005

    def total_cost_pct(self) -> float:
        return (self.buy_commission + self.slippage +
                self.sell_commission + self.sell_tax + self.slippage)


@dataclass
class BacktestResult:
    """백테스트 종합 결과."""
    ticker: str
    period: str
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    profit_factor: float
    total_return_pct: float
    avg_return_pct: float
    avg_hold_days: float
    max_drawdown_pct: float
    avg_winner_pct: float
    avg_loser_pct: float
    trades: list = field(default_factory=list)
    exit_reason_dist: dict = field(default_factory=dict)


def run_single_backtest(
    df: pd.DataFrame,
    ticker: str,
    signals: list,
    params,
    cost: CostModel = CostModel(),
    initial_capital: float = 1_250_000,
) -> BacktestResult:
    """단일 종목 백테스트 실행."""
    df = df.copy().sort_index() if isinstance(df.index, pd.DatetimeIndex) else df.copy()
    if 'date' not in df.columns:
        df['date'] = df.index
    df['date'] = pd.to_datetime(df['date'])
    df = df.reset_index(drop=True)

    date_to_idx = {d.strftime('%Y-%m-%d'): i for i, d in enumerate(df['date'])}

    from src.strategy.trend_following_v0 import calculate_indicators
    df = calculate_indicators(df, params)

    trades = []
    total_cost_pct = cost.total_cost_pct()

    occupied_dates = set()

    for signal in signals:
        if signal.signal_type != 'ENTRY':
            continue

        signal_date = signal.date[:10]
        if signal_date not in date_to_idx:
            continue

        signal_idx = date_to_idx[signal_date]
        entry_idx = signal_idx + 1

        if entry_idx >= len(df):
            continue

        if signal_date in occupied_dates:
            continue

        entry_row = df.iloc[entry_idx]
        entry_price = entry_row['open']
        entry_date = entry_row['date'].strftime('%Y-%m-%d')
        entry_atr = signal.atr

        if entry_price <= 0 or entry_atr <= 0:
            continue

        stop_price = entry_price - entry_atr * params.stop_loss_atr
        tp1_price = entry_price + entry_atr * params.take_profit_atr

        position_size = 1.0
        highest_since_entry = entry_price
        exit_price = None
        exit_date = None
        exit_reason = None
        hold_days = 0

        for j in range(entry_idx + 1, min(entry_idx + params.max_hold_days + 1, len(df))):
            day = df.iloc[j]
            hold_days += 1
            highest_since_entry = max(highest_since_entry, day['high'])
            trailing_stop = highest_since_entry - entry_atr * params.trailing_atr

            # 1. 손절
            if day['low'] <= stop_price:
                exit_price = stop_price
                exit_reason = 'STOP_LOSS'
                exit_date = day['date'].strftime('%Y-%m-%d')
                break

            # 2. 1차 익절 (기록만)
            if position_size == 1.0 and day['high'] >= tp1_price:
                position_size = 0.5

            # 3. 트레일링
            if day['low'] <= trailing_stop:
                exit_price = trailing_stop
                exit_reason = 'TRAILING'
                exit_date = day['date'].strftime('%Y-%m-%d')
                break

            # 4. 추세 이탈 (종가 기준 교차)
            if pd.notna(day.get('ma5')) and pd.notna(day.get('ma20')):
                prev_day = df.iloc[j - 1]
                if (pd.notna(prev_day.get('ma5')) and pd.notna(prev_day.get('ma20'))):
                    if prev_day['ma5'] >= prev_day['ma20'] and day['ma5'] < day['ma20']:
                        exit_price = day['close']
                        exit_reason = 'TREND_EXIT'
                        exit_date = day['date'].strftime('%Y-%m-%d')
                        break

        # 5. 시간 청산
        if exit_price is None:
            last_idx = min(entry_idx + params.max_hold_days, len(df) - 1)
            last_day = df.iloc[last_idx]
            exit_price = last_day['close']
            exit_reason = 'TIME_EXIT'
            exit_date = last_day['date'].strftime('%Y-%m-%d')
            hold_days = last_idx - entry_idx

        gross_pnl_pct = (exit_price / entry_price) - 1
        net_pnl_pct = gross_pnl_pct - total_cost_pct
        pnl_amount = initial_capital * net_pnl_pct

        trades.append(TradeResult(
            ticker=ticker,
            entry_date=entry_date,
            entry_price=entry_price,
            exit_date=exit_date,
            exit_price=exit_price,
            exit_reason=exit_reason,
            hold_days=hold_days,
            pnl_pct=net_pnl_pct,
            pnl_amount=pnl_amount,
            atr_at_entry=entry_atr,
        ))

        for k in range(entry_idx, min(entry_idx + hold_days + 1, len(df))):
            occupied_dates.add(df.iloc[k]['date'].strftime('%Y-%m-%d'))

    return _compute_summary(ticker, df, trades)


def _compute_summary(ticker: str, df: pd.DataFrame, trades: list) -> BacktestResult:
    """거래 리스트에서 종합 지표 계산."""
    period_str = f"{df['date'].iloc[0].strftime('%Y-%m-%d')} ~ {df['date'].iloc[-1].strftime('%Y-%m-%d')}"

    if not trades:
        return BacktestResult(
            ticker=ticker, period=period_str,
            total_trades=0, winning_trades=0, losing_trades=0,
            win_rate=0, profit_factor=0, total_return_pct=0,
            avg_return_pct=0, avg_hold_days=0, max_drawdown_pct=0,
            avg_winner_pct=0, avg_loser_pct=0, trades=trades,
        )

    winners = [t for t in trades if t.pnl_pct > 0]
    losers = [t for t in trades if t.pnl_pct <= 0]

    gross_profit = sum(t.pnl_pct for t in winners) if winners else 0
    gross_loss = abs(sum(t.pnl_pct for t in losers)) if losers else 0
    pf = gross_profit / gross_loss if gross_loss > 0 else float('inf')

    cum_returns = []
    cum = 1.0
    for t in sorted(trades, key=lambda x: x.entry_date):
        cum *= (1 + t.pnl_pct)
        cum_returns.append(cum)

    peak = cum_returns[0]
    max_dd = 0
    for cr in cum_returns:
        peak = max(peak, cr)
        dd = (peak - cr) / peak
        max_dd = max(max_dd, dd)

    exit_dist = {}
    for t in trades:
        exit_dist[t.exit_reason] = exit_dist.get(t.exit_reason, 0) + 1

    return BacktestResult(
        ticker=ticker,
        period=period_str,
        total_trades=len(trades),
        winning_trades=len(winners),
        losing_trades=len(losers),
        win_rate=len(winners) / len(trades) if trades else 0,
        profit_factor=pf,
        total_return_pct=sum(t.pnl_pct for t in trades),
        avg_return_pct=float(np.mean([t.pnl_pct for t in trades])),
        avg_hold_days=float(np.mean([t.hold_days for t in trades])),
        max_drawdown_pct=max_dd,
        avg_winner_pct=float(np.mean([t.pnl_pct for t in winners])) if winners else 0,
        avg_loser_pct=float(np.mean([t.pnl_pct for t in losers])) if losers else 0,
        trades=trades,
        exit_reason_dist=exit_dist,
    )
