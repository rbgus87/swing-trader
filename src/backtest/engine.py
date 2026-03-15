"""pandas 기반 백테스트 엔진.

CLI: python -m src.backtest.engine --strategy macd_rsi --period 2y
"""

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
from loguru import logger
from pykrx import stock

from data.column_mapper import OHLCV_MAP, map_columns
from src.strategy.signals import calculate_indicators

# 비용 모델
COMMISSION_RATE = 0.00015  # 수수료 0.015% (편도)
TAX_RATE = 0.002  # 거래세 0.2% (매도만)
SLIPPAGE_RATE = 0.001  # 슬리피지 0.1%


@dataclass
class BacktestResult:
    """백테스트 결과 지표."""

    total_return: float  # 총 수익률 (%)
    annual_return: float  # 연환산 수익률 (%)
    max_drawdown: float  # MDD (%)
    sharpe_ratio: float
    sortino_ratio: float
    win_rate: float  # 승률 (%)
    profit_factor: float
    avg_trade_return: float  # 평균 거래 수익 (%)
    trade_count: int
    avg_hold_days: float
    params: dict = field(default_factory=dict)  # 사용된 파라미터


class BacktestEngine:
    """pandas 기반 백테스트 실행 엔진."""

    def __init__(self, initial_capital: int = 10_000_000):
        self.initial_capital = initial_capital

    def load_price_data(
        self, codes: list[str], start_date: str, end_date: str
    ) -> dict[str, pd.DataFrame]:
        """pykrx로 일봉 데이터 로드 + 컬럼 매핑.

        Args:
            codes: 종목 코드 리스트.
            start_date: 시작일 (YYYYMMDD).
            end_date: 종료일 (YYYYMMDD).

        Returns:
            {종목코드: OHLCV DataFrame} 딕셔너리.
        """
        result = {}
        for code in codes:
            try:
                df = stock.get_market_ohlcv_by_date(start_date, end_date, code)
                if df.empty or len(df) < 60:
                    logger.warning(
                        f"{code}: 데이터 부족 ({len(df)}행), 건너뜀"
                    )
                    continue
                df = map_columns(df, OHLCV_MAP)
                df = df.reset_index(drop=True)
                result[code] = df
                logger.info(f"{code}: {len(df)}행 로드 완료")
            except Exception as e:
                logger.error(f"{code}: 데이터 로드 실패 - {e}")
        return result

    def generate_signals(
        self, df: pd.DataFrame, params: dict | None = None
    ) -> tuple[pd.Series, pd.Series]:
        """지표 기반 entry/exit 시그널 생성.

        Look-ahead bias 방지:
        - entry = 조건 충족.shift(1)  # 전일 신호 -> 익일 진입
        - exit = 조건 충족.shift(1)

        Args:
            df: OHLCV DataFrame (영문 컬럼).
            params: 지표 파라미터 딕셔너리.

        Returns:
            (entries, exits) 불리언 시리즈 튜플.
        """
        p = params or {}

        # 지표 파라미터 추출
        indicator_params = {
            "macd_fast": p.get("macd_fast", 12),
            "macd_slow": p.get("macd_slow", 26),
            "macd_signal": p.get("macd_signal", 9),
            "rsi_period": p.get("rsi_period", 14),
        }

        df_ind = calculate_indicators(df, **indicator_params)

        # 신호 파라미터
        rsi_min = p.get("rsi_min", 35)
        rsi_max = p.get("rsi_max", 70)
        volume_multiplier = p.get("volume_multiplier", 1.2)
        target_return = p.get("target_return", 0.08)
        stop_atr_mult = p.get("stop_atr_mult", 1.5)

        # --- Entry 조건 (AND) ---
        cond_ma = df_ind["close"] > df_ind["sma20"]
        cond_macd = (df_ind["macd_hist"] > 0) & (
            df_ind["macd_hist"].shift(1) < 0
        )
        cond_rsi = (df_ind["rsi"] >= rsi_min) & (df_ind["rsi"] <= rsi_max)
        cond_vol = df_ind["volume"] >= (
            df_ind["volume_sma20"] * volume_multiplier
        )

        raw_entries = cond_ma & cond_macd & cond_rsi & cond_vol

        # --- Exit 조건 (OR) ---
        cond_macd_dead = (df_ind["macd_hist"] < 0) & (
            df_ind["macd_hist"].shift(1) > 0
        )
        cond_rsi_high = df_ind["rsi"] > 70
        cond_stop = df_ind["close"] < (
            df_ind["close"].shift(1) - df_ind["atr"] * stop_atr_mult
        )

        raw_exits = cond_macd_dead | cond_rsi_high | cond_stop

        # Look-ahead bias 방지: shift(1) 적용
        entries = raw_entries.shift(1).astype("boolean").fillna(False).astype(bool)
        exits = raw_exits.shift(1).astype("boolean").fillna(False).astype(bool)

        # 인덱스를 원본 df_ind에 맞춤
        entries.index = df_ind.index
        exits.index = df_ind.index

        return entries, exits

    def _simulate_portfolio(
        self, close: pd.Series, entries: pd.Series, exits: pd.Series
    ) -> tuple[list[dict], pd.Series]:
        """순수 pandas 기반 포트폴리오 시뮬레이션.

        Args:
            close: 종가 시리즈.
            entries: 매수 신호 불리언 시리즈.
            exits: 매도 신호 불리언 시리즈.

        Returns:
            (trades_list, equity_curve) 튜플.
        """
        cash = self.initial_capital
        position = 0  # 보유 주식 수
        entry_price = 0
        entry_idx = 0
        trades = []
        equity = []

        for i in range(len(close)):
            price = int(close.iloc[i])

            if position == 0 and entries.iloc[i]:
                # 매수
                cost_per_share = price * (1 + COMMISSION_RATE + SLIPPAGE_RATE)
                shares = int(cash // cost_per_share)
                if shares > 0:
                    position = shares
                    entry_price = price
                    entry_idx = i
                    cash -= shares * cost_per_share

            elif position > 0 and exits.iloc[i]:
                # 매도
                proceeds_per_share = price * (
                    1 - COMMISSION_RATE - SLIPPAGE_RATE - TAX_RATE
                )
                cash += position * proceeds_per_share
                pnl_pct = (price - entry_price) / entry_price
                trades.append(
                    {
                        "entry_idx": entry_idx,
                        "exit_idx": i,
                        "entry_price": entry_price,
                        "exit_price": price,
                        "shares": position,
                        "return": pnl_pct,
                        "hold_days": i - entry_idx,
                    }
                )
                position = 0

            # 자산 추적
            equity_val = cash + (position * price if position > 0 else 0)
            equity.append(equity_val)

        return trades, pd.Series(equity, index=close.index)

    def _calculate_metrics(
        self, trades: list[dict], equity: pd.Series, params: dict
    ) -> BacktestResult:
        """거래 내역과 자산 곡선에서 성과 지표 계산.

        Args:
            trades: 개별 거래 딕셔너리 리스트.
            equity: 자산 곡선 시리즈.
            params: 사용된 파라미터 딕셔너리.

        Returns:
            BacktestResult 성과 지표.
        """
        total_return = (
            (equity.iloc[-1] - self.initial_capital) / self.initial_capital * 100
        )

        # MDD
        peak = equity.cummax()
        drawdown = (equity - peak) / peak * 100
        max_drawdown = drawdown.min()

        # Sharpe (일간 수익률, 연환산)
        daily_returns = equity.pct_change().dropna()
        if len(daily_returns) > 0 and daily_returns.std() > 0:
            sharpe = (daily_returns.mean() / daily_returns.std()) * np.sqrt(252)
        else:
            sharpe = 0.0

        # Sortino (하방 편차)
        negative_returns = daily_returns[daily_returns < 0]
        if len(negative_returns) > 0:
            downside_std = negative_returns.std()
            sortino = (
                (daily_returns.mean() / downside_std) * np.sqrt(252)
                if downside_std > 0
                else 0.0
            )
        else:
            sortino = 0.0

        # 거래 지표
        trade_count = len(trades)
        if trade_count > 0:
            returns = [t["return"] for t in trades]
            win_count = sum(1 for r in returns if r > 0)
            win_rate = win_count / trade_count * 100

            gross_profit = sum(r for r in returns if r > 0)
            gross_loss = abs(sum(r for r in returns if r < 0))
            profit_factor = gross_profit / gross_loss if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0.0)

            avg_trade_return = np.mean(returns) * 100
            avg_hold_days = np.mean([t["hold_days"] for t in trades])
        else:
            win_rate = 0.0
            profit_factor = 0.0
            avg_trade_return = 0.0
            avg_hold_days = 0.0

        # 연환산 수익률
        total_days = len(equity)
        years = total_days / 252 if total_days > 0 else 1
        if total_return > -100:
            annual_return = (
                (1 + total_return / 100) ** (1 / years) - 1
            ) * 100
        else:
            annual_return = -100.0

        return BacktestResult(
            total_return=round(total_return, 2),
            annual_return=round(annual_return, 2),
            max_drawdown=round(max_drawdown, 2),
            sharpe_ratio=round(sharpe, 2),
            sortino_ratio=round(sortino, 2),
            win_rate=round(win_rate, 2),
            profit_factor=round(profit_factor, 2),
            avg_trade_return=round(avg_trade_return, 2),
            trade_count=trade_count,
            avg_hold_days=round(avg_hold_days, 1),
            params=params,
        )

    def run(
        self,
        codes: list[str],
        start_date: str,
        end_date: str,
        params: dict | None = None,
    ) -> BacktestResult:
        """백테스트 실행.

        Steps:
        1. 데이터 로드
        2. 지표 계산 + 신호 생성 (look-ahead bias 방지)
        3. pandas 기반 포트폴리오 시뮬레이션
        4. 성과 지표 계산

        Args:
            codes: 종목 코드 리스트.
            start_date: 시작일 (YYYYMMDD).
            end_date: 종료일 (YYYYMMDD).
            params: 전략 파라미터 딕셔너리.

        Returns:
            BacktestResult 성과 지표.
        """
        price_data = self.load_price_data(codes, start_date, end_date)
        if not price_data:
            logger.error("유효한 데이터 없음, 빈 결과 반환")
            return BacktestResult(
                total_return=0.0,
                annual_return=0.0,
                max_drawdown=0.0,
                sharpe_ratio=0.0,
                sortino_ratio=0.0,
                win_rate=0.0,
                profit_factor=0.0,
                avg_trade_return=0.0,
                trade_count=0,
                avg_hold_days=0.0,
                params=params or {},
            )

        all_results = []

        for code, df in price_data.items():
            try:
                entries, exits = self.generate_signals(df, params)

                # calculate_indicators가 dropna하므로 close를 맞춤
                indicator_params = {
                    "macd_fast": (params or {}).get("macd_fast", 12),
                    "macd_slow": (params or {}).get("macd_slow", 26),
                    "macd_signal": (params or {}).get("macd_signal", 9),
                    "rsi_period": (params or {}).get("rsi_period", 14),
                }
                df_ind = calculate_indicators(df, **indicator_params)
                close = df_ind["close"]

                # pandas 기반 시뮬레이션
                trades, equity = self._simulate_portfolio(
                    close, entries, exits
                )
                result = self._calculate_metrics(
                    trades, equity, params or {}
                )

                all_results.append(result)
                logger.info(
                    f"{code}: 수익률 {result.total_return:.2f}%, "
                    f"MDD {result.max_drawdown:.2f}%, "
                    f"거래 {result.trade_count}건"
                )
            except Exception as e:
                logger.error(f"{code}: 백테스트 실패 - {e}")

        if not all_results:
            return BacktestResult(
                total_return=0.0,
                annual_return=0.0,
                max_drawdown=0.0,
                sharpe_ratio=0.0,
                sortino_ratio=0.0,
                win_rate=0.0,
                profit_factor=0.0,
                avg_trade_return=0.0,
                trade_count=0,
                avg_hold_days=0.0,
                params=params or {},
            )

        # 다중 종목: 평균 집계
        if len(all_results) == 1:
            return all_results[0]

        avg_result = BacktestResult(
            total_return=np.mean([r.total_return for r in all_results]),
            annual_return=np.mean([r.annual_return for r in all_results]),
            max_drawdown=np.min([r.max_drawdown for r in all_results]),
            sharpe_ratio=np.mean([r.sharpe_ratio for r in all_results]),
            sortino_ratio=np.mean([r.sortino_ratio for r in all_results]),
            win_rate=np.mean([r.win_rate for r in all_results]),
            profit_factor=np.mean([r.profit_factor for r in all_results]),
            avg_trade_return=np.mean(
                [r.avg_trade_return for r in all_results]
            ),
            trade_count=sum(r.trade_count for r in all_results),
            avg_hold_days=np.mean([r.avg_hold_days for r in all_results]),
            params=params or {},
        )
        return avg_result


