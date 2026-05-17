"""공유 청산 판단 모듈 테스트."""

import pytest

from src.models import ExitReason
from src.strategy.exit_evaluator import ExitContext, ExitParams, evaluate_exit


def _ctx(**overrides) -> ExitContext:
    """기본 ExitContext 생성 헬퍼. 키워드로 원하는 필드만 덮어씀."""
    defaults = dict(
        entry_price=10_000,
        day_low=10_000,
        day_high=10_000,
        stop_price=9_000,       # 초기 SL (entry - ATR×2: ATR=500)
        initial_stop_price=9_000,
        target_price=11_000,    # TP1 (entry + ATR×2)
        tp2_price=12_000,       # TP2 (entry + ATR×4)
        high_since_entry=10_000,
        atr_at_entry=500.0,
        partial_sold=False,
        partial_sold_2=False,
        hold_days=5,
        current_return=0.0,
        prev_ma5=None,
        prev_ma20=None,
        curr_ma5=None,
        curr_ma20=None,
    )
    defaults.update(overrides)
    return ExitContext(**defaults)


def _params(**overrides) -> ExitParams:
    """기본 ExitParams 생성 헬퍼."""
    defaults = dict(
        max_hold_days=20,
        trailing_atr_mult=4.0,
        early_exit_enabled=False,
        early_exit_hold_days=10,
        early_exit_return_min=-0.02,
        trend_exit_enabled=True,
    )
    defaults.update(overrides)
    return ExitParams(**defaults)


class TestStopLoss:
    def test_sl_triggered(self):
        ctx = _ctx(day_low=8_900)  # 9000 SL 돌파
        assert evaluate_exit(ctx, _params()) == ExitReason.STOP_LOSS

    def test_sl_exact(self):
        ctx = _ctx(day_low=9_000)  # 정확히 SL 가격
        assert evaluate_exit(ctx, _params()) == ExitReason.STOP_LOSS

    def test_sl_not_triggered(self):
        ctx = _ctx(day_low=9_100)
        assert evaluate_exit(ctx, _params()) is None

    def test_trailing_vs_sl(self):
        """트레일링이 초기 SL보다 높은 상태에서 hit → TRAILING_STOP."""
        # high=12000, atr=500, mult=4 → trailing=12000-2000=10000 → adjust_up=10000
        ctx = _ctx(
            day_low=9_900,          # 초기 SL(9000) 위지만 trailing(10000) 아래
            high_since_entry=12_000,
            stop_price=9_000,       # 초기 SL (백테스트에서 불변)
            initial_stop_price=9_000,
            atr_at_entry=500.0,
        )
        # trailing_candidate = adjust_price(12000 - 500*4, "up") = adjust_price(10000, "up") = 10000
        # effective_stop = max(9000, 10000) = 10000
        # 9900 <= 10000 → TRAILING_STOP (10000 > 9000)
        assert evaluate_exit(ctx, _params()) == ExitReason.TRAILING_STOP


class TestPartialTarget:
    def test_tp1_triggered(self):
        ctx = _ctx(day_high=11_000, partial_sold=False)
        assert evaluate_exit(ctx, _params()) == ExitReason.PARTIAL_TARGET

    def test_tp1_not_triggered_if_already_sold(self):
        ctx = _ctx(day_high=11_000, partial_sold=True)
        # TP2 비활성 상태 (tp2_price=0) → None
        assert evaluate_exit(ctx, _params()) is None

    def test_tp1_price_zero_skips(self):
        ctx = _ctx(day_high=15_000, target_price=0)
        assert evaluate_exit(ctx, _params()) is None

    def test_tp2_triggered(self):
        ctx = _ctx(day_high=12_000, partial_sold=True, partial_sold_2=False)
        assert evaluate_exit(ctx, _params()) == ExitReason.PARTIAL_TARGET_2

    def test_tp2_not_triggered_if_tp1_not_done(self):
        ctx = _ctx(day_high=12_000, partial_sold=False, partial_sold_2=False)
        # partial_sold=False → TP1 먼저 체크
        assert evaluate_exit(ctx, _params()) == ExitReason.PARTIAL_TARGET

    def test_tp2_not_triggered_if_already_sold(self):
        ctx = _ctx(day_high=12_000, partial_sold=True, partial_sold_2=True)
        assert evaluate_exit(ctx, _params()) is None


class TestTrendExit:
    def test_ma_crossover_triggers(self):
        """MA5가 MA20을 하향 돌파하면 TREND_EXIT."""
        ctx = _ctx(
            hold_days=5,
            prev_ma5=10_100.0, prev_ma20=10_000.0,   # 전일 MA5 > MA20
            curr_ma5=9_900.0,  curr_ma20=10_000.0,   # 당일 MA5 < MA20
        )
        assert evaluate_exit(ctx, _params()) == ExitReason.TREND_EXIT

    def test_no_crossover_no_exit(self):
        ctx = _ctx(
            hold_days=5,
            prev_ma5=10_100.0, prev_ma20=10_000.0,
            curr_ma5=10_050.0, curr_ma20=10_000.0,   # 당일도 MA5 > MA20
        )
        assert evaluate_exit(ctx, _params()) is None

    def test_ma_none_skips_trend_check(self):
        """MA 값이 None이면 추세이탈 체크 비활성 (실전 틱 모드)."""
        ctx = _ctx(hold_days=15, prev_ma5=None)
        assert evaluate_exit(ctx, _params()) is None

    def test_hold_days_1_skips_trend_check(self):
        """hold_days ≤ 1이면 크로스오버 체크 생략 (진입 당일 노이즈 방지)."""
        ctx = _ctx(
            hold_days=1,
            prev_ma5=10_100.0, prev_ma20=10_000.0,
            curr_ma5=9_900.0,  curr_ma20=10_000.0,
        )
        assert evaluate_exit(ctx, _params()) is None


class TestEarlyAndMaxHold:
    def test_early_exit_triggered(self):
        ctx = _ctx(hold_days=10, current_return=-0.03)  # -3% < -2% threshold
        p = _params(early_exit_enabled=True, early_exit_hold_days=10, early_exit_return_min=-0.02)
        assert evaluate_exit(ctx, p) == ExitReason.EARLY_TIME_EXIT

    def test_early_exit_disabled(self):
        ctx = _ctx(hold_days=10, current_return=-0.05)
        p = _params(early_exit_enabled=False)
        assert evaluate_exit(ctx, p) is None

    def test_early_exit_not_enough_days(self):
        ctx = _ctx(hold_days=5, current_return=-0.05)
        p = _params(early_exit_enabled=True, early_exit_hold_days=10)
        assert evaluate_exit(ctx, p) is None

    def test_max_hold(self):
        ctx = _ctx(hold_days=20)
        assert evaluate_exit(ctx, _params(max_hold_days=20)) == ExitReason.MAX_HOLD

    def test_max_hold_not_yet(self):
        ctx = _ctx(hold_days=19)
        assert evaluate_exit(ctx, _params(max_hold_days=20)) is None
