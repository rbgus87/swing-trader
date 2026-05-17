"""ETF IBS 평균회귀 전략 단위 테스트."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.strategy.etf_mean_reversion import (
    ETFStrategyParams,
    check_ibs_entry,
    check_ibs_exit,
    compute_ibs,
)
from src.engine.etf_handler import ETFHandler


# ── IBS 신호 계산 ──────────────────────────────────────────────


class TestIBSSignal:
    def test_compute_ibs_middle(self):
        """종가가 고저 중간이면 IBS = 0.5."""
        assert compute_ibs(100, 80, 90) == pytest.approx(0.5)

    def test_compute_ibs_at_high(self):
        """종가 = 고가이면 IBS = 1.0."""
        assert compute_ibs(100, 80, 100) == pytest.approx(1.0)

    def test_compute_ibs_at_low(self):
        """종가 = 저가이면 IBS = 0.0."""
        assert compute_ibs(100, 80, 80) == pytest.approx(0.0)

    def test_compute_ibs_equal_high_low(self):
        """고가 = 저가이면 0.5 반환 (0 나누기 방지)."""
        assert compute_ibs(100, 100, 100) == pytest.approx(0.5)

    def test_entry_below_threshold(self):
        """IBS < threshold → 진입 신호."""
        # IBS = (81 - 80) / (100 - 80) = 0.05 < 0.1
        assert check_ibs_entry(100, 80, 81, threshold=0.1) is True

    def test_entry_above_threshold(self):
        """IBS >= threshold → 진입 없음."""
        # IBS = (90 - 80) / (100 - 80) = 0.5
        assert check_ibs_entry(100, 80, 90, threshold=0.1) is False

    def test_exit_above_threshold(self):
        """IBS > threshold → 청산 신호."""
        # IBS = (98 - 80) / (100 - 80) = 0.9
        assert check_ibs_exit(100, 80, 98, threshold=0.8, hold_days=1, max_hold=10) is True

    def test_exit_below_threshold(self):
        """IBS <= threshold → 청산 없음 (보유 계속)."""
        # IBS = 0.5
        assert check_ibs_exit(100, 80, 90, threshold=0.8, hold_days=1, max_hold=10) is False

    def test_max_hold_exit(self):
        """최대 보유일 초과 시 IBS 무관 청산."""
        assert check_ibs_exit(100, 80, 85, threshold=0.8, hold_days=10, max_hold=10) is True

    def test_max_hold_not_yet(self):
        """보유일이 max_hold 미만이면 hold_days로 강제 청산 안 됨."""
        # IBS = 0.5, hold_days=9, max_hold=10 → IBS 조건 미충족이면 False
        assert check_ibs_exit(100, 80, 90, threshold=0.8, hold_days=9, max_hold=10) is False


# ── ETFStrategyParams ──────────────────────────────────────────


class TestETFStrategyParams:
    def test_default_values(self):
        p = ETFStrategyParams()
        assert p.enabled is True
        assert p.etf_code == "069500"
        assert p.ibs_entry == pytest.approx(0.1)
        assert p.ibs_exit == pytest.approx(0.8)

    def test_from_config_empty(self):
        """etf_strategy 섹션 없으면 기본값 반환."""
        p = ETFStrategyParams.from_config({})
        assert p.enabled is True
        assert p.etf_code == "069500"

    def test_from_config_custom(self):
        cfg = {
            "etf_strategy": {
                "enabled": False,
                "etf_code": "114800",
                "ibs_entry": 0.2,
                "ibs_exit": 0.9,
                "max_hold_days": 5,
                "min_idle_cash": 500_000,
                "cost_pct": 0.0005,
            }
        }
        p = ETFStrategyParams.from_config(cfg)
        assert p.enabled is False
        assert p.etf_code == "114800"
        assert p.ibs_entry == pytest.approx(0.2)
        assert p.ibs_exit == pytest.approx(0.9)
        assert p.max_hold_days == 5
        assert p.min_idle_cash == 500_000

    def test_ibs_threshold_not_hardcoded(self):
        """IBS 임계값이 하드코딩 아닌 config 참조 확인."""
        p = ETFStrategyParams.from_config({"etf_strategy": {"ibs_entry": 0.15}})
        assert p.ibs_entry == pytest.approx(0.15)


# ── ETFHandler ────────────────────────────────────────────────


@pytest.fixture
def etf_params():
    return ETFStrategyParams(
        enabled=True,
        etf_code="069500",
        index_code="KOSPI",
        ibs_entry=0.1,
        ibs_exit=0.8,
        max_hold_days=10,
        min_idle_cash=1_000_000,
        cost_pct=0.0003,
    )


@pytest.fixture
def mock_ds():
    ds = MagicMock()
    ds.get_open_etf_position.return_value = None
    ds.insert_etf_position.return_value = 1
    ds.close_etf_position.return_value = None
    return ds


@pytest.fixture
def mock_order_mgr():
    mgr = MagicMock()
    mgr.execute_order = AsyncMock()
    result = MagicMock()
    result.success = True
    result.order_no = "ETF001"
    result.message = "OK"
    mgr.execute_order.return_value = result
    return mgr


@pytest.fixture
def mock_telegram():
    t = MagicMock()
    t.send.return_value = True
    return t


@pytest.fixture
def handler(etf_params, mock_ds, mock_order_mgr, mock_telegram):
    return ETFHandler(
        ds=mock_ds,
        order_mgr=mock_order_mgr,
        telegram=mock_telegram,
        params=etf_params,
        mode="paper",
    )


class TestETFHandler:
    def test_initial_state(self, handler):
        assert handler.is_holding is False
        assert handler.invested_amount == 0
        assert handler.has_queued_order is False

    def test_evaluate_entry_queues_buy(self, handler):
        """IBS < 0.1이면 매수 큐잉."""
        # IBS = (82 - 80) / (100 - 80) = 0.1 → 경계값이므로 False
        # IBS = (81 - 80) / (100 - 80) = 0.05 < 0.1 → True
        ohlcv = {"open": 90, "high": 100, "low": 80, "close": 81}
        with patch.object(handler, "_get_last_etf_close", return_value=50000):
            handler.evaluate_at_screening(ohlcv, idle_cash=5_000_000)

        assert handler.has_queued_order is True
        assert handler._queued_action == "buy"
        assert handler._queued_qty > 0

    def test_evaluate_no_entry_insufficient_cash(self, handler):
        """유휴 현금이 min_idle_cash 미만이면 매수 없음."""
        ohlcv = {"open": 90, "high": 100, "low": 80, "close": 81}
        with patch.object(handler, "_get_last_etf_close", return_value=50000):
            handler.evaluate_at_screening(ohlcv, idle_cash=500_000)  # < 1_000_000

        assert handler.has_queued_order is False

    def test_evaluate_no_entry_high_ibs(self, handler):
        """IBS >= 0.1이면 매수 없음."""
        # IBS = (90 - 80) / (100 - 80) = 0.5 → 진입 없음
        ohlcv = {"open": 90, "high": 100, "low": 80, "close": 90}
        with patch.object(handler, "_get_last_etf_close", return_value=50000):
            handler.evaluate_at_screening(ohlcv, idle_cash=5_000_000)

        assert handler.has_queued_order is False

    def test_evaluate_exit_when_holding(self, handler):
        """보유 중이고 IBS > 0.8이면 매도 큐잉."""
        handler._position = {
            "code": "069500",
            "entry_price": 49000,
            "qty": 10,
            "entry_date": "2026-05-01",
            "hold_days": 0,
        }
        # IBS = (98 - 80) / (100 - 80) = 0.9 > 0.8
        ohlcv = {"open": 90, "high": 100, "low": 80, "close": 98}
        handler.evaluate_at_screening(ohlcv, idle_cash=5_000_000)

        assert handler._queued_action == "sell"

    def test_evaluate_hold_when_ibs_mid(self, handler):
        """보유 중이고 IBS <= 0.8이면 큐잉 없음 (보유 유지)."""
        handler._position = {
            "code": "069500",
            "entry_price": 49000,
            "qty": 10,
            "entry_date": "2026-05-01",
            "hold_days": 0,
        }
        # IBS = 0.5 → 청산 없음
        ohlcv = {"open": 90, "high": 100, "low": 80, "close": 90}
        handler.evaluate_at_screening(ohlcv, idle_cash=5_000_000)

        assert handler.has_queued_order is False

    def test_evaluate_max_hold_exit(self, handler):
        """최대 보유일 초과 시 IBS 무관 청산 큐잉."""
        handler._position = {
            "code": "069500",
            "entry_price": 49000,
            "qty": 10,
            "entry_date": "2026-04-21",
            "hold_days": 9,  # 1 증가하면 10 = max_hold_days
        }
        # IBS = 0.5 (청산 조건 아님)이지만 hold_days(10) = max_hold(10) → 청산
        ohlcv = {"open": 90, "high": 100, "low": 80, "close": 90}
        handler.evaluate_at_screening(ohlcv, idle_cash=5_000_000)

        assert handler._queued_action == "sell"

    def test_evaluate_missing_ohlcv(self, handler):
        """OHLCV 데이터 없으면 큐잉 없음 (오류 없이 스킵)."""
        ohlcv = {"open": 0, "high": 0, "low": 0, "close": 0}
        handler.evaluate_at_screening(ohlcv, idle_cash=5_000_000)
        assert handler.has_queued_order is False

    def test_disabled_no_action(self, mock_ds, mock_order_mgr, mock_telegram):
        """enabled=False이면 아무 동작 없음."""
        params = ETFStrategyParams(enabled=False)
        h = ETFHandler(mock_ds, mock_order_mgr, mock_telegram, params, "paper")
        ohlcv = {"open": 90, "high": 100, "low": 80, "close": 81}
        h.evaluate_at_screening(ohlcv, idle_cash=5_000_000)
        assert h.has_queued_order is False

    async def test_submit_buy_paper(self, handler, mock_ds):
        """Paper 매수 제출 → DB 저장 + 포지션 설정."""
        handler._queued_action = "buy"
        handler._queued_qty = 10

        with patch.object(handler, "_get_last_etf_close", return_value=50000):
            await handler.submit_queued_order()

        assert handler.is_holding is True
        assert handler._position["qty"] == 10
        assert handler._queued_action is None
        mock_ds.insert_etf_position.assert_called_once()

    async def test_submit_sell_paper(self, handler, mock_ds):
        """Paper 매도 제출 → DB 청산 + 포지션 초기화."""
        handler._position = {
            "code": "069500",
            "entry_price": 49000,
            "qty": 10,
            "entry_date": "2026-05-01",
            "hold_days": 3,
        }
        handler._queued_action = "sell"

        with patch.object(handler, "_get_last_etf_close", return_value=50000):
            await handler.submit_queued_order()

        assert handler.is_holding is False
        assert handler._queued_action is None
        mock_ds.close_etf_position.assert_called_once()

    async def test_submit_clears_queue_on_success(self, handler):
        """주문 제출 성공 시 큐 초기화."""
        handler._queued_action = "buy"
        handler._queued_qty = 5
        with patch.object(handler, "_get_last_etf_close", return_value=50000):
            await handler.submit_queued_order()
        assert handler._queued_action is None
        assert handler._queued_qty == 0

    def test_restore_from_db_no_position(self, handler, mock_ds):
        """DB에 open ETF 포지션 없으면 복원 안 됨."""
        mock_ds.get_open_etf_position.return_value = None
        handler.restore_from_db()
        assert handler.is_holding is False

    def test_restore_from_db_with_position(self, handler, mock_ds):
        """DB에 open ETF 포지션 있으면 메모리 복원."""
        mock_ds.get_open_etf_position.return_value = {
            "code": "069500",
            "entry_price": 48500,
            "qty": 20,
            "entry_date": "2026-05-10",
            "hold_days": 2,
            "status": "open",
        }
        handler.restore_from_db()

        assert handler.is_holding is True
        assert handler._position["qty"] == 20
        assert handler._position["entry_price"] == 48500
        assert handler.invested_amount == 48500 * 20

    async def test_telegram_sent_on_buy(self, handler, mock_telegram):
        """매수 시 텔레그램 알림 전송."""
        handler._queued_action = "buy"
        handler._queued_qty = 5
        with patch.object(handler, "_get_last_etf_close", return_value=50000):
            await handler.submit_queued_order()
        mock_telegram.send.assert_called()

    async def test_telegram_sent_on_sell(self, handler, mock_telegram):
        """매도 시 텔레그램 알림 전송."""
        handler._position = {
            "code": "069500",
            "entry_price": 49000,
            "qty": 5,
            "entry_date": "2026-05-01",
            "hold_days": 3,
        }
        handler._queued_action = "sell"
        with patch.object(handler, "_get_last_etf_close", return_value=50000):
            await handler.submit_queued_order()
        mock_telegram.send.assert_called()


# ── DataStore ETF 메서드 ──────────────────────────────────────


class TestDataStoreETF:
    def test_insert_and_get_etf_position(self, tmp_db):
        """ETF 포지션 삽입 후 open 조회."""
        tmp_db.insert_etf_position("069500", 50000, 10, "2026-05-17")
        row = tmp_db.get_open_etf_position()
        assert row is not None
        assert row["code"] == "069500"
        assert row["entry_price"] == 50000
        assert row["qty"] == 10
        assert row["status"] == "open"

    def test_close_etf_position(self, tmp_db):
        """ETF 포지션 청산 후 조회 없음."""
        tmp_db.insert_etf_position("069500", 50000, 10, "2026-05-17")
        tmp_db.close_etf_position("069500", 51000, 10000, "2026-05-20", 3, "ibs_exit")
        row = tmp_db.get_open_etf_position()
        assert row is None

    def test_get_etf_stats_empty(self, tmp_db):
        """거래 없을 때 기본 통계 반환."""
        stats = tmp_db.get_etf_stats()
        assert stats["count"] == 0
        assert stats["total_pnl"] == 0

    def test_get_etf_stats_with_trades(self, tmp_db):
        """거래 있을 때 통계 계산."""
        tmp_db.insert_etf_position("069500", 50000, 10, "2026-05-10")
        tmp_db.close_etf_position("069500", 51000, 10000, "2026-05-13", 3, "ibs_exit")
        tmp_db.insert_etf_position("069500", 50000, 10, "2026-05-14")
        tmp_db.close_etf_position("069500", 49500, -5000, "2026-05-17", 3, "ibs_exit")

        stats = tmp_db.get_etf_stats()
        assert stats["count"] == 2
        assert stats["total_pnl"] == 5000
        assert stats["win_rate"] == pytest.approx(0.5)

    def test_no_etf_interference_with_positions(self, tmp_db):
        """ETF 포지션이 주식 positions 테이블을 건드리지 않음."""
        tmp_db.insert_etf_position("069500", 50000, 10, "2026-05-17")
        stock_positions = tmp_db.get_open_positions()
        assert len(stock_positions) == 0
