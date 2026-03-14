"""전략 레이어 테스트.

고정 OHLCV 데이터(150행)를 사용하여 지표 계산, 매수/매도 신호,
신호 점수 계산을 검증.
"""

import numpy as np
import pandas as pd
import pytest

from src.models import ExitReason, Position
from src.strategy.signals import (
    calculate_indicators,
    calculate_signal_score,
    check_entry_signal,
    check_exit_signal,
)


@pytest.fixture
def ohlcv_df() -> pd.DataFrame:
    """고정 OHLCV 데이터 (150행).

    상승 추세를 시뮬레이션: 50,000원 부근에서 시작하여 완만하게 상승.
    """
    np.random.seed(42)
    n = 150

    # 기본 가격: 50,000에서 시작하여 완만 상승
    base = 50000 + np.cumsum(np.random.normal(50, 200, n))
    base = np.maximum(base, 10000)  # 최소 가격 보장

    close = np.round(base).astype(int)
    high = np.round(close * (1 + np.random.uniform(0.005, 0.03, n))).astype(int)
    low = np.round(close * (1 - np.random.uniform(0.005, 0.03, n))).astype(int)
    open_ = np.round(
        low + (high - low) * np.random.uniform(0.3, 0.7, n)
    ).astype(int)
    volume = np.random.randint(100000, 2000000, n)

    df = pd.DataFrame(
        {
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
        }
    )
    return df


@pytest.fixture
def indicators_df(ohlcv_df) -> pd.DataFrame:
    """지표 계산 완료된 DataFrame."""
    return calculate_indicators(ohlcv_df)


class TestCalculateIndicators:
    """calculate_indicators 테스트."""

    def test_all_indicator_columns_added(self, indicators_df):
        """모든 지표 컬럼이 추가되는지 확인."""
        expected_columns = [
            "open", "high", "low", "close", "volume",
            "sma5", "sma20", "sma60", "sma120",
            "macd", "macd_signal", "macd_hist",
            "rsi",
            "bb_upper", "bb_mid", "bb_lower",
            "stoch_k", "stoch_d",
            "atr", "adx",
            "volume_sma20",
        ]
        for col in expected_columns:
            assert col in indicators_df.columns, f"컬럼 {col} 누락"

    def test_no_nan_values(self, indicators_df):
        """NaN 행이 제거되었는지 확인."""
        assert not indicators_df.isna().any().any(), "NaN 값이 존재"

    def test_output_not_empty(self, indicators_df):
        """출력이 비어있지 않은지 확인."""
        assert len(indicators_df) > 0

    def test_sma_values_reasonable(self, indicators_df):
        """SMA 값이 합리적인 범위인지 확인."""
        assert (indicators_df["sma5"] > 0).all()
        assert (indicators_df["sma20"] > 0).all()
        assert (indicators_df["sma60"] > 0).all()
        assert (indicators_df["sma120"] > 0).all()

    def test_rsi_in_range(self, indicators_df):
        """RSI가 0~100 범위인지 확인."""
        assert (indicators_df["rsi"] >= 0).all()
        assert (indicators_df["rsi"] <= 100).all()

    def test_bb_ordering(self, indicators_df):
        """볼린저밴드 상한 > 중간 > 하한 순서 확인."""
        assert (indicators_df["bb_upper"] >= indicators_df["bb_mid"]).all()
        assert (indicators_df["bb_mid"] >= indicators_df["bb_lower"]).all()

    def test_custom_params(self, ohlcv_df):
        """커스텀 파라미터로 지표 계산."""
        result = calculate_indicators(
            ohlcv_df,
            macd_fast=8,
            macd_slow=21,
            macd_signal=5,
            rsi_period=10,
        )
        assert "macd" in result.columns
        assert "rsi" in result.columns
        assert not result.isna().any().any()


