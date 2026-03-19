"""전략 추상 클래스 및 팩토리.

모든 매매 전략은 BaseStrategy를 상속하여 구현.
config.yaml의 strategy.type으로 전략을 선택하면
스크리닝/장중진입/백테스트 모두 동일한 전략이 적용됨.
"""

from abc import ABC, abstractmethod

import pandas as pd

from src.models import ExitReason, Position


class BaseStrategy(ABC):
    """전략 추상 클래스."""

    name: str = ""  # 전략 이름 (config.yaml의 strategy.type 값)

    def __init__(self, params: dict | None = None):
        self.params = params or {}

    @abstractmethod
    def check_screening_entry(self, df: pd.DataFrame) -> bool:
        """장전 스크리닝 매수 신호 (일봉 기반).

        Args:
            df: 지표 계산 완료된 일봉 DataFrame.

        Returns:
            매수 후보 여부.
        """

    @abstractmethod
    def check_realtime_entry(
        self, df_daily: pd.DataFrame, df_60m: pd.DataFrame | None = None
    ) -> bool:
        """장중 실시간 진입 신호 (일봉 + 60분봉).

        Args:
            df_daily: 지표 계산 완료된 일봉 DataFrame.
            df_60m: 60분봉 DataFrame (선택).

        Returns:
            매수 진입 여부.
        """

    @abstractmethod
    def generate_backtest_signals(
        self, df: pd.DataFrame
    ) -> tuple[pd.Series, pd.Series]:
        """백테스트용 entry/exit 시그널 생성.

        Look-ahead bias 방지를 위해 shift(1) 적용 필수.

        Args:
            df: OHLCV DataFrame.

        Returns:
            (entries, exits) 불리언 시리즈 튜플.
        """


# ── 전략 레지스트리 ──

_STRATEGY_REGISTRY: dict[str, type[BaseStrategy]] = {}


def register_strategy(cls: type[BaseStrategy]) -> type[BaseStrategy]:
    """전략 클래스를 레지스트리에 등록하는 데코레이터."""
    _STRATEGY_REGISTRY[cls.name] = cls
    return cls


def get_strategy(name: str, params: dict | None = None) -> BaseStrategy:
    """이름으로 전략 인스턴스 생성.

    Args:
        name: 전략 이름 (config.yaml의 strategy.type).
        params: 전략 파라미터.

    Returns:
        BaseStrategy 인스턴스.

    Raises:
        ValueError: 등록되지 않은 전략 이름.
    """
    if name not in _STRATEGY_REGISTRY:
        available = ", ".join(_STRATEGY_REGISTRY.keys())
        raise ValueError(f"Unknown strategy '{name}'. Available: {available}")
    return _STRATEGY_REGISTRY[name](params)


def available_strategies() -> list[str]:
    """등록된 전략 이름 목록."""
    return list(_STRATEGY_REGISTRY.keys())
