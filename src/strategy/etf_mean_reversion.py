"""ETF IBS 평균회귀 전략."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ETFStrategyParams:
    """ETF IBS 전략 파라미터."""

    enabled: bool = True
    etf_code: str = "069500"        # KODEX 200
    index_code: str = "KOSPI"
    ibs_entry: float = 0.1          # IBS < ibs_entry → 매수
    ibs_exit: float = 0.8           # IBS > ibs_exit → 매도
    max_hold_days: int = 10
    min_idle_cash: int = 1_000_000
    cost_pct: float = 0.0003        # ETF 왕복 수수료 0.03%

    @classmethod
    def from_config(cls, config_dict: dict) -> "ETFStrategyParams":
        ec = config_dict.get("etf_strategy", {})
        return cls(
            enabled=bool(ec.get("enabled", True)),
            etf_code=str(ec.get("etf_code", "069500")),
            index_code=str(ec.get("index_code", "KOSPI")),
            ibs_entry=float(ec.get("ibs_entry", 0.1)),
            ibs_exit=float(ec.get("ibs_exit", 0.8)),
            max_hold_days=int(ec.get("max_hold_days", 10)),
            min_idle_cash=int(ec.get("min_idle_cash", 1_000_000)),
            cost_pct=float(ec.get("cost_pct", 0.0003)),
        )


def compute_ibs(high: float, low: float, close: float) -> float:
    """Internal Bar Strength (IBS) 계산.

    IBS = (close - low) / (high - low), 범위 [0, 1].
    고가 = 저가이면 0.5 반환.
    """
    if high == low:
        return 0.5
    return (close - low) / (high - low)


def check_ibs_entry(
    high: float,
    low: float,
    close: float,
    threshold: float = 0.1,
) -> bool:
    """IBS 진입 조건: IBS < threshold."""
    return compute_ibs(high, low, close) < threshold


def check_ibs_exit(
    high: float,
    low: float,
    close: float,
    threshold: float = 0.8,
    hold_days: int = 0,
    max_hold: int = 10,
) -> bool:
    """IBS 청산 조건: IBS > threshold 또는 최대 보유일 초과."""
    if hold_days >= max_hold:
        return True
    return compute_ibs(high, low, close) > threshold