class TestCheckEntrySignal:
    """check_entry_signal 테스트."""

    def test_all_conditions_met_returns_true(self):
        """모든 AND 조건 충족 시 True."""
        # 일봉 데이터 구성: 모든 조건을 만족하도록 설계
        df = pd.DataFrame(
            {
                "close": [10000, 10200],
                "sma20": [9800, 9900],
                "macd_hist": [-0.5, 0.3],  # 전일 음 → 당일 양
                "rsi": [55, 52],
                "volume": [500000, 600000],
                "volume_sma20": [300000, 350000],  # 600000 >= 350000 * 1.5
            }
        )
        # 60분봉 데이터: SMA5 > SMA20
        df_60m = pd.DataFrame({"sma5": [10100], "sma20": [9900]})

        assert check_entry_signal(df, df_60m) is True

    def test_price_below_sma20_returns_false(self):
        """종가 < SMA20 → False."""
        df = pd.DataFrame(
            {
                "close": [10000, 9800],  # 종가 < sma20
                "sma20": [9900, 10000],
                "macd_hist": [-0.5, 0.3],
                "rsi": [55, 52],
                "volume": [500000, 600000],
                "volume_sma20": [300000, 350000],
            }
        )
        df_60m = pd.DataFrame({"sma5": [10100], "sma20": [9900]})

        assert check_entry_signal(df, df_60m) is False

    def test_macd_no_crossover_returns_false(self):
        """MACD 양전환 없음 → False."""
        df = pd.DataFrame(
            {
                "close": [10000, 10200],
                "sma20": [9800, 9900],
                "macd_hist": [0.1, 0.3],  # 전일도 양수
                "rsi": [55, 52],
                "volume": [500000, 600000],
                "volume_sma20": [300000, 350000],
            }
        )
        df_60m = pd.DataFrame({"sma5": [10100], "sma20": [9900]})

        assert check_entry_signal(df, df_60m) is False

    def test_rsi_out_of_range_returns_false(self):
        """RSI 범위 밖 → False."""
        df = pd.DataFrame(
            {
                "close": [10000, 10200],
                "sma20": [9800, 9900],
                "macd_hist": [-0.5, 0.3],
                "rsi": [55, 75],  # RSI 75 > 65
                "volume": [500000, 600000],
                "volume_sma20": [300000, 350000],
            }
        )
        df_60m = pd.DataFrame({"sma5": [10100], "sma20": [9900]})

        assert check_entry_signal(df, df_60m) is False

    def test_low_volume_returns_false(self):
        """거래량 부족 → False."""
        df = pd.DataFrame(
            {
                "close": [10000, 10200],
                "sma20": [9800, 9900],
                "macd_hist": [-0.5, 0.3],
                "rsi": [55, 52],
                "volume": [500000, 400000],  # 400000 < 350000 * 1.5
                "volume_sma20": [300000, 350000],
            }
        )
        df_60m = pd.DataFrame({"sma5": [10100], "sma20": [9900]})

        assert check_entry_signal(df, df_60m) is False

    def test_60m_sma_downward_returns_false(self):
        """60분봉 SMA5 <= SMA20 → False."""
        df = pd.DataFrame(
            {
                "close": [10000, 10200],
                "sma20": [9800, 9900],
                "macd_hist": [-0.5, 0.3],
                "rsi": [55, 52],
                "volume": [500000, 600000],
                "volume_sma20": [300000, 350000],
            }
        )
        df_60m = pd.DataFrame({"sma5": [9800], "sma20": [9900]})  # 하향

        assert check_entry_signal(df, df_60m) is False

    def test_empty_df_returns_false(self):
        """빈 DataFrame → False."""
        df = pd.DataFrame(columns=["close", "sma20", "macd_hist", "rsi", "volume", "volume_sma20"])
        df_60m = pd.DataFrame({"sma5": [10100], "sma20": [9900]})

        assert check_entry_signal(df, df_60m) is False

    def test_single_row_returns_false(self):
        """1행만 있는 DataFrame → False (전일 데이터 없음)."""
        df = pd.DataFrame(
            {
                "close": [10200],
                "sma20": [9900],
                "macd_hist": [0.3],
                "rsi": [52],
                "volume": [600000],
                "volume_sma20": [350000],
            }
        )
        df_60m = pd.DataFrame({"sma5": [10100], "sma20": [9900]})

        assert check_entry_signal(df, df_60m) is False


