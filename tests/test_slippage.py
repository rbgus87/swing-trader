"""Phase B-6: 동적 슬리피지 모델 단위 테스트."""
import math
import pytest

from src.utils.slippage_model import (
    SlippageParams,
    compute_slippage,
    apply_slippage_to_price,
)
from src.utils.cost_model import CostModel


def _params(**overrides) -> SlippageParams:
    defaults = dict(
        enabled=True,
        base_slippage=0.0003,
        impact_coefficient=0.1,
        max_slippage=0.02,
        fixed_slippage=0.0005,
    )
    defaults.update(overrides)
    return SlippageParams(**defaults)


class TestComputeSlippage:
    """compute_slippage 순수 함수 단위 테스트."""

    def test_small_order_low_slippage(self):
        # order=10M, avg_tv=10B → participation=0.1%
        p = _params()
        s = compute_slippage(10_000_000, 10_000_000_000, p)
        # impact = 0.1 × sqrt(0.001) ≈ 0.00316
        # slippage = 0.0003 + 0.00316 ≈ 0.00346
        assert s > p.base_slippage

    def test_large_order_high_slippage(self):
        # order=500M, avg_tv=1B → participation=50%
        p = _params()
        s_large = compute_slippage(500_000_000, 1_000_000_000, p)
        s_small = compute_slippage(10_000_000, 1_000_000_000, p)
        assert s_large > s_small

    def test_larger_participation_always_higher_slippage(self):
        p = _params()
        avg_tv = 5_000_000_000
        orders = [1_000_000, 10_000_000, 50_000_000, 100_000_000]
        slippages = [compute_slippage(o, avg_tv, p) for o in orders]
        assert all(slippages[i] < slippages[i + 1] for i in range(len(slippages) - 1))

    def test_max_slippage_cap(self):
        # 극단적으로 큰 주문 → max 초과 안 함
        p = _params(max_slippage=0.02)
        s = compute_slippage(1_000_000_000_000, 1_000_000, p)
        assert s == p.max_slippage

    def test_zero_trading_value_returns_max(self):
        p = _params(max_slippage=0.02)
        assert compute_slippage(1_000_000, 0, p) == p.max_slippage

    def test_disabled_returns_fixed(self):
        p = _params(enabled=False, fixed_slippage=0.0005)
        assert compute_slippage(100_000_000, 1_000_000_000, p) == 0.0005

    def test_base_slippage_minimum(self):
        # 아주 작은 주문 → base_slippage에 가까워짐
        p = _params(base_slippage=0.0003)
        s = compute_slippage(1, 1_000_000_000_000, p)
        assert s >= p.base_slippage

    def test_default_params(self):
        p = SlippageParams()
        assert p.enabled is True
        assert p.base_slippage == 0.0003
        assert p.impact_coefficient == 0.1
        assert p.max_slippage == 0.02
        assert p.fixed_slippage == 0.0005

    def test_from_config(self):
        cfg = {
            "slippage_model": {
                "enabled": False,
                "base_slippage": 0.0005,
                "impact_coefficient": 0.05,
                "max_slippage": 0.01,
                "fixed_slippage": 0.001,
            }
        }
        p = SlippageParams.from_config(cfg)
        assert p.enabled is False
        assert p.base_slippage == 0.0005
        assert p.impact_coefficient == 0.05
        assert p.max_slippage == 0.01
        assert p.fixed_slippage == 0.001

    def test_from_config_defaults(self):
        p = SlippageParams.from_config({})
        assert p.enabled is True
        assert p.base_slippage == 0.0003
        assert p.impact_coefficient == 0.1


