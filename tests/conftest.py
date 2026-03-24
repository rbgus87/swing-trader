"""공통 pytest fixture 정의."""

import os
import sys
import tempfile
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.datastore import DataStore
from src.models import Position, Tick, TradeRecord


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
            "entry_start_time": "00:00",
            "entry_end_time": "23:59",
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


# ── E2E 테스트용 추가 Fixture ──


@pytest.fixture
def mock_kiwoom():
    """KiwoomAPI AsyncMock — REST/WebSocket 기반 async 메서드.

    모든 async 메서드를 AsyncMock으로 설정한다.
    """
    mock = AsyncMock()
    mock._connected = True
    mock.on_tick_callback = None
    mock.on_chejan_callback = None
    # async 메서드
    mock.connect = AsyncMock(return_value=None)
    mock.disconnect = AsyncMock(return_value=None)
    return mock


@pytest.fixture
def mock_telegram():
    """TelegramBot mock — requests 호출 없이 알림 검증.

    TelegramBot은 동기 코드이므로 MagicMock을 사용한다.
    """
    mock = MagicMock()
    mock.send.return_value = True
    mock.send_with_cooldown.return_value = True
    mock.send_signal_alert.return_value = True
    mock.send_buy_executed.return_value = True
    mock.send_sell_executed_profit.return_value = True
    mock.send_sell_executed_loss.return_value = True
    mock.send_daily_warning.return_value = True
    mock.send_halt_alert.return_value = True
    mock.send_daily_report.return_value = True
    mock.send_system_error.return_value = True
    return mock


@pytest.fixture
def populated_db(tmp_db):
    """테스트 데이터가 채워진 DataStore.

    포지션 2개(open), 매매기록 2개(buy)를 미리 삽입한다.
    """
    # 포지션 1: 삼성전자
    pos1 = Position(
        id=0,
        code="005930",
        name="삼성전자",
        entry_date="2026-03-10",
        entry_price=50000,
        quantity=20,
        stop_price=47000,
        target_price=54000,
        status="open",
        high_since_entry=50000,
    )
    tmp_db.insert_position(pos1)

    # 포지션 2: SK하이닉스
    pos2 = Position(
        id=0,
        code="000660",
        name="SK하이닉스",
        entry_date="2026-03-12",
        entry_price=120000,
        quantity=5,
        stop_price=111600,
        target_price=129600,
        status="open",
        high_since_entry=120000,
    )
    tmp_db.insert_position(pos2)

    # 매수 기록
    trade1 = TradeRecord(
        code="005930",
        name="삼성전자",
        side="buy",
        price=50000,
        quantity=20,
        amount=1_000_000,
        fee=150.0,
        tax=0.0,
        pnl=0.0,
        pnl_pct=0.0,
        reason="signal",
        executed_at="2026-03-10 09:30:00",
    )
    tmp_db.record_trade(trade1)

    trade2 = TradeRecord(
        code="000660",
        name="SK하이닉스",
        side="buy",
        price=120000,
        quantity=5,
        amount=600_000,
        fee=90.0,
        tax=0.0,
        pnl=0.0,
        pnl_pct=0.0,
        reason="signal",
        executed_at="2026-03-12 10:00:00",
    )
    tmp_db.record_trade(trade2)

    return tmp_db


@pytest.fixture
def trading_engine(tmp_db, mock_kiwoom, mock_telegram, sample_config):
    """모든 의존성이 mock된 TradingEngine (paper 모드).

    실제 DataStore, RiskManager, PositionSizer, StopManager를 사용하고,
    KiwoomAPI, TelegramBot, Screener, Scheduler만 mock한다.
    AsyncIOScheduler를 MagicMock으로 교체한다.
    """
    # loguru TRADE 레벨 등록
    from loguru import logger as _logger

    try:
        _logger.level("TRADE", no=25, color="<yellow>")
    except (TypeError, ValueError):
        pass  # 이미 등록된 경우

    from src.risk.position_sizer import PositionSizer
    from src.risk.risk_manager import RiskManager
    from src.risk.stop_manager import StopManager

    # OrderManager mock — execute_order는 async
    mock_order_mgr = MagicMock()
    mock_order_mgr.execute_order = AsyncMock()
    from src.models import OrderResult
    mock_order_mgr.execute_order.return_value = OrderResult(
        success=True, order_no="ORD001", message="OK"
    )

    # Screener mock (동기)
    mock_screener = MagicMock()

    # AsyncIOScheduler mock
    mock_scheduler = MagicMock()

    with (
        patch("src.engine.KiwoomAPI", return_value=mock_kiwoom),
        patch("src.engine.OrderManager", return_value=mock_order_mgr),
        patch("src.engine.Screener", return_value=mock_screener),
        patch("src.engine.TelegramBot", return_value=mock_telegram),
        patch("src.engine.AsyncIOScheduler", return_value=mock_scheduler),
        patch("src.engine.config") as mock_config,
        patch("src.engine.DataStore", return_value=tmp_db),
        patch("src.engine.RiskManager", return_value=RiskManager(tmp_db, sample_config)),
        patch("src.engine.PositionSizer", return_value=PositionSizer(max_ratio=0.15, min_ratio=0.03)),
        patch("src.engine.StopManager") as MockStopMgr,
    ):
        # config mock
        mock_config.get_env.return_value = ""
        mock_config.mode = "paper"
        mock_config.is_paper = True
        mock_config.data = sample_config

        def config_get(key, default=None):
            """sample_config에서 dot-notation key 조회."""
            keys = key.split(".")
            value = sample_config
            for k in keys:
                if isinstance(value, dict):
                    value = value.get(k)
                else:
                    return default
                if value is None:
                    return default
            return value

        mock_config.get.side_effect = config_get

        # StopManager — 실제 인스턴스 사용
        stop_mgr = StopManager(
            stop_atr_mult=1.5,
            max_stop_pct=0.07,
            trailing_atr_mult=2.0,
            trailing_activate_pct=0.03,
        )
        MockStopMgr.return_value = stop_mgr

        from src.engine import TradingEngine

        eng = TradingEngine(mode="paper")
        eng._running = True

        yield eng