class TestCheckExitSignal:
    """check_exit_signal 테스트 — OR 조건 + 우선순위."""

    def _make_position(self, **kwargs) -> Position:
        """테스트용 Position 생성."""
        defaults = {
            "id": 1,
            "code": "005930",
            "name": "삼성전자",
            "entry_date": "2024-01-15",
            "entry_price": 70000,
            "quantity": 10,
            "stop_price": 65000,
            "target_price": 76000,
            "status": "open",
            "high_since_entry": 72000,
            "trailing_stop": 68000,
            "hold_days": 5,
            "prev_macd_hist": 0.5,
        }
        defaults.update(kwargs)
        return Position(**defaults)

    def _make_latest(self, macd_hist: float = 0.3) -> pd.Series:
        """테스트용 latest Series."""
        return pd.Series({"macd_hist": macd_hist})

    def test_stop_loss(self):
        """손절가 이탈 → STOP_LOSS."""
        pos = self._make_position(stop_price=65000)
        result = check_exit_signal(pos, 64000, self._make_latest())
        assert result == ExitReason.STOP_LOSS

    def test_trailing_stop(self):
        """트레일링스탑 발동 → TRAILING_STOP."""
        pos = self._make_position(stop_price=60000, trailing_stop=68000)
        result = check_exit_signal(pos, 67000, self._make_latest())
        assert result == ExitReason.TRAILING_STOP

    def test_target_reached(self):
        """목표가 도달 → TARGET_REACHED."""
        pos = self._make_position(
            stop_price=60000, trailing_stop=60000, target_price=76000
        )
        result = check_exit_signal(pos, 77000, self._make_latest())
        assert result == ExitReason.TARGET_REACHED

    def test_macd_dead_cross(self):
        """MACD 데드크로스 (수익 2%+) → MACD_DEAD."""
        pos = self._make_position(
            entry_price=70000,
            stop_price=60000,
            trailing_stop=60000,
            target_price=90000,
            prev_macd_hist=0.5,
        )
        # 현재가 71500 → 수익률 2.14%
        latest = self._make_latest(macd_hist=-0.3)
        result = check_exit_signal(pos, 71500, latest)
        assert result == ExitReason.MACD_DEAD

    def test_max_hold_exceeded(self):
        """최대 보유기간 초과 → MAX_HOLD."""
        pos = self._make_position(
            stop_price=60000,
            trailing_stop=60000,
            target_price=90000,
            hold_days=15,
            prev_macd_hist=-0.1,  # MACD 데드크로스 조건 미충족
        )
        result = check_exit_signal(pos, 71000, self._make_latest())
        assert result == ExitReason.MAX_HOLD

    def test_no_exit_signal(self):
        """어떤 조건도 미충족 → None."""
        pos = self._make_position(
            stop_price=60000,
            trailing_stop=60000,
            target_price=90000,
            hold_days=3,
            prev_macd_hist=-0.1,
        )
        result = check_exit_signal(pos, 71000, self._make_latest(macd_hist=0.2))
        assert result is None

    def test_priority_stop_loss_over_trailing(self):
        """손절가와 트레일링스탑 동시 이탈 → STOP_LOSS (우선)."""
        pos = self._make_position(stop_price=65000, trailing_stop=66000)
        result = check_exit_signal(pos, 64000, self._make_latest())
        assert result == ExitReason.STOP_LOSS

    def test_priority_trailing_over_target(self):
        """트레일링스탑 발동 + 목표가 동시 → TRAILING_STOP (우선).

        edge case: 현재가가 target_price 이상이지만 trailing_stop 이하인 경우.
        실제로는 불가능하지만 로직 순서 검증용.
        """
        pos = self._make_position(
            stop_price=60000,
            trailing_stop=78000,
            target_price=76000,
        )
        result = check_exit_signal(pos, 77000, self._make_latest())
        assert result == ExitReason.TRAILING_STOP

    def test_macd_dead_requires_2pct_profit(self):
        """MACD 데드크로스는 수익 2% 미만이면 미발동."""
        pos = self._make_position(
            entry_price=70000,
            stop_price=60000,
            trailing_stop=60000,
            target_price=90000,
            prev_macd_hist=0.5,
        )
        # 현재가 71000 → 수익률 1.43% < 2%
        latest = self._make_latest(macd_hist=-0.3)
        result = check_exit_signal(pos, 71000, latest)
        assert result is None  # MACD_DEAD 미발동, MAX_HOLD도 미해당


class TestCalculateSignalScore:
    """calculate_signal_score 테스트."""

    def test_score_range(self, indicators_df):
        """점수가 0~5 범위인지 확인."""
        score = calculate_signal_score(indicators_df)
        assert 0.0 <= score <= 5.0

    def test_empty_df_returns_zero(self):
        """빈 DataFrame → 0.0."""
        df = pd.DataFrame()
        assert calculate_signal_score(df) == 0.0

    def test_score_is_float(self, indicators_df):
        """점수가 float 타입인지 확인."""
        score = calculate_signal_score(indicators_df)
        assert isinstance(score, float)

    def test_high_score_conditions(self):
        """강한 신호 조건에서 높은 점수."""
        df = pd.DataFrame(
            {
                "close": [10000],
                "rsi": [52],           # RSI 50 근처 → 높은 점수
                "macd_hist": [50],     # 큰 양수 → 높은 점수
                "volume": [1000000],
                "volume_sma20": [400000],  # 2.5배 → 높은 점수
                "adx": [40],           # 강한 추세 → 높은 점수
                "bb_upper": [11000],
                "bb_mid": [10000],
                "bb_lower": [9000],    # 중간~상단 → 높은 점수
            }
        )
        score = calculate_signal_score(df)
        assert score >= 3.0

    def test_low_score_conditions(self):
        """약한 신호 조건에서 낮은 점수."""
        df = pd.DataFrame(
            {
                "close": [10000],
                "rsi": [85],             # RSI 극단 → 낮은 점수
                "macd_hist": [-10],      # 음수 → 0
                "volume": [100000],
                "volume_sma20": [500000],  # 0.2배 → 0
                "adx": [10],             # 약한 추세 → 0
                "bb_upper": [11000],
                "bb_mid": [10500],
                "bb_lower": [10000],     # 하단 근처
            }
        )
        score = calculate_signal_score(df)
        assert score < 1.5
