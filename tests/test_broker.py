"""브로커 레이어 테스트.

REST/WebSocket 기반 Mock 테스트.
"""

import inspect
import time
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.broker.rate_limiter import RateLimiter
from src.broker.tr_codes import (
    ERR_CODES,
    FID_CHANGE_RATE,
    FID_CURRENT_PRICE,
    FID_VOLUME,
    ORDER_BUY,
    ORDER_BUY_CANCEL,
    ORDER_SELL,
    ORDER_SELL_CANCEL,
    PRICE_LIMIT,
    PRICE_MARKET,
    SCREEN_ACCOUNT,
    SCREEN_OHLCV,
    SCREEN_ORDER,
    SCREEN_REALTIME,
    TR_OPT10080,
    TR_OPT10081,
    TR_OPT10001,
    TR_OPT20006,
    TR_OPTKWFID,
    TR_OPW00004,
    TR_OPW00018,
    # REST API 상수
    API_STOCK_ORDER,
    API_STOCK_CANCEL,
    API_STOCK_PRICE,
    API_STOCK_DAILY,
    API_STOCK_MINUTE,
    API_ACCOUNT_BALANCE,
    EP_ORDER,
    EP_STOCK,
    EP_CHART,
    EP_ACCOUNT,
    WS_TYPE_TICK,
    WS_TYPE_ORDER,
)
from src.models import Order, OrderResult, Tick


# ──────────────────────────────────────────────
# TR 코드 상수 검증
# ──────────────────────────────────────────────


class TestTrCodes:
    """TR 코드 및 상수 검증 테스트."""

    def test_data_tr_codes(self):
        """데이터 조회 TR 코드 값 검증."""
        assert TR_OPT10081 == "OPT10081"
        assert TR_OPT10080 == "OPT10080"
        assert TR_OPT10001 == "OPT10001"
        assert TR_OPT20006 == "OPT20006"
        assert TR_OPTKWFID == "OPTKWFID"

    def test_account_tr_codes(self):
        """계좌 조회 TR 코드 값 검증."""
        assert TR_OPW00018 == "OPW00018"
        assert TR_OPW00004 == "OPW00004"

    def test_order_types(self):
        """주문 구분 상수 검증."""
        assert ORDER_BUY == 1
        assert ORDER_SELL == 2
        assert ORDER_BUY_CANCEL == 3
        assert ORDER_SELL_CANCEL == 4

    def test_price_types(self):
        """호가 구분 상수 검증."""
        assert PRICE_LIMIT == "00"
        assert PRICE_MARKET == "03"

    def test_fid_constants(self):
        """실시간 FID 상수 검증."""
        assert FID_CURRENT_PRICE == 10
        assert FID_VOLUME == 15
        assert FID_CHANGE_RATE == 12

    def test_screen_numbers(self):
        """화면번호 상수 검증."""
        assert SCREEN_OHLCV == "0101"
        assert SCREEN_ORDER == "0201"
        assert SCREEN_REALTIME == "0301"
        assert SCREEN_ACCOUNT == "0401"

    def test_err_codes(self):
        """오류 코드 딕셔너리 검증."""
        assert ERR_CODES[0] == "정상"
        assert ERR_CODES[-100] == "사용자 정보 교환 실패"
        assert ERR_CODES[-101] == "서버 접속 실패"
        assert ERR_CODES[-102] == "버전 처리 실패"
        assert ERR_CODES[-200] == "시세 제한 초과"
        assert ERR_CODES[-201] == "조회 과부하"

    def test_rest_api_ids(self):
        """REST API ID 상수 검증."""
        assert API_STOCK_ORDER == "kt10000"
        assert API_STOCK_CANCEL == "kt10001"
        assert API_STOCK_PRICE == "ka10001"
        assert API_STOCK_DAILY == "ka10002"
        assert API_STOCK_MINUTE == "ka10003"
        assert API_ACCOUNT_BALANCE == "ka10070"

    def test_rest_endpoints(self):
        """REST 엔드포인트 상수 검증."""
        assert EP_ORDER == "/api/dostk/ordr"
        assert EP_STOCK == "/api/dostk/stkinfo"
        assert EP_CHART == "/api/dostk/chart"
        assert EP_ACCOUNT == "/api/dostk/acnt"

    def test_ws_types(self):
        """WebSocket 실시간 타입 상수 검증."""
        assert WS_TYPE_TICK == "0B"
        assert WS_TYPE_ORDER == "00"


