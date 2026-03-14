"""전략 추상 클래스 정의."""

from abc import ABC, abstractmethod

import pandas as pd


class BaseStrategy(ABC):
    """전략 추상 클래스."""

    @abstractmethod
    def calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """지표 계산."""
        pass

    @abstractmethod
    def check_entry_signal(self, df: pd.DataFrame, df_sub: pd.DataFrame) -> bool:
        """매수 신호 확인."""
        pass

    @abstractmethod
    def check_exit_signal(self, position, current_price: int, latest: pd.Series):
        """매도 신호 확인."""
        pass
