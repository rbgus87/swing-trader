"""Walk-Forward 검증 — portfolio_backtester 기반.

순수 데이터클래스 + 유틸 함수만 포함. I/O 없음.
실행은 experiments/run_walk_forward.py 참조.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta


@dataclass
class WFWindow:
    """하나의 Walk-Forward 윈도우."""

    train_start: str
    train_end: str
    test_start: str
    test_end: str


@dataclass
class WFResult:
    """하나의 윈도우 결과."""

    window: WFWindow
    train_pf: float = 0.0
    train_cagr: float = 0.0
    train_mdd: float = 0.0
    train_trades: int = 0
    test_pf: float = 0.0
    test_cagr: float = 0.0
    test_mdd: float = 0.0
    test_trades: int = 0

    @property
    def pf_retention(self) -> float:
        """PF 유지율 (test / train). train_pf <= 0 이면 0.0."""
        if self.train_pf <= 0:
            return 0.0
        return self.test_pf / self.train_pf

    @property
    def is_robust(self) -> bool:
        """견고성 판정: test PF > 1.0 AND PF 유지율 > 50%."""
        return self.test_pf > 1.0 and self.pf_retention > 0.5


@dataclass
class WFSummary:
    """Walk-Forward 전체 요약."""

    results: list[WFResult] = field(default_factory=list)
    total_windows: int = 0
    robust_windows: int = 0
    avg_test_pf: float = 0.0
    avg_pf_retention: float = 0.0
    overall_verdict: str = ""  # "PASS" / "WARN" / "FAIL"

    @property
    def robustness_rate(self) -> float:
        """견고 윈도우 비율 (0~1)."""
        if self.total_windows == 0:
            return 0.0
        return self.robust_windows / self.total_windows


def generate_windows(
    start_date: str,
    end_date: str,
    train_years: int = 2,
    test_years: int = 1,
    step_months: int = 12,
) -> list[WFWindow]:
    """롤링 Walk-Forward 윈도우 생성.

    Args:
        start_date:   전체 시작일 (YYYY-MM-DD)
        end_date:     전체 종료일 (YYYY-MM-DD)
        train_years:  훈련 기간 (년)
        test_years:   테스트 기간 (년)
        step_months:  윈도우 이동 간격 (월, 30일 단위 근사)

    Returns:
        겹치지 않는 Train/Test 쌍의 리스트. test_end 가 end_date 초과 시 중단.
    """
    start = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d")
    windows: list[WFWindow] = []

    current = start
    while True:
        train_start = current
        train_end = train_start + timedelta(days=train_years * 365)
        test_start = train_end + timedelta(days=1)
        test_end = test_start + timedelta(days=test_years * 365)

        if test_end > end:
            break

        windows.append(
            WFWindow(
                train_start=train_start.strftime("%Y-%m-%d"),
                train_end=train_end.strftime("%Y-%m-%d"),
                test_start=test_start.strftime("%Y-%m-%d"),
                test_end=test_end.strftime("%Y-%m-%d"),
            )
        )
        current += timedelta(days=step_months * 30)

    return windows


def build_summary(results: list[WFResult]) -> WFSummary:
    """WFResult 리스트로 WFSummary 계산.

    test_pf=inf (거래 없음)인 윈도우는 평균 계산에서 제외.
    """
    if not results:
        return WFSummary(overall_verdict="FAIL")

    robust = sum(1 for r in results if r.is_robust)

    finite = [r for r in results if r.test_pf != float("inf")]
    avg_test_pf = (
        sum(r.test_pf for r in finite) / len(finite) if finite else float("inf")
    )
    finite_ret = [r for r in results if r.pf_retention != float("inf")]
    avg_retention = (
        sum(r.pf_retention for r in finite_ret) / len(finite_ret)
        if finite_ret else float("inf")
    )

    summary = WFSummary(
        results=results,
        total_windows=len(results),
        robust_windows=robust,
        avg_test_pf=avg_test_pf,
        avg_pf_retention=avg_retention,
    )
    summary.overall_verdict = judge_verdict(summary)
    return summary


def judge_verdict(summary: WFSummary) -> str:
    """전체 판정.

    견고 윈도우 비율:
        >= 70% → PASS
        50~70% → WARN
        < 50%  → FAIL
    """
    rate = summary.robustness_rate
    if rate >= 0.7:
        return "PASS"
    elif rate >= 0.5:
        return "WARN"
    return "FAIL"
