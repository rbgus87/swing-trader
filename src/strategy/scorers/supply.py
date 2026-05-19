"""수급 점수 스코어러.

입력: supply_records — list[dict] (DB supply_demand 행, 최신순 정렬)
출력: 0~100 float
"""
from __future__ import annotations

from app.services.scorers import normalize_score, weighted_average

# 서브 점수 가중치 (합 = 1.0)
_W_SMART_MONEY = 0.50
_W_FOREIGN_TREND = 0.30
_W_SHORT_PRESSURE = 0.20


def _safe_float(val: object) -> float | None:
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _score_smart_money(records: list[dict]) -> float:
    """외국인 + 기관 순매수 기반 스마트머니 점수 (0~100).

    최근 10거래일 누적 순매수 합계 기준.
    """
    if not records:
        return 50.0

    recent = records[:10]
    total_net = 0.0
    valid_count = 0

    for r in recent:
        foreign = _safe_float(r.get("foreign_net_buy")) or 0.0
        institution = _safe_float(r.get("institution_net_buy")) or 0.0
        total_net += foreign + institution
        valid_count += 1

    if valid_count == 0:
        return 50.0

    avg_net = total_net / valid_count

    # 기준: ±5억 (500,000,000) 사이를 중립으로 처리
    # 10억 이상 순매수 → 고점수, -10억 이하 → 저점수
    threshold = 1_000_000_000  # 10억
    return normalize_score(avg_net, -threshold, threshold)


def _score_foreign_trend(records: list[dict]) -> float:
    """외국인 순매수 추세 점수 (0~100).

    최근 5일 평균 외국인 순매수 vs 이전 5일 평균 비교 (추세 방향).
    """
    if len(records) < 5:
        return 50.0

    recent5 = records[:5]
    prev5 = records[5:10] if len(records) >= 10 else records[5:]

    def _avg_foreign(recs: list[dict]) -> float | None:
        vals = [_safe_float(r.get("foreign_net_buy")) for r in recs]
        vals = [v for v in vals if v is not None]
        return sum(vals) / len(vals) if vals else None

    curr_avg = _avg_foreign(recent5)
    prev_avg = _avg_foreign(prev5) if prev5 else None

    if curr_avg is None:
        return 50.0

    # 절대 수준 점수 (기본)
    level_score = normalize_score(curr_avg, -1_000_000_000, 1_000_000_000)

    if prev_avg is None:
        return level_score

    # 방향 보정: 최근 5일이 이전 5일보다 개선되면 +10점
    direction_bonus = 10.0 if curr_avg > prev_avg else -10.0
    return float(max(0.0, min(100.0, level_score + direction_bonus)))


def _score_short_pressure(records: list[dict]) -> float:
    """공매도 압력 점수 (0~100). 공매도 비율이 낮을수록 높은 점수.

    short_sell_volume이 없으면 50 반환.
    """
    if not records:
        return 50.0

    recent = records[:10]
    short_vols = []

    for r in recent:
        sv = _safe_float(r.get("short_sell_volume"))
        if sv is not None and sv >= 0:
            short_vols.append(sv)

    if not short_vols:
        return 50.0

    avg_short = sum(short_vols) / len(short_vols)

    # 공매도 볼륨 기준: 0 → 100점, 5만주 이상 → 0점
    # 역방향 정규화
    threshold = 50_000.0
    return normalize_score(-avg_short, -threshold, 0.0)


def compute_supply_score(supply_records: list[dict]) -> float:
    """수급 종합 점수 계산 (0~100).

    Args:
        supply_records: DB supply_demand 행 목록.
            최신순 정렬 필수.
            필드: foreign_net_buy, institution_net_buy, individual_net_buy,
                  foreign_hold_ratio, short_sell_volume

    Returns:
        0~100 float (50 = 데이터 없음 또는 중립)
    """
    if not supply_records:
        return 50.0

    scores = [
        _score_smart_money(supply_records),
        _score_foreign_trend(supply_records),
        _score_short_pressure(supply_records),
    ]
    weights = [_W_SMART_MONEY, _W_FOREIGN_TREND, _W_SHORT_PRESSURE]
    return weighted_average(scores, weights)
