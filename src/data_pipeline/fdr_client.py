"""FinanceDataReader wrapper for swing-trader data pipeline."""
from __future__ import annotations

from datetime import date
from typing import Optional

import FinanceDataReader as fdr
import pandas as pd
from loguru import logger


class FdrClient:
    """FinanceDataReader의 얇은 래퍼. 실패 시 빈 DataFrame 반환."""

    def get_daily_ohlcv(
        self,
        ticker: str,
        start_date: date,
        end_date: Optional[date] = None,
    ) -> pd.DataFrame:
        try:
            start_str = start_date.strftime("%Y-%m-%d")
            end_str = end_date.strftime("%Y-%m-%d") if end_date else None
            df = fdr.DataReader(ticker, start_str, end_str)
            if df is None or df.empty:
                logger.debug(f"FDR returned empty for {ticker}")
                return pd.DataFrame()
            return df
        except Exception as e:
            logger.warning(f"FDR DataReader failed for {ticker}: {e}")
            return pd.DataFrame()
