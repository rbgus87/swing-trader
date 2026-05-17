"""Walk-Forward 검증 유닛 테스트."""
from __future__ import annotations

import pytest

from src.backtest.walk_forward import (
    WFResult,
    WFSummary,
    WFWindow,
    build_summary,
    generate_windows,
    judge_verdict,
)


# ─────────────────────────────────────────────────────────────────────────────
# generate_windows
# ─────────────────────────────────────────────────────────────────────────────

class TestGenerateWindows:
    def test_basic_windows(self):
        windows = generate_windows("2014-01-01", "2020-12-31", train_years=2, test_years=1)
        assert len(windows) > 0
        # 첫 윈도우 확인
        w = windows[0]
        assert w.train_start == "2014-01-01"
        assert w.test_start > w.train_end

    def test_no_overlap_between_train_and_test(self):
        windows = generate_windows("2014-01-01", "2026-05-15", train_years=2, test_years=1)
        for w in windows:
            assert w.test_start > w.train_end

    def test_test_periods_non_overlapping(self):
        """step_months=12 → 각 test 구간이 순차적으로 이동해야 함."""
        windows = generate_windows("2014-01-01", "2026-05-15",
                                   train_years=2, test_years=1, step_months=12)
        for i in range(1, len(windows)):
            assert windows[i].train_start > windows[i - 1].train_start

    def test_step_months_affects_count(self):
        w12 = generate_windows("2014-01-01", "2026-05-15", train_years=2, test_years=1, step_months=12)
        w6 = generate_windows("2014-01-01", "2026-05-15", train_years=2, test_years=1, step_months=6)
        assert len(w6) > len(w12)

    def test_short_data_no_windows(self):
        """훈련 + 테스트 기간보다 데이터가 짧으면 빈 리스트 반환."""
        windows = generate_windows("2014-01-01", "2015-06-30", train_years=2, test_years=1)
        assert windows == []

    def test_exact_boundary(self):
        """train+test 합계 = 데이터 전체 길이와 정확히 일치하는 경우 1개 윈도우."""
        windows = generate_windows("2014-01-01", "2017-01-05", train_years=2, test_years=1)
        assert len(windows) == 1

    def test_window_format(self):
        windows = generate_windows("2020-01-01", "2024-12-31", train_years=2, test_years=1)
        for w in windows:
            # 날짜 형식 검증 (파싱 가능 여부)
            from datetime import datetime
            datetime.strptime(w.train_start, "%Y-%m-%d")
            datetime.strptime(w.train_end, "%Y-%m-%d")
            datetime.strptime(w.test_start, "%Y-%m-%d")
            datetime.strptime(w.test_end, "%Y-%m-%d")


# ─────────────────────────────────────────────────────────────────────────────
# WFResult
# ─────────────────────────────────────────────────────────────────────────────

class TestWFResult:
    def _make_window(self) -> WFWindow:
        return WFWindow("2020-01-01", "2021-12-31", "2022-01-01", "2022-12-31")

    def test_pf_retention_normal(self):
        r = WFResult(window=self._make_window(), train_pf=2.0, test_pf=1.5)
        assert r.pf_retention == pytest.approx(0.75)

    def test_pf_retention_zero_train(self):
        r = WFResult(window=self._make_window(), train_pf=0.0, test_pf=1.2)
        assert r.pf_retention == 0.0

    def test_pf_retention_negative_train(self):
        r = WFResult(window=self._make_window(), train_pf=-1.0, test_pf=1.0)
        assert r.pf_retention == 0.0

    def test_is_robust_true(self):
        r = WFResult(window=self._make_window(), train_pf=2.0, test_pf=1.2)
        # retention = 0.6 > 0.5, test_pf > 1.0 → True
        assert r.is_robust is True

    def test_is_robust_false_low_test_pf(self):
        r = WFResult(window=self._make_window(), train_pf=2.0, test_pf=0.9)
        assert r.is_robust is False

    def test_is_robust_false_low_retention(self):
        # test_pf > 1.0 이지만 retention <= 0.5
        r = WFResult(window=self._make_window(), train_pf=3.0, test_pf=1.1)
        # retention = 1.1/3.0 = 0.367 < 0.5
        assert r.is_robust is False

    def test_is_robust_exact_boundary(self):
        # retention = 0.5 → False (strictly > 0.5 필요)
        r = WFResult(window=self._make_window(), train_pf=2.0, test_pf=1.0)
        # test_pf == 1.0 → False (strictly > 1.0 필요)
        assert r.is_robust is False


# ─────────────────────────────────────────────────────────────────────────────
# judge_verdict
# ─────────────────────────────────────────────────────────────────────────────

class TestJudgeVerdict:
    def _make_summary(self, robust: int, total: int) -> WFSummary:
        return WFSummary(
            total_windows=total,
            robust_windows=robust,
        )

    def test_pass(self):
        s = self._make_summary(robust=7, total=10)
        assert judge_verdict(s) == "PASS"

    def test_pass_exact_70(self):
        s = self._make_summary(robust=7, total=10)
        assert judge_verdict(s) == "PASS"

    def test_warn(self):
        s = self._make_summary(robust=6, total=10)
        assert judge_verdict(s) == "WARN"

    def test_warn_exact_50(self):
        s = self._make_summary(robust=5, total=10)
        assert judge_verdict(s) == "WARN"

    def test_fail(self):
        s = self._make_summary(robust=4, total=10)
        assert judge_verdict(s) == "FAIL"

    def test_fail_zero_windows(self):
        s = WFSummary()
        assert judge_verdict(s) == "FAIL"

    def test_fail_all_zero(self):
        s = self._make_summary(robust=0, total=10)
        assert judge_verdict(s) == "FAIL"

    def test_pass_all_robust(self):
        s = self._make_summary(robust=10, total=10)
        assert judge_verdict(s) == "PASS"


# ─────────────────────────────────────────────────────────────────────────────
# build_summary
# ─────────────────────────────────────────────────────────────────────────────

class TestBuildSummary:
    def _make_window(self) -> WFWindow:
        return WFWindow("2020-01-01", "2021-12-31", "2022-01-01", "2022-12-31")

    def test_empty_results(self):
        s = build_summary([])
        assert s.overall_verdict == "FAIL"
        assert s.total_windows == 0

    def test_all_robust(self):
        results = [
            WFResult(window=self._make_window(), train_pf=2.0, test_pf=1.5)
            for _ in range(5)
        ]
        s = build_summary(results)
        assert s.total_windows == 5
        assert s.robust_windows == 5
        assert s.overall_verdict == "PASS"

    def test_avg_test_pf(self):
        results = [
            WFResult(window=self._make_window(), train_pf=2.0, test_pf=1.0),
            WFResult(window=self._make_window(), train_pf=2.0, test_pf=2.0),
        ]
        s = build_summary(results)
        assert s.avg_test_pf == pytest.approx(1.5)

    def test_avg_pf_retention(self):
        results = [
            WFResult(window=self._make_window(), train_pf=2.0, test_pf=1.0),  # 0.5
            WFResult(window=self._make_window(), train_pf=2.0, test_pf=2.0),  # 1.0
        ]
        s = build_summary(results)
        assert s.avg_pf_retention == pytest.approx(0.75)

    def test_verdict_in_summary(self):
        results = [
            WFResult(window=self._make_window(), train_pf=2.0, test_pf=1.5)
            for _ in range(7)
        ] + [
            WFResult(window=self._make_window(), train_pf=2.0, test_pf=0.8)
            for _ in range(3)
        ]
        s = build_summary(results)
        assert s.overall_verdict == "PASS"
