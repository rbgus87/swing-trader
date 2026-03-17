"""키움 REST API WebSocket 클라이언트.

실시간 시세(체결/호가) 및 주문 체결 이벤트 수신.
자동 재연결 (최대 5회, 5초 간격) 포함.
"""
import asyncio
import json
from datetime import datetime
import websockets
from loguru import logger
from src.models import Tick


class KiwoomWebSocketClient:
    """WebSocket 기반 실시간 데이터 클라이언트."""

    def __init__(self, ws_url: str, ws_key: str):
        self._ws_url = ws_url
        self._ws_key = ws_key
        self._ws = None
        self._running = False
        self._listen_task: asyncio.Task | None = None
        self.on_tick_callback = None       # async Tick 수신 콜백
        self.on_order_callback = None      # async 체결 수신 콜백

    async def connect(self):
        """WebSocket 연결."""
        self._ws = await websockets.connect(
            f"{self._ws_url}?ws_key={self._ws_key}",
            ping_interval=30,
            ping_timeout=10,
        )
        self._running = True
        self._listen_task = asyncio.create_task(self._listen())
        logger.info("WebSocket 연결 성공")

    async def disconnect(self):
        """WebSocket 종료."""
        self._running = False
        if self._listen_task and not self._listen_task.done():
            self._listen_task.cancel()
            try:
                await self._listen_task
            except asyncio.CancelledError:
                pass
        if self._ws:
            await self._ws.close()
            self._ws = None
        logger.info("WebSocket 연결 종료")

    async def subscribe(self, codes: list[str], real_type: str = "0B"):
        """실시간 시세 등록.

        Args:
            codes: 종목 코드 리스트
            real_type: "0B"(체결), "0D"(호가), "00"(주문체결)
        """
        if not self._ws:
            logger.warning("WebSocket 미연결 — 구독 실패")
            return
        msg = {
            "trnm": "REG",
            "grp_no": "1",
            "refresh": "1",
            "data": [{"item": codes, "type": [real_type]}]
        }
        await self._ws.send(json.dumps(msg))
        logger.info(f"실시간 등록: {len(codes)}종목 (type={real_type})")

    async def unsubscribe(self, codes: list[str], real_type: str = "0B"):
        """실시간 시세 해지."""
        if not self._ws:
            return
        msg = {
            "trnm": "REMOVE",
            "grp_no": "1",
            "data": [{"item": codes, "type": [real_type]}]
        }
        await self._ws.send(json.dumps(msg))
        logger.info(f"실시간 해지: {len(codes)}종목")

    async def _listen(self):
        """메시지 수신 루프."""
        try:
            while self._running and self._ws:
                try:
                    message = await asyncio.wait_for(
                        self._ws.recv(), timeout=60.0
                    )
                    data = json.loads(message)
                    await self._dispatch(data)
                except asyncio.TimeoutError:
                    continue  # 타임아웃은 정상 (heartbeat 대기)
                except Exception as e:
                    if self._running:
                        logger.error(f"WebSocket 수신 에러: {e}")
                        await self._reconnect()
                    break
        except asyncio.CancelledError:
            pass

    async def _dispatch(self, data: dict):
        """수신 데이터 타입별 콜백 라우팅."""
        msg_type = data.get("type", "")

        if msg_type == "0B" and self.on_tick_callback:
            # 체결 데이터 → Tick 변환
            try:
                values = data.get("values", {})
                if isinstance(values, str):
                    values = json.loads(values)
                tick = Tick(
                    code=data.get("item", ""),
                    price=abs(int(values.get("10", 0))),  # FID 10: 현재가
                    volume=int(values.get("15", 0)),        # FID 15: 거래량
                    timestamp=datetime.now(),
                )
                await self.on_tick_callback(tick)
            except (ValueError, KeyError) as e:
                logger.warning(f"체결 데이터 파싱 실패: {e}")

        elif msg_type == "00" and self.on_order_callback:
            # 주문 체결 데이터
            try:
                await self.on_order_callback(data)
            except Exception as e:
                logger.error(f"주문 체결 콜백 에러: {e}")

    async def _reconnect(self, max_retries: int = 5, base_delay: float = 2.0):
        """자동 재연결 (지수 백오프, 최대 5회)."""
        for attempt in range(1, max_retries + 1):
            try:
                logger.warning(f"WebSocket 재연결 시도 ({attempt}/{max_retries})")
                if self._ws:
                    await self._ws.close()
                self._ws = await websockets.connect(
                    f"{self._ws_url}?ws_key={self._ws_key}",
                    ping_interval=30,
                    ping_timeout=10,
                )
                logger.info("WebSocket 재연결 성공")
                # 수신 루프 재시작
                self._listen_task = asyncio.create_task(self._listen())
                return
            except Exception as e:
                logger.error(f"재연결 실패: {e}")
                if attempt < max_retries:
                    delay = base_delay * (2 ** (attempt - 1))  # 2, 4, 8, 16초
                    delay = min(delay, 60)  # 최대 60초
                    logger.info(f"재연결 대기: {delay:.0f}초")
                    await asyncio.sleep(delay)
        logger.critical("WebSocket 최대 재연결 횟수 초과")
        self._running = False

    @property
    def connected(self) -> bool:
        """연결 상태."""
        return self._ws is not None and self._running
