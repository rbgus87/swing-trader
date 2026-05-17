"""tests/test_paper_fill.py — Paper 체결 시뮬레이터 단위 테스트."""
from __future__ import annotations

import pytest

from src.engine.paper_fill import (
    PaperFillParams,
    check_volume_feasibility,
    simulate_fill_price,
)
from src.utils.slippage_model import SlippageParams


# ---------------------------------------------------------------------------
# 헬퍼
# ---------------------------------------------------------------------------

def _default_slip() -> SlippageParams:
    return SlippageParams(
        enabled=True,
        base_slippage=0.0003,
        impact_coefficient=0.1,
        max_slippage=0.02,
        fixed_slippage=0.0005,
    )


def _default_params() -> PaperFillParams:
    return PaperFillParams(slippage_enabled=True, enabled=True)


# ---------------------------------------------------------------------------
# PaperFillParams.from_config
# ---------------------------------------------------------------------------

class TestPaperFillParams:
    def test_defaults(self):
        p = PaperFillParams()
        assert p.enabled is True
        assert p.slippage_enabled is True
        assert p.volume_limit_pct == 0.10

    def test_from_config_empty(self):
        p = PaperFillParams.from_config({})
        assert p.enabled is True

    def test_from_config_disabled(self):
        p = PaperFillParams.from_config({"paper_fill": {"enabled": False}})
        assert p.enabled is False

    def test_from_config_custom(self):
        p = PaperFillParams.from_config({
            "paper_fill": {
                "slippage_enabled": False,
                "spread_bps": 10.0,
                "volume_limit_pct": 0.05,
            }
        })
        assert p.slippage_enabled is False
        assert p.spread_bps == 10.0
        assert p.volume_limit_pct == 0.05


# ---------------------------------------------------------------------------
# simulate_fill_price
# ---------------------------------------------------------------------------

class TestSimulateFillPrice:
    def test_buy_price_higher_than_market(self):
        """매수 체결가는 시장가보다 높아야 한다 (슬리피지 반영)."""
        fill = simulate_fill_price(
            price=50_000,
            order_value=5_000_000,
            avg_trading_value=10_000_000_000,
            side="buy",
            params=_default_params(),
            slippage_params=_default_slip(),
        )
        assert fill >= 50_000

    def test_sell_price_lower_than_or_equal_market(self):
        """매도 체결가는 시장가보다 낮아야 한다 (슬리피지 반영)."""
        fill = simulate_fill_price(
            price=50_000,
            order_value=5_000_000,
            avg_trading_value=10_000_000_000,
            side="sell",
            params=_default_params(),
            slippage_params=_default_slip(),
        )
        assert fill <= 50_000

    def test_disabled_returns_original(self):
        """enabled=False이면 슬리피지 없이 원래 가격 반환."""
        params = PaperFillParams(enabled=False)
        fill = simulate_fill_price(
            price=50_000,
            order_value=5_000_000,
            avg_trading_value=10_000_000_000,
            side="buy",
            params=params,
        )
        assert fill == 50_000

    def test_slippage_disabled_returns_original(self):
        """slippage_enabled=False이면 원래 가격 반환."""
        params = PaperFillParams(enabled=True, slippage_enabled=False)
        fill = simulate_fill_price(
            price=50_000,
            order_value=5_000_000,
            avg_trading_value=10_000_000_000,
            side="buy",
            params=params,
        )
        assert fill == 50_000

    def test_tick_size_adjusted_buy(self):
        """매수 체결가는 호가단위로 올림 처리된다."""
        # 5만원대 호가단위: 50원
        fill = simulate_fill_price(
            price=50_000,
            order_value=5_000_000,
            avg_trading_value=10_000_000_000,
            side="buy",
            params=_default_params(),
            slippage_params=_default_slip(),
        )
        # 50원 단위로 나누어 떨어져야 함
        assert fill % 50 == 0

    def test_tick_size_adjusted_sell(self):
        """매도 체결가는 호가단위로 내림 처리된다."""
        fill = simulate_fill_price(
            price=50_000,
            order_value=5_000_000,
            avg_trading_value=10_000_000_000,
            side="sell",
            params=_default_params(),
            slippage_params=_default_slip(),
        )
        assert fill % 50 == 0

    def test_high_avg_volume_low_slippage(self):
        """거래대금이 크면 슬리피지가 작아야 한다."""
        fill_high_vol = simulate_fill_price(
            price=50_000,
            order_value=5_000_000,
            avg_trading_value=1_000_000_000_000,  # 1조
            side="buy",
            params=_default_params(),
            slippage_params=_default_slip(),
        )
        fill_low_vol = simulate_fill_price(
            price=50_000,
            order_value=5_000_000,
            avg_trading_value=50_000_000,  # 5000만
            side="buy",
            params=_default_params(),
            slippage_params=_default_slip(),
        )
        # 거래대금 큰 경우가 슬리피지가 작아야 함 (체결가가 낮음)
        assert fill_high_vol <= fill_low_vol

    def test_low_avg_volume_high_slippage(self):
        """거래대금이 작으면 슬리피지가 커야 한다."""
        fill = simulate_fill_price(
            price=50_000,
            order_value=5_000_000,
            avg_trading_value=1_000_000,  # 100만원 (극소)
            side="buy",
            params=_default_params(),
            slippage_params=_default_slip(),
        )
        # 원가보다 높아야 함
        assert fill > 50_000

    def test_no_params_uses_defaults(self):
        """params=None이면 PaperFillParams 기본값 사용."""
        fill = simulate_fill_price(
            price=10_000,
            order_value=1_000_000,
            avg_trading_value=1_000_000_000,
            side="buy",
        )
        # 기본값은 enabled=True이므로 슬리피지가 반영됨
        assert isinstance(fill, int)

    def test_zero_price_returns_zero(self):
        """가격 0은 0 반환 (adjust_price 결과)."""
        fill = simulate_fill_price(
            price=0,
            order_value=0,
            avg_trading_value=1_000_000_000,
            side="buy",
            params=_default_params(),
            slippage_params=_default_slip(),
        )
        assert fill == 0


# ---------------------------------------------------------------------------
# check_volume_feasibility
# ---------------------------------------------------------------------------

class TestVolumeFeasibility:
    def test_within_limit(self):
        feasible, rate = check_volume_feasibility(
            order_qty=100,
            avg_daily_volume=5_000,
            limit_pct=0.10,
        )
        assert feasible is True
        assert rate == pytest.approx(0.02)

    def test_exceeds_limit(self):
        feasible, rate = check_volume_feasibility(
            order_qty=600,
            avg_daily_volume=5_000,
            limit_pct=0.10,
        )
        assert feasible is False
        assert rate == pytest.approx(0.12)

    def test_zero_volume(self):
        """일평균 거래량 0이면 항상 실현 불가."""
        feasible, rate = check_volume_feasibility(
            order_qty=10,
            avg_daily_volume=0,
            limit_pct=0.10,
        )
        assert feasible is False
        assert rate == 1.0

    def test_exactly_at_limit(self):
        """정확히 한도와 같으면 허용."""
        feasible, rate = check_volume_feasibility(
            order_qty=100,
            avg_daily_volume=1_000,
            limit_pct=0.10,
        )
        assert feasible is True
        assert rate == pytest.approx(0.10)
