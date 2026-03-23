"""전략 패키지 — 전략 클래스 자동 등록."""

# 전략 클래스를 import하면 @register_strategy로 자동 등록됨
from src.strategy.golden_cross_strategy import GoldenCrossStrategy  # noqa: F401
from src.strategy.macd_rsi_strategy import MacdRsiStrategy  # noqa: F401
from src.strategy.bb_bounce_strategy import BbBounceStrategy  # noqa: F401
from src.strategy.breakout_strategy import BreakoutStrategy  # noqa: F401
from src.strategy.macd_pullback_strategy import MacdPullbackStrategy  # noqa: F401
from src.strategy.stoch_reversal_strategy import StochReversalStrategy  # noqa: F401
from src.strategy.base_strategy import get_strategy, available_strategies  # noqa: F401
