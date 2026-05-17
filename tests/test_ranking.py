"""복합 랭킹 스코어 단위 테스트."""

import pytest

from src.strategy.ranking import RankingWeights, compute_composite_score


def _cand(code="A", rs=0.05, stock_ret_n=0.10, atr_ratio=0.04,
          adx=25.0, avg_trading_value_20=10e9, ma60_dist=0.10) -> dict:
    """기본값으로 후보 dict 생성 헬퍼."""
    return dict(
        code=code,
        rs=rs,
        stock_ret_n=stock_ret_n,
        atr_ratio=atr_ratio,
        adx=adx,
        avg_trading_value_20=avg_trading_value_20,
        ma60_dist=ma60_dist,
    )


class TestCompositeScore:
    def test_single_candidate(self):
        """후보 1개 → 모두 같은 값이므로 0.5."""
        c = _cand()
        result = compute_composite_score([c])
        assert result[0]["composite_score"] == pytest.approx(0.5, abs=0.01)

    def test_multiple_sorted_by_score(self):
        """복수 후보는 composite_score 내림차순 정렬."""
        candidates = [
            _cand("A", rs=0.01, adx=20.0),
            _cand("B", rs=0.10, adx=40.0),
            _cand("C", rs=0.05, adx=30.0),
        ]
        result = compute_composite_score(candidates)
        scores = [c["composite_score"] for c in result]
        assert scores == sorted(scores, reverse=True)

    def test_rs_dominant_when_weighted(self):
        """RS 가중치를 크게 주면 RS 높은 종목이 1위."""
        candidates = [
            _cand("LOW_RS",  rs=0.01, adx=50.0),
            _cand("HIGH_RS", rs=0.20, adx=20.0),
        ]
        weights = RankingWeights(rs=0.90, momentum_atr=0.025, adx=0.025,
                                 liquidity=0.025, ma_alignment=0.025)
        result = compute_composite_score(candidates, weights)
        assert result[0]["code"] == "HIGH_RS"

    def test_equal_weights(self):
        """동일 가중치: 단조 증가 팩터를 가진 종목이 상위."""
        candidates = [
            _cand("LO", rs=0.01, stock_ret_n=0.03, adx=20.0, avg_trading_value_20=1e9),
            _cand("HI", rs=0.10, stock_ret_n=0.15, adx=40.0, avg_trading_value_20=9e9),
        ]
        w = RankingWeights(rs=0.2, momentum_atr=0.2, adx=0.2,
                           liquidity=0.2, ma_alignment=0.2)
        result = compute_composite_score(candidates, w)
        assert result[0]["code"] == "HI"

    def test_zero_atr_ratio_safe(self):
        """atr_ratio=0이어도 ZeroDivisionError 없음 (mom_atr=0으로 처리)."""
        candidates = [
            _cand("A", atr_ratio=0.0),
            _cand("B", atr_ratio=0.04),
        ]
        result = compute_composite_score(candidates)
        assert all("composite_score" in c for c in result)

    def test_all_same_values(self):
        """모든 팩터가 동일 → 각 정규화값 = 0.5 → composite_score = 0.5."""
        candidates = [_cand("X"), _cand("Y"), _cand("Z")]
        result = compute_composite_score(candidates)
        for c in result:
            assert c["composite_score"] == pytest.approx(0.5, abs=0.01)

    def test_empty_list(self):
        """빈 리스트 → 빈 리스트 반환."""
        assert compute_composite_score([]) == []

    def test_rank_detail_present(self):
        """_rank_detail 딕셔너리가 각 후보에 추가됨."""
        candidates = [_cand("A"), _cand("B")]
        result = compute_composite_score(candidates)
        for c in result:
            assert "_rank_detail" in c
            detail = c["_rank_detail"]
            for key in ("rs", "mom_atr", "adx", "liq", "ma"):
                assert key in detail
                assert 0.0 <= detail[key] <= 1.0

    def test_from_config_defaults(self):
        """from_config: ranking_weights 없으면 기본값 사용."""
        w = RankingWeights.from_config({})
        assert w.rs == pytest.approx(0.35)
        assert w.momentum_atr == pytest.approx(0.25)
        assert w.adx == pytest.approx(0.20)
        assert w.liquidity == pytest.approx(0.10)
        assert w.ma_alignment == pytest.approx(0.10)

    def test_from_config_custom(self):
        """from_config: ranking_weights 값 정상 로드."""
        cfg = {"ranking_weights": {"rs": 0.50, "adx": 0.50,
                                   "momentum_atr": 0.0,
                                   "liquidity": 0.0, "ma_alignment": 0.0}}
        w = RankingWeights.from_config(cfg)
        assert w.rs == pytest.approx(0.50)
        assert w.adx == pytest.approx(0.50)

    def test_default_weights_none(self):
        """weights=None → RankingWeights() 기본값 사용."""
        candidates = [_cand("A"), _cand("B")]
        result = compute_composite_score(candidates, weights=None)
        assert all("composite_score" in c for c in result)

    def test_score_in_zero_one_range(self):
        """composite_score는 항상 [0, 1] 범위."""
        import random
        random.seed(42)
        candidates = [
            _cand(str(i),
                  rs=random.uniform(-0.05, 0.15),
                  stock_ret_n=random.uniform(0, 0.20),
                  atr_ratio=random.uniform(0.02, 0.08),
                  adx=random.uniform(20, 60),
                  avg_trading_value_20=random.uniform(5e9, 100e9),
                  ma60_dist=random.uniform(0.05, 0.20))
            for i in range(20)
        ]
        result = compute_composite_score(candidates)
        for c in result:
            assert 0.0 <= c["composite_score"] <= 1.0
