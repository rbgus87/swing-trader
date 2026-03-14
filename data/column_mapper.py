"""pykrx 데이터 컬럼 한→영 변환 및 파생 지표 계산.

pykrx에서 반환하는 한글 컬럼명을 영문으로 매핑하고,
제공되지 않는 ROE 등 파생 지표를 수동 계산.
"""

import pandas as pd

# OHLCV 컬럼 매핑 (pykrx → 내부)
OHLCV_MAP: dict[str, str] = {
    "시가": "open",
    "고가": "high",
    "저가": "low",
    "종가": "close",
    "거래량": "volume",
    "거래대금": "amount",
    "등락률": "change_rate",
}

# 펀더멘탈 컬럼 매핑
FUNDAMENTAL_MAP: dict[str, str] = {
    "BPS": "bps",
    "PER": "per",
    "PBR": "pbr",
    "EPS": "eps",
    "DIV": "div_yield",
    "DPS": "dps",
}


def map_columns(df: pd.DataFrame, col_map: dict[str, str]) -> pd.DataFrame:
    """DataFrame 컬럼명을 매핑 딕셔너리에 따라 변환.

    col_map에 존재하는 컬럼만 변환하고, 없는 컬럼은 그대로 유지.

    Args:
        df: 원본 DataFrame.
        col_map: {원래컬럼명: 새컬럼명} 매핑.

    Returns:
        컬럼명이 변환된 DataFrame (복사본).
    """
    rename_map = {k: v for k, v in col_map.items() if k in df.columns}
    return df.rename(columns=rename_map)


def calculate_roe(df: pd.DataFrame) -> pd.DataFrame:
    """ROE(%) 계산: EPS / BPS * 100.

    BPS > 0인 종목만 계산하고, 나머지는 NaN 처리.
    eps, bps 컬럼이 이미 영문 변환되어 있어야 함.

    Args:
        df: eps, bps 컬럼을 포함하는 DataFrame.

    Returns:
        roe 컬럼이 추가된 DataFrame (복사본).
    """
    result = df.copy()
    result["roe"] = None

    mask = result["bps"] > 0
    result.loc[mask, "roe"] = result.loc[mask, "eps"] / result.loc[mask, "bps"] * 100

    return result
