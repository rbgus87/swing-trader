"""Phase 1 재설계 중 — 전략 레이어 무력화.

archive/strategies_legacy/ 의 10개 전략은 참고용. import 금지.
Phase 3에서 4-레이어 구조 (Regime → Router → Strategy → PM)로 재구축 예정.

현재 살아있는 모듈:
- src.strategy.signals        인프라급 공용 지표 계산
- src.strategy.market_regime  시장 국면 판단기 (Phase 3 Regime 레이어의 기반)
- src.strategy.screener       스크리닝 (전략 호출부는 stub)
- src.strategy.base_strategy  추상 클래스 (레지스트리 비어있음)
"""


def get_strategy(name, params=None):  # noqa: ARG001
    raise NotImplementedError(
        "Strategy layer disabled (Phase 1 restructure). "
        "Phase 3에서 4-레이어 구조로 재구축 예정."
    )


def available_strategies() -> list[str]:
    return []
