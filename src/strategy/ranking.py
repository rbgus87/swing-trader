"""복합 랭킹 스코어 — 백테스트/실전 공용.

ADX 단일 정렬을 RS + ATR정규화 모멘텀 + 유동성 + ADX 복합 스코어로 교체.
순수 함수 설계 — DB/API 호출 없음.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RankingWeights:
    """복합 스코어 가중치 (Min-Max 정규화 후 가중합)."""

    rs: float = 0.35            # 상대강도 (stock_ret_n - bench_ret)
    momentum_atr: float = 0.25  # ATR 정규화 모멘텀 (stock_ret_n / atr_ratio)
    adx: float = 0.20           # 추세 강도
    liquidity: float = 0.10     # 거래대금
    ma_alignment: float = 0.10  # MA60 이격도 (정렬 품질)

    @classmethod
    def from_config(cls, config_dict: dict) -> "RankingWeights":
        """trend_following 섹션 dict에서 ranking_weights 로드."""
        rw = config_dict.get("ranking_weights", {})
        return cls(
            rs=float(rw.get("rs", 0.35)),
            momentum_atr=float(rw.get("momentum_atr", 0.25)),
            adx=float(rw.get("adx", 0.20)),
            liquidity=float(rw.get("liquidity", 0.10)),
            ma_alignment=float(rw.get("ma_alignment", 0.10)),
        )


def compute_composite_score(
    candidates: list[dict],
    weights: RankingWeights | None = None,
) -> list[dict]:
    """후보 종목에 복합 스코어를 계산하여 내림차순 정렬한다.

    각 candidate dict에 필요한 키:
        code                  (str)   종목코드
        rs                    (float) 상대강도 = stock_ret_n - bench_ret
        stock_ret_n           (float) N일 수익률
        atr_ratio             (float) atr / close
        adx                   (float)
        avg_trading_value_20  (float) 20일 평균 거래대금
        ma60_dist             (float) MA60 이격도

    결과:
        composite_score, _rank_detail 키가 추가된 candidate list (내림차순).
        입력 list의 각 dict를 in-place 수정하고, 정렬된 동일 list를 반환.
    """
    if not candidates:
        return []

    if weights is None:
        weights = RankingWeights()

    rs_vals: list[float] = []
    mom_atr_vals: list[float] = []
    adx_vals: list[float] = []
    liq_vals: list[float] = []
    ma_vals: list[float] = []

    for c in candidates:
        rs_vals.append(float(c.get("rs", 0.0)))

        atr_ratio = float(c.get("atr_ratio", 0.0))
        ret_n = float(c.get("stock_ret_n", 0.0))
        mom_atr_vals.append(ret_n / atr_ratio if atr_ratio > 0 else 0.0)

        adx_vals.append(float(c.get("adx", 0.0)))
        liq_vals.append(float(c.get("avg_trading_value_20", 0.0)))
        ma_vals.append(float(c.get("ma60_dist", 0.0)))

    def _minmax(vals: list[float]) -> list[float]:
        mn, mx = min(vals), max(vals)
        rng = mx - mn
        if rng == 0:
            return [0.5] * len(vals)
        return [(v - mn) / rng for v in vals]

    rs_norm = _minmax(rs_vals)
    mom_norm = _minmax(mom_atr_vals)
    adx_norm = _minmax(adx_vals)
    liq_norm = _minmax(liq_vals)
    ma_norm = _minmax(ma_vals)

    total_w = (
        weights.rs + weights.momentum_atr + weights.adx
        + weights.liquidity + weights.ma_alignment
    )
    if total_w == 0:
        total_w = 1.0

    for i, c in enumerate(candidates):
        raw = (
            weights.rs * rs_norm[i]
            + weights.momentum_atr * mom_norm[i]
            + weights.adx * adx_norm[i]
            + weights.liquidity * liq_norm[i]
            + weights.ma_alignment * ma_norm[i]
        )
        c["composite_score"] = round(raw / total_w, 4)
        c["_rank_detail"] = {
            "rs": round(rs_norm[i], 3),
            "mom_atr": round(mom_norm[i], 3),
            "adx": round(adx_norm[i], 3),
            "liq": round(liq_norm[i], 3),
            "ma": round(ma_norm[i], 3),
        }

    candidates.sort(key=lambda x: x["composite_score"], reverse=True)
    return candidates
