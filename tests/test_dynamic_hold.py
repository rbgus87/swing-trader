"""Phase B-4: ADX 기반 동적 보유기간 테스트."""
import pytest

from src.strategy.dynamic_hold import DynamicHoldParams, compute_dynamic_max_hold
from src.strategy.exit_evaluator import ExitContext, ExitParams, evaluate_exit
from src.models import ExitReason


class TestComputeDynamicMaxHold:
    """compute_dynamic_max_hold 순수 함수 단위 테스트."""

    def _default_params(self, **overrides) -> DynamicHoldParams:
        defaults = dict(
            base_hold_days=20,
            extend_multiplier=1.5,
            shorten_multiplier=0.5,
            adx_strong_threshold=30,
            adx_weak_threshold=15,
            enabled=True,
        )
        defaults.update(overrides)
        return DynamicHoldParams(**defaults)

    def test_strong_adx_extends(self):
        params = self._default_params()
        result = compute_dynamic_max_hold(params, current_adx=35.0)
        assert result == 30  # 20 × 1.5

    def test_weak_adx_shortens(self):
        params = self._default_params()
        result = compute_dynamic_max_hold(params, current_adx=10.0)
        assert result == 10  # 20 × 0.5

    def test_medium_adx_base(self):
        params = self._default_params()
        result = compute_dynamic_max_hold(params, current_adx=22.0)
        assert result == 20  # base_hold_days

    def test_boundary_strong_threshold(self):
        params = self._default_params()
        # 정확히 strong_threshold면 연장
        result = compute_dynamic_max_hold(params, current_adx=30.0)
        assert result == 30

    def test_boundary_weak_threshold(self):
        params = self._default_params()
        # 정확히 weak_threshold면 단축
        result = compute_dynamic_max_hold(params, current_adx=15.0)
        assert result == 10

    def test_disabled_returns_base(self):
        params = self._default_params(enabled=False)
        assert compute_dynamic_max_hold(params, current_adx=35.0) == 20
        assert compute_dynamic_max_hold(params, current_adx=5.0) == 20

    def test_minimum_3_days(self):
        # 매우 짧은 base_hold_days × shorten_multiplier < 3 이어도 최소 3일 보장
        params = DynamicHoldParams(
            base_hold_days=4,
            extend_multiplier=1.5,
            shorten_multiplier=0.5,
            adx_strong_threshold=30,
            adx_weak_threshold=15,
            enabled=True,
        )
        result = compute_dynamic_max_hold(params, current_adx=5.0)
        assert result >= 3  # 4 × 0.5 = 2 → clamp to 3

    def test_from_config_with_dynamic_hold_section(self):
        cfg = {
            "max_hold_days": 20,
            "dynamic_hold": {
                "enabled": True,
                "base_hold_days": 25,
                "extend_multiplier": 2.0,
                "shorten_multiplier": 0.4,
                "adx_strong_threshold": 35,
                "adx_weak_threshold": 12,
            },
        }
        params = DynamicHoldParams.from_config(cfg)
        assert params.enabled is True
        assert params.base_hold_days == 25
        assert params.extend_multiplier == 2.0
        assert params.shorten_multiplier == 0.4
        assert params.adx_strong_threshold == 35
        assert params.adx_weak_threshold == 12

    def test_from_config_defaults_to_max_hold_days(self):
        cfg = {
            "max_hold_days": 15,
            "dynamic_hold": {"enabled": True},
        }
        params = DynamicHoldParams.from_config(cfg)
        assert params.base_hold_days == 15

    def test_default_params(self):
        params = DynamicHoldParams()
        assert params.base_hold_days == 20
        assert params.extend_multiplier == 1.5
        assert params.shorten_multiplier == 0.5
        assert params.adx_strong_threshold == 30
        assert params.adx_weak_threshold == 15
        assert params.enabled is True


class TestExitEvaluatorWithDynamicHold:
    """evaluate_exit의 동적 보유기간 분기 통합 테스트."""

    def _base_ctx(self, hold_days: int, current_adx: float = 22.0) -> ExitContext:
        return ExitContext(
            entry_price=10000,
            day_low=10100,
            day_high=10200,
            stop_price=9000,
            initial_stop_price=9000,
            target_price=0,
            tp2_price=0,
            high_since_entry=10200,
            atr_at_entry=0,
            partial_sold=False,
            partial_sold_2=False,
            hold_days=hold_days,
            current_return=0.01,
            current_adx=current_adx,
            entry_adx=22.0,
        )

    def _base_params(self, dynamic_hold=None) -> ExitParams:
        return ExitParams(
            max_hold_days=20,
            trailing_atr_mult=0,
            trend_exit_enabled=False,
            dynamic_hold=dynamic_hold,
        )

    def test_max_hold_extended_by_strong_adx(self):
        dh = DynamicHoldParams(base_hold_days=20, extend_multiplier=1.5,
                               adx_strong_threshold=30, adx_weak_threshold=15, enabled=True)
        params = self._base_params(dynamic_hold=dh)

        # hold_days=20 이면 static max_hold_days=20으로는 청산 → dynamic 30으로 연장되면 유지
        ctx = self._base_ctx(hold_days=20, current_adx=35.0)
        result = evaluate_exit(ctx, params)
        assert result is None  # 아직 청산 안 함

    def test_max_hold_triggered_after_extension(self):
        dh = DynamicHoldParams(base_hold_days=20, extend_multiplier=1.5,
                               adx_strong_threshold=30, adx_weak_threshold=15, enabled=True)
        params = self._base_params(dynamic_hold=dh)

        # hold_days=30 이면 extended max(30) 초과 → MAX_HOLD 청산
        ctx = self._base_ctx(hold_days=30, current_adx=35.0)
        result = evaluate_exit(ctx, params)
        assert result == ExitReason.MAX_HOLD

    def test_max_hold_shortened_by_weak_adx(self):
        dh = DynamicHoldParams(base_hold_days=20, extend_multiplier=1.5,
                               adx_strong_threshold=30, adx_weak_threshold=15, enabled=True)
        params = self._base_params(dynamic_hold=dh)

        # hold_days=10 이면 shortened max(10) 초과 → MAX_HOLD 청산
        ctx = self._base_ctx(hold_days=10, current_adx=8.0)
        result = evaluate_exit(ctx, params)
        assert result == ExitReason.MAX_HOLD

    def test_no_dynamic_hold_uses_fixed(self):
        params = self._base_params(dynamic_hold=None)

        # hold_days=20, max_hold_days=20 → MAX_HOLD
        ctx = self._base_ctx(hold_days=20, current_adx=35.0)
        result = evaluate_exit(ctx, params)
        assert result == ExitReason.MAX_HOLD

    def test_dynamic_hold_disabled_uses_fixed(self):
        dh = DynamicHoldParams(base_hold_days=20, enabled=False)
        params = self._base_params(dynamic_hold=dh)

        # disabled → fixed max=20, hold_days=20 → MAX_HOLD
        ctx = self._base_ctx(hold_days=20, current_adx=35.0)
        result = evaluate_exit(ctx, params)
        assert result == ExitReason.MAX_HOLD
