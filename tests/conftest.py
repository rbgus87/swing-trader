"""공통 pytest fixture 정의."""

import os
import tempfile

import pytest

from src.datastore import DataStore


@pytest.fixture
def tmp_db(tmp_path):
    """임시 SQLite DB를 생성하고 DataStore 인스턴스를 반환.

    테스트 종료 시 자동 정리됨.
    """
    db_path = str(tmp_path / "test.db")
    store = DataStore(db_path=db_path)
    store.connect()
    store.create_tables()
    yield store
    store.close()


@pytest.fixture
def sample_config():
    """테스트용 config 딕셔너리."""
    return {
        "trading": {
            "mode": "paper",
            "universe": "kospi_kosdaq",
            "max_positions": 5,
            "reentry_cooldown_days": 3,
        },
        "screening": {
            "min_daily_amount": 5_000_000_000,
            "min_market_cap": 30_000_000_000,
            "min_price": 1000,
            "max_price": 500000,
            "top_n": 30,
        },
        "strategy": {
            "macd_fast": 12,
            "macd_slow": 26,
            "macd_signal": 9,
            "rsi_period": 14,
            "target_return": 0.08,
        },
        "risk": {
            "max_position_ratio": 0.15,
            "min_position_ratio": 0.03,
            "daily_loss_limit": -0.03,
            "max_mdd": -0.20,
        },
        "backtest": {
            "commission": 0.00015,
            "tax": 0.002,
            "slippage": 0.001,
            "initial_capital": 10_000_000,
        },
    }


@pytest.fixture
def tmp_config_file(tmp_path, sample_config):
    """임시 config.yaml 파일 생성."""
    import yaml

    config_path = tmp_path / "config.yaml"
    with open(config_path, "w", encoding="utf-8") as f:
        yaml.dump(sample_config, f, allow_unicode=True)
    return str(config_path)
