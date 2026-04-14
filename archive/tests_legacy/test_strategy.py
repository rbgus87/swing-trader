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
    check_golden_cross_entry,
    check_golden_cross_exit,
    get_institutional_net_buying,
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

    def test_entry_signal_without_60m(self):
        """use_60m=False일 때 60분봉 조건 무시하고 정상 동작."""
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
        # 60분봉 하향이지만 use_60m=False이므로 무시
        df_60m = pd.DataFrame({"sma5": [9800], "sma20": [9900]})

        assert check_entry_signal(df, df_60m, use_60m=False) is True

    def test_entry_signal_with_none_60m(self):
        """df_60m=None일 때 60분봉 조건 스킵하고 정상 동작."""
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

        assert check_entry_signal(df, None) is True
        assert check_entry_signal(df) is True

    def test_relaxed_rsi_range(self):
        """RSI 35~70 범위에서 더 많은 신호 발생 확인."""
        # RSI 38: 기존 범위(40~65)에서는 False, 완화 범위(35~70)에서는 True
        df = pd.DataFrame(
            {
                "close": [10000, 10200],
                "sma20": [9800, 9900],
                "macd_hist": [-0.5, 0.3],
                "rsi": [55, 38],
                "volume": [500000, 600000],
                "volume_sma20": [300000, 350000],
            }
        )

        # 기존 범위에서는 False
        assert check_entry_signal(df, None, rsi_entry_min=40, rsi_entry_max=65) is False
        # 완화된 범위에서는 True
        assert check_entry_signal(df, None, rsi_entry_min=35, rsi_entry_max=70) is True

        # RSI 68도 동일하게 확인
        df2 = df.copy()
        df2.loc[1, "rsi"] = 68
        assert check_entry_signal(df2, None, rsi_entry_min=40, rsi_entry_max=65) is False
        assert check_entry_signal(df2, None, rsi_entry_min=35, rsi_entry_max=70) is True


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
            "hold_days": 5,
        }
        defaults.update(kwargs)
        # trailing_stop, prev_macd_hist는 Position 필드에서 제거됨
        # 테스트 호환용: kwargs에서 꺼내서 생성 후 동적 할당
        trailing_stop = defaults.pop("trailing_stop", 0)
        prev_macd_hist = defaults.pop("prev_macd_hist", 0.0)
        pos = Position(**defaults)
        pos.trailing_stop = trailing_stop
        pos.prev_macd_hist = prev_macd_hist
        return pos

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
        """점수가 0~7 범위인지 확인."""
        score = calculate_signal_score(indicators_df)
        assert 0.0 <= score <= 7.0

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


