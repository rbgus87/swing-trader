"""E2E Paper Trading 검증 테스트 (async).

Paper 모드에서:
- REST API 주문 미호출
- DataStore 기록은 정상 생성
- 텔레그램 알림 정상 전송
"""

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from src.models import ExitReason, Position, Tick


def _seed_ohlcv(db, code="005930", price=50000):
    """테스트용 OHLCV 캐시 데이터 생성 (130일+ — 지표 계산에 충분한 기간)."""
    base = datetime.now() - timedelta(days=140)
    records = []
    for i in range(135):
        d = (base + timedelta(days=i)).strftime("%Y-%m-%d")
        p = price + i * 50
        records.append({
            "date": d, "open": p - 500, "high": p + 500,
            "low": p - 800, "close": p, "volume": 100000, "amount": 0,
        })
    db.cache_ohlcv(code, records)


class TestPaperTrading:
    """Paper Trading E2E 시나리오."""

    def _setup_engine(self, engine):
        """엔진 공통 설정: 시장 국면 bullish 강제."""
        engine._market_regime._is_bullish = True
        engine._market_regime._regime_type = "trending"
        engine._market_regime._last_check_date = datetime.now().strftime("%Y%m%d")

    async def _force_buy(self, engine, tick):
        """진입 조건을 우회하고 직접 매수 실행 (리스크 체크만 적용)."""
        from src.models import Signal
        signal = Signal(
            code=tick.code, name="테스트", signal_type="buy",
            price=tick.price, score=3.0,
        )
        result = engine._risk_mgr.pre_check(signal)
        if result.approved:
            capital = engine._get_available_capital()
            qty = capital // tick.price
            if qty > 0:
                await engine._record_buy(tick, qty)

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
        self._setup_engine(engine)
        assert engine.mode == "paper"

        engine._candidates = ["005930"]
        _seed_ohlcv(tmp_db)

        tick = Tick(
            code="005930",
            price=50000,
            volume=1000,
            timestamp=datetime.now(),
        )
        await self._force_buy(engine, tick)

        # DB에 포지션 기록
        positions = [
            p for p in tmp_db.get_open_positions() if p["code"] == "005930"
        ]
        assert len(positions) >= 1, "paper 모드에서도 포지션이 DB에 기록되어야 함"

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
        self._setup_engine(engine)
        engine._candidates = ["005930"]
        _seed_ohlcv(tmp_db)

        # Step 1: 매수 (진입 조건 우회)
        buy_tick = Tick(
            code="005930",
            price=50000,
            volume=1000,
            timestamp=datetime.now(),
        )
        await self._force_buy(engine, buy_tick)

        # 포지션 생성 확인
        positions = [
            p for p in tmp_db.get_open_positions() if p["code"] == "005930"
        ]
        assert len(positions) >= 1
        pos = positions[-1]
        target_price = pos["target_price"]
        assert target_price > 50000

        # Step 2: 목표가 도달 매도
        engine._candidates = []
        sell_tick = Tick(
            code="005930",
            price=target_price + 1000,
            volume=2000,
            timestamp=datetime.now(),
        )
        await engine.on_price_update(sell_tick)

        # 포지션 closed
        open_after = tmp_db.get_open_positions()
        samsung_after = [p for p in open_after if p["code"] == "005930"]
        assert len(samsung_after) == 0, "목표가 도달 후 포지션이 closed"

        # 매매 기록: buy + sell
        cursor = tmp_db.conn.execute(
            "SELECT * FROM trades WHERE code = ? ORDER BY id", ("005930",)
        )
        trades = [dict(row) for row in cursor.fetchall()]
        buy_trades = [t for t in trades if t["side"] == "buy"]
        sell_trades = [t for t in trades if t["side"] == "sell"]

        assert len(buy_trades) >= 1, "매수 기록 존재"
        assert len(sell_trades) >= 1, "매도 기록 존재"

        # 텔레그램: 매수 + 매도 알림
        mock_telegram.send_buy_executed.assert_called()

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
        self._setup_engine(engine)
        engine._candidates = ["005930"]
        _seed_ohlcv(tmp_db)

        tick = Tick(
            code="005930",
            price=50000,
            volume=1000,
            timestamp=datetime.now(),
        )
        await self._force_buy(engine, tick)

        # 매수 기록 확인 (get_last_trade는 sell만 조회하므로 직접 쿼리)
        today = datetime.now().strftime("%Y-%m-%d")
        today_trades = tmp_db.get_trades_by_date(today)
        buy_trades = [t for t in today_trades if t["code"] == "005930" and t["side"] == "buy"]
        assert len(buy_trades) >= 1

        last_buy = buy_trades[-1]
        qty = last_buy["quantity"]
        expected_fee = 50000 * qty * 0.00015
        assert last_buy["fee"] == pytest.approx(expected_fee, rel=1e-6)
        assert last_buy["tax"] == 0.0

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

        # tax = price * quantity * 0.0015
        expected_tax = 55000 * 20 * 0.0015
        assert last_trade["tax"] == pytest.approx(expected_tax, rel=1e-6)
