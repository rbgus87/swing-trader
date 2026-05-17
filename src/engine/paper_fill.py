"""Paper 체결 시뮬레이터 — 슬리피지 + 호가단위 보정."""
from __future__ import annotations

from dataclasses import dataclass

from src.utils.slippage_model import SlippageParams, apply_slippage_to_price


@dataclass
class PaperFillParams:
    """Paper 체결 시뮬레이션 파라미터."""

    slippage_enabled: bool = True
    spread_bps: float = 5.0          # 스프레드 0.05% (호가 1틱 가정, 현재 미사용)
    volume_limit_pct: float = 0.10   # 일평균 거래량의 10% 초과 시 경고
    enabled: bool = True

    @classmethod
    def from_config(cls, config_dict: dict) -> "PaperFillParams":
        pf = config_dict.get("paper_fill", {})
        return cls(
            slippage_enabled=bool(pf.get("slippage_enabled", True)),
            spread_bps=float(pf.get("spread_bps", 5.0)),
            volume_limit_pct=float(pf.get("volume_limit_pct", 0.10)),
            enabled=bool(pf.get("enabled", True)),
        )


def simulate_fill_price(
    price: int,
    order_value: float,
    avg_trading_value: float,
    side: str = "buy",
    params: PaperFillParams | None = None,
    slippage_params: SlippageParams | None = None,
) -> int:
    """Paper 체결가 시뮬레이션.

    Args:
        price: 현재 시장가 (원)
        order_value: 주문 금액 (원)
        avg_trading_value: 20일 평균 거래대금 (원)
        side: "buy" → 슬리피지만큼 가격 상승, "sell" → 하락
        params: Paper 체결 파라미터
        slippage_params: B-6 동적 슬리피지 모델 파라미터

    Returns:
        슬리피지 + 호가단위 보정이 반영된 체결 예상가
    """
    if params is None:
        params = PaperFillParams()

    if not params.enabled or not params.slippage_enabled:
        return price

    # B-6 동적 슬리피지 적용
    filled = apply_slippage_to_price(
        price, order_value, avg_trading_value, side, slippage_params
    )

    # 호가단위 보정: 매수는 올림, 매도는 내림
    from src.utils.tick_size import adjust_price
    if side == "buy":
        filled = adjust_price(filled, "up")
    else:
        filled = adjust_price(filled, "down")

    return filled


def check_volume_feasibility(
    order_qty: int,
    avg_daily_volume: int,
    limit_pct: float = 0.10,
) -> tuple[bool, float]:
    """주문량이 일평균 거래량 대비 적정한지 검사.

    Args:
        order_qty: 주문 수량
        avg_daily_volume: 20일 평균 거래량 (주)
        limit_pct: 참여율 상한 (기본 10%)

    Returns:
        (feasible, participation_rate) — feasible=False이면 경고 로그 권장
    """
    if avg_daily_volume <= 0:
        return False, 1.0
    rate = order_qty / avg_daily_volume
    return rate <= limit_pct, rate