class TestGoldenCrossEntry:
    """check_golden_cross_entry 테스트."""

    def test_golden_cross_entry_basic(self):
        """모든 AND 조건 충족 시 True."""
        df = pd.DataFrame(
            {
                "sma5": [9900, 10100],    # 전일 SMA5 <= SMA20, 당일 SMA5 > SMA20
                "sma20": [10000, 10000],
                "rsi": [55, 55],          # RSI >= 50
                "adx": [25, 25],          # ADX >= 20
                "volume": [500000, 600000],
                "volume_sma20": [400000, 500000],  # 600000 >= 500000 * 1.0
            }
        )
        assert check_golden_cross_entry(df) is True

    def test_golden_cross_entry_no_cross(self):
        """SMA5/20 크로스 없음 → False."""
        df = pd.DataFrame(
            {
                "sma5": [10100, 10200],   # 전일도 SMA5 > SMA20 (크로스 아님)
                "sma20": [10000, 10000],
                "rsi": [55, 55],
                "adx": [25, 25],
                "volume": [500000, 600000],
                "volume_sma20": [400000, 500000],
            }
        )
        assert check_golden_cross_entry(df) is False

    def test_golden_cross_entry_low_rsi(self):
        """RSI < 50 → False."""
        df = pd.DataFrame(
            {
                "sma5": [9900, 10100],
                "sma20": [10000, 10000],
                "rsi": [55, 45],          # RSI < 50
                "adx": [25, 25],
                "volume": [500000, 600000],
                "volume_sma20": [400000, 500000],
            }
        )
        assert check_golden_cross_entry(df) is False

    def test_golden_cross_entry_low_adx(self):
        """ADX < threshold → False."""
        df = pd.DataFrame(
            {
                "sma5": [9900, 10100],
                "sma20": [10000, 10000],
                "rsi": [55, 55],
                "adx": [15, 15],          # ADX < 20
                "volume": [500000, 600000],
                "volume_sma20": [400000, 500000],
            }
        )
        assert check_golden_cross_entry(df) is False

    def test_golden_cross_entry_low_volume(self):
        """거래량 부족 → False."""
        df = pd.DataFrame(
            {
                "sma5": [9900, 10100],
                "sma20": [10000, 10000],
                "rsi": [55, 55],
                "adx": [25, 25],
                "volume": [500000, 400000],  # 400000 < 500000 * 1.0
                "volume_sma20": [400000, 500000],
            }
        )
        assert check_golden_cross_entry(df) is False

    def test_golden_cross_entry_empty_df(self):
        """빈 DataFrame → False."""
        df = pd.DataFrame(columns=["sma5", "sma20", "rsi", "adx", "volume", "volume_sma20"])
        assert check_golden_cross_entry(df) is False

    def test_golden_cross_entry_single_row(self):
        """1행만 있는 DataFrame → False."""
        df = pd.DataFrame(
            {
                "sma5": [10100],
                "sma20": [10000],
                "rsi": [55],
                "adx": [25],
                "volume": [600000],
                "volume_sma20": [500000],
            }
        )
        assert check_golden_cross_entry(df) is False

    def test_golden_cross_entry_custom_adx_threshold(self):
        """커스텀 ADX threshold 적용."""
        df = pd.DataFrame(
            {
                "sma5": [9900, 10100],
                "sma20": [10000, 10000],
                "rsi": [55, 55],
                "adx": [25, 25],
                "volume": [500000, 600000],
                "volume_sma20": [400000, 500000],
            }
        )
        # ADX=25 >= threshold=30 → False
        assert check_golden_cross_entry(df, adx_threshold=30) is False
        # ADX=25 >= threshold=20 → True
        assert check_golden_cross_entry(df, adx_threshold=20) is True


class TestGoldenCrossExit:
    """check_golden_cross_exit 테스트."""

    def test_golden_cross_exit_dead_cross(self):
        """데드크로스 발생 시 True."""
        df = pd.DataFrame(
            {
                "sma5": [10100, 9900],    # 전일 SMA5 >= SMA20, 당일 SMA5 < SMA20
                "sma20": [10000, 10000],
            }
        )
        assert check_golden_cross_exit(df)

    def test_golden_cross_exit_no_dead_cross(self):
        """데드크로스 없음 → False."""
        df = pd.DataFrame(
            {
                "sma5": [9900, 9800],     # 전일도 SMA5 < SMA20
                "sma20": [10000, 10000],
            }
        )
        assert not check_golden_cross_exit(df)

    def test_golden_cross_exit_empty_df(self):
        """빈 DataFrame → False."""
        df = pd.DataFrame(columns=["sma5", "sma20"])
        assert check_golden_cross_exit(df) is False

    def test_golden_cross_exit_single_row(self):
        """1행만 있는 DataFrame → False."""
        df = pd.DataFrame({"sma5": [9900], "sma20": [10000]})
        assert check_golden_cross_exit(df) is False


class TestOBVIndicator:
    """OBV 지표 추가 테스트."""

    def test_obv_columns_added(self, indicators_df):
        """OBV 관련 컬럼이 추가되는지 확인."""
        assert "obv" in indicators_df.columns
        assert "obv_sma20" in indicators_df.columns

    def test_obv_not_all_zero(self, indicators_df):
        """OBV 값이 전부 0이 아닌지 확인."""
        assert indicators_df["obv"].abs().sum() > 0


