"""스코어 기반 감시 목록. 매매 판단과 독립 — 모니터링/폴링/GUI/텔레그램 전용."""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from src.strategy.scorers.technical import compute_technical_score
from src.strategy.scorers.momentum import compute_momentum_score


@dataclass
class WatchlistConfig:
    enabled: bool = True
    min_score: float = 40.0
    max_size: int = 50
    weight_technical: float = 0.60
    weight_momentum: float = 0.40
    poll_top_n: int = 20

    @classmethod
    def from_config(cls, cfg: dict) -> "WatchlistConfig":
        s = cfg.get("watchlist", {}).get("scorer", {})
        return cls(
            enabled=bool(s.get("enabled", True)),
            min_score=float(s.get("min_score", 40.0)),
            max_size=int(s.get("max_size", 50)),
            weight_technical=float(s.get("weight_technical", 0.60)),
            weight_momentum=float(s.get("weight_momentum", 0.40)),
            poll_top_n=int(s.get("poll_top_n", 20)),
        )


@dataclass
class WatchlistItem:
    ticker: str
    name: str
    score: float
    technical_score: float
    momentum_score: float
    market: str = ""
    industry: str = ""

    def as_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "name": self.name,
            "score": round(self.score, 1),
            "technical_score": round(self.technical_score, 1),
            "momentum_score": round(self.momentum_score, 1),
            "market": self.market,
            "industry": self.industry,
        }


def _make_weekly(daily_df: pd.DataFrame) -> pd.DataFrame:
    """일봉 DataFrame → 주봉 DataFrame (close 기준 resample, W-FRI 기준)."""
    if daily_df is None or len(daily_df) < 5:
        return pd.DataFrame(columns=["close"])

    df = daily_df.copy()
    # date 컬럼을 인덱스로
    if not pd.api.types.is_datetime64_any_dtype(df.index):
        if "date" in df.columns:
            df = df.set_index("date")
        df.index = pd.to_datetime(df.index)

    weekly_close = df["close"].resample("W-FRI").last().dropna()
    return pd.DataFrame({"close": weekly_close})


def build_watchlist(
    ticker_data: dict[str, pd.DataFrame],
    market_df_map: dict[str, pd.DataFrame],
    cfg: WatchlistConfig,
    ticker_meta: dict[str, dict] | None = None,
) -> list[WatchlistItem]:
    """스코어 기반 감시 목록 생성.

    Args:
        ticker_data:   ticker → daily_df (columns: open, high, low, close, volume)
        market_df_map: 'KOSPI'/'KOSDAQ' → market daily_df (column: close)
        cfg:           WatchlistConfig
        ticker_meta:   ticker → {name, market, industry} (선택)

    Returns:
        WatchlistItem 리스트, score 내림차순, 최대 cfg.max_size개
    """
    if not cfg.enabled:
        return []

    kospi_df = market_df_map.get("KOSPI") if market_df_map else None
    kosdaq_df = market_df_map.get("KOSDAQ") if market_df_map else None

    items: list[WatchlistItem] = []
    for ticker, daily_df in ticker_data.items():
        if daily_df is None or len(daily_df) < 20:
            continue

        meta = (ticker_meta or {}).get(ticker, {})
        mkt = meta.get("market", "")
        market_df = kosdaq_df if mkt == "KOSDAQ" else kospi_df

        weekly_df = _make_weekly(daily_df)
        weekly_arg = weekly_df if len(weekly_df) >= 10 else None

        tech = compute_technical_score(daily_df, weekly_arg)
        mom = compute_momentum_score(daily_df, market_df)

        score = cfg.weight_technical * tech + cfg.weight_momentum * mom
        if score < cfg.min_score:
            continue

        items.append(
            WatchlistItem(
                ticker=ticker,
                name=meta.get("name", ticker),
                score=round(score, 1),
                technical_score=round(tech, 1),
                momentum_score=round(mom, 1),
                market=mkt,
                industry=meta.get("industry", ""),
            )
        )

    items.sort(key=lambda x: x.score, reverse=True)
    return items[: cfg.max_size]
