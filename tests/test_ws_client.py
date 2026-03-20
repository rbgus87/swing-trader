import pytest
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime
from src.broker.ws_client import KiwoomWebSocketClient
from src.models import Tick


class TestWebSocketConnection:
    @pytest.fixture
    def ws_client(self):
        return KiwoomWebSocketClient("wss://test.api.com/ws", access_token="test_token")

    @pytest.mark.asyncio
    async def test_connect(self, ws_client):
        """연결 성공."""
        mock_ws = AsyncMock()
        with patch("src.broker.ws_client.websockets") as mock_websockets:
            mock_websockets.connect = AsyncMock(return_value=mock_ws)
            await ws_client.connect()

        assert ws_client._running is True
        assert ws_client._ws is mock_ws

    @pytest.mark.asyncio
    async def test_disconnect(self, ws_client):
        """연결 종료."""
        ws_client._ws = AsyncMock()
        ws_client._running = True
        ws_client._listen_task = asyncio.create_task(asyncio.sleep(100))

        await ws_client.disconnect()

        assert ws_client._running is False
        assert ws_client._ws is None

    @pytest.mark.asyncio
    async def test_connected_property(self, ws_client):
        """connected 프로퍼티."""
        assert ws_client.connected is False
        ws_client._ws = MagicMock()
        ws_client._running = True
        assert ws_client.connected is True


class TestSubscription:
    @pytest.fixture
    def ws_client(self):
        client = KiwoomWebSocketClient("wss://test.api.com/ws", access_token="token")
        client._ws = AsyncMock()
        client._running = True
        return client

    @pytest.mark.asyncio
    async def test_subscribe(self, ws_client):
        """실시간 등록."""
        await ws_client.subscribe(["005930", "000660"])
        ws_client._ws.send.assert_called_once()
        sent = json.loads(ws_client._ws.send.call_args[0][0])
        assert sent["trnm"] == "REG"
        assert "005930" in sent["data"][0]["item"]

    @pytest.mark.asyncio
    async def test_unsubscribe(self, ws_client):
        """실시간 해지."""
        await ws_client.unsubscribe(["005930"])
        ws_client._ws.send.assert_called_once()
        sent = json.loads(ws_client._ws.send.call_args[0][0])
        assert sent["trnm"] == "REMOVE"

    @pytest.mark.asyncio
    async def test_subscribe_without_connection(self, ws_client):
        """미연결 시 구독 무시."""
        ws_client._ws = None
        await ws_client.subscribe(["005930"])
        # 에러 없이 무시


class TestDispatch:
    @pytest.fixture
    def ws_client(self):
        return KiwoomWebSocketClient("wss://test.api.com/ws", access_token="token")

    @pytest.mark.asyncio
    async def test_tick_dispatch(self, ws_client):
        """체결 데이터 → on_tick_callback."""
        received = []
        async def on_tick(tick):
            received.append(tick)

        ws_client.on_tick_callback = on_tick

        data = {
            "type": "0B",
            "item": "005930",
            "values": {"10": "50000", "15": "1000"}
        }
        await ws_client._dispatch(data)

        assert len(received) == 1
        assert received[0].code == "005930"
        assert received[0].price == 50000
        assert received[0].volume == 1000

    @pytest.mark.asyncio
    async def test_order_dispatch(self, ws_client):
        """주문 체결 → on_order_callback."""
        received = []
        async def on_order(data):
            received.append(data)

        ws_client.on_order_callback = on_order

        data = {"type": "00", "ord_no": "ORD001"}
        await ws_client._dispatch(data)

        assert len(received) == 1

    @pytest.mark.asyncio
    async def test_no_callback_no_error(self, ws_client):
        """콜백 미등록 시 에러 없음."""
        data = {"type": "0B", "item": "005930", "values": {"10": "50000", "15": "100"}}
        await ws_client._dispatch(data)  # 에러 없이 무시

    @pytest.mark.asyncio
    async def test_tick_negative_price_abs(self, ws_client):
        """음수 현재가 abs 처리."""
        received = []
        async def on_tick(tick):
            received.append(tick)
        ws_client.on_tick_callback = on_tick

        data = {"type": "0B", "item": "005930", "values": {"10": "-50000", "15": "100"}}
        await ws_client._dispatch(data)
        assert received[0].price == 50000


class TestHeaders:
    def test_bearer_token_header(self):
        """Bearer 토큰 헤더만 포함 (approval_key 없음)."""
        client = KiwoomWebSocketClient("wss://test.api.com/ws", access_token="my_token")
        headers = client._build_headers()
        assert headers == {"authorization": "Bearer my_token"}
        assert "approval_key" not in headers

    def test_no_token_empty_headers(self):
        """토큰 없으면 빈 헤더."""
        client = KiwoomWebSocketClient("wss://test.api.com/ws")
        headers = client._build_headers()
        assert headers == {}
