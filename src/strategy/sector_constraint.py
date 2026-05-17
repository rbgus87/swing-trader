"""섹터(업종) 분산 제약 — 백테스트/실전 공용.

순수 함수 설계 — DB/API 호출 없음.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SectorConstraint:
    """섹터별 최대 보유 종목 수."""

    max_per_sector: int = 2
    enabled: bool = True
    unknown_sector_name: str = "UNKNOWN"

    @classmethod
    def from_config(cls, config_dict: dict) -> "SectorConstraint":
        """trend_following 섹션 dict에서 sector_constraint 로드."""
        sc = config_dict.get("sector_constraint", {})
        return cls(
            max_per_sector=int(sc.get("max_per_sector", 2)),
            enabled=bool(sc.get("enabled", True)),
        )


def filter_by_sector(
    candidates: list[dict],
    held_positions: list[dict],
    constraint: SectorConstraint,
    sector_key: str = "industry",
    code_key: str = "code",
) -> list[dict]:
    """섹터 분산 제약을 적용하여 후보를 필터링.

    composite_score 내림차순 정렬된 candidates를 순서대로 순회하면서
    동일 업종 보유가 max_per_sector에 도달한 종목을 제외한다.

    Args:
        candidates:      스코어 순 정렬된 후보 리스트
        held_positions:  현재 보유 중인 포지션 [{sector_key: ..., code_key: ...}]
        constraint:      섹터 제약 설정
        sector_key:      dict에서 업종값을 읽을 키 이름 (기본 "industry")
        code_key:        dict에서 종목코드를 읽을 키 이름 (기본 "code")

    Returns:
        제약 통과한 후보 리스트 (입력 순서 유지)
    """
    if not constraint.enabled:
        return candidates

    sector_count: dict[str, int] = {}
    for pos in held_positions:
        sector = pos.get(sector_key) or constraint.unknown_sector_name
        sector_count[sector] = sector_count.get(sector, 0) + 1

    filtered: list[dict] = []
    for c in candidates:
        sector = c.get(sector_key) or constraint.unknown_sector_name
        current = sector_count.get(sector, 0)
        if current >= constraint.max_per_sector:
            continue
        filtered.append(c)
        sector_count[sector] = current + 1

    return filtered
