"""Anomaly detection for daily OHLCV data."""
from __future__ import annotations

import json

import pandas as pd


DAILY_CHANGE_THRESHOLD = 0.30


def detect_anomalies(ticker: str, df: pd.DataFrame) -> list[dict]:
    """이상치 목록 반환."""
    anomalies: list[dict] = []
    if df.empty:
        return anomalies

    zero_rows = df[
        (df["Open"] == 0)
        & (df["High"] == 0)
        & (df["Low"] == 0)
        & (df["Close"] == 0)
    ]
    for idx in zero_rows.index:
        anomalies.append(
            {
                "ticker": ticker,
                "date": idx.date().isoformat(),
                "anomaly_type": "OHLC_ALL_ZERO",
                "details": json.dumps({"volume": int(df.loc[idx, "Volume"])}),
                "severity": "INFO",
            }
        )

    invalid = df[df["High"] < df["Low"]]
    for idx in invalid.index:
        anomalies.append(
            {
                "ticker": ticker,
                "date": idx.date().isoformat(),
                "anomaly_type": "OHLC_INCONSISTENT",
                "details": json.dumps(
                    {
                        "high": float(df.loc[idx, "High"]),
                        "low": float(df.loc[idx, "Low"]),
                    }
                ),
                "severity": "ERROR",
            }
        )

    if "Close" in df.columns and len(df) > 1:
        df_sorted = df.sort_index()
        change = df_sorted["Close"].pct_change().abs()
        large_changes = df_sorted[change > DAILY_CHANGE_THRESHOLD]
        for idx in large_changes.index:
            anomalies.append(
                {
                    "ticker": ticker,
                    "date": idx.date().isoformat(),
                    "anomaly_type": "LARGE_DAILY_CHANGE",
                    "details": json.dumps(
                        {
                            "change_rate": float(change.loc[idx]),
                            "close": float(df_sorted.loc[idx, "Close"]),
                        }
                    ),
                    "severity": "WARN",
                }
            )

    if "Volume" in df.columns:
        neg_vol = df[df["Volume"] < 0]
        for idx in neg_vol.index:
            anomalies.append(
                {
                    "ticker": ticker,
                    "date": idx.date().isoformat(),
                    "anomaly_type": "NEGATIVE_VOLUME",
                    "details": json.dumps({"volume": int(df.loc[idx, "Volume"])}),
                    "severity": "ERROR",
                }
            )

    return anomalies


def filter_invalid_rows(df: pd.DataFrame) -> pd.DataFrame:
    """저장 전 잘못된 행 제거."""
    if df.empty:
        return df

    df = df.dropna(subset=["Open", "High", "Low", "Close"])
    mask_all_zero = (
        (df["Open"] == 0)
        & (df["High"] == 0)
        & (df["Low"] == 0)
        & (df["Close"] == 0)
    )
    df = df[~mask_all_zero]
    df = df[df["Close"] > 0]
    return df
