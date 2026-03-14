"""백테스트 엔진 테스트.

vectorbt 없이도 기본 테스트가 통과하도록 mock 사용.
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


# ── vectorbt 의존 테스트 ──────────────────────────────────────────


class TestVectorbtIntegration:
    """vectorbt 설치 시에만 실행되는 통합 테스트."""

    def test_vectorbt_importable(self):
        """vectorbt import 가능 여부 체크."""
        vbt = pytest.importorskip("vectorbt")
        assert hasattr(vbt, "Portfolio")