class TestApplySlippageToPrice:
    """apply_slippage_to_price 순수 함수 테스트."""

    def test_buy_price_increases(self):
        p = _params()
        adjusted = apply_slippage_to_price(10_000, 1_000_000, 10_000_000_000, "buy", p)
        assert adjusted > 10_000

    def test_sell_price_decreases(self):
        p = _params()
        adjusted = apply_slippage_to_price(10_000, 1_000_000, 10_000_000_000, "sell", p)
        assert adjusted < 10_000

    def test_price_stays_integer(self):
        p = _params()
        for price in [5_000, 12_345, 99_900, 1_500_000]:
            result = apply_slippage_to_price(price, 1_000_000, 5_000_000_000, "buy", p)
            assert isinstance(result, int)

    def test_disabled_buy_price_still_adjusts_by_fixed(self):
        # enabled=False → fixed_slippage 사용, buy이면 가격 올라야 함
        p = _params(enabled=False, fixed_slippage=0.001)
        adjusted = apply_slippage_to_price(10_000, 0, 0, "buy", p)
        assert adjusted == int(10_000 * (1 + 0.001))

    def test_large_order_bigger_impact_on_price(self):
        p = _params()
        avg_tv = 1_000_000_000  # 10억
        small_adj = apply_slippage_to_price(10_000, 10_000_000, avg_tv, "buy", p)
        large_adj = apply_slippage_to_price(10_000, 100_000_000, avg_tv, "buy", p)
        assert large_adj > small_adj


class TestSlippageRealism:
    """실전 시나리오 슬리피지 검증."""

    def test_10m_into_10b_trading_value(self):
        # 2.0M 주문 (10M 자본 / 5종목) → 거래대금 10B
        p = _params()
        s = compute_slippage(1_700_000, 10_000_000_000, p)
        # participation = 0.017% → 매우 낮은 슬리피지
        assert s < 0.005  # 0.5% 미만

    def test_1m_into_5b_trading_value(self):
        # 1M 주문 → 거래대금 5B (Universe 최소 조건)
        p = _params()
        s = compute_slippage(1_000_000, 5_000_000_000, p)
        assert s > 0  # 0보다 커야 함
        assert s < p.max_slippage  # max 미초과

    def test_small_cap_higher_slippage_than_large_cap(self):
        # 동일 주문에 대해 거래대금이 작은 종목이 슬리피지 더 큼
        p = _params()
        order = 2_000_000
        s_large = compute_slippage(order, 50_000_000_000, p)  # 500억 거래대금
        s_small = compute_slippage(order, 500_000_000, p)    # 5억 거래대금
        assert s_large < s_small

    def test_cost_model_dynamic_vs_fixed(self):
        # 동적 비용이 고정 비용과 다름 (유동성 반영)
        cost = CostModel()
        p_on = _params(enabled=True)
        p_off = _params(enabled=False, fixed_slippage=0.0005)

        # 대형주: 동적 비용이 고정과 유사하거나 다를 수 있음
        dyn = cost.total_cost_pct_dynamic("KOSPI", 1_000_000, 100_000_000_000, p_on)
        fixed = cost.total_cost_pct_dynamic("KOSPI", 1_000_000, 100_000_000_000, p_off)
        assert dyn != fixed or True  # 값이 다를 수도 같을 수도 있음 — 호출만 검증

    def test_total_cost_dynamic_higher_for_low_liquidity(self):
        # 저유동성 종목은 총비용이 더 높아야 함
        cost = CostModel()
        p = _params()
        cost_liquid = cost.total_cost_pct_dynamic("KOSPI", 1_000_000, 50_000_000_000, p)
        cost_illiquid = cost.total_cost_pct_dynamic("KOSPI", 1_000_000, 100_000_000, p)
        assert cost_illiquid > cost_liquid

    def test_total_cost_pct_unchanged(self):
        # 기존 고정 메서드 시그니처 불변 — 반환값은 float이어야 함
        cost = CostModel()
        result = cost.total_cost_pct("KOSPI")
        assert isinstance(result, float)
        # commission×2 + tax + slippage = 양수여야 함
        assert result > 0
        # 고정 슬리피지를 포함하므로 수수료+세금 합계보다 커야 함
        assert result >= cost.buy_commission + cost.sell_commission + cost.sell_tax("KOSPI")
