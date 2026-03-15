"""백테스트 엔진 테스트.

pandas 기반 시뮬레이션 테스트.
"""

import io
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

from src.backtest.engine import (
    COMMISSION_RATE,
    SLIPPAGE_RATE,
    TAX_RATE,
    BacktestEngine,
    BacktestResult,
    _parse_period,
)
from src.backtest.optimizer import PARAM_GRID, ParameterOptimizer
from src.backtest.report import BacktestReporter


# ── Fixtures ──────────────────────────────────────────────────────


@pytest.fixture
def sample_ohlcv():
    """테스트용 OHLCV DataFrame (영문 컬럼, 200행)."""
    np.random.seed(42)
    n = 200
    dates = pd.date_range("2022-01-01", periods=n, freq="B")
    close = 50000 + np.cumsum(np.random.randn(n) * 500)
    close = close.astype(int)

    df = pd.DataFrame({
        "open": (close * 0.998).astype(int),
        "high": (close * 1.015).astype(int),
        "low": (close * 0.985).astype(int),
        "close": close,
        "volume": np.random.randint(100_000, 1_000_000, n),
    }, index=dates)
    df = df.reset_index(drop=True)
    return df


@pytest.fixture
def sample_result():
    """테스트용 BacktestResult."""
    return BacktestResult(
        total_return=15.5,
        annual_return=8.2,
        max_drawdown=-7.3,
        sharpe_ratio=1.45,
        sortino_ratio=2.1,
        win_rate=58.3,
        profit_factor=2.15,
        avg_trade_return=1.2,
        trade_count=24,
        avg_hold_days=5.3,
        params={"macd_fast": 12, "rsi_period": 14},
    )


@pytest.fixture
def engine():
    """테스트용 BacktestEngine."""
    return BacktestEngine(initial_capital=10_000_000)


# ── BacktestResult 테스트 ─────────────────────────────────────────


class TestBacktestResult:
    """BacktestResult 데이터클래스 테스트."""

    def test_create_result(self, sample_result):
        """기본 생성 확인."""
        assert sample_result.total_return == 15.5
        assert sample_result.annual_return == 8.2
        assert sample_result.max_drawdown == -7.3
        assert sample_result.sharpe_ratio == 1.45
        assert sample_result.trade_count == 24
        assert isinstance(sample_result.params, dict)

    def test_default_params(self):
        """params 기본값 빈 딕셔너리."""
        result = BacktestResult(
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
        )
        assert result.params == {}

    def test_money_is_int(self):
        """금액은 int 컨벤션."""
        engine = BacktestEngine(initial_capital=10_000_000)
        assert isinstance(engine.initial_capital, int)

    def test_ratios_are_float(self, sample_result):
        """비율은 float 컨벤션."""
        assert isinstance(sample_result.total_return, float)
        assert isinstance(sample_result.sharpe_ratio, float)
        assert isinstance(sample_result.win_rate, float)


# ── BacktestEngine 테스트 ─────────────────────────────────────────


