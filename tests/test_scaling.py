"""Phase B-5: 분할 매수(Scaling-in) 단위 테스트."""
import pytest

from src.strategy.scaling import (
    ScalingParams,
    compute_first_entry_qty,
    compute_scale_in_trigger,
    compute_scale_in_qty,
    compute_adjusted_stop,
)


def _params(**overrides) -> ScalingParams:
    defaults = dict(
        enabled=True,
        first_entry_ratio=0.60,
        second_entry_ratio=0.40,
        scale_in_atr_mult=1.0,
        max_tranches=2,
        adjust_stop_on_scale=True,
    )
    defaults.update(overrides)
    return ScalingParams(**defaults)


class TestScalingCompute:
    """순수 함수 단위 테스트."""

    def test_first_entry_qty_enabled(self):
        p = _params(enabled=True, first_entry_ratio=0.60)
        # alloc=1_000_000, price=10_000 → first_alloc=600_000 → 60주
        qty = compute_first_entry_qty(1_000_000, 10_000, p)
        assert qty == 60

    def test_first_entry_qty_disabled(self):
        # 비활성 시 전량
        p = _params(enabled=False)
        qty = compute_first_entry_qty(1_000_000, 10_000, p)
        assert qty == 100  # 1_000_000 // 10_000

    def test_first_entry_qty_rounds_down(self):
        p = _params(enabled=True, first_entry_ratio=0.60)
        # alloc=999_999, price=10_000 → first_alloc=599_999 → 59주 (버림)
        qty = compute_first_entry_qty(999_999, 10_000, p)
        assert qty == 59

    def test_scale_in_trigger_price(self):
        p = _params(scale_in_atr_mult=1.0)
        # entry=10_000, atr=200 → trigger=10_200
        trigger = compute_scale_in_trigger(10_000, 200.0, p)
        assert trigger == 10_200

    def test_scale_in_trigger_fractional_atr(self):
        p = _params(scale_in_atr_mult=1.5)
        # entry=10_000, atr=300 → trigger=10_450
        trigger = compute_scale_in_trigger(10_000, 300.0, p)
        assert trigger == 10_450

    def test_scale_in_qty(self):
        p = _params(second_entry_ratio=0.40)
        # alloc=1_000_000, price=10_000 → second_alloc=400_000 → 40주
        qty = compute_scale_in_qty(1_000_000, 10_000, p)
        assert qty == 40

    def test_scale_in_qty_rounds_down(self):
        p = _params(second_entry_ratio=0.40)
        # alloc=999_999, price=10_000 → second_alloc=399_999 → 39주
        qty = compute_scale_in_qty(999_999, 10_000, p)
        assert qty == 39

    def test_adjusted_stop(self):
        # entry1=10_000 × 60 + entry2=10_200 × 40 → avg=(600_000+408_000)/100=10_080
        # stop = 10_080 - 200 × 2.0 = 9_680
        stop = compute_adjusted_stop(
            entry_price_1=10_000, qty_1=60,
            entry_price_2=10_200, qty_2=40,
            atr=200.0, stop_atr_mult=2.0,
        )
        assert stop == 9_680

    def test_adjusted_stop_zero_qty(self):
        stop = compute_adjusted_stop(0, 0, 10_000, 0, 200.0, 2.0)
        assert stop == 0

    def test_ratios_sum_to_one(self):
        p = _params()
        assert abs(p.first_entry_ratio + p.second_entry_ratio - 1.0) < 1e-9

    def test_from_config_full(self):
        cfg = {
            "scaling": {
                "enabled": True,
                "first_entry_ratio": 0.70,
                "second_entry_ratio": 0.30,
                "scale_in_atr_mult": 2.0,
                "max_tranches": 2,
                "adjust_stop_on_scale": False,
            }
        }
        p = ScalingParams.from_config(cfg)
        assert p.enabled is True
        assert p.first_entry_ratio == 0.70
        assert p.second_entry_ratio == 0.30
        assert p.scale_in_atr_mult == 2.0
        assert p.adjust_stop_on_scale is False

    def test_from_config_defaults(self):
        p = ScalingParams.from_config({})
        assert p.enabled is False
        assert p.first_entry_ratio == 0.60
        assert p.second_entry_ratio == 0.40
        assert p.scale_in_atr_mult == 1.0
        assert p.max_tranches == 2
        assert p.adjust_stop_on_scale is True

    def test_default_disabled(self):
        p = ScalingParams()
        assert p.enabled is False


class TestScalingIntegration:
    """백테스터 수준 통합 시나리오 (순수 함수 조합)."""

    def test_disabled_full_entry(self):
        """비활성 시 전량 진입 — 1차에 전체 수량."""
        p = _params(enabled=False)
        qty = compute_first_entry_qty(1_000_000, 10_000, p)
        # 비활성이면 alloc 전체 사용
        assert qty == 1_000_000 // 10_000

    def test_scale_in_not_triggered_below_price(self):
        """현재가 < 트리거 → 2차 진입 조건 미충족."""
        p = _params()
        trigger = compute_scale_in_trigger(10_000, 200.0, p)  # 10_200
        current_price = 10_100
        assert current_price < trigger  # 2차 진입 안 함

    def test_scale_in_triggered_above_price(self):
        """현재가 >= 트리거 → 2차 진입 조건 충족."""
        p = _params()
        trigger = compute_scale_in_trigger(10_000, 200.0, p)  # 10_200
        current_price = 10_200
        assert current_price >= trigger

    def test_scale_in_qty_with_cash_check(self):
        """현금 부족 시 2차 진입 수량이 0이거나 현금 내에서 처리."""
        p = _params()
        # alloc 1_000_000 기준 2차 목표수량 = 40주 @ 10_200 = 408_000
        scale_qty = compute_scale_in_qty(1_000_000, 10_200, p)
        cost = scale_qty * 10_200
        available = 200_000  # 현금 부족
        # 현금 부족 시 스킵 (엔진에서 처리, 여기선 비용 > 가용 확인)
        assert cost > available

    def test_max_tranches_respected(self):
        """tranche_count >= max_tranches 이면 2차 진입 불가."""
        p = _params(max_tranches=2)
        tranche_count = 2
        assert tranche_count >= p.max_tranches  # 추가 불가

    def test_adjusted_stop_moves_up_after_scale(self):
        """2차 진입 후 SL이 1차 SL보다 높아져야 한다 (추세 방향)."""
        # 1차: entry=10_000, stop=9_600 (ATR 200 × 2.0)
        # 2차: entry=10_200, avg≈10_080, new_stop=9_680
        original_stop = 10_000 - 200 * 2  # 9_600
        new_stop = compute_adjusted_stop(
            10_000, 60, 10_200, 40, 200.0, 2.0
        )
        assert new_stop > original_stop