# ──────────────────────────────────────────────
# RateLimiter 테스트
# ──────────────────────────────────────────────


class TestRateLimiter:
    """RateLimiter 동작 테스트."""

    def test_can_call_within_limit(self):
        """제한 이내 호출 시 can_call이 True를 반환."""
        limiter = RateLimiter(max_calls=3, period=1.0)
        assert limiter.can_call() is True

    def test_can_call_at_limit(self):
        """제한에 도달하면 can_call이 False를 반환."""
        limiter = RateLimiter(max_calls=2, period=1.0)
        limiter.wait()
        limiter.wait()
        assert limiter.can_call() is False

    def test_wait_records_call(self):
        """wait() 호출 후 내부 deque에 기록이 남는다."""
        limiter = RateLimiter(max_calls=5, period=1.0)
        limiter.wait()
        assert len(limiter._calls) == 1

    def test_wait_multiple_calls(self):
        """여러 번 wait() 호출 시 올바르게 기록된다."""
        limiter = RateLimiter(max_calls=5, period=1.0)
        for _ in range(3):
            limiter.wait()
        assert len(limiter._calls) == 3

    def test_wait_blocks_when_exceeded(self):
        """제한 초과 시 wait()가 대기한다."""
        limiter = RateLimiter(max_calls=2, period=0.1)
        limiter.wait()
        limiter.wait()

        start = time.monotonic()
        limiter.wait()  # 대기 후 호출
        elapsed = time.monotonic() - start

        # 최소 대기 시간이 있어야 함 (약간의 여유)
        assert elapsed >= 0.05

    def test_old_calls_purged(self):
        """period 경과 후 오래된 호출이 제거된다."""
        limiter = RateLimiter(max_calls=2, period=0.1)
        limiter.wait()
        limiter.wait()
        assert limiter.can_call() is False

        time.sleep(0.15)
        assert limiter.can_call() is True


# ──────────────────────────────────────────────
# KiwoomAPI 테스트
# ──────────────────────────────────────────────