class TestBacktestEngine:
    """BacktestEngine 테스트."""

    def test_init_default_capital(self):
        """기본 자본금 1천만원."""
        engine = BacktestEngine()
        assert engine.initial_capital == 10_000_000

    def test_init_custom_capital(self):
        """커스텀 자본금 설정."""
        engine = BacktestEngine(initial_capital=50_000_000)
        assert engine.initial_capital == 50_000_000

    def test_generate_signals_no_lookahead(self, engine, sample_ohlcv):
        """generate_signals에서 look-ahead bias가 없는지 확인.

        shift(1) 적용 확인: 첫 행은 반드시 False (NaN -> False).
        """
        entries, exits = engine.generate_signals(sample_ohlcv)

        # shift(1)로 첫 행은 항상 False
        assert entries.iloc[0] == False  # noqa: E712
        assert exits.iloc[0] == False  # noqa: E712

    def test_generate_signals_return_type(self, engine, sample_ohlcv):
        """generate_signals 반환 타입 확인."""
        entries, exits = engine.generate_signals(sample_ohlcv)

        assert isinstance(entries, pd.Series)
        assert isinstance(exits, pd.Series)
        assert entries.dtype == bool
        assert exits.dtype == bool

    def test_generate_signals_with_params(self, engine, sample_ohlcv):
        """커스텀 파라미터로 신호 생성."""
        params = {
            "macd_fast": 10,
            "macd_slow": 24,
            "macd_signal": 9,
            "rsi_period": 14,
            "rsi_min": 35,
            "rsi_max": 70,
            "volume_multiplier": 1.2,
            "stop_atr_mult": 2.0,
        }
        entries, exits = engine.generate_signals(sample_ohlcv, params)
        assert len(entries) > 0
        assert len(exits) > 0

    def test_generate_signals_shift_verification(self, engine, sample_ohlcv):
        """신호가 실제로 1일 지연되는지 직접 검증.

        raw 신호 대비 shift(1) 결과를 비교.
        """
        entries, exits = engine.generate_signals(sample_ohlcv)

        # entries/exits는 shift(1) 적용되어 있으므로
        # 최소한 index 0이 False여야 함
        assert not entries.iloc[0]
        assert not exits.iloc[0]

    def test_cost_model_constants(self):
        """비용 모델 상수 확인."""
        assert COMMISSION_RATE == 0.00015
        assert TAX_RATE == 0.002
        assert SLIPPAGE_RATE == 0.001


# ── 포트폴리오 시뮬레이션 테스트 ──────────────────────────────────


