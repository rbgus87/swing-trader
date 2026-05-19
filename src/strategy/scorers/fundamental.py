"""펀더멘털 점수 스코어러.

입력: financial_records — list[dict] (DB financial_data 행, 최신순 정렬)
출력: 0~100 float
"""
from __future__ import annotations

from app.services.scorers import normalize_score, weighted_average

# 서브 점수 가중치 (합 = 1.0)
_W_PROFITABILITY = 0.30
_W_GROWTH = 0.30
_W_STABILITY = 0.20
_W_VALUATION = 0.20

# 업종별 부채비율 기준 없이 절대 기준 사용 (DB: 소수, 1.5 = 150%)
_MAX_HEALTHY_DEBT_RATIO = 2.0   # 200%
_MAX_DEBT_RATIO = 5.0           # 500%


def _safe_float(val: object) -> float | None:
    """None이 아닌 경우 float으로 변환."""
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _score_profitability(records: list[dict]) -> float:
    """수익성 점수 (0~100): 영업이익률 + ROE.

    최근 2개 분기 평균 사용.
    """
    if not records:
        return 50.0

    op_margins = []
    roes = []

    for r in records[:4]:  # 최근 4분기
        rev = _safe_float(r.get("revenue"))
        op = _safe_float(r.get("operating_profit"))
        roe = _safe_float(r.get("roe"))

        if rev and rev > 0 and op is not None:
            op_margins.append(op / rev * 100.0)
        if roe is not None:
            roes.append(roe * 100.0 if abs(roe) < 1.0 else roe)  # 소수 → 퍼센트 변환

    op_score = 50.0
    if op_margins:
        avg_margin = sum(op_margins) / len(op_margins)
        # 영업이익률: 0% → 30점, 10% → 70점, 20%+ → 100점
        op_score = normalize_score(avg_margin, -5.0, 25.0)

    roe_score = 50.0
    if roes:
        avg_roe = sum(roes) / len(roes)
        # ROE: 0% → 30점, 15% → 75점, 25%+ → 100점
        roe_score = normalize_score(avg_roe, -10.0, 30.0)

    return weighted_average([op_score, roe_score], [0.5, 0.5])


def _score_growth(records: list[dict]) -> float:
    """성장성 점수 (0~100): YoY 매출/영업이익 성장률.

    최소 5개 분기 필요 (4분기 전과 비교).
    """
    if len(records) < 5:
        return 50.0

    # 가장 최근 분기 vs 1년 전 분기
    curr = records[0]
    prev_yr = records[4]

    rev_curr = _safe_float(curr.get("revenue"))
    rev_prev = _safe_float(prev_yr.get("revenue"))
    op_curr = _safe_float(curr.get("operating_profit"))
    op_prev = _safe_float(prev_yr.get("operating_profit"))

    rev_growth_score = 50.0
    op_growth_score = 50.0

    if rev_curr is not None and rev_prev and rev_prev > 0:
        rev_growth = (rev_curr - rev_prev) / rev_prev * 100.0
        # 성장률: -20% → 0점, 0% → 50점, 20%+ → 100점
        rev_growth_score = normalize_score(rev_growth, -20.0, 30.0)

    if op_curr is not None and op_prev and op_prev > 0:
        op_growth = (op_curr - op_prev) / op_prev * 100.0
        op_growth_score = normalize_score(op_growth, -30.0, 40.0)

    return weighted_average([rev_growth_score, op_growth_score], [0.5, 0.5])


def _score_stability(records: list[dict]) -> float:
    """재무 안정성 점수 (0~100): 부채비율.

    DB 값은 소수 (1.5 = 150%). 낮을수록 좋음.
    """
    if not records:
        return 50.0

    debt_ratios = []
    for r in records[:2]:
        dr = _safe_float(r.get("debt_ratio"))
        if dr is not None and dr >= 0:
            debt_ratios.append(dr)

    if not debt_ratios:
        return 50.0

    avg_dr = sum(debt_ratios) / len(debt_ratios)

    # 부채비율 0 → 100점, 200%(2.0) → 50점, 500%(5.0) → 0점
    return normalize_score(-avg_dr, -_MAX_DEBT_RATIO, 0.0)


def _score_valuation(records: list[dict]) -> float:
    """밸류에이션 점수 (0~100): EPS 추세.

    EPS가 지속 성장 중이면 높은 점수.
    """
    if not records:
        return 50.0

    eps_vals = []
    for r in records[:4]:
        eps = _safe_float(r.get("eps"))
        if eps is not None:
            eps_vals.append(eps)

    if not eps_vals:
        return 50.0

    if len(eps_vals) == 1:
        # EPS 양수면 중립 이상
        return 70.0 if eps_vals[0] > 0 else 30.0

    # EPS 추세 (연속 증가 비율)
    increases = sum(
        1 for i in range(1, len(eps_vals)) if eps_vals[i - 1] > eps_vals[i]
    )
    trend_ratio = increases / (len(eps_vals) - 1)

    # 최근 EPS 양수 여부
    eps_positive = eps_vals[0] > 0

    base = trend_ratio * 80.0
    if eps_positive:
        base = min(100.0, base + 20.0)

    return float(base)


def compute_fundamental_score(financial_records: list[dict]) -> float:
    """펀더멘털 종합 점수 계산 (0~100).

    Args:
        financial_records: DB financial_data 행 목록.
            최신순 정렬 필수.
            필드: revenue, operating_profit, net_income, eps, roe, debt_ratio

    Returns:
        0~100 float (50 = 데이터 없음 또는 중립)
    """
    if not financial_records:
        return 50.0

    scores = [
        _score_profitability(financial_records),
        _score_growth(financial_records),
        _score_stability(financial_records),
        _score_valuation(financial_records),
    ]
    weights = [_W_PROFITABILITY, _W_GROWTH, _W_STABILITY, _W_VALUATION]
    return weighted_average(scores, weights)