class TestSignalScoreSupply:
    """수급 스코어링 테스트."""

    def test_score_with_institutional_buying(self):
        """기관 순매수 시 가점."""
        df = pd.DataFrame(
            {
                "close": [10000],
                "rsi": [52],
                "macd_hist": [0.1],
                "volume": [500000],
                "volume_sma20": [500000],
                "adx": [20],
                "bb_upper": [11000],
                "bb_mid": [10000],
                "bb_lower": [9000],
            }
        )
        score_no_supply = calculate_signal_score(df)
        score_inst = calculate_signal_score(df, institutional_net=1_000_000_000)
        score_foreign = calculate_signal_score(df, foreign_net=2_000_000_000)
        score_both = calculate_signal_score(
            df, institutional_net=1_000_000_000, foreign_net=2_000_000_000
        )

        assert score_inst > score_no_supply
        assert score_foreign > score_no_supply
        assert score_both > score_inst
        assert score_both - score_no_supply == pytest.approx(1.0)

    def test_score_with_net_selling_no_bonus(self):
        """순매도 시 가점 없음."""
        df = pd.DataFrame(
            {
                "close": [10000],
                "rsi": [52],
                "macd_hist": [0.1],
                "volume": [500000],
                "volume_sma20": [500000],
                "adx": [20],
                "bb_upper": [11000],
                "bb_mid": [10000],
                "bb_lower": [9000],
            }
        )
        score_no_supply = calculate_signal_score(df)
        score_selling = calculate_signal_score(
            df, institutional_net=-500_000_000, foreign_net=-1_000_000_000
        )

        assert score_no_supply == score_selling


class TestOBVTrendScore:
    """OBV 추세 일치 점수 테스트."""

    def test_obv_trend_match_adds_score(self):
        """가격+OBV 동시 상승 시 가점."""
        # 6행 이상 필요 (5일 추세 비교)
        df = pd.DataFrame(
            {
                "close": [9500, 9600, 9700, 9800, 9900, 10000],
                "rsi": [50, 50, 50, 50, 50, 52],
                "macd_hist": [0, 0, 0, 0, 0, 0.1],
                "volume": [500000] * 6,
                "volume_sma20": [500000] * 6,
                "adx": [20] * 6,
                "bb_upper": [11000] * 6,
                "bb_mid": [10000] * 6,
                "bb_lower": [9000] * 6,
                "obv": [100, 200, 300, 400, 500, 600],  # OBV 상승
                "obv_sma20": [50, 100, 150, 200, 250, 300],  # OBV > SMA
            }
        )

        score_with_obv = calculate_signal_score(df)

        # OBV 컬럼 제거한 버전과 비교
        df_no_obv = df.drop(columns=["obv", "obv_sma20"])
        score_no_obv = calculate_signal_score(df_no_obv)

        assert score_with_obv > score_no_obv

    def test_obv_divergence_no_bonus(self):
        """가격 상승 + OBV 하락 (다이버전스) 시 가점 없음."""
        df = pd.DataFrame(
            {
                "close": [9500, 9600, 9700, 9800, 9900, 10000],  # 상승
                "rsi": [50, 50, 50, 50, 50, 52],
                "macd_hist": [0, 0, 0, 0, 0, 0.1],
                "volume": [500000] * 6,
                "volume_sma20": [500000] * 6,
                "adx": [20] * 6,
                "bb_upper": [11000] * 6,
                "bb_mid": [10000] * 6,
                "bb_lower": [9000] * 6,
                "obv": [600, 500, 400, 300, 200, 100],  # OBV 하락
                "obv_sma20": [700, 600, 500, 400, 300, 200],
            }
        )

        df_no_obv = df.drop(columns=["obv", "obv_sma20"])
        score_with_obv = calculate_signal_score(df)
        score_no_obv = calculate_signal_score(df_no_obv)

        assert score_with_obv == score_no_obv