class TestKiwoomAPI:
    """KiwoomAPI REST/WebSocket 래퍼 테스트."""

    def _make_api(self):
        """Mock REST/WS 클라이언트로 KiwoomAPI 생성."""
        from src.broker.kiwoom_api import KiwoomAPI

        api = KiwoomAPI(
            base_url="https://test.api.com",
            ws_url="wss://test.ws.com",
            appkey="test_appkey",
            secretkey="test_secretkey",
        )
        return api

    @pytest.mark.asyncio
    async def test_connect_rest_only(self):
        """connect(use_websocket=False) 시 REST 인증만."""
        api = self._make_api()
        api._rest = AsyncMock()
        api._rest.authenticate = AsyncMock(return_value="test_token")

        await api.connect(use_websocket=False)

        api._rest.authenticate.assert_awaited_once()
        assert api.connected is True

    @pytest.mark.asyncio
    async def test_connect_with_websocket(self):
        """connect(use_websocket=True) 시 REST + WS 연결."""
        api = self._make_api()
        api._rest = AsyncMock()
        api._rest.authenticate = AsyncMock(return_value="test_token")
        api._rest.get_ws_key = AsyncMock(return_value="test_ws_key")

        with patch("src.broker.kiwoom_api.KiwoomWebSocketClient") as MockWS:
            mock_ws_instance = AsyncMock()
            MockWS.return_value = mock_ws_instance

            await api.connect(use_websocket=True)

            api._rest.authenticate.assert_awaited_once()
            api._rest.get_ws_key.assert_awaited_once()
            MockWS.assert_called_once_with("wss://test.ws.com", "test_ws_key")
            mock_ws_instance.connect.assert_awaited_once()
            assert api.connected is True

    @pytest.mark.asyncio
    async def test_disconnect(self):
        """disconnect 시 WS + REST 종료."""
        api = self._make_api()
        api._rest = AsyncMock()
        api._ws = AsyncMock()
        api._connected = True

        await api.disconnect()

        api._ws.disconnect.assert_awaited_once()
        api._rest.close.assert_awaited_once()
        assert api.connected is False

    @pytest.mark.asyncio
    async def test_send_order(self):
        """send_order가 REST 클라이언트에 위임."""
        api = self._make_api()
        api._rest = AsyncMock()
        api._rest.send_order = AsyncMock(
            return_value={"return_code": 0, "ord_no": "12345"}
        )

        result = await api.send_order(
            code="005930", qty=10, price=0,
            order_type=ORDER_BUY, hoga_type=PRICE_MARKET,
            account="1234567890"
        )

        assert result["return_code"] == 0
        assert result["ord_no"] == "12345"
        api._rest.send_order.assert_awaited_once_with(
            "005930", 10, 0, ORDER_BUY, PRICE_MARKET, "1234567890"
        )

    @pytest.mark.asyncio
    async def test_subscribe_realtime(self):
        """subscribe_realtime이 WS 클라이언트에 위임."""
        api = self._make_api()
        api._ws = AsyncMock()

        await api.subscribe_realtime(["005930", "000660"])

        api._ws.subscribe.assert_awaited_once_with(["005930", "000660"], "0B")

    @pytest.mark.asyncio
    async def test_subscribe_realtime_no_ws(self):
        """WS 미연결 시 subscribe_realtime은 아무것도 안 함."""
        api = self._make_api()
        api._ws = None

        # 에러 없이 실행되어야 함
        await api.subscribe_realtime(["005930"])

    @pytest.mark.asyncio
    async def test_unsubscribe_realtime(self):
        """unsubscribe_realtime이 WS 클라이언트에 위임."""
        api = self._make_api()
        api._ws = AsyncMock()

        await api.unsubscribe_realtime(["005930"])

        api._ws.unsubscribe.assert_awaited_once_with(["005930"], "0B")

    @pytest.mark.asyncio
    async def test_get_daily_ohlcv(self):
        """get_daily_ohlcv가 REST 클라이언트에 위임."""
        api = self._make_api()
        api._rest = AsyncMock()
        api._rest.get_daily_ohlcv = AsyncMock(return_value=[{"date": "20240101"}])

        result = await api.get_daily_ohlcv("005930", "20240101", "20240131")

        assert result == [{"date": "20240101"}]
        api._rest.get_daily_ohlcv.assert_awaited_once_with(
            "005930", "20240101", "20240131"
        )

    @pytest.mark.asyncio
    async def test_get_account_info(self):
        """get_account_info가 REST 클라이언트에 위임."""
        api = self._make_api()
        api._rest = AsyncMock()
        api._rest.get_account_balance = AsyncMock(
            return_value={"balance": 1000000}
        )

        result = await api.get_account_info("1234567890")

        assert result["balance"] == 1000000

    @pytest.mark.asyncio
    async def test_cancel_order(self):
        """cancel_order가 REST 클라이언트에 위임."""
        api = self._make_api()
        api._rest = AsyncMock()
        api._rest.cancel_order = AsyncMock(
            return_value={"return_code": 0}
        )

        result = await api.cancel_order("ORD001", "005930", 10, "1234567890")

        assert result["return_code"] == 0


# ──────────────────────────────────────────────
# OrderManager 테스트
# ──────────────────────────────────────────────


