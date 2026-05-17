"""섹터 분산 제약 단위 테스트."""

import pytest

from src.strategy.sector_constraint import SectorConstraint, filter_by_sector


def _cand(code: str, industry: str) -> dict:
    return {"code": code, "industry": industry, "composite_score": 0.5}


def _held(code: str, industry: str) -> dict:
    return {"code": code, "industry": industry}


class TestFilterBySector:
    def test_no_held_all_pass(self):
        """보유 없을 때 모든 후보 통과."""
        cands = [_cand("A", "전기전자"), _cand("B", "의약품"), _cand("C", "음식료품")]
        result = filter_by_sector(cands, [], SectorConstraint(max_per_sector=2))
        assert [c["code"] for c in result] == ["A", "B", "C"]

    def test_same_sector_limit(self):
        """동일 업종 최대 2개 — 3번째는 제외."""
        cands = [
            _cand("A", "전기전자"),
            _cand("B", "전기전자"),
            _cand("C", "전기전자"),
            _cand("D", "의약품"),
        ]
        result = filter_by_sector(cands, [], SectorConstraint(max_per_sector=2))
        assert [c["code"] for c in result] == ["A", "B", "D"]

    def test_different_sectors_all_pass(self):
        """업종 다르면 max_per_sector 초과 없음."""
        cands = [
            _cand("A", "전기전자"),
            _cand("B", "의약품"),
            _cand("C", "음식료품"),
            _cand("D", "화학"),
        ]
        result = filter_by_sector(cands, [], SectorConstraint(max_per_sector=1))
        assert len(result) == 4

    def test_held_counts_toward_limit(self):
        """보유 중 종목도 한도에 포함됨."""
        held = [_held("X", "전기전자")]
        cands = [_cand("A", "전기전자"), _cand("B", "전기전자"), _cand("C", "의약품")]
        result = filter_by_sector(cands, held, SectorConstraint(max_per_sector=2))
        # 전기전자: 보유 1 + 후보 1만 통과
        assert [c["code"] for c in result] == ["A", "C"]

    def test_unknown_sector_grouped(self):
        """industry=None 종목은 UNKNOWN으로 그룹화 후 제약 적용."""
        cands = [
            {"code": "A", "industry": None, "score": 1.0},
            {"code": "B", "industry": None, "score": 0.9},
            {"code": "C", "industry": None, "score": 0.8},
        ]
        result = filter_by_sector(cands, [], SectorConstraint(max_per_sector=2))
        assert [c["code"] for c in result] == ["A", "B"]

    def test_disabled(self):
        """enabled=False → 모든 후보 통과."""
        cands = [
            _cand("A", "전기전자"),
            _cand("B", "전기전자"),
            _cand("C", "전기전자"),
        ]
        result = filter_by_sector(cands, [], SectorConstraint(enabled=False, max_per_sector=1))
        assert len(result) == 3

    def test_preserves_order(self):
        """입력 순서 유지 (스코어 순 정렬은 호출자 책임)."""
        cands = [_cand("C", "의약품"), _cand("A", "전기전자"), _cand("B", "화학")]
        result = filter_by_sector(cands, [], SectorConstraint(max_per_sector=2))
        assert [c["code"] for c in result] == ["C", "A", "B"]

    def test_from_config_defaults(self):
        """from_config: sector_constraint 없으면 기본값."""
        sc = SectorConstraint.from_config({})
        assert sc.max_per_sector == 2
        assert sc.enabled is True

    def test_from_config_custom(self):
        """from_config: 값 정상 로드."""
        cfg = {"sector_constraint": {"enabled": False, "max_per_sector": 3}}
        sc = SectorConstraint.from_config(cfg)
        assert sc.enabled is False
        assert sc.max_per_sector == 3

    def test_max_per_sector_one(self):
        """max_per_sector=1: 각 업종 1종목만."""
        cands = [
            _cand("A", "전기전자"),
            _cand("B", "전기전자"),
            _cand("C", "의약품"),
            _cand("D", "의약품"),
        ]
        result = filter_by_sector(cands, [], SectorConstraint(max_per_sector=1))
        assert [c["code"] for c in result] == ["A", "C"]

    def test_empty_candidates(self):
        """빈 후보 → 빈 리스트."""
        result = filter_by_sector([], [], SectorConstraint())
        assert result == []

    def test_custom_sector_key(self):
        """sector_key 파라미터 변경 가능."""
        cands = [
            {"code": "A", "sector": "IT", "score": 1.0},
            {"code": "B", "sector": "IT", "score": 0.9},
            {"code": "C", "sector": "BIO", "score": 0.8},
        ]
        result = filter_by_sector(cands, [], SectorConstraint(max_per_sector=1), sector_key="sector")
        assert [c["code"] for c in result] == ["A", "C"]