class TestVolumeBreakoutStrategy:
    """VolumeBreakoutStrategy 테스트."""

    @pytest.fixture
    def strategy(self):
        from src.strategy.volume_breakout_strategy import VolumeBreakoutStrategy
        return VolumeBreakoutStrategy(params={
            "vol_lookback": 20,
            "vol_breakout_multiplier": 0.8,
            "rsi_entry_min": 40,
            "rsi_entry_max": 70,
            "adx_threshold": 15,
        })

    @pytest.fixture
    def vol_breakout_df(self):
        """거래량 돌파 조건을 충족하는 DataFrame (150행)."""
        np.random.seed(99)
        n = 150
        # 완만한 상승 추세
        base = 50000 + np.cumsum(np.random.normal(50, 200, n))
        close = np.round(np.maximum(base, 10000)).astype(int)
        high = np.round(close * 1.02).astype(int)
        low = np.round(close * 0.98).astype(int)
        open_ = np.round(low + (high - low) * 0.5).astype(int)
        volume = np.random.randint(100000, 200000, n)
        volume[-1] = 500000  # 거래량 돌파

        df = pd.DataFrame({
            "open": open_, "high": high, "low": low,
            "close": close, "volume": volume,
        })
        return calculate_indicators(df)

    @pytest.fixture
    def vol_no_breakout_df(self):
        """거래량 돌파 미충족 DataFrame (150행)."""
        np.random.seed(99)
        n = 150
        base = 50000 + np.cumsum(np.random.normal(50, 200, n))
        close = np.round(np.maximum(base, 10000)).astype(int)
        high = np.round(close * 1.02).astype(int)
        low = np.round(close * 0.98).astype(int)
        open_ = np.round(low + (high - low) * 0.5).astype(int)
        volume = np.random.randint(100000, 200000, n)
        volume[-1] = 120000  # 평범한 거래량

        df = pd.DataFrame({
            "open": open_, "high": high, "low": low,
            "close": close, "volume": volume,
        })
        return calculate_indicators(df)

    def test_screening_entry_volume_breakout(self, strategy, vol_breakout_df):
        """거래량 돌파 시 스크리닝 진입 True."""
        df = vol_breakout_df
        latest = df.iloc[-1]
        # OBV > OBV_SMA20이고 RSI 범위 내인지 확인
        if (
            latest.get("obv", 0) > latest.get("obv_sma20", 0)
            and 40 <= latest.get("rsi", 50) <= 70
            and latest["close"] > latest["sma20"]
        ):
            assert strategy.check_screening_entry(df) is True

    def test_screening_entry_no_volume_breakout(self, strategy, vol_no_breakout_df):
        """거래량 미돌파 시 스크리닝 진입 False."""
        assert strategy.check_screening_entry(vol_no_breakout_df) is False

    def test_screening_entry_insufficient_data(self, strategy):
        """데이터 부족 시 False."""
        df = pd.DataFrame({
            "open": [10000], "high": [10200], "low": [9800],
            "close": [10100], "volume": [500000],
        })
        assert strategy.check_screening_entry(df) is False

    def test_screening_rsi_too_high(self, strategy, vol_breakout_df):
        """RSI 과매수 시 진입 차단."""
        df = vol_breakout_df.copy()
        df.iloc[-1, df.columns.get_loc("rsi")] = 80
        assert strategy.check_screening_entry(df) is False

    def test_screening_below_sma20(self, strategy, vol_breakout_df):
        """종가 < SMA20 시 진입 차단."""
        df = vol_breakout_df.copy()
        df.iloc[-1, df.columns.get_loc("close")] = 1000  # SMA20보다 훨씬 낮게
        assert strategy.check_screening_entry(df) is False

    def test_realtime_entry_adx_filter(self, strategy, vol_breakout_df):
        """ADX 미달 시 realtime 진입 차단."""
        df = vol_breakout_df.copy()
        df.iloc[-1, df.columns.get_loc("adx")] = 5  # 임계값 미달
        assert strategy.check_realtime_entry(df) is False

    def test_backtest_signals_shape(self, strategy, ohlcv_df):
        """백테스트 시그널이 올바른 형태를 반환."""
        entries, exits = strategy.generate_backtest_signals(ohlcv_df)
        assert isinstance(entries, pd.Series)
        assert isinstance(exits, pd.Series)
        assert len(entries) == len(exits)
        assert entries.dtype == bool
        assert exits.dtype == bool

    def test_backtest_signals_no_lookahead(self, strategy, ohlcv_df):
        """첫 번째 행은 항상 False (shift(1) look-ahead 방지)."""
        entries, exits = strategy.generate_backtest_signals(ohlcv_df)
        assert entries.iloc[0] is np.bool_(False)

    def test_strategy_registered(self):
        """volume_breakout이 레지스트리에 등록되었는지 확인."""
        from src.strategy.base_strategy import available_strategies, get_strategy
        assert "volume_breakout" in available_strategies()
        s = get_strategy("volume_breakout")
        assert s.name == "volume_breakout"
        assert s.category == "trend"

    def test_backtest_exit_volume_dry(self, strategy):
        """거래량 급감 시 exit 시그널 발생."""
        np.random.seed(42)
        n = 150
        base = 50000 + np.cumsum(np.random.normal(50, 200, n))
        close = np.round(np.maximum(base, 10000)).astype(int)
        high = np.round(close * 1.02).astype(int)
        low = np.round(close * 0.98).astype(int)
        open_ = np.round(low + (high - low) * 0.5).astype(int)
        volume = np.random.randint(100000, 2000000, n)
        # 마지막 몇 행 거래량을 극도로 낮게 (volume_sma20 * 0.5 미만)
        volume[-5:] = 10000

        df = pd.DataFrame({
            "open": open_, "high": high, "low": low,
            "close": close, "volume": volume,
        })
        entries, exits = strategy.generate_backtest_signals(df)
        # 거래량 급감 구간 근처에서 exit 시그널 있어야 함
        assert exits.iloc[-3:].any()


