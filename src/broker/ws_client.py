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
from src.utils.market_calendar import is_ws_active_hours


class KiwoomWebSocketClient:
    """WebSocket 기반 실시간 데이터 클라이언트."""

    def __init__(self, ws_url: str, access_token: str | None = None):
        self._ws_url = ws_url
        self._access_token = access_token
        self._ws = None
        self._running = False
        self._listen_task: asyncio.Task | None = None
        self._subscribed: dict[str, list[str]] = {}  # {real_type: [codes]}
        self._close_1000_count = 0  # close 1000 연속 발생 횟수 (무한루프 방지)
        self.on_tick_callback = None       # async Tick 수신 콜백
        self.on_order_callback = None      # async 체결 수신 콜백

    def _build_headers(self) -> dict:
        """WebSocket 연결 헤더 구성 (Bearer 토큰 인증)."""
        headers = {}
        if self._access_token:
            headers["authorization"] = f"Bearer {self._access_token}"
        return headers

    async def connect(self):
        """WebSocket 연결 (Bearer 토큰 인증)."""
        self._ws = await websockets.connect(
            self._ws_url,
            ping_interval=30,
            ping_timeout=10,
            additional_headers=self._build_headers(),
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
        # 구독 목록 기록 (재연결 시 복구용)
        existing = set(self._subscribed.get(real_type, []))
        existing.update(codes)
        self._subscribed[real_type] = list(existing)
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
        # 구독 목록에서 제거
        existing = set(self._subscribed.get(real_type, []))
        existing -= set(codes)
        self._subscribed[real_type] = list(existing)
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
                    self._close_1000_count = 0  # 정상 수신 시 close 카운터 리셋
                    await self._dispatch(data)
                except asyncio.TimeoutError:
                    if not is_ws_active_hours():
                        logger.info("장 시간 외 — WebSocket 수신 대기 중지")
                        self._running = False
                        break
                    continue  # 장중 타임아웃은 정상 (heartbeat 대기)
                except Exception as e:
                    err_str = str(e)
                    if "1000" in err_str:
                        self._close_1000_count += 1
                        sub_count = len(self._subscribed.get("0B", []))
                        if is_ws_active_hours() and self._close_1000_count <= 3:
                            # 장중 close 1000: 최대 3회까지 재연결 (무한루프 방지)
                            wait = 10 * self._close_1000_count  # 10, 20, 30초 대기
                            logger.warning(
                                f"WebSocket close 1000 (장중, {self._close_1000_count}/3) — "
                                f"{wait}초 후 재연결 (구독={sub_count}종목)"
                            )
                            await asyncio.sleep(wait)
                            await self._reconnect()
                        elif self._close_1000_count > 3:
                            logger.error(
                                f"WebSocket close 1000 연속 {self._close_1000_count}회 — "
                                f"재연결 중단 (서버 구독 등록 실패 의심, 구독={sub_count}종목)"
                            )
                            self._running = False
                        else:
                            logger.info("WebSocket 서버 정상 종료 (장 시간 외)")
                            self._running = False
                        break
                    # close 1000 외 에러는 카운터 리셋
                    self._close_1000_count = 0
                    if self._running:
                        logger.warning(f"WebSocket 수신 에러: {e}")
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
        """자동 재연결 (지수 백오프, 최대 5회). 장 시간 외에는 시도하지 않음."""
        if not is_ws_active_hours():
            logger.info("장 시간 외 — 재연결 생략 (다음 장 시작 전 자동 연결)")
            self._running = False
            return
        for attempt in range(1, max_retries + 1):
            try:
                logger.warning(f"WebSocket 재연결 시도 ({attempt}/{max_retries})")
                if self._ws:
                    await self._ws.close()
                self._ws = await websockets.connect(
                    self._ws_url,
                    ping_interval=30,
                    ping_timeout=10,
                    additional_headers=self._build_headers(),
                )
                logger.info("WebSocket 재연결 성공")
                # 이전 listen 태스크 정리 후 수신 루프 재시작
                if self._listen_task and not self._listen_task.done():
                    self._listen_task.cancel()
                    try:
                        await self._listen_task
                    except asyncio.CancelledError:
                        pass
                self._listen_task = asyncio.create_task(self._listen())
                # 이전 구독 복구
                await self._restore_subscriptions()
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

    async def _restore_subscriptions(self):
        """재연결 후 이전 구독 목록 복구."""
        if not self._subscribed:
            return
        for real_type, codes in self._subscribed.items():
            if codes:
                msg = {
                    "trnm": "REG",
                    "grp_no": "1",
                    "refresh": "1",
                    "data": [{"item": codes, "type": [real_type]}]
                }
                try:
                    await self._ws.send(json.dumps(msg))
                    logger.info(f"구독 복구: {len(codes)}종목 (type={real_type})")
                except Exception as e:
                    logger.error(f"구독 복구 실패 (type={real_type}): {e}")

    @property
    def connected(self) -> bool:
        """연결 상태."""
        return self._ws is not None and self._running