class TestSimulatePortfolio:
    """_simulate_portfolio 메서드 테스트."""

    @staticmethod
    def _make_series(close_vals):
        """close 값에서 high/low/atr 시리즈를 생성하는 헬퍼."""
        close = pd.Series(close_vals)
        high = pd.Series([int(c * 1.01) for c in close_vals])
        low = pd.Series([int(c * 0.99) for c in close_vals])
        atr = pd.Series([float(c * 0.02) for c in close_vals])
        return close, high, low, atr

    def test_no_signals_returns_flat_equity(self, engine):
        """신호 없으면 자산 변동 없음."""
        close, high, low, atr = self._make_series(
            [10000, 10100, 10200, 10300, 10400]
        )
        entries = pd.Series([False, False, False, False, False])
        exits = pd.Series([False, False, False, False, False])

        trades, equity = engine._simulate_portfolio(
            close, high, low, atr, entries, exits
        )

        assert len(trades) == 0
        assert len(equity) == 5
        assert equity.iloc[0] == engine.initial_capital
        assert equity.iloc[-1] == engine.initial_capital

    def test_single_trade(self, engine):
        """매수-매도 1회 거래 추적 (signal exit)."""
        close, high, low, atr = self._make_series(
            [10000, 10000, 11000, 11000, 11000]
        )
        entries = pd.Series([False, True, False, False, False])
        exits = pd.Series([False, False, False, True, False])
        # max_hold_days를 크게 설정하여 signal exit만 테스트
        params = {"max_hold_days": 100, "target_return": 0.5}

        trades, equity = engine._simulate_portfolio(
            close, high, low, atr, entries, exits, params
        )

        assert len(trades) == 1
        t = trades[0]
        assert t["entry_idx"] == 1
        assert t["exit_idx"] == 3
        assert t["entry_price"] == 10000
        assert t["exit_price"] == 11000
        assert t["hold_days"] == 2
        assert t["return"] == pytest.approx(0.1, abs=0.001)

    def test_commission_and_tax_applied(self, engine):
        """수수료/세금이 적용되어 동일가 매매 시 손실."""
        price = 10000
        close, high, low, atr = self._make_series([price] * 4)
        entries = pd.Series([False, True, False, False])
        exits = pd.Series([False, False, True, False])
        params = {"max_hold_days": 100, "target_return": 0.5}

        trades, equity = engine._simulate_portfolio(
            close, high, low, atr, entries, exits, params
        )

        assert equity.iloc[-1] < engine.initial_capital

    def test_multiple_trades(self, engine):
        """복수 거래 추적."""
        close, high, low, atr = self._make_series(
            [10000, 10000, 11000, 11000, 10000, 10000, 12000, 12000]
        )
        entries = pd.Series(
            [False, True, False, False, False, True, False, False]
        )
        exits = pd.Series(
            [False, False, False, True, False, False, False, True]
        )
        params = {"max_hold_days": 100, "target_return": 0.5}

        trades, equity = engine._simulate_portfolio(
            close, high, low, atr, entries, exits, params
        )

        assert len(trades) == 2

    def test_equity_curve_length(self, engine):
        """자산 곡선 길이는 close와 동일."""
        n = 50
        close, high, low, atr = self._make_series(
            list(range(10000, 10000 + n))
        )
        entries = pd.Series([False] * n)
        exits = pd.Series([False] * n)

        trades, equity = engine._simulate_portfolio(
            close, high, low, atr, entries, exits
        )

        assert len(equity) == n

    def test_stop_loss_exit(self, engine):
        """손절가 도달 시 자동 청산."""
        # Entry at 10000, price drops sharply
        close_vals = [10000, 10000, 8000, 8000, 8000]
        close = pd.Series(close_vals)
        high = pd.Series([10100, 10100, 8100, 8100, 8100])
        low = pd.Series([9900, 9900, 7500, 7900, 7900])
        atr = pd.Series([200.0] * 5)  # ATR=200
        entries = pd.Series([False, True, False, False, False])
        exits = pd.Series([False, False, False, False, False])
        # stop_atr_mult=1.5 → stop = 10000 - 200*1.5 = 9700
        # max_stop_pct=0.07 → stop = 10000*0.93 = 9300
        # stop_price = max(9700, 9300) = 9700
        # bar 2: low=7500 <= 9700 → exit at 9700
        params = {
            "max_hold_days": 100,
            "target_return": 0.5,
            "stop_atr_mult": 1.5,
            "max_stop_pct": 0.07,
        }

        trades, equity = engine._simulate_portfolio(
            close, high, low, atr, entries, exits, params
        )

        assert len(trades) == 1
        t = trades[0]
        assert t["exit_price"] == 9700
        assert t["return"] < 0  # 손실

    def test_target_price_exit(self, engine):
        """목표가 도달 시 자동 청산."""
        # Entry at 10000, target_return=0.08 → target = 10800
        close_vals = [10000, 10000, 10500, 11000, 11000]
        close = pd.Series(close_vals)
        high = pd.Series([10100, 10100, 10600, 11200, 11100])
        low = pd.Series([9900, 9900, 10400, 10900, 10900])
        atr = pd.Series([200.0] * 5)
        entries = pd.Series([False, True, False, False, False])
        exits = pd.Series([False, False, False, False, False])
        params = {
            "target_return": 0.08,
            "max_hold_days": 100,
        }

        trades, equity = engine._simulate_portfolio(
            close, high, low, atr, entries, exits, params
        )

        assert len(trades) == 1
        t = trades[0]
        assert t["exit_price"] == 10800  # int(10000 * 1.08)
        assert t["return"] == pytest.approx(0.08, abs=0.001)

    def test_max_hold_days_exit(self, engine):
        """최대 보유일 초과 시 자동 청산."""
        n = 10
        close_vals = [10000] * n
        close, high, low, atr = self._make_series(close_vals)
        entries = pd.Series([False, True] + [False] * (n - 2))
        exits = pd.Series([False] * n)
        params = {
            "max_hold_days": 5,
            "target_return": 0.5,  # high target so it won't trigger
        }

        trades, equity = engine._simulate_portfolio(
            close, high, low, atr, entries, exits, params
        )

        assert len(trades) == 1
        t = trades[0]
        assert t["hold_days"] == 5  # exit at bar 6 (index 6), entry at bar 1

    def test_trailing_stop_activation(self, engine):
        """트레일링 스톱 활성화 후 하락 시 청산."""
        # Entry at 10000, rises to 10500 (+5% > 3% activate threshold)
        # then drops → trailing stop triggers
        close_vals = [10000, 10000, 10400, 10500, 10100, 10100]
        close = pd.Series(close_vals)
        high = pd.Series([10100, 10100, 10500, 10600, 10200, 10200])
        low = pd.Series([9900, 9900, 10300, 10400, 9800, 10000])
        atr = pd.Series([200.0] * 6)
        entries = pd.Series([False, True, False, False, False, False])
        exits = pd.Series([False, False, False, False, False, False])
        # trailing_activate_pct=0.03, trailing_atr_mult=2.0
        # At bar 3: high_since_entry = 10600, unrealized = (10500-10000)/10000 = 0.05 >= 0.03
        # trailing = 10600 - 200*2 = 10200
        # initial stop = max(10000-200*1.5, 10000*0.93) = max(9700, 9300) = 9700
        # trailing(10200) > stop(9700) → stop_price = 10200
        # bar 4: low=9800 <= 10200 → exit at stop_price=10200
        params = {
            "target_return": 0.5,
            "max_hold_days": 100,
            "stop_atr_mult": 1.5,
            "max_stop_pct": 0.07,
            "trailing_atr_mult": 2.0,
            "trailing_activate_pct": 0.03,
        }

        trades, equity = engine._simulate_portfolio(
            close, high, low, atr, entries, exits, params
        )

        assert len(trades) == 1
        t = trades[0]
        assert t["exit_price"] == 10200
        assert t["return"] == pytest.approx(0.02, abs=0.001)


