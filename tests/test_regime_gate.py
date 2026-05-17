"""이중 국면 게이트 (breadth + KOSPI MA200) 단위 테스트."""

import pytest

from src.backtest.portfolio_backtester import _compute_ma200_above_series

THRESHOLD = 0.40


def _gate(breadth: float, regime_ok: bool, regime_gate_enabled: bool) -> bool:
    """run_portfolio_backtest 내부 게이트 판정 로직 재현."""
    breadth_ok = breadth >= THRESHOLD
    effective_regime = regime_ok if regime_gate_enabled else True
    return breadth_ok and effective_regime


class TestDualGate:
    def test_breadth_ok_regime_ok(self):
        """breadth OK + MA200 OK → OPEN."""
        assert _gate(0.50, True, True) is True

    def test_breadth_ok_regime_fail(self):
        """breadth OK + MA200 FAIL → CLOSED."""
        assert _gate(0.50, False, True) is False

    def test_breadth_fail_regime_ok(self):
        """breadth FAIL + MA200 OK → CLOSED."""
        assert _gate(0.30, True, True) is False

    def test_both_fail(self):
        """breadth FAIL + MA200 FAIL → CLOSED."""
        assert _gate(0.30, False, True) is False

    def test_regime_disabled(self):
        """regime_gate_enabled=False → breadth만 적용 (MA200 무시)."""
        assert _gate(0.50, False, False) is True
        assert _gate(0.30, True, False) is False

    def test_index_ma200_map_short_series(self):
        """200일 미만 구간은 데이터 부족 → True 반환."""
        dates = [f"2020-01-{i + 1:02d}" for i in range(10)]
        closes = [100.0 + i for i in range(10)]
        result = _compute_ma200_above_series(dates, closes)
        assert all(result[d] is True for d in dates)

    def test_index_ma200_map_rising_above(self):
        """단조 상승 시계열 — 200일 후 종가 > MA200."""
        n = 210
        dates = [f"2020-{i:04d}" for i in range(n)]
        closes = [float(i + 1) for i in range(n)]  # 1, 2, ..., 210
        result = _compute_ma200_above_series(dates, closes)
        # 200일 미만 → True
        for d in dates[:199]:
            assert result[d] is True
        # 200일 이상 — 종가(200+)가 MA200보다 큼 (상승 추세이므로 항상 위)
        for d in dates[199:]:
            assert result[d] is True

    def test_index_ma200_map_falling_below(self):
        """단조 하락 시계열 — 200일 후 종가 < MA200."""
        n = 210
        dates = [f"2020-{i:04d}" for i in range(n)]
        closes = [float(n - i) for i in range(n)]  # 210, 209, ..., 1
        result = _compute_ma200_above_series(dates, closes)
        # 200일 이상 — 종가(≤11)가 MA200(≈110.5)보다 낮음
        for d in dates[199:]:
            assert result[d] is False

    def test_index_ma200_map_exact_boundary(self):
        """종가 == MA200 → False (초과 조건 불충족)."""
        n = 200
        dates = [f"2020-{i:04d}" for i in range(n)]
        closes = [100.0] * n  # 모두 동일 → MA200 == 100 == close
        result = _compute_ma200_above_series(dates, closes)
        # close == ma200 → False
        assert result[dates[199]] is False
