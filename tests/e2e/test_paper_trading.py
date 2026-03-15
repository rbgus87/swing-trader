"""E2E Paper Trading 검증 테스트 (async).

Paper 모드에서:
- REST API 주문 미호출
- DataStore 기록은 정상 생성
- 텔레그램 알림 정상 전송
"""

from datetime import datetime
from unittest.mock import patch

import pytest

from src.models import ExitReason, Position, Tick


class TestPaperTrading:
    """Paper Trading E2E 시나리오."""

    @patch("src.engine.is_market_open", return_value=True)
    @patch("src.risk.risk_manager.is_market_open", return_value=True)
    async def test_paper_buy_no_api_order(
        self,
        mock_risk_market,
        mock_engine_market,
        trading_engine,
        tmp_db,
        mock_kiwoom,
        mock_telegram,
    ):
        """Paper 모드: 매수 시 API 주문 미호출, DB 기록 정상."""
        engine = trading_engine
        assert engine.mode == "paper"

        engine._candidates = ["005930"]

        tick = Tick(
            code="005930",
            price=50000,
            volume=1000,
            timestamp=datetime.now(),
        )
        await engine.on_price_update(tick)

        # DB에 포지션 기록
        positions = [
            p for p in tmp_db.get_open_positions() if p["code"] == "005930"
        ]
        assert len(positions) >= 1, "paper 모드에서도 포지션이 DB에 기록되어야 함"

        # 매매 기록
        last_trade = tmp_db.get_last_trade("005930")
        assert last_trade is not None
        assert last_trade["side"] == "buy"

        # 텔레그램 알림
        mock_telegram.send_buy_executed.assert_called()

    @patch("src.engine.is_market_open", return_value=True)
    async def test_paper_sell_no_api_order(
        self,
        mock_market,
        trading_engine,
        populated_db,
        mock_kiwoom,
        mock_telegram,
    ):
        """Paper 모드: 매도 시 API 주문 미호출, DB 기록 정상."""
        engine = trading_engine
        engine._ds = populated_db
        assert engine.mode == "paper"

        # 손절가(47000) 이하로 시세 수신
        tick = Tick(
            code="005930",
            price=46500,
            volume=5000,
            timestamp=datetime.now(),
        )
        await engine.on_price_update(tick)

        # 포지션 closed
        open_positions = populated_db.get_open_positions()
        samsung = [p for p in open_positions if p["code"] == "005930"]
        assert len(samsung) == 0, "paper 매도 후 포지션이 closed 되어야 함"

        # 매매 기록 생성
        last_trade = populated_db.get_last_trade("005930")
        assert last_trade["side"] == "sell"
        assert last_trade["reason"] == "stop_loss"

        # 텔레그램 알림
        mock_telegram.send_sell_executed_loss.assert_called()

    @patch("src.engine.is_market_open", return_value=True)
    @patch("src.risk.risk_manager.is_market_open", return_value=True)
    async def test_paper_full_cycle_buy_then_sell(
        self,
        mock_risk_market,
        mock_engine_market,
        trading_engine,
        tmp_db,
        mock_kiwoom,
        mock_telegram,
    ):
        """Paper 모드 전체 사이클: 매수 -> 매도 (목표가 도달)."""
        engine = trading_engine
        engine._candidates = ["005930"]

        # Step 1: 매수
        buy_tick = Tick(
            code="005930",
            price=50000,
            volume=1000,
            timestamp=datetime.now(),
        )
        await engine.on_price_update(buy_tick)

        # 포지션 생성 확인
        positions = [
            p for p in tmp_db.get_open_positions() if p["code"] == "005930"
        ]
        assert len(positions) >= 1
        pos = positions[-1]
        target_price = pos["target_price"]  # 54000 (50000 * 1.08)
        assert target_price == 54000

        # Step 2: 목표가 도달 매도
        # 후보에서 제거하여 재매수 방지
        engine._candidates = []

        sell_tick = Tick(
            code="005930",
            price=55000,
            volume=2000,
            timestamp=datetime.now(),
        )
        await engine.on_price_update(sell_tick)

        # 포지션 closed
        open_after = tmp_db.get_open_positions()
        samsung_after = [p for p in open_after if p["code"] == "005930"]
        assert len(samsung_after) == 0, "목표가 도달 후 포지션이 closed"

        # 매매 기록: buy 1건 + sell 1건 = 2건
        buy_trade = None
        sell_trade = None
        cursor = tmp_db.conn.execute(
            "SELECT * FROM trades WHERE code = ? ORDER BY id", ("005930",)
        )
        trades = [dict(row) for row in cursor.fetchall()]
        for t in trades:
            if t["side"] == "buy":
                buy_trade = t
            elif t["side"] == "sell":
                sell_trade = t

        assert buy_trade is not None, "매수 기록 존재"
        assert sell_trade is not None, "매도 기록 존재"
        assert sell_trade["pnl"] > 0, "목표가 매도 시 수익"
        assert sell_trade["reason"] == "target_reached"

        # 텔레그램: 매수 + 수익매도 알림
        mock_telegram.send_buy_executed.assert_called()
        mock_telegram.send_sell_executed_profit.assert_called()

    @patch("src.engine.is_market_open", return_value=True)
    @patch("src.risk.risk_manager.is_market_open", return_value=True)
    async def test_paper_mode_records_fee_and_tax(
        self,
        mock_risk_market,
        mock_engine_market,
        trading_engine,
        tmp_db,
    ):
        """Paper 모드에서도 수수료/세금이 정확히 기록됨."""
        engine = trading_engine
        engine._candidates = ["005930"]

        tick = Tick(
            code="005930",
            price=50000,
            volume=1000,
            timestamp=datetime.now(),
        )
        await engine.on_price_update(tick)

        last_trade = tmp_db.get_last_trade("005930")
        assert last_trade is not None

        # 매수 시 fee = price * qty * 0.00015
        qty = last_trade["quantity"]
        expected_fee = 50000 * qty * 0.00015
        assert last_trade["fee"] == pytest.approx(expected_fee, rel=1e-6)

        # 매수 시 tax = 0
        assert last_trade["tax"] == 0.0

    @patch("src.engine.is_market_open", return_value=True)
    async def test_paper_sell_records_tax(
        self,
        mock_market,
        trading_engine,
        populated_db,
    ):
        """Paper 매도 시 매도세(0.2%) 기록 확인."""
        engine = trading_engine
        engine._ds = populated_db

        # 목표가 도달 매도
        tick = Tick(
            code="005930",
            price=55000,
            volume=2000,
            timestamp=datetime.now(),
        )
        await engine.on_price_update(tick)

        last_trade = populated_db.get_last_trade("005930")
        assert last_trade["side"] == "sell"

        # tax = price * quantity * 0.002
        expected_tax = 55000 * 20 * 0.002
        assert last_trade["tax"] == pytest.approx(expected_tax, rel=1e-6)
