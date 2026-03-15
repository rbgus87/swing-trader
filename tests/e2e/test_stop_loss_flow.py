"""E2E 손절 시나리오 테스트 (async).

보유 포지션이 손절가 이하로 하락 시:
포지션 종료 -> 매매 기록(pnl 음수) -> 텔레그램 손실 알림
전 과정을 실제 내부 모듈 연동으로 검증한다.
"""

from datetime import datetime
from unittest.mock import patch

import pytest

from src.models import Position, Tick


class TestStopLossFlow:
    """손절 E2E 시나리오."""

    @patch("src.engine.is_market_open", return_value=True)
    async def test_stop_loss_closes_position(
        self,
        mock_market,
        trading_engine,
        populated_db,
        mock_telegram,
    ):
        """손절가 이하 시 포지션 종료, 매매 기록, 텔레그램 손실 알림."""
        engine = trading_engine
        engine._ds = populated_db

        # 초기 상태: open 포지션 존재
        open_before = populated_db.get_open_positions()
        samsung_pos = [p for p in open_before if p["code"] == "005930"]
        assert len(samsung_pos) == 1
        assert samsung_pos[0]["stop_price"] == 47000

        # 손절가 이하로 시세 수신
        tick = Tick(
            code="005930",
            price=46500,
            volume=5000,
            timestamp=datetime.now(),
        )
        await engine.on_price_update(tick)

        # 포지션이 closed로 변경
        open_after = populated_db.get_open_positions()
        samsung_after = [p for p in open_after if p["code"] == "005930"]
        assert len(samsung_after) == 0, "손절 후 005930은 open 포지션이 없어야 함"

        # 매매 기록 확인
        last_trade = populated_db.get_last_trade("005930")
        assert last_trade is not None
        assert last_trade["side"] == "sell"
        assert last_trade["reason"] == "stop_loss"
        assert last_trade["pnl"] < 0, "손절 매매의 PnL은 음수"
        assert last_trade["price"] == 46500

        # pnl 계산 검증: (46500 - 50000) * 20 = -70000
        expected_pnl = (46500 - 50000) * 20
        assert last_trade["pnl"] == expected_pnl

        # 텔레그램 손실 알림 확인
        mock_telegram.send_sell_executed_loss.assert_called_once()

    @patch("src.engine.is_market_open", return_value=True)
    async def test_stop_loss_does_not_trigger_above_stop(
        self,
        mock_market,
        trading_engine,
        populated_db,
        mock_telegram,
    ):
        """손절가 초과 시세에서는 포지션이 유지됨."""
        engine = trading_engine
        engine._ds = populated_db

        # 손절가(47000) 위의 가격
        tick = Tick(
            code="005930",
            price=48000,
            volume=3000,
            timestamp=datetime.now(),
        )
        await engine.on_price_update(tick)

        # 포지션 유지
        open_positions = populated_db.get_open_positions()
        samsung = [p for p in open_positions if p["code"] == "005930"]
        assert len(samsung) == 1, "손절가 위에서는 포지션이 유지되어야 함"
        assert samsung[0]["status"] == "open"

        # 매도 알림 미전송
        mock_telegram.send_sell_executed_loss.assert_not_called()

    @patch("src.engine.is_market_open", return_value=True)
    async def test_target_reached_closes_with_profit(
        self,
        mock_market,
        trading_engine,
        populated_db,
        mock_telegram,
    ):
        """목표가 도달 시 수익 매도."""
        engine = trading_engine
        engine._ds = populated_db

        tick = Tick(
            code="005930",
            price=55000,
            volume=2000,
            timestamp=datetime.now(),
        )
        await engine.on_price_update(tick)

        # 포지션 closed
        open_after = populated_db.get_open_positions()
        samsung_after = [p for p in open_after if p["code"] == "005930"]
        assert len(samsung_after) == 0

        # 매매 기록: 수익
        last_trade = populated_db.get_last_trade("005930")
        assert last_trade["side"] == "sell"
        assert last_trade["reason"] == "target_reached"
        assert last_trade["pnl"] > 0

        # (55000 - 50000) * 20 = 100000
        expected_pnl = (55000 - 50000) * 20
        assert last_trade["pnl"] == expected_pnl

        # 수익 알림
        mock_telegram.send_sell_executed_profit.assert_called_once()

    @patch("src.engine.is_market_open", return_value=True)
    async def test_multiple_positions_independent(
        self,
        mock_market,
        trading_engine,
        populated_db,
        mock_telegram,
    ):
        """복수 보유 종목에서 한 종목만 손절되어도 다른 종목은 영향 없음."""
        engine = trading_engine
        engine._ds = populated_db

        # 005930만 손절 (stop_price=47000 이하)
        tick = Tick(
            code="005930",
            price=46000,
            volume=5000,
            timestamp=datetime.now(),
        )
        await engine.on_price_update(tick)

        # 005930은 closed, 000660은 여전히 open
        open_positions = populated_db.get_open_positions()
        codes = [p["code"] for p in open_positions]
        assert "005930" not in codes
        assert "000660" in codes
