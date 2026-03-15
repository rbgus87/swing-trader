"""E2E 매수 전체 흐름 테스트 (async).

스크리닝 -> 실시간 시세 -> 진입 조건 체크 -> 리스크 체크 ->
포지션 사이징 -> 포지션 생성 -> 매매 기록 -> 텔레그램 알림
전 과정을 실제 내부 모듈 연동으로 검증한다.
"""

from datetime import datetime
from unittest.mock import patch

import pytest

from src.models import Tick


class TestBuyFlow:
    """매수 전체 흐름 E2E 시나리오."""

    @patch("src.engine.is_market_open", return_value=True)
    @patch("src.risk.risk_manager.is_market_open", return_value=True)
    async def test_full_buy_flow(
        self,
        mock_risk_market,
        mock_engine_market,
        trading_engine,
        tmp_db,
        mock_telegram,
    ):
        """전체 매수 흐름: 후보 등록 -> 시세 수신 -> 매수 -> DB 기록 -> 알림."""
        engine = trading_engine

        # 1. 후보 종목 등록
        engine._candidates = ["005930"]

        # 기존 open 포지션 없는 상태에서 시작 (DB 초기 상태)
        initial_open = tmp_db.count_open_positions()

        # 2. 실시간 시세 수신
        tick = Tick(
            code="005930",
            price=50000,
            volume=1000,
            timestamp=datetime.now(),
        )

        # 3~7. on_price_update 호출 -> 전체 파이프라인 실행
        await engine.on_price_update(tick)

        # 6. 포지션이 DB에 생성되었는지 확인
        open_positions = tmp_db.get_open_positions()
        new_positions = [p for p in open_positions if p["code"] == "005930"]
        assert len(new_positions) >= 1, "매수 후 005930 포지션이 존재해야 함"

        new_pos = new_positions[-1]  # 마지막으로 생성된 포지션
        assert new_pos["entry_price"] == 50000
        assert new_pos["status"] == "open"
        assert new_pos["stop_price"] > 0, "손절가가 설정되어야 함"
        assert new_pos["target_price"] > 50000, "목표가는 진입가보다 높아야 함"
        assert new_pos["quantity"] > 0, "수량이 양수여야 함"

        # 7. 매매 기록이 DB에 생성되었는지 확인
        last_trade = tmp_db.get_last_trade("005930")
        assert last_trade is not None
        assert last_trade["side"] == "buy"
        assert last_trade["price"] == 50000
        assert last_trade["quantity"] > 0
        assert last_trade["reason"] == "signal"

        # 8. 텔레그램 매수 알림 전송 확인
        mock_telegram.send_buy_executed.assert_called()

    @patch("src.engine.is_market_open", return_value=True)
    @patch("src.risk.risk_manager.is_market_open", return_value=True)
    async def test_buy_rejected_when_max_positions(
        self,
        mock_risk_market,
        mock_engine_market,
        trading_engine,
        tmp_db,
        mock_telegram,
    ):
        """최대 보유 종목 수 초과 시 매수 거부."""
        engine = trading_engine

        # max_positions=5 설정에서, 5개 포지션을 미리 채움
        from src.models import Position

        for i in range(5):
            pos = Position(
                id=0,
                code=f"00{i:04d}",
                name=f"테스트{i}",
                entry_date="2026-03-15",
                entry_price=10000,
                quantity=10,
                stop_price=9300,
                target_price=10800,
                status="open",
                high_since_entry=10000,
            )
            tmp_db.insert_position(pos)

        assert tmp_db.count_open_positions() >= 5

        engine._candidates = ["999999"]

        tick = Tick(
            code="999999",
            price=10000,
            volume=500,
            timestamp=datetime.now(),
        )
        await engine.on_price_update(tick)

        # 신규 포지션이 생성되지 않아야 함
        new_positions = [
            p for p in tmp_db.get_open_positions() if p["code"] == "999999"
        ]
        assert len(new_positions) == 0, "최대 보유 종목 초과 시 매수 거부"

    @patch("src.engine.is_market_open", return_value=True)
    @patch("src.risk.risk_manager.is_market_open", return_value=True)
    async def test_buy_only_for_candidate(
        self,
        mock_risk_market,
        mock_engine_market,
        trading_engine,
        tmp_db,
    ):
        """후보 종목이 아닌 종목은 매수하지 않음."""
        engine = trading_engine
        engine._candidates = ["005930"]

        tick = Tick(
            code="000660",  # 후보가 아닌 종목
            price=120000,
            volume=200,
            timestamp=datetime.now(),
        )

        initial_count = tmp_db.count_open_positions()
        await engine.on_price_update(tick)

        assert tmp_db.count_open_positions() == initial_count

    @patch("src.engine.is_market_open", return_value=True)
    @patch("src.risk.risk_manager.is_market_open", return_value=True)
    async def test_buy_records_correct_stop_and_target(
        self,
        mock_risk_market,
        mock_engine_market,
        trading_engine,
        tmp_db,
    ):
        """매수 시 손절가/목표가가 올바르게 계산되어 기록됨."""
        engine = trading_engine
        engine._candidates = ["005930"]

        tick = Tick(
            code="005930",
            price=50000,
            volume=1000,
            timestamp=datetime.now(),
        )
        await engine.on_price_update(tick)

        positions = [
            p for p in tmp_db.get_open_positions() if p["code"] == "005930"
        ]
        assert len(positions) >= 1

        pos = positions[-1]
        # target_return=0.08 -> target_price = 50000 * 1.08 = 54000
        assert pos["target_price"] == 54000
        # stop_price: max(50000 - 50000*0.02*1.5, 50000*(1-0.07))
        #           = max(48500, 46500) = 48500
        assert pos["stop_price"] == 48500
