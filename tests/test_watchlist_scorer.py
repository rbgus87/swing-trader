"""감시 목록 스코어러 단위 테스트."""
import numpy as np
import pandas as pd
import pytest

from src.strategy.watchlist_scorer import (
    WatchlistConfig,
    WatchlistItem,
    _make_weekly,
    build_watchlist,
)


# ── 공통 픽스처 ─────────────────────────────────────────────────────────────

def _make_daily(n: int = 60, base_price: float = 10_000.0) -> pd.DataFrame:
    """n행 일봉 DataFrame 생성 (상승 추세)."""
    dates = pd.date_range("2025-01-01", periods=n, freq="B")
    close = base_price + np.arange(n, dtype=float) * 10
    high = close * 1.005
    low = close * 0.995
    open_ = close * 0.998
    volume = np.full(n, 500_000, dtype=float)
    return pd.DataFrame(
        {"date": dates, "open": open_, "high": high, "low": low,
         "close": close, "volume": volume}
    )


def _make_market_df(n: int = 60, base: float = 2_500.0) -> pd.DataFrame:
    """n행 시장 지수 DataFrame."""
    dates = pd.date_range("2025-01-01", periods=n, freq="B")
    close = base + np.arange(n, dtype=float) * 0.5
    return pd.DataFrame({"date": dates, "close": close})


# ── WatchlistConfig 테스트 ──────────────────────────────────────────────────

class TestWatchlistConfig:
    def test_defaults(self):
        cfg = WatchlistConfig()
        assert cfg.enabled is True
        assert cfg.min_score == 40.0
        assert cfg.max_size == 50
        assert cfg.weight_technical == 0.60
        assert cfg.weight_momentum == 0.40
        assert cfg.poll_top_n == 20

    def test_from_config(self):
        raw = {
            "watchlist": {
                "scorer": {
                    "enabled": False,
                    "min_score": 55,
                    "max_size": 30,
                    "weight_technical": 0.70,
                    "weight_momentum": 0.30,
                    "poll_top_n": 10,
                }
            }
        }
        cfg = WatchlistConfig.from_config(raw)
        assert cfg.enabled is False
        assert cfg.min_score == 55.0
        assert cfg.max_size == 30
        assert cfg.weight_technical == 0.70
        assert cfg.weight_momentum == 0.30
        assert cfg.poll_top_n == 10

    def test_from_config_empty_uses_defaults(self):
        cfg = WatchlistConfig.from_config({})
        assert cfg.enabled is True
        assert cfg.min_score == 40.0


# ── _make_weekly 테스트 ─────────────────────────────────────────────────────

class TestMakeWeekly:
    def test_basic(self):
        daily = _make_daily(60)
        weekly = _make_weekly(daily)
        assert "close" in weekly.columns
        assert len(weekly) > 0
        assert len(weekly) < len(daily)

    def test_too_short_returns_empty(self):
        daily = _make_daily(3)
        weekly = _make_weekly(daily)
        assert len(weekly) == 0

    def test_none_returns_empty(self):
        weekly = _make_weekly(None)
        assert len(weekly) == 0


# ── build_watchlist 테스트 ──────────────────────────────────────────────────

class TestBuildWatchlist:
    def test_empty_input_returns_empty(self):
        cfg = WatchlistConfig()
        result = build_watchlist({}, {}, cfg)
        assert result == []

    def test_disabled_returns_empty(self):
        cfg = WatchlistConfig(enabled=False)
        ticker_data = {"A005930": _make_daily(60)}
        result = build_watchlist(ticker_data, {}, cfg)
        assert result == []

    def test_too_short_data_filtered(self):
        cfg = WatchlistConfig(min_score=0.0)  # min_score=0이면 점수 상관없이 통과
        ticker_data = {"A005930": _make_daily(10)}  # 10행 < 20행 최소 기준
        result = build_watchlist(ticker_data, {}, cfg)
        assert result == []

    def test_returns_watchlist_items(self):
        cfg = WatchlistConfig(min_score=0.0)
        ticker_data = {"A005930": _make_daily(60)}
        market_map = {"KOSPI": _make_market_df(60)}
        result = build_watchlist(ticker_data, market_map, cfg)
        assert len(result) == 1
        item = result[0]
        assert item.ticker == "A005930"
        assert 0.0 <= item.score <= 100.0
        assert 0.0 <= item.technical_score <= 100.0
        assert 0.0 <= item.momentum_score <= 100.0

    def test_sorted_by_score_desc(self):
        cfg = WatchlistConfig(min_score=0.0)
        ticker_data = {
            f"T{i:04d}": _make_daily(60, base_price=10_000.0 + i * 100)
            for i in range(5)
        }
        result = build_watchlist(ticker_data, {}, cfg)
        scores = [item.score for item in result]
        assert scores == sorted(scores, reverse=True)

    def test_max_size_respected(self):
        cfg = WatchlistConfig(min_score=0.0, max_size=3)
        ticker_data = {
            f"T{i:04d}": _make_daily(60, base_price=10_000.0 + i * 50)
            for i in range(10)
        }
        result = build_watchlist(ticker_data, {}, cfg)
        assert len(result) <= 3

    def test_min_score_filter(self):
        cfg = WatchlistConfig(min_score=99.9)  # 사실상 통과 불가
        ticker_data = {"A005930": _make_daily(60)}
        result = build_watchlist(ticker_data, {}, cfg)
        assert result == []


# ── WatchlistItem 테스트 ────────────────────────────────────────────────────

class TestWatchlistItem:
    def test_as_dict(self):
        item = WatchlistItem(
            ticker="A005930",
            name="삼성전자",
            score=72.5,
            technical_score=75.0,
            momentum_score=68.8,
            market="KOSPI",
            industry="반도체",
        )
        d = item.as_dict()
        assert d["ticker"] == "A005930"
        assert d["name"] == "삼성전자"
        assert d["score"] == 72.5
        assert d["technical_score"] == 75.0
        assert d["momentum_score"] == 68.8
        assert d["market"] == "KOSPI"
        assert d["industry"] == "반도체"
