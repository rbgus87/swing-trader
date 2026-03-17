"""E2E 일일 한도 halt 시나리오 테스트 (async).

일일 손실 한도(-3%) 초과 시 매매 중단되는 전체 흐름을 검증한다.
"""

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from src.models import RiskCheckResult, Signal, Tick


def _seed_ohlcv(db, code="005930", price=50000):
    """테스트용 OHLCV 캐시 데이터 생성."""
    base = datetime.now() - timedelta(days=40)
    records = []
    for i in range(35):
        d = (base + timedelta(days=i)).strftime("%Y-%m-%d")
        records.append({
            "date": d, "open": price - 1000, "high": price + 1000,
            "low": price - 1500, "close": price, "volume": 100000, "amount": 0,
        })
    db.cache_ohlcv(code, records)


@patch("src.strategy.signals.check_entry_signal", return_value=True)
@patch("src.strategy.signals.calculate_signal_score", return_value=3.0)
@patch("src.strategy.signals.calculate_indicators", side_effect=lambda df, **kw: df)
class TestHaltFlow:
    """일일 한도 초과 halt E2E 시나리오."""

    @patch("src.engine.is_market_open", return_value=True)
    @patch("src.risk.risk_manager.is_market_open", return_value=True)
    async def test_halt_blocks_new_buy(
        self,
        mock_risk_market,
        mock_engine_market,
        mock_calc_ind,
        mock_score,
        mock_entry,
        trading_engine,
        tmp_db,
        mock_telegram,
    ):
        """daily_pnl_pct가 한도 초과 시 새 매수가 차단됨."""
        engine = trading_engine
        engine._risk_mgr.daily_pnl_pct = -0.031

        engine._candidates = ["005930"]
        _seed_ohlcv(tmp_db)
        initial_count = tmp_db.count_open_positions()

        tick = Tick(
            code="005930", price=50000, volume=1000, timestamp=datetime.now(),
        )
        await engine.on_price_update(tick)

        assert tmp_db.count_open_positions() == initial_count, \
            "일일 손실 한도 초과 시 매수가 차단되어야 함"
        mock_telegram.send_buy_executed.assert_not_called()

    @patch("src.engine.is_market_open", return_value=True)
    @patch("src.risk.risk_manager.is_market_open", return_value=True)
    async def test_pre_check_returns_rejected_reason(
        self,
        mock_risk_market,
        mock_engine_market,
        mock_calc_ind,
        mock_score,
        mock_entry,
        trading_engine,
        tmp_db,
    ):
        """pre_check가 '일일 손실 한도 초과' 사유로 거부."""
        engine = trading_engine
        engine._risk_mgr.daily_pnl_pct = -0.031

        signal = Signal(
            code="005930", name="삼성전자", signal_type="buy",
            price=50000, score=3.0,
        )
        result = engine._risk_mgr.pre_check(signal)

        assert result.approved is False
        assert "일일 손실 한도 초과" in result.reason

    @patch("src.engine.is_market_open", return_value=True)
    @patch("src.risk.risk_manager.is_market_open", return_value=True)
    async def test_halt_state_after_engine_halt(
        self,
        mock_risk_market,
        mock_engine_market,
        mock_calc_ind,
        mock_score,
        mock_entry,
        trading_engine,
        tmp_db,
        mock_telegram,
    ):
        """engine.halt() 호출 후 RiskManager가 halted 상태."""
        engine = trading_engine
        engine.halt()

        assert engine._risk_mgr.is_halted is True
        mock_telegram.send_halt_alert.assert_called_once()

        engine._candidates = ["005930"]
        tick = Tick(
            code="005930", price=50000, volume=1000, timestamp=datetime.now(),
        )
        await engine.on_price_update(tick)

        mock_telegram.send_buy_executed.assert_not_called()
        mock_telegram.send_sell_executed_loss.assert_not_called()

    @patch("src.engine.is_market_open", return_value=True)
    @patch("src.risk.risk_manager.is_market_open", return_value=True)
    async def test_daily_reset_resumes_trading(
        self,
        mock_risk_market,
        mock_engine_market,
        mock_calc_ind,
        mock_score,
        mock_entry,
        trading_engine,
        tmp_db,
        mock_telegram,
    ):
        """daily_reset 후 halt 상태가 해제되어 매매 재개."""
        engine = trading_engine
        engine.halt()
        assert engine._risk_mgr.is_halted is True

        engine._daily_reset()
        assert engine._risk_mgr.is_halted is False
        assert engine._risk_mgr.daily_pnl_pct == 0.0

        engine._candidates = ["005930"]
        _seed_ohlcv(tmp_db)
        tick = Tick(
            code="005930", price=50000, volume=1000, timestamp=datetime.now(),
        )
        await engine.on_price_update(tick)

        positions = [
            p for p in tmp_db.get_open_positions() if p["code"] == "005930"
        ]
        assert len(positions) >= 1, "halt 해제 후 매수가 가능해야 함"

    @patch("src.engine.is_market_open", return_value=True)
    @patch("src.risk.risk_manager.is_market_open", return_value=True)
    async def test_borderline_loss_not_halted(
        self,
        mock_risk_market,
        mock_engine_market,
        mock_calc_ind,
        mock_score,
        mock_entry,
        trading_engine,
        tmp_db,
        mock_telegram,
    ):
        """경계값: daily_pnl_pct == -0.03 (한도 정확히)이면 차단."""
        engine = trading_engine
        engine._risk_mgr.daily_pnl_pct = -0.03
        engine._candidates = ["005930"]
        _seed_ohlcv(tmp_db)

        tick = Tick(
            code="005930", price=50000, volume=1000, timestamp=datetime.now(),
        )
        await engine.on_price_update(tick)

        positions = [
            p for p in tmp_db.get_open_positions() if p["code"] == "005930"
        ]
        assert len(positions) == 0, "경계값에서도 차단되어야 함"

    @patch("src.engine.is_market_open", return_value=True)
    @patch("src.risk.risk_manager.is_market_open", return_value=True)
    async def test_just_above_limit_allows_buy(
        self,
        mock_risk_market,
        mock_engine_market,
        mock_calc_ind,
        mock_score,
        mock_entry,
        trading_engine,
        tmp_db,
    ):
        """경계값: daily_pnl_pct == -0.029 (한도 미만)이면 허용."""
        engine = trading_engine
        engine._risk_mgr.daily_pnl_pct = -0.029
        engine._candidates = ["005930"]
        _seed_ohlcv(tmp_db)

        tick = Tick(
            code="005930", price=50000, volume=1000, timestamp=datetime.now(),
        )
        await engine.on_price_update(tick)

        positions = [
            p for p in tmp_db.get_open_positions() if p["code"] == "005930"
        ]
        assert len(positions) >= 1, "한도 미만이면 매수 허용"
