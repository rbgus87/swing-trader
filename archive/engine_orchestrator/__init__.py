"""Phase 3 4-레이어 엔진.

레이어:
- Layer 1: RegimeDetector (시장 국면)
- Layer 2: StrategyRouter (전략 라우팅)
- Layer 3: Strategy Modules (src/strategy/*)
- Layer 4: PortfolioManager (포지션 관리)

실행/주문은 orchestrator (Step 2b)에서.
"""