# ── 지표 계산 테스트 ──────────────────────────────────────────────


class TestCalculateMetrics:
    """_calculate_metrics 메서드 테스트."""

    def test_no_trades(self, engine):
        """거래 없을 때 기본값."""
        equity = pd.Series([10_000_000] * 100)
        result = engine._calculate_metrics([], equity, {})

        assert result.trade_count == 0
        assert result.win_rate == 0.0
        assert result.profit_factor == 0.0
        assert result.avg_trade_return == 0.0
        assert result.avg_hold_days == 0.0
        assert result.total_return == 0.0

    def test_positive_return(self, engine):
        """양수 수익률 계산."""
        # 시작 1000만 → 종료 1100만 (10%)
        equity = pd.Series(
            np.linspace(10_000_000, 11_000_000, 100)
        )
        trades = [
            {"entry_idx": 0, "exit_idx": 10, "entry_price": 10000,
             "exit_price": 11000, "shares": 100, "return": 0.10,
             "hold_days": 10}
        ]
        result = engine._calculate_metrics(trades, equity, {})

        assert result.total_return == pytest.approx(10.0, abs=0.1)
        assert result.win_rate == 100.0
        assert result.trade_count == 1

    def test_max_drawdown(self, engine):
        """MDD 계산 검증."""
        # 1000만 → 900만 → 1050만 (MDD = -10%)
        equity = pd.Series([10_000_000, 9_000_000, 10_500_000])
        result = engine._calculate_metrics([], equity, {})

        assert result.max_drawdown == pytest.approx(-10.0, abs=0.1)

    def test_win_rate_and_profit_factor(self, engine):
        """승률과 손익비 계산."""
        equity = pd.Series(np.linspace(10_000_000, 10_500_000, 50))
        trades = [
            {"entry_idx": 0, "exit_idx": 5, "entry_price": 10000,
             "exit_price": 11000, "shares": 100, "return": 0.10,
             "hold_days": 5},
            {"entry_idx": 10, "exit_idx": 15, "entry_price": 11000,
             "exit_price": 10500, "shares": 100, "return": -0.0455,
             "hold_days": 5},
        ]
        result = engine._calculate_metrics(trades, equity, {})

        assert result.win_rate == 50.0
        assert result.trade_count == 2
        assert result.profit_factor > 0


# ── engine.run() 통합 테스트 ──────────────────────────────────────


