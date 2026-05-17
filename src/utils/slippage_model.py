"""동적 슬리피지 모델 — 주문량/거래량 비율 기반.

제곱근 시장 충격 모델 (Zipline/QuantConnect 표준):
  slippage = base_slippage + impact_coefficient × sqrt(order_value / avg_trading_value)
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class SlippageParams:
    """슬리피지 파라미터."""

    enabled: bool = True
    base_slippage: float = 0.0003      # 기본 스프레드 0.03%
    impact_coefficient: float = 0.1    # 시장 충격 계수
    max_slippage: float = 0.02         # 최대 2% 캡
    fixed_slippage: float = 0.0005     # 비활성 시 고정 0.05% (기존 호환)

    @classmethod
    def from_config(cls, config_dict: dict) -> "SlippageParams":
        sm = config_dict.get("slippage_model", {})
        return cls(
            enabled=bool(sm.get("enabled", True)),
            base_slippage=float(sm.get("base_slippage", 0.0003)),
            impact_coefficient=float(sm.get("impact_coefficient", 0.1)),
            max_slippage=float(sm.get("max_slippage", 0.02)),
            fixed_slippage=float(sm.get("fixed_slippage", 0.0005)),
        )


def compute_slippage(
    order_value: float,
    avg_trading_value: float,
    params: SlippageParams | None = None,
) -> float:
    """동적 슬리피지 계산.

    Args:
        order_value: 주문 금액 (원)
        avg_trading_value: 20일 평균 거래대금 (원)
        params: 슬리피지 파라미터

    Returns:
        슬리피지 비율 (0.001 = 0.1%)
    """
    if params is None:
        params = SlippageParams()

    if not params.enabled:
        return params.fixed_slippage

    if avg_trading_value <= 0:
        return params.max_slippage

    participation = order_value / avg_trading_value
    impact = params.impact_coefficient * math.sqrt(participation)
    slippage = params.base_slippage + impact
    return min(slippage, params.max_slippage)


def apply_slippage_to_price(
    price: int,
    order_value: float,
    avg_trading_value: float,
    side: str = "buy",
    params: SlippageParams | None = None,
) -> int:
    """슬리피지를 가격에 직접 반영.

    Args:
        price: 기준 가격 (원)
        side: "buy" → 가격 올림 (매수 불리), "sell" → 가격 내림 (매도 불리)

    Returns:
        슬리피지 반영된 가격 (정수)
    """
    slip = compute_slippage(order_value, avg_trading_value, params)
    if side == "buy":
        return int(price * (1 + slip))
    return int(price * (1 - slip))