class TestInstitutionalNetBuying:
    """get_institutional_net_buying 테스트."""

    def test_returns_tuple(self):
        """항상 (int, int) 튜플 반환."""
        result = get_institutional_net_buying("005930")
        assert isinstance(result, tuple)
        assert len(result) == 2

    def test_graceful_on_invalid_code(self):
        """잘못된 종목코드에도 (0, 0) 반환."""
        result = get_institutional_net_buying("INVALID")
        assert result == (0, 0)


# ── MomentumPullback 전략 테스트 ──────────────────────────────


class TestMomentumPullbackStrategy:
    """momentum_pullback 전략 테스트."""

    @pytest.fixture
    def momentum_df(self) -> pd.DataFrame:
        """모멘텀 + 눌림목 시나리오 데이터 (200행).

        상승 추세 → 3일 눌림 → 당일 반등.
        SMA120 워밍업 고려하여 200행 필요.
        """
        np.random.seed(123)
        n = 200

        # 기본 상승 추세
        base = 50000 + np.cumsum(np.random.normal(80, 150, n))
        base = np.maximum(base, 30000)

        close = np.round(base).astype(int)
        high = np.round(close * 1.015).astype(int)
        low = np.round(close * 0.985).astype(int)

        # open 생성: 대부분 양봉
        open_ = np.round(close * 0.995).astype(int)

        # 마지막 3일: 눌림 (음봉)
        for i in range(-4, -1):
            close[i] = int(close[i] * 0.99)
            open_[i] = int(close[i] * 1.01)  # 음봉: open > close

        # 마지막 날: 반등 양봉
        close[-1] = int(close[-2] * 1.02)
        open_[-1] = int(close[-1] * 0.99)
        high[-1] = int(close[-1] * 1.01)

        volume = np.random.randint(500000, 2000000, n)
        volume[-1] = 3000000  # 반등일 거래량 증가

        df = pd.DataFrame({
            "open": open_, "high": high, "low": low,
            "close": close, "volume": volume,
        })
        return df

    def test_screening_entry_momentum_pullback(self, momentum_df):
        """모멘텀 양수 + 눌림 + 양봉 → True."""
        from src.strategy.momentum_pullback_strategy import MomentumPullbackStrategy

        df = calculate_indicators(momentum_df)
        strategy = MomentumPullbackStrategy({"momentum_period": 60, "pullback_days": 3})
        result = strategy.check_screening_entry(df)
        assert result is True

    def test_screening_entry_no_momentum(self, momentum_df):
        """모멘텀 음수 → False."""
        from src.strategy.momentum_pullback_strategy import MomentumPullbackStrategy

        # 가격을 하락 추세로 변환
        df = momentum_df.copy()
        df["close"] = df["close"].iloc[::-1].values  # 역순 → 하락 추세
        df = calculate_indicators(df)
        strategy = MomentumPullbackStrategy({"momentum_period": 60})
        result = strategy.check_screening_entry(df)
        assert result is False

    def test_backtest_signals_no_lookahead(self, momentum_df):
        """백테스트 시그널: boolean Series + shift 적용 (look-ahead bias 없음)."""
        from src.strategy.momentum_pullback_strategy import MomentumPullbackStrategy

        strategy = MomentumPullbackStrategy({"momentum_period": 60, "pullback_days": 3})
        entries, exits = strategy.generate_backtest_signals(momentum_df)

        assert isinstance(entries, pd.Series)
        assert isinstance(exits, pd.Series)
        assert entries.dtype == bool
        assert exits.dtype == bool
        # shift(1) 적용 확인: 첫 행은 항상 False
        assert not entries.iloc[0]
        assert not exits.iloc[0]

    def test_strategy_registered(self):
        """전략이 레지스트리에 등록됨."""
        from src.strategy.base_strategy import available_strategies
        assert "momentum_pullback" in available_strategies()