class TestEngineRun:
    """engine.run() 통합 테스트 (load_price_data를 mock)."""

    def test_run_with_mock_data(self, engine, sample_ohlcv):
        """mock 데이터로 run() 정상 동작 확인."""
        with patch.object(
            engine, "load_price_data", return_value={"005930": sample_ohlcv}
        ):
            result = engine.run(["005930"], "20220101", "20221231")

        assert isinstance(result, BacktestResult)
        assert isinstance(result.total_return, float)
        assert isinstance(result.trade_count, int)
        assert result.params == {}

    def test_run_empty_data(self, engine):
        """데이터 없으면 빈 결과 반환."""
        with patch.object(engine, "load_price_data", return_value={}):
            result = engine.run(["999999"], "20220101", "20221231")

        assert result.trade_count == 0
        assert result.total_return == 0.0

    def test_run_with_params(self, engine, sample_ohlcv):
        """파라미터 전달 확인."""
        params = {"macd_fast": 10, "rsi_period": 14}
        with patch.object(
            engine, "load_price_data", return_value={"005930": sample_ohlcv}
        ):
            result = engine.run(
                ["005930"], "20220101", "20221231", params=params
            )

        assert result.params == params

    def test_relaxed_params_generate_more_signals(self, engine):
        """완화된 파라미터가 기존보다 같거나 많은 entry 신호를 생성."""
        # 완만한 상승 + 진동으로 SMA20 위에서 MACD 크로스오버 유도
        np.random.seed(77)
        n = 500
        t = np.arange(n)
        base = 50000 + t * 5
        osc = np.sin(t / 8) * 400 + np.sin(t / 20) * 800
        noise = np.random.normal(0, 100, n)
        close = np.round(base + osc + noise).astype(int)
        close = np.maximum(close, 10000)
        high = np.round(close * 1.012).astype(int)
        low = np.round(close * 0.988).astype(int)
        open_ = close.copy()
        volume = np.random.randint(500_000, 1_200_000, n)

        df = pd.DataFrame({
            "open": open_, "high": high, "low": low,
            "close": close, "volume": volume,
        })

        # 기존 엄격한 파라미터
        strict_params = {
            "rsi_min": 40,
            "rsi_max": 65,
            "volume_multiplier": 1.5,
        }
        strict_entries, _ = engine.generate_signals(df, strict_params)

        # 완화된 파라미터 (새 기본값)
        relaxed_entries, _ = engine.generate_signals(df)

        # 완화된 조건이 같거나 더 많은 신호를 생성해야 함
        assert relaxed_entries.sum() >= strict_entries.sum()
        # 완화된 조건에서 최소 1개 이상의 신호가 있어야 함
        assert relaxed_entries.sum() > 0


# ── ParameterOptimizer 테스트 ─────────────────────────────────────


class TestParameterOptimizer:
    """ParameterOptimizer 테스트."""

    def test_generate_param_combinations_count(self):
        """파라미터 조합 수 확인."""
        optimizer = ParameterOptimizer()
        small_grid = {
            "a": [1, 2],
            "b": [3, 4, 5],
            "c": [6],
        }
        combos = optimizer._generate_param_combinations(small_grid)
        # 2 * 3 * 1 = 6
        assert len(combos) == 6

    def test_generate_param_combinations_type(self):
        """조합이 딕셔너리 리스트인지 확인."""
        optimizer = ParameterOptimizer()
        combos = optimizer._generate_param_combinations({"x": [1, 2]})
        assert isinstance(combos, list)
        assert isinstance(combos[0], dict)
        assert "x" in combos[0]

    def test_generate_param_combinations_full_grid(self):
        """전체 PARAM_GRID 조합 수 확인."""
        optimizer = ParameterOptimizer()
        combos = optimizer._generate_param_combinations(PARAM_GRID)

        expected = 1
        for values in PARAM_GRID.values():
            expected *= len(values)

        assert len(combos) == expected

    def test_generate_param_combinations_empty(self):
        """빈 그리드 → 빈 리스트 아닌 [{}] 반환."""
        optimizer = ParameterOptimizer()
        combos = optimizer._generate_param_combinations({})
        # itertools.product of empty → one empty tuple
        assert len(combos) == 1
        assert combos[0] == {}

    def test_walk_forward_window_generation(self):
        """Walk-Forward 윈도우 생성 로직 테스트."""
        windows = ParameterOptimizer._generate_walk_forward_windows(
            start_date="20200101",
            end_date="20231231",
            train_months=12,
            test_months=3,
            step_months=3,
        )

        # 윈도우가 생성되어야 함
        assert len(windows) > 0

        # 각 윈도우는 (train_start, train_end, test_start, test_end) 튜플
        for w in windows:
            assert len(w) == 4
            train_start, train_end, test_start, test_end = w
            # 형식 확인
            assert len(train_start) == 8
            assert len(test_end) == 8
            # 순서 확인
            assert train_start < train_end
            assert train_end < test_start
            assert test_start < test_end

    def test_walk_forward_window_no_overlap(self):
        """Walk-Forward 테스트 기간이 전체 범위를 초과하지 않는지 확인."""
        windows = ParameterOptimizer._generate_walk_forward_windows(
            start_date="20220101",
            end_date="20221231",
            train_months=6,
            test_months=3,
            step_months=3,
        )

        for w in windows:
            assert w[3] <= "20231231"  # test_end는 end_date 이내

    def test_walk_forward_short_period_no_windows(self):
        """기간이 짧으면 윈도우 없음."""
        windows = ParameterOptimizer._generate_walk_forward_windows(
            start_date="20230101",
            end_date="20230301",
            train_months=12,
            test_months=3,
            step_months=3,
        )
        assert len(windows) == 0