class TestOrderManager:
    """OrderManager async Mock 기반 테스트."""

    def _make_manager(self):
        """Mock KiwoomAPI로 OrderManager 생성."""
        from src.broker.order_manager import OrderManager

        mock_api = AsyncMock()
        manager = OrderManager(kiwoom_api=mock_api, account="1234567890")
        return manager, mock_api

    @pytest.mark.asyncio
    async def test_execute_order_success(self):
        """주문 성공 시 OrderResult.success=True."""
        manager, mock_api = self._make_manager()
        mock_api.send_order = AsyncMock(
            return_value={"return_code": 0, "ord_no": "ORD001"}
        )

        result = await manager.execute_order(
            code="005930",
            qty=10,
            price=0,
            order_type=ORDER_BUY,
            hoga_type=PRICE_MARKET,
        )

        assert result.success is True
        assert result.order_no == "ORD001"
        mock_api.send_order.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_execute_order_failure(self):
        """주문 실패 시 OrderResult.success=False."""
        manager, mock_api = self._make_manager()
        mock_api.send_order = AsyncMock(
            return_value={"return_code": -1, "ord_no": ""}
        )

        result = await manager.execute_order(
            code="005930",
            qty=10,
            price=0,
            order_type=ORDER_BUY,
        )

        assert result.success is False

    @pytest.mark.asyncio
    async def test_execute_order_adds_to_pending(self):
        """주문 성공 시 미체결 목록에 추가된다."""
        manager, mock_api = self._make_manager()
        mock_api.send_order = AsyncMock(
            return_value={"return_code": 0, "ord_no": "ORD001"}
        )

        await manager.execute_order(
            code="005930", qty=10, price=0, order_type=ORDER_BUY
        )

        pending = manager.get_pending_orders()
        assert len(pending) == 1
        assert pending[0].code == "005930"

    @pytest.mark.asyncio
    async def test_execute_order_invalid_code(self):
        """잘못된 종목코드 형식이면 실패."""
        manager, mock_api = self._make_manager()

        result = await manager.execute_order(
            code="12345",  # 5자리 — 잘못된 형식
            qty=10,
            price=0,
            order_type=ORDER_BUY,
        )

        assert result.success is False
        assert "종목코드" in result.message
        mock_api.send_order.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_execute_order_zero_qty(self):
        """주문수량이 0이면 실패."""
        manager, mock_api = self._make_manager()

        result = await manager.execute_order(
            code="005930", qty=0, price=0, order_type=ORDER_BUY
        )

        assert result.success is False
        assert "양수" in result.message

    @pytest.mark.asyncio
    async def test_execute_order_negative_price(self):
        """주문가격이 음수이면 실패."""
        manager, mock_api = self._make_manager()

        result = await manager.execute_order(
            code="005930", qty=10, price=-100, order_type=ORDER_BUY
        )

        assert result.success is False
        assert "0 이상" in result.message

    @pytest.mark.asyncio
    async def test_cancel_order_not_found(self):
        """없는 주문번호 취소 시 False 반환."""
        manager, mock_api = self._make_manager()
        result = await manager.cancel_order("nonexistent")
        assert result is False

    @pytest.mark.asyncio
    async def test_cancel_order_success(self):
        """미체결 주문 취소 성공 테스트."""
        manager, mock_api = self._make_manager()
        mock_api.send_order = AsyncMock(
            return_value={"return_code": 0, "ord_no": "ORD001"}
        )
        mock_api.cancel_order = AsyncMock(
            return_value={"return_code": 0}
        )

        result = await manager.execute_order(
            code="005930", qty=10, price=0, order_type=ORDER_BUY
        )
        order_no = result.order_no

        assert await manager.cancel_order(order_no) is True

    @pytest.mark.asyncio
    async def test_on_chejan_removes_filled_order(self):
        """체결 이벤트로 미체결 목록에서 제거된다."""
        manager, mock_api = self._make_manager()
        mock_api.send_order = AsyncMock(
            return_value={"return_code": 0, "ord_no": "ORD001"}
        )

        result = await manager.execute_order(
            code="005930", qty=10, price=0, order_type=ORDER_BUY
        )
        order_no = result.order_no

        await manager.on_chejan(
            {"order_no": order_no, "status": "체결", "code": "005930"}
        )

        assert len(manager.get_pending_orders()) == 0

    def test_risk_check_required_comment_exists(self):
        """execute_order 메서드에 RISK_CHECK_REQUIRED 주석 존재 확인."""
        from src.broker.order_manager import OrderManager

        source = inspect.getsource(OrderManager)
        assert "RISK_CHECK_REQUIRED" in source


