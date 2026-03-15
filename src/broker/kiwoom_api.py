"""키움 REST API 통합 인터페이스.

REST 클라이언트 + WebSocket 클라이언트를 래핑하여
기존 engine.py 호환 인터페이스 제공.
"""
from loguru import logger
from src.broker.rest_client import KiwoomRestClient
from src.broker.ws_client import KiwoomWebSocketClient


class KiwoomAPI:
    """키움 REST/WebSocket 기반 API 래퍼.

    engine.py에서 사용하는 인터페이스를 유지하면서
    내부적으로 REST + WebSocket을 사용합니다.
    """

    def __init__(self, base_url: str, ws_url: str,
                 appkey: str, secretkey: str):
        self._rest = KiwoomRestClient(base_url, appkey, secretkey)
        self._ws: KiwoomWebSocketClient | None = None
        self._ws_url = ws_url
        self._connected = False
        self.on_tick_callback = None     # async callable
        self.on_chejan_callback = None   # async callable

    async def connect(self):
        """인증 + WebSocket 연결."""
        # 1. REST 인증
        await self._rest.authenticate()
        # 2. WebSocket 접속키 발급
        ws_key = await self._rest.get_ws_key()
        # 3. WebSocket 연결
        self._ws = KiwoomWebSocketClient(self._ws_url, ws_key)
        self._ws.on_tick_callback = self.on_tick_callback
        self._ws.on_order_callback = self.on_chejan_callback
        await self._ws.connect()
        self._connected = True
        logger.info("키움 API 연결 완료 (REST + WebSocket)")

    async def disconnect(self):
        """연결 종료."""
        if self._ws:
            await self._ws.disconnect()
        await self._rest.close()
        self._connected = False
        logger.info("키움 API 연결 종료")

    # ── 데이터 조회 (REST) ──

    async def get_daily_ohlcv(self, code: str, start_date: str,
                               end_date: str = "", adj_price: bool = True):
        """일봉 데이터 조회."""
        return await self._rest.get_daily_ohlcv(code, start_date, end_date)

    async def get_minute_ohlcv(self, code: str, tick_range: int = 60,
                                count: int = 100):
        """분봉 데이터 조회."""
        return await self._rest.get_minute_ohlcv(code, tick_range, count)

    async def get_current_price(self, code: str) -> dict:
        """현재가 조회."""
        return await self._rest.get_current_price(code)

    async def get_account_info(self, account: str) -> dict:
        """계좌 잔고 조회."""
        return await self._rest.get_account_balance(account)

    # ── 주문 (REST) ──

    async def send_order(self, code: str, qty: int, price: int,
                         order_type: int, hoga_type: str,
                         account: str) -> dict:
        """주문 전송.

        Returns:
            {"return_code": 0, "ord_no": "..."} 형태 dict
        """
        return await self._rest.send_order(
            code, qty, price, order_type, hoga_type, account
        )

    async def cancel_order(self, order_no: str, code: str, qty: int,
                           account: str) -> dict:
        """주문 취소."""
        return await self._rest.cancel_order(order_no, code, qty, account)

    # ── 실시간 (WebSocket) ──

    async def subscribe_realtime(self, codes: list[str]):
        """실시간 시세 등록."""
        if self._ws:
            await self._ws.subscribe(codes, "0B")

    async def unsubscribe_realtime(self, codes: list[str]):
        """실시간 시세 해지."""
        if self._ws:
            await self._ws.unsubscribe(codes, "0B")

    @property
    def connected(self) -> bool:
        """연결 상태."""
        return self._connected
