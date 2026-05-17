"""분할 매수(Scaling-in) 로직 — 백테스트/실전 공용."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ScalingParams:
    """분할 매수 파라미터."""

    enabled: bool = False
    first_entry_ratio: float = 0.60
    second_entry_ratio: float = 0.40
    scale_in_atr_mult: float = 1.0
    max_tranches: int = 2
    adjust_stop_on_scale: bool = True

    @classmethod
    def from_config(cls, config_dict: dict) -> "ScalingParams":
        sc = config_dict.get("scaling", {})
        return cls(
            enabled=bool(sc.get("enabled", False)),
            first_entry_ratio=float(sc.get("first_entry_ratio", 0.60)),
            second_entry_ratio=float(sc.get("second_entry_ratio", 0.40)),
            scale_in_atr_mult=float(sc.get("scale_in_atr_mult", 1.0)),
            max_tranches=int(sc.get("max_tranches", 2)),
            adjust_stop_on_scale=bool(sc.get("adjust_stop_on_scale", True)),
        )


def compute_first_entry_qty(total_alloc: int, price: int, params: ScalingParams) -> int:
    """1차 진입 수량 계산. 비활성 시 전량."""
    if not params.enabled:
        return total_alloc // max(1, price)
    alloc = int(total_alloc * params.first_entry_ratio)
    return alloc // max(1, price)


def compute_scale_in_trigger(entry_price: int, atr: float, params: ScalingParams) -> int:
    """2차 진입 트리거 가격 (진입가 + ATR × 배수)."""
    return int(entry_price + atr * params.scale_in_atr_mult)


def compute_scale_in_qty(total_alloc: int, price: int, params: ScalingParams) -> int:
    """2차 진입 수량 계산."""
    alloc = int(total_alloc * params.second_entry_ratio)
    return alloc // max(1, price)


def compute_adjusted_stop(
    entry_price_1: int,
    qty_1: int,
    entry_price_2: int,
    qty_2: int,
    atr: float,
    stop_atr_mult: float,
) -> int:
    """2차 진입 후 평균단가 기준 SL 재계산."""
    total_qty = qty_1 + qty_2
    if total_qty <= 0:
        return 0
    avg_price = (entry_price_1 * qty_1 + entry_price_2 * qty_2) // total_qty
    return int(avg_price - atr * stop_atr_mult)
