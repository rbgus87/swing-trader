"""전략 패키지 — 전략 클래스 자동 등록."""

# ── 기존 전략 (비활성, 코드 보존) ──
# from src.strategy.golden_cross_strategy import GoldenCrossStrategy
# from src.strategy.macd_rsi_strategy import MacdRsiStrategy
# from src.strategy.bb_bounce_strategy import BbBounceStrategy
# from src.strategy.breakout_strategy import BreakoutStrategy
# from src.strategy.macd_pullback_strategy import MacdPullbackStrategy
# from src.strategy.stoch_reversal_strategy import StochReversalStrategy
# from src.strategy.volume_breakout_strategy import VolumeBreakoutStrategy

# ── v2 전략 (검증 실패, 코드 보존) ──
# from src.strategy.momentum_pullback_strategy import MomentumPullbackStrategy
# from src.strategy.institutional_flow_strategy import InstitutionalFlowStrategy

# ── 확정 전략 ──
from src.strategy.disparity_reversion_strategy import DisparityReversionStrategy  # noqa: F401

from src.strategy.base_strategy import get_strategy, available_strategies  # noqa: F401
