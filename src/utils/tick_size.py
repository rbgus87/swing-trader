"""한국거래소 호가단위 보정 유틸.

2023.01~ 현행 기준 (KRX 고시).
"""

from __future__ import annotations


def get_tick_size(price: int) -> int:
    """가격대별 호가단위 반환."""
    if price < 1_000:
        return 1
    elif price < 5_000:
        return 5
    elif price < 10_000:
        return 10
    elif price < 50_000:
        return 50
    elif price < 100_000:
        return 100
    elif price < 500_000:
        return 500
    else:
        return 1_000


def adjust_price(price: int | float, direction: str = "down") -> int:
    """가격을 호가단위에 맞게 보정.

    Args:
        price: 원시 가격. float는 int 변환 후 처리.
        direction: "down" — 내림 (매수/손절, 보수적).
                   "up"   — 올림 (매도/목표가, 보수적).

    Returns:
        호가단위에 맞는 정수 가격. price <= 0이면 0.
    """
    p = int(price)
    if p <= 0:
        return 0
    tick = get_tick_size(p)
    remainder = p % tick
    if remainder == 0:
        return p
    if direction == "up":
        return p + (tick - remainder)
    return p - remainder  # down