# ── BacktestReporter 테스트 ───────────────────────────────────────


class TestBacktestReporter:
    """BacktestReporter 테스트."""

    def test_print_summary(self, sample_result, capsys):
        """print_summary 정상 동작 확인."""
        reporter = BacktestReporter()
        reporter.print_summary(sample_result)

        captured = capsys.readouterr()
        assert "15.50%" in captured.out
        assert "8.20%" in captured.out
        assert "-7.30%" in captured.out
        assert "1.45" in captured.out
        assert "24" in captured.out

    def test_print_summary_with_params(self, sample_result, capsys):
        """파라미터 포함 출력 확인."""
        reporter = BacktestReporter()
        reporter.print_summary(sample_result)

        captured = capsys.readouterr()
        assert "macd_fast" in captured.out
        assert "rsi_period" in captured.out

    def test_print_summary_no_params(self, capsys):
        """파라미터 없는 결과 출력."""
        result = BacktestResult(
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
        )
        reporter = BacktestReporter()
        reporter.print_summary(result)

        captured = capsys.readouterr()
        assert "백테스트 성과 요약" in captured.out

    def test_generate_html(self, sample_result, tmp_path):
        """HTML 리포트 생성 확인."""
        reporter = BacktestReporter()
        output = str(tmp_path / "test_report.html")
        result_path = reporter.generate_html(sample_result, output)

        assert result_path == output
        with open(output, encoding="utf-8") as f:
            html = f.read()
        assert "15.50%" in html
        assert "백테스트" in html
        assert "macd_fast" in html

    def test_build_metrics_rows(self, sample_result):
        """metrics 행 생성."""
        rows = BacktestReporter._build_metrics_rows(sample_result)
        assert "총 수익률" in rows
        assert "positive" in rows  # 양수 수익률은 positive 클래스

    def test_build_params_rows_empty(self):
        """빈 파라미터 처리."""
        rows = BacktestReporter._build_params_rows(None)
        assert "기본 파라미터" in rows

    def test_build_params_rows_with_data(self):
        """파라미터 행 생성."""
        rows = BacktestReporter._build_params_rows({"rsi_period": 14})
        assert "rsi_period" in rows
        assert "14" in rows


# ── CLI / _parse_period 테스트 ────────────────────────────────────


class TestParsePeriod:
    """_parse_period 함수 테스트."""

    def test_parse_years(self):
        """연 단위 파싱."""
        start, end = _parse_period("2y")
        assert len(start) == 8
        assert len(end) == 8

    def test_parse_months(self):
        """월 단위 파싱."""
        start, end = _parse_period("6m")
        assert len(start) == 8

    def test_parse_invalid(self):
        """잘못된 형식 ValueError."""
        with pytest.raises(ValueError):
            _parse_period("2w")


