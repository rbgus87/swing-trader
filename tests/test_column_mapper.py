"""column_mapper 모듈 테스트."""

import pandas as pd

from data.column_mapper import (
    FUNDAMENTAL_MAP,
    OHLCV_MAP,
    calculate_roe,
    map_columns,
)


class TestMapColumns:
    def test_ohlcv_mapping(self):
        """OHLCV 한글 컬럼을 영문으로 변환."""
        df = pd.DataFrame(
            {
                "시가": [69000],
                "고가": [71000],
                "저가": [68500],
                "종가": [70000],
                "거래량": [1000000],
                "거래대금": [70000000000],
                "등락률": [1.5],
            }
        )
        result = map_columns(df, OHLCV_MAP)

        assert "open" in result.columns
        assert "close" in result.columns
        assert "volume" in result.columns
        assert "amount" in result.columns
        assert "change_rate" in result.columns
        assert result["close"].iloc[0] == 70000

    def test_fundamental_mapping(self):
        """펀더멘탈 컬럼 변환."""
        df = pd.DataFrame(
            {
                "BPS": [50000],
                "PER": [10.5],
                "PBR": [1.4],
                "EPS": [6700],
                "DIV": [2.1],
                "DPS": [1500],
            }
        )
        result = map_columns(df, FUNDAMENTAL_MAP)

        assert "bps" in result.columns
        assert "eps" in result.columns
        assert "div_yield" in result.columns
        assert result["bps"].iloc[0] == 50000

    def test_partial_mapping(self):
        """매핑에 없는 컬럼은 원래 이름 유지."""
        df = pd.DataFrame({"시가": [100], "extra_col": [999]})
        result = map_columns(df, OHLCV_MAP)

        assert "open" in result.columns
        assert "extra_col" in result.columns

    def test_empty_dataframe(self):
        """빈 DataFrame 처리."""
        df = pd.DataFrame()
        result = map_columns(df, OHLCV_MAP)
        assert len(result) == 0


class TestCalculateRoe:
    def test_positive_bps(self):
        """BPS > 0일 때 ROE 계산."""
        df = pd.DataFrame(
            {
                "eps": [6700, 3000, 500],
                "bps": [50000, 25000, 10000],
            }
        )
        result = calculate_roe(df)

        assert "roe" in result.columns
        # 6700 / 50000 * 100 = 13.4
        assert abs(result["roe"].iloc[0] - 13.4) < 0.01
        # 3000 / 25000 * 100 = 12.0
        assert abs(result["roe"].iloc[1] - 12.0) < 0.01

    def test_zero_bps_excluded(self):
        """BPS <= 0인 종목은 ROE가 None."""
        df = pd.DataFrame(
            {
                "eps": [6700, -1000, 500],
                "bps": [50000, 0, -5000],
            }
        )
        result = calculate_roe(df)

        assert result["roe"].iloc[0] is not None
        assert result["roe"].iloc[1] is None
        assert result["roe"].iloc[2] is None

    def test_original_not_modified(self):
        """원본 DataFrame이 수정되지 않음."""
        df = pd.DataFrame({"eps": [6700], "bps": [50000]})
        _ = calculate_roe(df)
        assert "roe" not in df.columns