def _parse_period(period_str: str) -> tuple[str, str]:
    """기간 문자열을 시작일/종료일로 변환.

    Args:
        period_str: '1y', '2y', '6m' 등.

    Returns:
        (start_date, end_date) YYYYMMDD 형식 튜플.
    """
    end_date = datetime.now()
    if period_str.endswith("y"):
        years = int(period_str[:-1])
        start_date = end_date - timedelta(days=years * 365)
    elif period_str.endswith("m"):
        months = int(period_str[:-1])
        start_date = end_date - timedelta(days=months * 30)
    else:
        raise ValueError(f"지원하지 않는 기간 형식: {period_str}")
    return start_date.strftime("%Y%m%d"), end_date.strftime("%Y%m%d")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="백테스트 엔진 CLI")
    parser.add_argument(
        "--strategy",
        type=str,
        default="macd_rsi",
        help="전략 이름 (기본: macd_rsi)",
    )
    parser.add_argument(
        "--period", type=str, default="2y", help="백테스트 기간 (예: 1y, 2y, 6m)"
    )
    parser.add_argument(
        "--codes",
        type=str,
        nargs="+",
        default=["005930"],
        help="종목 코드 (기본: 005930 삼성전자)",
    )
    parser.add_argument(
        "--start", type=str, default=None, help="시작일 (YYYYMMDD)"
    )
    parser.add_argument(
        "--end", type=str, default=None, help="종료일 (YYYYMMDD)"
    )
    parser.add_argument(
        "--capital",
        type=int,
        default=10_000_000,
        help="초기 자본금 (기본: 10,000,000원)",
    )

    args = parser.parse_args()

    if args.start and args.end:
        start_date, end_date = args.start, args.end
    else:
        start_date, end_date = _parse_period(args.period)

    logger.info(
        f"백테스트 시작: {args.strategy} | "
        f"기간: {start_date}~{end_date} | "
        f"종목: {args.codes}"
    )

    engine = BacktestEngine(initial_capital=args.capital)
    result = engine.run(args.codes, start_date, end_date)

    from src.backtest.report import BacktestReporter

    reporter = BacktestReporter()
    reporter.print_summary(result)