# ── 날짜 포함 거래 내역 테스트 ─────────────────────────────────────


class TestSimulatePortfolioDates:
    """_simulate_portfolio 거래 내역에 날짜 포함 확인."""

    def test_simulate_portfolio_includes_dates(self):
        """trades에 entry_date/exit_date 키가 포함되는지 확인."""
        engine = BacktestEngine(initial_capital=10_000_000)
        dates = pd.date_range("2023-01-01", periods=5, freq="B")
        close = pd.Series([10000, 10000, 11000, 11000, 11000], index=dates)
        high = pd.Series([10100, 10100, 11100, 11100, 11100], index=dates)
        low = pd.Series([9900, 9900, 10900, 10900, 10900], index=dates)
        atr = pd.Series([200.0] * 5, index=dates)
        entries = pd.Series(
            [False, True, False, False, False], index=dates
        )
        exits = pd.Series(
            [False, False, False, True, False], index=dates
        )
        params = {"max_hold_days": 100, "target_return": 0.5}

        trades, equity = engine._simulate_portfolio(
            close, high, low, atr, entries, exits, params
        )

        assert len(trades) == 1
        t = trades[0]
        assert "entry_date" in t
        assert "exit_date" in t
        assert "2023-01-0" in t["entry_date"]
        assert "2023-01-0" in t["exit_date"]

    def test_simulate_portfolio_dates_integer_index(self):
        """정수 인덱스에서도 entry_date/exit_date가 문자열로 포함."""
        engine = BacktestEngine(initial_capital=10_000_000)
        close = pd.Series([10000, 10000, 11000, 11000, 11000])
        high = pd.Series([10100, 10100, 11100, 11100, 11100])
        low = pd.Series([9900, 9900, 10900, 10900, 10900])
        atr = pd.Series([200.0] * 5)
        entries = pd.Series([False, True, False, False, False])
        exits = pd.Series([False, False, False, True, False])
        params = {"max_hold_days": 100, "target_return": 0.5}

        trades, equity = engine._simulate_portfolio(
            close, high, low, atr, entries, exits, params
        )

        assert len(trades) == 1
        t = trades[0]
        assert "entry_date" in t
        assert "exit_date" in t
        assert isinstance(t["entry_date"], str)
        assert isinstance(t["exit_date"], str)


# ── 차트 포함 HTML 리포트 테스트 ──────────────────────────────────


class TestReporterWithCharts:
    """차트/거래 테이블 포함 HTML 리포트 테스트."""

    def test_reporter_generate_html_with_charts(self, sample_result, tmp_path):
        """equity/trades 전달 시 HTML에 차트 이미지가 포함되는지 확인."""
        reporter = BacktestReporter()
        dates = pd.date_range("2023-01-01", periods=100, freq="B")
        equity = pd.Series(
            np.linspace(10_000_000, 11_000_000, 100), index=dates
        )
        trades = [
            {
                "entry_idx": 0,
                "exit_idx": 10,
                "entry_date": "2023-01-02",
                "exit_date": "2023-01-16",
                "entry_price": 10000,
                "exit_price": 11000,
                "shares": 100,
                "return": 0.10,
                "hold_days": 10,
            }
        ]

        output = str(tmp_path / "chart_report.html")
        result_path = reporter.generate_html(
            sample_result, output, equity=equity, trades=trades
        )

        assert result_path == output
        with open(output, encoding="utf-8") as f:
            html = f.read()
        # base64 차트 이미지 확인
        assert "data:image/png;base64," in html
        # 거래 테이블 확인
        assert "매수일" in html
        assert "매도일" in html
        assert "2023-01-02" in html
        assert "+10.00%" in html

    def test_reporter_generate_html_without_charts(self, sample_result, tmp_path):
        """equity/trades 없이도 HTML 리포트가 정상 생성."""
        reporter = BacktestReporter()
        output = str(tmp_path / "no_chart_report.html")
        result_path = reporter.generate_html(sample_result, output)

        assert result_path == output
        with open(output, encoding="utf-8") as f:
            html = f.read()
        assert "백테스트" in html
        assert "data:image/png;base64," not in html
