"""동적 보유기간 계산 — 백테스트/실전 공용.

ADX 추세 강도에 연동하여 최대 보유일을 동적 조절한다.
순수 함수 설계 — DB/API 호출 없음.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class DynamicHoldParams:
    """동적 보유기간 파라미터."""

    base_hold_days: int = 20          # 기본 최대 보유일
    extend_multiplier: float = 1.5    # 강한 추세 시 연장 배율
    shorten_multiplier: float = 0.5   # 약한 추세 시 단축 배율
    adx_strong_threshold: float = 30  # 이 이상이면 강한 추세 (보유 연장)
    adx_weak_threshold: float = 15    # 이 이하면 약한 추세 (보유 단축)
    enabled: bool = True

    @classmethod
    def from_config(cls, config_dict: dict) -> "DynamicHoldParams":
        """trend_following 섹션 dict에서 dynamic_hold 로드."""
        dh = config_dict.get("dynamic_hold", {})
        base = int(
            dh.get("base_hold_days", config_dict.get("max_hold_days", 20))
        )
        return cls(
            base_hold_days=base,
            extend_multiplier=float(dh.get("extend_multiplier", 1.5)),
            shorten_multiplier=float(dh.get("shorten_multiplier", 0.5)),
            adx_strong_threshold=float(dh.get("adx_strong_threshold", 30)),
            adx_weak_threshold=float(dh.get("adx_weak_threshold", 15)),
            enabled=bool(dh.get("enabled", True)),
        )


def compute_dynamic_max_hold(
    params: DynamicHoldParams,
    current_adx: float,
    entry_adx: float = 0.0,
) -> int:
    """현재 ADX 기반 동적 최대 보유일 계산.

    Args:
        params:      동적 보유기간 파라미터
        current_adx: 현재 ADX 값
        entry_adx:   진입 시점 ADX (현재 미사용, 향후 확장용)

    Returns:
        동적 최대 보유일 (정수, 최소 3일)
    """
    if not params.enabled:
        return params.base_hold_days

    base = params.base_hold_days

    if current_adx >= params.adx_strong_threshold:
        return int(base * params.extend_multiplier)
    if current_adx <= params.adx_weak_threshold:
        return max(3, int(base * params.shorten_multiplier))
    return base