# ──────────────────────────────────────────────
# RealtimeDataManager 테스트
# ──────────────────────────────────────────────


class TestRealtimeDataManager:
    """RealtimeDataManager async Mock 기반 테스트."""

    def _make_manager(self):
        """Mock KiwoomAPI로 RealtimeDataManager 생성."""
        from src.broker.realtime_data import RealtimeDataManager

        mock_api = AsyncMock()
        manager = RealtimeDataManager(kiwoom_api=mock_api)
        return manager, mock_api

    @pytest.mark.asyncio
    async def test_subscribe(self):
        """종목 구독 시 subscribed_codes에 추가된다."""
        manager, mock_api = self._make_manager()
        await manager.subscribe("005930")

        assert "005930" in manager.subscribed_codes
        mock_api.subscribe_realtime.assert_awaited_once_with(["005930"])

    @pytest.mark.asyncio
    async def test_subscribe_duplicate(self):
        """이미 구독 중인 종목 재구독 시 중복 호출 안 함."""
        manager, mock_api = self._make_manager()
        await manager.subscribe("005930")
        await manager.subscribe("005930")

        assert mock_api.subscribe_realtime.await_count == 1

    @pytest.mark.asyncio
    async def test_unsubscribe(self):
        """종목 해제 시 subscribed_codes에서 제거된다."""
        manager, mock_api = self._make_manager()
        await manager.subscribe("005930")
        await manager.unsubscribe("005930")

        assert "005930" not in manager.subscribed_codes
        mock_api.unsubscribe_realtime.assert_awaited_once_with(["005930"])

    @pytest.mark.asyncio
    async def test_unsubscribe_not_subscribed(self):
        """구독하지 않은 종목 해제 시 아무 일도 안 함."""
        manager, mock_api = self._make_manager()
        await manager.unsubscribe("005930")

        mock_api.unsubscribe_realtime.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_subscribe_list(self):
        """복수 종목 구독 테스트."""
        manager, mock_api = self._make_manager()
        codes = ["005930", "000660", "035720"]
        await manager.subscribe_list(codes)

        assert manager.subscribed_codes == set(codes)

    def test_get_current_price_default(self):
        """데이터 없는 종목 현재가 조회 시 0 반환."""
        manager, _ = self._make_manager()
        assert manager.get_current_price("005930") == 0

    def test_on_tick_updates_price(self):
        """틱 수신 시 현재가가 업데이트된다."""
        manager, _ = self._make_manager()
        tick = Tick(
            code="005930",
            price=72000,
            volume=1000,
            timestamp=datetime.now(),
        )
        manager.on_tick(tick)

        assert manager.get_current_price("005930") == 72000

    def test_on_tick_multiple_codes(self):
        """여러 종목 틱 수신 시 각각 현재가 관리."""
        manager, _ = self._make_manager()
        now = datetime.now()

        manager.on_tick(Tick(code="005930", price=72000, volume=100, timestamp=now))
        manager.on_tick(Tick(code="000660", price=130000, volume=200, timestamp=now))

        assert manager.get_current_price("005930") == 72000
        assert manager.get_current_price("000660") == 130000

    @pytest.mark.asyncio
    async def test_unsubscribe_clears_price(self):
        """구독 해제 시 현재가 캐시도 제거된다."""
        manager, _ = self._make_manager()
        await manager.subscribe("005930")
        manager.on_tick(
            Tick(code="005930", price=72000, volume=100, timestamp=datetime.now())
        )

        await manager.unsubscribe("005930")
        assert manager.get_current_price("005930") == 0
