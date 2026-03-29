"""전략 패키지 — 전략 클래스 자동 등록."""

# 기존 전략 (비활성 — v2 전략으로 교체됨)
# from src.strategy.golden_cross_strategy import GoldenCrossStrategy  # noqa: F401
# from src.strategy.macd_rsi_strategy import MacdRsiStrategy  # noqa: F401
# from src.strategy.bb_bounce_strategy import BbBounceStrategy  # noqa: F401
# from src.strategy.breakout_strategy import BreakoutStrategy  # noqa: F401
# from src.strategy.macd_pullback_strategy import MacdPullbackStrategy  # noqa: F401
# from src.strategy.stoch_reversal_strategy import StochReversalStrategy  # noqa: F401
# from src.strategy.volume_breakout_strategy import VolumeBreakoutStrategy  # noqa: F401

# v2 전략
from src.strategy.momentum_pullback_strategy import MomentumPullbackStrategy  # noqa: F401
from src.strategy.institutional_flow_strategy import InstitutionalFlowStrategy  # noqa: F401

from src.strategy.base_strategy import get_strategy, available_strategies  # noqa: F401
