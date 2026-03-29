"""pandas 기반 백테스트 엔진.

CLI: python -m src.backtest.engine --strategy golden_cross --period 2y
"""

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
from loguru import logger

from data.column_mapper import OHLCV_MAP, map_columns
from data.provider import get_provider
from src.strategy import get_strategy
from src.strategy.signals import calculate_indicators

# 비용 모델 기본값 (fallback)
_DEFAULT_COMMISSION = 0.00015  # 수수료 0.015% (편도)
_DEFAULT_TAX = 0.0015  # 거래세 0.15% (2025년, 매도만)
_DEFAULT_SLIPPAGE = 0.001  # 슬리피지 0.1%


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

    def __init__(self, initial_capital: int = 10_000_000, cost_config: dict | None = None):
        self.initial_capital = initial_capital
        self._price_cache: dict[str, pd.DataFrame] = {}

        # 비용 모델: cost_config > config.yaml > 기본값
        cc = cost_config or {}
        try:
            from src.utils.config import config as app_config
            bt = app_config.data.get("backtest", {})
        except Exception:
            bt = {}

        self.commission = cc.get("commission", bt.get("commission", _DEFAULT_COMMISSION))
        self.tax = cc.get("tax", bt.get("tax", _DEFAULT_TAX))
        self.slippage = cc.get("slippage", bt.get("slippage", _DEFAULT_SLIPPAGE))

    def clear_cache(self) -> None:
        """가격 데이터 캐시 초기화."""
        self._price_cache.clear()

    def load_price_data(
        self, codes: list[str], start_date: str, end_date: str,
        warmup_days: int = 280,
    ) -> dict[str, pd.DataFrame]:
        """pykrx로 일봉 데이터 로드 + 컬럼 매핑 (캐싱 지원).

        지표 계산(SMA60, SMA120 등)에 필요한 워밍업 기간을 자동으로
        시작일 이전에 추가 로딩합니다. 반환된 DataFrame에는 워밍업
        데이터가 포함되어 있으며, 호출부에서 원래 기간으로 슬라이싱합니다.

        캐시된 데이터가 요청 범위를 포함하면 슬라이싱으로 반환합니다.

        Args:
            codes: 종목 코드 리스트.
            start_date: 시작일 (YYYYMMDD).
            end_date: 종료일 (YYYYMMDD).
            warmup_days: 지표 워밍업용 추가 캘린더일 수 (기본 120일).

        Returns:
            {종목코드: OHLCV DataFrame} 딕셔너리.
        """
        from datetime import datetime as dt, timedelta

        # 워밍업 기간만큼 시작일을 앞당겨 데이터 로딩
        actual_start = dt.strptime(start_date, "%Y%m%d")
        warmup_start = (actual_start - timedelta(days=warmup_days)).strftime("%Y%m%d")
        warmup_start_ts = pd.Timestamp(dt.strptime(warmup_start, "%Y%m%d"))
        end_ts = pd.Timestamp(dt.strptime(end_date, "%Y%m%d"))

        provider = get_provider()
        result = {}
        for code in codes:
            try:
                # 캐시 히트: 캐시된 데이터가 요청 범위를 포함하면 슬라이싱
                # 종료일은 영업일 기준 5일 여유 허용 (pykrx 반환 범위 차이)
                if code in self._price_cache:
                    cached = self._price_cache[code]
                    end_margin = end_ts - pd.Timedelta(days=5)
                    if cached.index[0] <= warmup_start_ts and cached.index[-1] >= end_margin:
                        sliced = cached[(cached.index >= warmup_start_ts) & (cached.index <= end_ts)]
                        if len(sliced) >= 60:
                            result[code] = sliced
                            continue

                df = provider.get_ohlcv_by_date_range(code, warmup_start, end_date)
                if df.empty or len(df) < 60:
                    logger.warning(
                        f"{code}: 데이터 부족 ({len(df) if not df.empty else 0}행), 건너뜀"
                    )
                    continue
                # 캐시에 저장 (기존 캐시보다 범위가 넓으면 병합)
                if code in self._price_cache:
                    existing = self._price_cache[code]
                    merged = pd.concat([existing, df])
                    merged = merged[~merged.index.duplicated(keep="last")].sort_index()
                    self._price_cache[code] = merged
                else:
                    self._price_cache[code] = df.copy()
                result[code] = df
                logger.info(f"{code}: {len(df)}행 로드 완료 (워밍업 포함)")
            except Exception as e:
                logger.error(f"{code}: 데이터 로드 실패 - {e}")
        return result

    def preload_data(
        self, codes: list[str], start_date: str, end_date: str,
        warmup_days: int = 280,
    ) -> None:
        """전체 기간 데이터를 미리 로드하여 캐시에 저장.

        WF 검증 시 전체 기간을 한 번에 로드하면
        구간별 반복 로딩을 방지할 수 있습니다.
        """
        logger.info(f"데이터 프리로드: {len(codes)}종목, {start_date}~{end_date}")
        self.load_price_data(codes, start_date, end_date, warmup_days)
        logger.info(f"프리로드 완료: 캐시 {len(self._price_cache)}종목")

    def generate_signals(
        self, df: pd.DataFrame, params: dict | None = None,
        strategy_name: str = "momentum_pullback",
    ) -> tuple[pd.Series, pd.Series]:
        """전략 인터페이스를 통한 entry/exit 시그널 생성.

        Args:
            df: OHLCV DataFrame (영문 컬럼).
            params: 전략 파라미터 딕셔너리.
            strategy_name: 전략 이름 (config.yaml의 strategy.type).

        Returns:
            (entries, exits) 불리언 시리즈 튜플.
        """
        strategy = get_strategy(strategy_name, params or {})
        return strategy.generate_backtest_signals(df)

    def _simulate_portfolio(
        self,
        close: pd.Series,
        high: pd.Series,
        low: pd.Series,
        atr: pd.Series,
        entries: pd.Series,
        exits: pd.Series,
        params: dict | None = None,
        weekly_sma20: pd.Series | None = None,
        macd_hist: pd.Series | None = None,
    ) -> tuple[list[dict], pd.Series]:
        """포트폴리오 시뮬레이션 (손절/트레일링/목표가/부분매도/최대보유 포함).

        Args:
            close: 종가 시리즈.
            high: 고가 시리즈.
            low: 저가 시리즈.
            atr: ATR 시리즈.
            entries: 매수 신호 불리언 시리즈.
            exits: 매도 신호 불리언 시리즈.
            params: 전략 파라미터 딕셔너리.
            weekly_sma20: 주봉 SMA20 시리즈 (일봉 인덱스에 매핑, 선택).

        Returns:
            (trades_list, equity_curve) 튜플.
        """
        p = params or {}
        target_return = p.get("target_return", 0.10)
        stop_atr_mult = p.get("stop_atr_mult", 1.5)
        trailing_atr_mult = p.get("trailing_atr_mult", 2.5)
        trailing_activate_pct = p.get("trailing_activate_pct", 0.10)
        max_hold_days = p.get("max_hold_days", 10)
        max_stop_pct = p.get("max_stop_pct", 0.07)

        # 부분 매도 파라미터
        partial_enabled = p.get("partial_sell_enabled", True)
        partial_target_pct = p.get("partial_target_pct", 0.5)
        partial_sell_ratio = p.get("partial_sell_ratio", 0.5)

        cash = self.initial_capital
        position = 0  # 보유 주식 수
        entry_price = 0
        entry_idx = 0
        stop_price = 0
        target_price_val = 0
        high_since_entry = 0
        partial_done = False  # 부분 매도 완료 플래그
        trades = []
        equity = []

        for i in range(len(close)):
            price = int(close.iloc[i])
            bar_high = (
                int(high.iloc[i]) if not pd.isna(high.iloc[i]) else price
            )
            bar_low = (
                int(low.iloc[i]) if not pd.isna(low.iloc[i]) else price
            )
            current_atr = (
                float(atr.iloc[i])
                if not pd.isna(atr.iloc[i])
                else price * 0.02
            )

            if position > 0:
                # Update high since entry
                high_since_entry = max(high_since_entry, bar_high)

                # Check exit conditions (priority order)
                should_exit = False
                exit_price = price

                # 1. Stop loss
                if bar_low <= stop_price:
                    should_exit = True
                    exit_price = stop_price

                # 2a. Partial sell: 목표가의 N% 도달 시 절반 매도
                elif (
                    partial_enabled
                    and not partial_done
                    and target_price_val > 0
                ):
                    partial_trigger = int(
                        entry_price * (1 + target_return * partial_target_pct)
                    )
                    if bar_high >= partial_trigger:
                        sell_qty = max(1, int(position * partial_sell_ratio))
                        remaining = position - sell_qty
                        if remaining > 0:
                            # 부분 매도 기록
                            actual_exit = int(partial_trigger * (1 - self.slippage))
                            proceeds = actual_exit * (1 - self.commission - self.tax)
                            cash += sell_qty * proceeds
                            pnl_pct = (partial_trigger - entry_price) / entry_price
                            entry_date = self._format_date(close.index[entry_idx])
                            exit_date = self._format_date(close.index[i])
                            trades.append({
                                "entry_idx": entry_idx,
                                "exit_idx": i,
                                "entry_date": entry_date,
                                "exit_date": exit_date,
                                "entry_price": entry_price,
                                "exit_price": partial_trigger,
                                "shares": sell_qty,
                                "return": pnl_pct,
                                "hold_days": i - entry_idx,
                                "partial": True,
                            })
                            position = remaining
                            partial_done = True

                # 2b. Target reached (전량 매도)
                if not should_exit and bar_high >= target_price_val:
                    should_exit = True
                    exit_price = target_price_val

                # 3. Trailing stop update & check
                if not should_exit:
                    unrealized_pct = (price - entry_price) / entry_price
                    if unrealized_pct >= trailing_activate_pct:
                        trailing = int(
                            high_since_entry
                            - current_atr * trailing_atr_mult
                        )
                        trailing = max(trailing, stop_price)  # no retreat
                        if trailing > stop_price:
                            stop_price = trailing
                        if bar_low <= stop_price:
                            should_exit = True
                            exit_price = stop_price

                # 4. MACD 데드크로스 (수익 +2% 이상, macd_hist 음전환)
                if not should_exit and macd_hist is not None and i >= 1:
                    pnl_pct_unrealized = (price - entry_price) / entry_price
                    if pnl_pct_unrealized >= 0.02:
                        prev_hist = macd_hist.iloc[i - 1]
                        curr_hist = macd_hist.iloc[i]
                        if not np.isnan(prev_hist) and not np.isnan(curr_hist):
                            if prev_hist > 0 and curr_hist < 0:
                                should_exit = True
                                exit_price = price

                # 5. Max hold days
                if not should_exit and (i - entry_idx) >= max_hold_days:
                    should_exit = True
                    exit_price = price

                # 6. Signal-based exit (from generate_signals)
                if not should_exit and exits.iloc[i]:
                    should_exit = True
                    exit_price = price

                if should_exit:
                    actual_exit = int(exit_price * (1 - self.slippage))
                    proceeds = actual_exit * (1 - self.commission - self.tax)
                    cash += position * proceeds
                    pnl_pct = (exit_price - entry_price) / entry_price
                    entry_date = self._format_date(close.index[entry_idx])
                    exit_date = self._format_date(close.index[i])
                    trades.append(
                        {
                            "entry_idx": entry_idx,
                            "exit_idx": i,
                            "entry_date": entry_date,
                            "exit_date": exit_date,
                            "entry_price": entry_price,
                            "exit_price": exit_price,
                            "shares": position,
                            "return": pnl_pct,
                            "hold_days": i - entry_idx,
                        }
                    )
                    position = 0
                    partial_done = False

            elif position == 0 and entries.iloc[i]:
                # 주봉 SMA20 필터: 주간 종가 > SMA20일 때만 진입
                if weekly_sma20 is not None:
                    ws = weekly_sma20.iloc[i] if i < len(weekly_sma20) else np.nan
                    if not np.isnan(ws) and price <= ws:
                        equity_val = cash
                        equity.append(equity_val)
                        continue

                # 매수
                actual_entry = int(price * (1 + self.slippage))
                cost_per_share = actual_entry * (1 + self.commission)
                shares = int(cash // cost_per_share)
                if shares > 0:
                    position = shares
                    entry_price = price
                    entry_idx = i
                    high_since_entry = bar_high
                    partial_done = False
                    # Initial stop price
                    atr_stop = int(price - current_atr * stop_atr_mult)
                    pct_stop = int(price * (1 - max_stop_pct))
                    stop_price = max(atr_stop, pct_stop)  # tighter of two
                    target_price_val = int(price * (1 + target_return))
                    cash -= shares * cost_per_share

            # 자산 추적
            equity_val = cash + (position * price if position > 0 else 0)
            equity.append(equity_val)

        return trades, pd.Series(equity, index=close.index)

    @staticmethod
    def _format_date(index_val) -> str:
        """인덱스 값을 날짜 문자열로 변환."""
        if hasattr(index_val, "strftime"):
            return index_val.strftime("%Y-%m-%d")
        return str(index_val)

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
        strategy_name: str = "momentum_pullback",
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
        self._last_trades: list[dict] = []
        self._last_equity: pd.Series | None = None

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

        from datetime import datetime as dt
        actual_start_dt = pd.Timestamp(dt.strptime(start_date, "%Y%m%d"))

        all_results = []

        for code, df in price_data.items():
            try:
                entries, exits = self.generate_signals(df, params, strategy_name)

                # calculate_indicators가 dropna하므로 close를 맞춤
                indicator_params = {
                    "macd_fast": (params or {}).get("macd_fast", 12),
                    "macd_slow": (params or {}).get("macd_slow", 26),
                    "macd_signal": (params or {}).get("macd_signal", 9),
                    "rsi_period": (params or {}).get("rsi_period", 14),
                }
                df_ind = calculate_indicators(df, **indicator_params)
                # 워밍업 기간 제거: 원래 시작일 이후만 사용
                df_ind = df_ind[df_ind.index >= actual_start_dt]
                entries = entries[entries.index >= actual_start_dt]
                exits = exits[exits.index >= actual_start_dt]
                if df_ind.empty:
                    continue
                close = df_ind["close"]
                high = df_ind["high"]
                low = df_ind["low"]
                atr_series = df_ind["atr"]

                # 주봉 SMA20 계산 (일봉 인덱스에 forward-fill 매핑)
                weekly_sma20 = None
                if hasattr(df_ind.index, "to_period"):
                    try:
                        weekly = df_ind.resample("W").agg({"close": "last"}).dropna()
                        if len(weekly) >= 20:
                            ws = weekly["close"].rolling(20).mean()
                            weekly_sma20 = ws.reindex(df_ind.index, method="ffill")
                    except Exception:
                        pass

                # pandas 기반 시뮬레이션
                macd_hist_series = df_ind.get("macd_hist")
                trades, equity = self._simulate_portfolio(
                    close, high, low, atr_series, entries, exits, params,
                    weekly_sma20=weekly_sma20,
                    macd_hist=macd_hist_series,
                )
                result = self._calculate_metrics(
                    trades, equity, params or {}
                )

                self._last_trades.extend(trades)
                if self._last_equity is None:
                    self._last_equity = equity

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

        # profit_factor: inf 제외 후 평균 (한 종목에서 손실 0이면 inf)
        pf_values = [
            r.profit_factor
            for r in all_results
            if r.profit_factor != float("inf")
        ]
        avg_pf = float(np.mean(pf_values)) if pf_values else 0.0

        avg_result = BacktestResult(
            total_return=np.mean([r.total_return for r in all_results]),
            annual_return=np.mean([r.annual_return for r in all_results]),
            max_drawdown=np.min([r.max_drawdown for r in all_results]),
            sharpe_ratio=np.mean([r.sharpe_ratio for r in all_results]),
            sortino_ratio=np.mean([r.sortino_ratio for r in all_results]),
            win_rate=np.mean([r.win_rate for r in all_results]),
            profit_factor=round(avg_pf, 2),
            avg_trade_return=np.mean(
                [r.avg_trade_return for r in all_results]
            ),
            trade_count=sum(r.trade_count for r in all_results),
            avg_hold_days=np.mean([r.avg_hold_days for r in all_results]),
            params=params or {},
        )
        return avg_result


    def prepare_portfolio_context(
        self,
        codes: list[str],
        start_date: str,
        end_date: str,
        strategy_name: str = "momentum_pullback",
        use_market_filter: bool = True,
    ) -> dict | None:
        """포트폴리오 백테스트용 데이터/지표를 미리 계산.

        그리드 서치 시 한 번만 호출하면, 이후 run_portfolio()에서
        데이터 로딩/지표 계산을 건너뛰어 대폭 빨라집니다.

        Returns:
            프리컴퓨팅된 컨텍스트 딕셔너리 (run_portfolio의 _context 인자로 전달).
        """
        from datetime import datetime as dt

        price_data = self.load_price_data(codes, start_date, end_date)
        if not price_data:
            return None

        actual_start_dt = pd.Timestamp(dt.strptime(start_date, "%Y%m%d"))

        indicator_params = {
            "macd_fast": 12, "macd_slow": 26,
            "macd_signal": 9, "rsi_period": 14,
        }

        indicator_cache = {}
        for code, df in price_data.items():
            try:
                df_ind = calculate_indicators(df, **indicator_params)
                if df_ind.empty:
                    continue
                df_ind = df_ind[df_ind.index >= actual_start_dt]
                if df_ind.empty:
                    continue
                indicator_cache[code] = df_ind
            except Exception as e:
                logger.error(f"{code}: 지표 계산 실패 - {e}")

        if not indicator_cache:
            return None

        # KOSPI
        is_adaptive = strategy_name == "adaptive"
        kospi_data = kospi_sma200 = kospi_adx_series = None
        if use_market_filter or is_adaptive:
            try:
                from data.provider import get_provider
                from datetime import timedelta
                kospi_warmup_start = (actual_start_dt - timedelta(days=300)).strftime("%Y%m%d")
                kospi_cache_key = f"_kospi_{kospi_warmup_start}_{end_date}"

                if hasattr(self, '_kospi_cache') and kospi_cache_key in self._kospi_cache:
                    kospi_data, kospi_sma200, kospi_adx_series = self._kospi_cache[kospi_cache_key]
                    if is_adaptive and kospi_adx_series is None:
                        kospi_adx_series = self._calc_adx_series(kospi_data)
                else:
                    if not hasattr(self, '_kospi_cache'):
                        self._kospi_cache = {}
                    kospi_data = get_provider().get_kospi_ohlcv(kospi_warmup_start, end_date)
                    if not kospi_data.empty and len(kospi_data) >= 200:
                        kospi_sma200 = kospi_data["close"].rolling(200).mean()
                        if is_adaptive:
                            kospi_adx_series = self._calc_adx_series(kospi_data)
                        self._kospi_cache[kospi_cache_key] = (kospi_data, kospi_sma200, kospi_adx_series)
                        logger.info(f"KOSPI 지수 로드: {len(kospi_data)}행")
            except Exception as e:
                logger.warning(f"KOSPI 데이터 로드 실패: {e}")

        # 주봉 SMA20
        weekly_sma20_cache = {}
        for code, df_ind in indicator_cache.items():
            if hasattr(df_ind.index, "to_period"):
                try:
                    weekly = df_ind.resample("W").agg({"close": "last"}).dropna()
                    if len(weekly) >= 20:
                        ws = weekly["close"].rolling(20).mean()
                        weekly_sma20_cache[code] = ws.reindex(df_ind.index, method="ffill")
                except Exception:
                    pass

        all_dates = sorted(
            set().union(*(df.index for df in indicator_cache.values()))
        )

        return {
            "price_data": price_data,
            "indicator_cache": indicator_cache,
            "kospi_data": kospi_data,
            "kospi_sma200": kospi_sma200,
            "kospi_adx_series": kospi_adx_series,
            "weekly_sma20_cache": weekly_sma20_cache,
            "all_dates": all_dates,
            "actual_start_dt": actual_start_dt,
        }

    def run_portfolio(
        self,
        codes: list[str],
        start_date: str,
        end_date: str,
        params: dict | None = None,
        strategy_name: str = "momentum_pullback",
        max_positions: int = 3,
        use_market_filter: bool = True,
        _context: dict | None = None,
    ) -> BacktestResult:
        """포트폴리오 레벨 백테스트 — 하나의 자본금으로 다종목 순차 매매.

        기존 run()은 종목별 독립 시뮬레이션이지만, 이 메서드는
        실전과 동일하게 자본 경합·포지션 제한을 반영합니다.

        Args:
            codes: 종목 코드 리스트.
            start_date: 시작일 (YYYYMMDD).
            end_date: 종료일 (YYYYMMDD).
            params: 전략 파라미터.
            strategy_name: 전략 이름 (adaptive 지원).
            max_positions: 동시 보유 최대 종목 수.
            use_market_filter: KOSPI 200일선 시장 필터 사용 여부.
            _context: prepare_portfolio_context()의 결과 (그리드 서치 최적화용).

        Returns:
            BacktestResult 성과 지표.
        """
        self._last_trades = []
        self._last_equity = None

        p = params or {}
        target_return = p.get("target_return", 0.06)
        stop_atr_mult = p.get("stop_atr_mult", 1.5)
        trailing_atr_mult = p.get("trailing_atr_mult", 2.0)
        trailing_activate_pct = p.get("trailing_activate_pct", 0.10)
        max_hold_days = p.get("max_hold_days", 10)
        max_stop_pct = p.get("max_stop_pct", 0.07)
        partial_enabled = p.get("partial_sell_enabled", True)
        partial_target_pct = p.get("partial_target_pct", 0.5)
        partial_sell_ratio = p.get("partial_sell_ratio", 0.5)

        # 프리컴퓨팅 컨텍스트가 있으면 재사용, 없으면 직접 계산
        if _context:
            price_data = _context["price_data"]
            indicator_cache = _context["indicator_cache"]
            kospi_data = _context["kospi_data"]
            kospi_sma200 = _context["kospi_sma200"]
            kospi_adx_series = _context["kospi_adx_series"]
            weekly_sma20_cache = _context["weekly_sma20_cache"]
            all_dates = _context["all_dates"]
        else:
            # 기존 방식: 직접 로드 + 계산
            price_data = self.load_price_data(codes, start_date, end_date)
            if not price_data:
                logger.error("유효한 데이터 없음")
                return self._empty_result(p)

            indicator_params = {
                "macd_fast": p.get("macd_fast", 12),
                "macd_slow": p.get("macd_slow", 26),
                "macd_signal": p.get("macd_signal", 9),
                "rsi_period": p.get("rsi_period", 14),
            }

            indicator_cache = {}
            from datetime import datetime as dt
            actual_start_dt = pd.Timestamp(dt.strptime(start_date, "%Y%m%d"))

            for code, df in price_data.items():
                try:
                    df_ind = calculate_indicators(df, **indicator_params)
                    if df_ind.empty:
                        continue
                    df_ind = df_ind[df_ind.index >= actual_start_dt]
                    if df_ind.empty:
                        continue
                    indicator_cache[code] = df_ind
                except Exception as e:
                    logger.error(f"{code}: 지표 계산 실패 - {e}")

            if not indicator_cache:
                return self._empty_result(p)

            # KOSPI 시장 필터 + 국면 판단 (캐싱 지원)
            is_adaptive_check = strategy_name == "adaptive"
            kospi_data = kospi_sma200 = kospi_adx_series = None
            if use_market_filter or is_adaptive_check:
                try:
                    from data.provider import get_provider
                    from datetime import timedelta
                    kospi_warmup_start = (actual_start_dt - timedelta(days=300)).strftime("%Y%m%d")
                    kospi_cache_key = f"_kospi_{kospi_warmup_start}_{end_date}"

                    if hasattr(self, '_kospi_cache') and kospi_cache_key in self._kospi_cache:
                        kospi_data, kospi_sma200, kospi_adx_series = self._kospi_cache[kospi_cache_key]
                        if is_adaptive_check and kospi_adx_series is None:
                            kospi_adx_series = self._calc_adx_series(kospi_data)
                    else:
                        if not hasattr(self, '_kospi_cache'):
                            self._kospi_cache = {}
                        kospi_data = get_provider().get_kospi_ohlcv(kospi_warmup_start, end_date)
                        if not kospi_data.empty and len(kospi_data) >= 200:
                            kospi_sma200 = kospi_data["close"].rolling(200).mean()
                            if is_adaptive_check:
                                kospi_adx_series = self._calc_adx_series(kospi_data)
                            self._kospi_cache[kospi_cache_key] = (kospi_data, kospi_sma200, kospi_adx_series)
                            logger.info(f"KOSPI 지수 로드: {len(kospi_data)}행")
                except Exception as e:
                    logger.warning(f"KOSPI 데이터 로드 실패: {e}")

            # 주봉 SMA20
            weekly_sma20_cache = {}
            for code, df_ind in indicator_cache.items():
                if hasattr(df_ind.index, "to_period"):
                    try:
                        weekly = df_ind.resample("W").agg({"close": "last"}).dropna()
                        if len(weekly) >= 20:
                            ws = weekly["close"].rolling(20).mean()
                            weekly_sma20_cache[code] = ws.reindex(df_ind.index, method="ffill")
                    except Exception:
                        pass

            all_dates = sorted(
                set().union(*(df.index for df in indicator_cache.values()))
            )

        # adaptive 모드
        is_adaptive = strategy_name == "adaptive"
        regime_map = p.get("regime_strategy", {
            "trending": "momentum_pullback",
            "sideways": "bb_bounce",
        })

        # 신호 생성 (파라미터 의존적이므로 항상 재계산)
        signals_cache: dict[str, tuple[pd.Series, pd.Series]] = {}
        if is_adaptive:
            # 멀티전략 지원: regime_map 값이 리스트일 수 있음
            all_strat_names = set()
            for v in regime_map.values():
                if isinstance(v, list):
                    all_strat_names.update(v)
                elif isinstance(v, str):
                    all_strat_names.add(v)
            for strat_name in all_strat_names:
                for code, df in price_data.items():
                    if code not in indicator_cache:
                        continue
                    try:
                        en, ex = self.generate_signals(df, p, strat_name)
                        signals_cache[(code, strat_name)] = (en, ex)
                    except Exception:
                        pass
        else:
            for code, df in price_data.items():
                if code not in indicator_cache:
                    continue
                try:
                    en, ex = self.generate_signals(df, p, strategy_name)
                    signals_cache[(code, strategy_name)] = (en, ex)
                except Exception:
                    pass

        # 포트폴리오 시뮬레이션
        cash = float(self.initial_capital)
        positions: dict[str, dict] = {}
        trades: list[dict] = []
        equity_dates = []
        equity_vals = []

        adx_threshold = p.get("adx_threshold", 25)

        # 연속 손실 차단 (쿨다운)
        consecutive_losses = 0
        cooldown_max_losses = p.get("cooldown_max_losses", 3)  # N연패 시 차단
        cooldown_days = p.get("cooldown_days", 5)  # M일간 진입 중지
        cooldown_until = None  # 쿨다운 종료일

        for date in all_dates:
            # 시장 필터
            market_ok = True
            if use_market_filter and kospi_data is not None and kospi_sma200 is not None:
                if date in kospi_data.index:
                    idx = kospi_data.index.get_loc(date)
                    if not pd.isna(kospi_sma200.iloc[idx]):
                        market_ok = kospi_data["close"].iloc[idx] > kospi_sma200.iloc[idx]

            # adaptive 국면 판단
            current_regime = "sideways"
            if is_adaptive and kospi_data is not None and kospi_adx_series is not None:
                if date in kospi_data.index:
                    idx = kospi_data.index.get_loc(date)
                    k_adx = kospi_adx_series.iloc[idx] if not pd.isna(kospi_adx_series.iloc[idx]) else 25
                    if not market_ok:
                        current_regime = "bearish"
                    elif k_adx >= adx_threshold:
                        current_regime = "trending"
                    else:
                        current_regime = "sideways"

            active_strategies = [strategy_name]  # 멀티전략 리스트
            bearish_max_positions = max(1, max_positions // 3)  # 약세장 포지션 제한
            if is_adaptive:
                if current_regime == "bearish":
                    # 약세장: bb_bounce 허용 (포지션 축소)
                    active_strategies = ["bb_bounce"]
                else:
                    mapped = regime_map.get(current_regime, "bb_bounce")
                    if isinstance(mapped, list):
                        active_strategies = mapped
                    else:
                        active_strategies = [mapped]
            active_strategy = active_strategies[0]  # 하위 호환 (청산용)

            # 1. 기존 포지션 청산 체크
            codes_to_close = []
            for code, pos in positions.items():
                if code not in indicator_cache:
                    continue
                df_ind = indicator_cache[code]
                if date not in df_ind.index:
                    continue

                idx = df_ind.index.get_loc(date)
                price = int(df_ind["close"].iloc[idx])
                bar_high = int(df_ind["high"].iloc[idx])
                bar_low = int(df_ind["low"].iloc[idx])
                cur_atr = float(df_ind["atr"].iloc[idx]) if not pd.isna(df_ind["atr"].iloc[idx]) else price * 0.02
                hold_days = (date - pos["entry_date"]).days if hasattr(date, "day") else 0

                pos["high_since"] = max(pos["high_since"], bar_high)

                should_exit = False
                exit_price = price

                # 손절
                if bar_low <= pos["stop_price"]:
                    should_exit = True
                    exit_price = pos["stop_price"]
                # 부분 매도
                elif partial_enabled and not pos.get("partial_done", False) and pos["target_price"] > 0:
                    partial_trigger = int(pos["entry_price"] * (1 + target_return * partial_target_pct))
                    if bar_high >= partial_trigger:
                        sell_qty = max(1, int(pos["shares"] * partial_sell_ratio))
                        remaining = pos["shares"] - sell_qty
                        if remaining > 0:
                            actual_exit = int(partial_trigger * (1 - self.slippage))
                            proceeds = sell_qty * actual_exit * (1 - self.commission - self.tax)
                            cash += proceeds
                            pnl_pct = (partial_trigger - pos["entry_price"]) / pos["entry_price"]
                            trades.append({
                                "code": code,
                                "entry_date": self._format_date(pos["entry_date"]),
                                "exit_date": self._format_date(date),
                                "entry_price": pos["entry_price"],
                                "exit_price": partial_trigger,
                                "shares": sell_qty,
                                "return": pnl_pct,
                                "hold_days": hold_days,
                                "partial": True,
                            })
                            pos["shares"] = remaining
                            pos["partial_done"] = True

                # 목표가
                if not should_exit and bar_high >= pos["target_price"]:
                    should_exit = True
                    exit_price = pos["target_price"]
                # 트레일링
                if not should_exit:
                    unrealized = (price - pos["entry_price"]) / pos["entry_price"]
                    if unrealized >= trailing_activate_pct:
                        trailing = int(pos["high_since"] - cur_atr * trailing_atr_mult)
                        trailing = max(trailing, pos["stop_price"])
                        if trailing > pos["stop_price"]:
                            pos["stop_price"] = trailing
                        if bar_low <= pos["stop_price"]:
                            should_exit = True
                            exit_price = pos["stop_price"]
                # MACD 데드크로스 (수익 +2% 이상, macd_hist 음전환)
                if not should_exit and code in indicator_cache:
                    pnl_unrealized = (price - pos["entry_price"]) / pos["entry_price"]
                    if pnl_unrealized >= 0.02:
                        df_c = indicator_cache[code]
                        if "macd_hist" in df_c.columns and idx >= 1:
                            prev_h = df_c["macd_hist"].iloc[idx - 1]
                            curr_h = df_c["macd_hist"].iloc[idx]
                            if not np.isnan(prev_h) and not np.isnan(curr_h):
                                if prev_h > 0 and curr_h < 0:
                                    should_exit = True
                                    exit_price = price

                # 최대보유
                if not should_exit and hold_days >= max_hold_days:
                    should_exit = True

                # 전략 매도 신호
                if not should_exit:
                    sig_key = (code, active_strategy)
                    if sig_key not in signals_cache:
                        sig_key = (code, strategy_name)
                    if sig_key in signals_cache:
                        _, exits = signals_cache[sig_key]
                        if date in exits.index and exits.loc[date]:
                            should_exit = True

                if should_exit:
                    actual_exit = int(exit_price * (1 - self.slippage))
                    proceeds = pos["shares"] * actual_exit * (1 - self.commission - self.tax)
                    cash += proceeds
                    pnl_pct = (exit_price - pos["entry_price"]) / pos["entry_price"]
                    trades.append({
                        "code": code,
                        "entry_date": self._format_date(pos["entry_date"]),
                        "exit_date": self._format_date(date),
                        "entry_price": pos["entry_price"],
                        "exit_price": exit_price,
                        "shares": pos["shares"],
                        "return": pnl_pct,
                        "hold_days": hold_days,
                    })
                    codes_to_close.append(code)

                    # 연속 손실 카운트 업데이트
                    if pnl_pct < 0:
                        consecutive_losses += 1
                        if consecutive_losses >= cooldown_max_losses:
                            from datetime import timedelta as td
                            cooldown_until = date + td(days=cooldown_days)
                    else:
                        consecutive_losses = 0

            for code in codes_to_close:
                del positions[code]

            # 쿨다운 해제 체크
            if cooldown_until is not None and date >= cooldown_until:
                cooldown_until = None
                consecutive_losses = 0

            # 2. 새 진입 (시장 필터 + 포지션 제한 + 쿨다운 + 주봉 SMA20)
            # adaptive 모드: bearish에서도 bb_bounce 허용 (포지션 축소)
            # 비-adaptive: 기존 로직 유지 (market_ok 필수)
            allow_entry = market_ok and current_regime != "bearish"
            if is_adaptive and current_regime == "bearish":
                allow_entry = True  # bb_bounce로 진입 허용

            # 쿨다운 중이면 진입 차단
            if cooldown_until is not None:
                allow_entry = False

            current_max_pos = bearish_max_positions if (is_adaptive and current_regime == "bearish") else max_positions

            if allow_entry:
                # === 동적 스크리닝: 매일 유동성+기술적 필터 적용 후 점수 순 진입 ===
                screening_top_n = p.get("screening_top_n", 0)  # 0=비활성, >0=상위 N종목만
                min_daily_amount = p.get("min_daily_amount", 1_000_000_000)
                min_price = p.get("min_price", 1000)
                max_price = p.get("max_price", 500000)

                # 1단계: 매수 신호 + 유동성 + 가격 필터 통과 종목 수집
                daily_candidates = []
                for code, df_ind in indicator_cache.items():
                    if code in positions:
                        continue
                    if date not in df_ind.index:
                        continue

                    idx = df_ind.index.get_loc(date)
                    price = int(df_ind["close"].iloc[idx])

                    # 가격 범위 필터
                    if screening_top_n > 0:
                        if price < min_price or price > max_price:
                            continue

                    # 유동성 필터: 5일 평균 거래대금
                    if screening_top_n > 0 and idx >= 5:
                        recent_amount = (
                            df_ind["close"].iloc[idx-4:idx+1] * df_ind["volume"].iloc[idx-4:idx+1]
                        ).mean()
                        if recent_amount < min_daily_amount:
                            continue

                    # 종목별 국면 판단 (stock_regime_mode 파라미터로 제어)
                    # off: KOSPI 기반 (기본), on: 전 종목 투표, hybrid: 시총별 분기
                    stock_regime_mode = p.get("stock_regime_mode", "off")
                    use_stock_regime = False
                    if stock_regime_mode == "on" and is_adaptive:
                        use_stock_regime = True
                    elif stock_regime_mode == "hybrid" and is_adaptive:
                        # 하이브리드: 시총 기반 분기 (5일 평균 거래대금으로 시총 추정)
                        sr_mcap_threshold = p.get("sr_mcap_threshold", 5_000_000_000_000)  # 5조원
                        if idx >= 5:
                            avg_amount = (df_ind["close"].iloc[idx-4:idx+1] * df_ind["volume"].iloc[idx-4:idx+1]).mean()
                            # 거래대금/회전율 기반 시총 추정 (회전율 ~0.5% 가정)
                            est_mcap = avg_amount * 200  # 거래대금 × 200 ≈ 시총
                            use_stock_regime = est_mcap < sr_mcap_threshold
                        # 대형주(시총 5조+)는 KOSPI 기반 유지

                    if use_stock_regime:
                        sr_adx_thresh = p.get("sr_adx_threshold", 25)
                        sr_min_votes = p.get("sr_min_votes", 2)
                        sr_bear_adx = p.get("sr_bearish_adx", 25)
                        row = df_ind.iloc[idx]
                        _adx = float(row.get("adx", 0))
                        _pdi = float(row.get("plus_di", 0)) if "plus_di" in row.index else 0
                        _mdi = float(row.get("minus_di", 0)) if "minus_di" in row.index else 0
                        # bearish 필터
                        if _adx > sr_bear_adx and _mdi > _pdi:
                            continue  # 이 종목 스킵
                        # 투표
                        v_adx = (_adx > sr_adx_thresh) and (_pdi > _mdi)
                        v_ma = (float(row.get("sma20", 0)) > float(row.get("sma60", 0))) and (price > float(row.get("sma20", 0)))
                        v_mom = False
                        if idx >= 20:
                            v_mom = float(df_ind["close"].iloc[idx]) > float(df_ind["close"].iloc[idx-20])
                        stock_regime = "trending" if sum([v_adx, v_ma, v_mom]) >= sr_min_votes else "sideways"
                        stock_strategies = regime_map.get(stock_regime, active_strategies)
                        if isinstance(stock_strategies, str):
                            stock_strategies = [stock_strategies]
                    else:
                        stock_strategies = active_strategies

                    # 전략 매수 신호 체크 — 멀티전략 OR
                    has_entry = False
                    for ast in stock_strategies:
                        sig_key = (code, ast)
                        if sig_key in signals_cache:
                            entries, _ = signals_cache[sig_key]
                            if date in entries.index and entries.loc[date]:
                                has_entry = True
                                break
                    if not has_entry:
                        continue

                    # 모멘텀 필터: 60일 수익률 하한 이하 종목 제외
                    momentum_floor = p.get("momentum_floor", -0.15)
                    if idx >= 60:
                        past_close = df_ind["close"].iloc[idx - 60]
                        if past_close > 0:
                            momentum_60d = (price - past_close) / past_close
                            if momentum_60d < momentum_floor:
                                continue

                    # 주봉 SMA20 필터
                    if code in weekly_sma20_cache:
                        ws = weekly_sma20_cache[code]
                        if date in ws.index and not pd.isna(ws.loc[date]):
                            if price <= ws.loc[date]:
                                continue

                    # 거래량 필터 — 20일 평균 대비 최소 배율 이상
                    vol_min_ratio = p.get("volume_min_ratio", 0.8)
                    if "volume" in df_ind.columns:
                        cur_vol = df_ind["volume"].iloc[idx]
                        vol_sma20 = df_ind["volume"].rolling(20).mean().iloc[idx]
                        if not pd.isna(vol_sma20) and vol_sma20 > 0:
                            if cur_vol / vol_sma20 < vol_min_ratio:
                                continue

                    # Signal Score 계산 (동적 스크리닝 모드에서 순위용)
                    sig_score = 0.0
                    if screening_top_n > 0:
                        from src.strategy.signals import calculate_signal_score
                        sig_score = calculate_signal_score(df_ind.iloc[:idx+1])

                    daily_candidates.append((code, sig_score))

                # 2단계: Signal Score 내림차순 정렬 → 상위 N종목만 진입
                if screening_top_n > 0:
                    daily_candidates.sort(key=lambda x: x[1], reverse=True)
                    daily_candidates = daily_candidates[:screening_top_n]

                # 3단계: 진입 실행
                for code, _score in daily_candidates:
                    if len(positions) >= current_max_pos:
                        break

                    df_ind = indicator_cache[code]
                    idx = df_ind.index.get_loc(date)
                    price = int(df_ind["close"].iloc[idx])
                    bar_high = int(df_ind["high"].iloc[idx])
                    cur_atr = float(df_ind["atr"].iloc[idx]) if not pd.isna(df_ind["atr"].iloc[idx]) else price * 0.02

                    # 포지션 사이징 (가용 자본 / 남은 포지션)
                    actual_entry = int(price * (1 + self.slippage))
                    cost_per_share = actual_entry * (1 + self.commission)
                    position_budget = cash / max(1, current_max_pos - len(positions))
                    # 국면별 포지션 스케일링
                    regime_scale = {"trending": 1.0, "sideways": 0.5, "bearish": 0.0}
                    position_budget = position_budget * regime_scale.get(current_regime, 1.0)
                    shares = int(position_budget // cost_per_share)
                    if shares <= 0:
                        continue

                    atr_stop = int(price - cur_atr * stop_atr_mult)
                    pct_stop = int(price * (1 - max_stop_pct))
                    stop_price = max(atr_stop, pct_stop)
                    target_price = int(price * (1 + target_return))

                    cost = shares * cost_per_share
                    cash -= cost
                    positions[code] = {
                        "entry_price": price,
                        "shares": shares,
                        "entry_date": date,
                        "stop_price": stop_price,
                        "target_price": target_price,
                        "high_since": bar_high,
                        "partial_done": False,
                    }

            # 에퀴티 계산
            portfolio_value = cash
            for code, pos in positions.items():
                if code in indicator_cache:
                    df_ind = indicator_cache[code]
                    if date in df_ind.index:
                        idx = df_ind.index.get_loc(date)
                        portfolio_value += pos["shares"] * int(df_ind["close"].iloc[idx])
            equity_dates.append(date)
            equity_vals.append(portfolio_value)

        equity = pd.Series(equity_vals, index=equity_dates)
        self._last_trades = trades
        self._last_equity = equity

        result = self._calculate_metrics(trades, equity, p)
        logger.info(
            f"포트폴리오 백테스트 완료: "
            f"수익률 {result.total_return:.2f}%, MDD {result.max_drawdown:.2f}%, "
            f"거래 {result.trade_count}건, 최종 자산 {equity.iloc[-1]:,.0f}원"
        )
        return result

    @staticmethod
    def _calc_adx_series(df: pd.DataFrame) -> pd.Series:
        """KOSPI DataFrame에서 ADX 시리즈 계산."""
        try:
            import pandas_ta as ta
            adx_df = ta.adx(
                df["high"].astype(float),
                df["low"].astype(float),
                df["close"].astype(float),
                length=14,
            )
            if adx_df is not None and "ADX_14" in adx_df.columns:
                return adx_df["ADX_14"]
        except Exception:
            pass
        return pd.Series(25.0, index=df.index)

    def _empty_result(self, params: dict) -> BacktestResult:
        """빈 결과 반환 헬퍼."""
        return BacktestResult(
            total_return=0.0, annual_return=0.0, max_drawdown=0.0,
            sharpe_ratio=0.0, sortino_ratio=0.0, win_rate=0.0,
            profit_factor=0.0, avg_trade_return=0.0, trade_count=0,
            avg_hold_days=0.0, params=params,
        )


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
        default="momentum_pullback",
        help="전략 이름 (기본: momentum_pullback)",
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
        default=3_000_000,
        help="초기 자본금 (기본: 3,000,000원)",
    )
    parser.add_argument(
        "--optimize",
        action="store_true",
        help="파라미터 최적화 실행",
    )
    parser.add_argument(
        "--portfolio",
        action="store_true",
        help="포트폴리오 모드 (다종목 통합 자본 시뮬레이션)",
    )
    parser.add_argument(
        "--max-positions",
        type=int,
        default=3,
        help="포트폴리오 모드: 최대 동시 보유 종목 수 (기본: 3)",
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

    if args.optimize:
        from src.backtest.optimizer import ParameterOptimizer

        optimizer = ParameterOptimizer(engine)
        # 전략별 그리드 자동 선택
        strategy_grids = {
            "momentum_pullback": {
                "momentum_period": [40, 60],
                "pullback_days": [3, 5],
                "rsi_pullback_threshold": [25, 30, 35],
                "stop_atr_mult": [1.5, 2.0],
                "target_return": [0.08, 0.10],
                "max_hold_days": [7, 10, 15],
            },
            "institutional_flow": {
                "adx_threshold": [15, 20, 25],
                "volume_multiplier": [0.8, 1.0],
                "stop_atr_mult": [1.5, 2.0],
                "target_return": [0.08, 0.10],
                "max_hold_days": [10, 15],
            },
            "disparity_reversion": {
                "disparity_entry": [94, 96, 98],
                "rsi_oversold": [30, 35, 40],
                "stop_atr_mult": [1.5, 2.0],
                "target_return": [0.04, 0.06],
                "max_hold_days": [5, 7],
            },
        }
        grid = strategy_grids.get(args.strategy, {
            "stop_atr_mult": [1.5, 2.0],
            "target_return": [0.08, 0.10],
            "max_hold_days": [7, 10, 15],
        })
        results = optimizer.run_grid_search(
            args.codes, start_date, end_date, grid,
            strategy_name=args.strategy,
        )
        if not results.empty:
            print(f"\n최적화 완료: {len(results)}개 조합 중 상위 결과:")
            print(results.head(10).to_string())
        else:
            print("\n필터 통과 조합 없음")
    elif args.portfolio:
        result = engine.run_portfolio(
            args.codes, start_date, end_date,
            strategy_name=args.strategy,
            max_positions=args.max_positions,
        )
    else:
        result = engine.run(args.codes, start_date, end_date, strategy_name=args.strategy)

    if not args.optimize:
        from src.backtest.report import BacktestReporter

        reporter = BacktestReporter()
        reporter.print_summary(result, trades=engine._last_trades)

        mode_label = "portfolio" if args.portfolio else "backtest"
        report_path = reporter.generate_html(
            result,
            output_path=f"reports/{mode_label}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html",
            equity=engine._last_equity,
            trades=engine._last_trades,
        )
        print(f"\nHTML 리포트: {report_path}")