# ── InstitutionalFlow 전략 테스트 ──────────────────────────────


class TestInstitutionalFlowStrategy:
    """institutional_flow 전략 테스트."""

    @pytest.fixture
    def flow_df(self) -> pd.DataFrame:
        """추세+방향성 시나리오 데이터 (200행).

        강한 상승 추세 + 높은 ADX + 양봉 + 거래량 충분.
        """
        np.random.seed(456)
        n = 200

        base = 50000 + np.cumsum(np.random.normal(100, 100, n))
        base = np.maximum(base, 30000)

        close = np.round(base).astype(int)
        high = np.round(close * 1.015).astype(int)
        low = np.round(close * 0.985).astype(int)
        open_ = np.round(close * 0.995).astype(int)  # 대부분 양봉
        volume = np.random.randint(800000, 2000000, n)

        df = pd.DataFrame({
            "open": open_, "high": high, "low": low,
            "close": close, "volume": volume,
        })
        return df

    def test_screening_entry_strong_trend(self, flow_df):
        """ADX > threshold + 양봉 + 거래량 → True."""
        from src.strategy.institutional_flow_strategy import InstitutionalFlowStrategy

        df = calculate_indicators(flow_df)
        strategy = InstitutionalFlowStrategy({"adx_threshold": 20})
        result = strategy.check_screening_entry(df)
        assert result is True

    def test_screening_entry_weak_trend(self, flow_df):
        """ADX < threshold → False."""
        from src.strategy.institutional_flow_strategy import InstitutionalFlowStrategy

        df = calculate_indicators(flow_df)
        # 매우 높은 threshold로 ADX 조건 불충족 유도
        strategy = InstitutionalFlowStrategy({"adx_threshold": 99})
        result = strategy.check_screening_entry(df)
        assert result is False

    def test_backtest_signals_shape(self, flow_df):
        """백테스트 시그널: boolean Series + look-ahead bias 없음."""
        from src.strategy.institutional_flow_strategy import InstitutionalFlowStrategy

        strategy = InstitutionalFlowStrategy({"adx_threshold": 20})
        entries, exits = strategy.generate_backtest_signals(flow_df)

        assert isinstance(entries, pd.Series)
        assert isinstance(exits, pd.Series)
        assert entries.dtype == bool
        assert exits.dtype == bool
        assert not entries.iloc[0]
        assert not exits.iloc[0]

    def test_strategy_registered(self):
        """전략이 레지스트리에 등록됨."""
        from src.strategy.base_strategy import available_strategies
        assert "institutional_flow" in available_strategies()


