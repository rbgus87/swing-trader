"""스코어링 공통 유틸리티."""
from __future__ import annotations


def normalize_score(value: float, min_val: float, max_val: float) -> float:
    """value를 [min_val, max_val] 범위 → 0~100으로 선형 정규화.

    min_val == max_val이면 50 반환 (중립).
    결과는 항상 0.0~100.0으로 클리핑.
    """
    if max_val == min_val:
        return 50.0
    raw = (value - min_val) / (max_val - min_val) * 100.0
    return float(max(0.0, min(100.0, raw)))


def weighted_average(scores: list[float], weights: list[float]) -> float:
    """가중 평균 점수 계산. 결과는 0~100으로 클리핑.

    scores와 weights 길이가 0이면 50 반환.
    """
    if not scores:
        return 50.0
    total_weight = sum(weights)
    if total_weight == 0.0:
        return 50.0
    result = sum(s * w for s, w in zip(scores, weights)) / total_weight
    return float(max(0.0, min(100.0, result)))
