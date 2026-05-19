"""DB에서 스코어 계산용 데이터 로드. get_data_db()와 daily_candles 테이블 사용."""
from __future__ import annotations

from datetime import date

import pandas as pd

from src.data_pipeline.db import get_data_db


def load_daily_for_scorer(
    tickers: list[str],
    lookback_days: int = 180,
    today: str | None = None,
) -> dict[str, pd.DataFrame]:
    """daily_candles에서 스코어 계산용 일봉 로드.

    Returns:
        ticker → DataFrame(date, open, high, low, close, volume), date 오름차순
    """
    if today is None:
        today = date.today().isoformat()

    result: dict[str, pd.DataFrame] = {}
    if not tickers:
        return result

    with get_data_db() as conn:
        for ticker in tickers:
            rows = conn.execute(
                "SELECT date, open, high, low, close, volume "
                "FROM daily_candles WHERE ticker = ? AND date <= ? "
                "ORDER BY date DESC LIMIT ?",
                (ticker, today, lookback_days),
            ).fetchall()
            if not rows:
                continue
            df = pd.DataFrame([dict(r) for r in reversed(rows)])
            df["date"] = pd.to_datetime(df["date"])
            for col in ("open", "high", "low", "close", "volume"):
                df[col] = pd.to_numeric(df[col], errors="coerce")
            result[ticker] = df

    return result


def load_market_df(
    index_code: str = "KOSPI",
    lookback_days: int = 180,
    today: str | None = None,
) -> pd.DataFrame:
    """index_daily에서 시장 지수 일봉 로드 (close 컬럼 포함).

    Returns:
        DataFrame(date, close), date 오름차순. 데이터 없으면 빈 DataFrame.
    """
    if today is None:
        today = date.today().isoformat()

    with get_data_db() as conn:
        rows = conn.execute(
            "SELECT date, close FROM index_daily "
            "WHERE index_code = ? AND date <= ? "
            "ORDER BY date DESC LIMIT ?",
            (index_code, today, lookback_days),
        ).fetchall()

    if not rows:
        return pd.DataFrame(columns=["date", "close"])

    df = pd.DataFrame([dict(r) for r in reversed(rows)])
    df["date"] = pd.to_datetime(df["date"])
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    return df