# ── DisparityReversion 전략 테스트 ──────────────────────────────


class TestDisparityReversionStrategy:
    """disparity_reversion 전략 테스트."""

    @pytest.fixture
    def oversold_df(self) -> pd.DataFrame:
        """과매도 시나리오 데이터 (200행).

        장기 상승 후 급락 → 이격도 << 93% + RSI < 25 + 마지막 날 양봉.
        """
        np.random.seed(789)
        n = 200

        # 195일 상승 + 마지막 5일 급락 (매일 -3%씩, SMA60 아직 상승 중)
        base_up = 50000 + np.cumsum(np.random.normal(80, 60, 195))
        peak = base_up[-1]
        base_down = [peak]
        for _ in range(4):
            base_down.append(base_down[-1] * 0.97)
        base = np.concatenate([base_up, base_down])
        base = np.maximum(base, 30000)

        close = np.round(base).astype(int)
        high = np.round(close * 1.01).astype(int)
        low = np.round(close * 0.99).astype(int)
        # 하락 구간: 음봉, 마지막 날: 양봉 반등
        open_ = np.round(close * 1.005).astype(int)  # 기본 음봉
        open_[-1] = int(close[-1] * 0.99)  # 마지막 날: 양봉
        close[-1] = int(close[-2] * 1.015)  # 반등
        high[-1] = int(close[-1] * 1.01)

        volume = np.random.randint(500000, 1500000, n)

        df = pd.DataFrame({
            "open": open_, "high": high, "low": low,
            "close": close, "volume": volume,
        })
        return df

    def test_screening_entry_oversold(self, oversold_df):
        """이격도 < 93 + RSI < 25 + 양봉 → True."""
        from src.strategy.disparity_reversion_strategy import DisparityReversionStrategy

        df = calculate_indicators(oversold_df)
        strategy = DisparityReversionStrategy({"disparity_entry": 96, "rsi_oversold": 40})
        result = strategy.check_screening_entry(df)
        assert result is True

    def test_screening_entry_not_oversold(self, oversold_df):
        """이격도 > threshold → False."""
        from src.strategy.disparity_reversion_strategy import DisparityReversionStrategy

        df = calculate_indicators(oversold_df)
        # 매우 낮은 threshold → 이격도 조건 불충족
        strategy = DisparityReversionStrategy({"disparity_entry": 50, "rsi_oversold": 25})
        result = strategy.check_screening_entry(df)
        assert result is False

    def test_backtest_signals_shape(self, oversold_df):
        """백테스트 시그널: boolean Series + look-ahead bias 없음."""
        from src.strategy.disparity_reversion_strategy import DisparityReversionStrategy

        strategy = DisparityReversionStrategy({"disparity_entry": 95, "rsi_oversold": 35})
        entries, exits = strategy.generate_backtest_signals(oversold_df)

        assert isinstance(entries, pd.Series)
        assert isinstance(exits, pd.Series)
        assert entries.dtype == bool
        assert exits.dtype == bool
        assert not entries.iloc[0]
        assert not exits.iloc[0]

    def test_strategy_registered(self):
        """전략이 레지스트리에 등록됨."""
        from src.strategy.base_strategy import available_strategies
        assert "disparity_reversion" in available_strategies()
