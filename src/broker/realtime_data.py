"""실시간 시세 데이터 관리.

키움 REST API의 WebSocket을 통한 실시간 시세 등록/해제 및 현재가 캐싱을 담당한다.
"""

from loguru import logger

from src.broker.kiwoom_api import KiwoomAPI
from src.models import Tick


class RealtimeDataManager:
    """실시간 시세 데이터 관리 클래스.

    종목별 실시간 시세를 구독/해제하고 최신 현재가를 관리한다.

    Args:
        kiwoom_api: KiwoomAPI 인스턴스.
    """

    def __init__(self, kiwoom_api: KiwoomAPI):
        self._api = kiwoom_api
        self._subscribed_codes: set[str] = set()
        self._current_prices: dict[str, int] = {}

    async def subscribe(self, code: str) -> None:
        """단일 종목 실시간 시세 구독.

        Args:
            code: 종목코드 (6자리).
        """
        if code in self._subscribed_codes:
            logger.debug("이미 구독 중인 종목: {}", code)
            return

        await self._api.subscribe_realtime([code])
        self._subscribed_codes.add(code)
        logger.info("실시간 구독 시작: {}", code)

    async def unsubscribe(self, code: str) -> None:
        """단일 종목 실시간 시세 해제.

        Args:
            code: 종목코드 (6자리).
        """
        if code not in self._subscribed_codes:
            logger.debug("구독 중이 아닌 종목: {}", code)
            return

        await self._api.unsubscribe_realtime([code])
        self._subscribed_codes.discard(code)
        self._current_prices.pop(code, None)
        logger.info("실시간 구독 해제: {}", code)

    async def subscribe_list(self, codes: list[str]) -> None:
        """복수 종목 실시간 시세 구독.

        Args:
            codes: 종목코드 리스트.
        """
        for code in codes:
            await self.subscribe(code)

    def get_current_price(self, code: str) -> int:
        """종목 현재가 조회.

        Args:
            code: 종목코드.

        Returns:
            현재가(int). 데이터 없으면 0.
        """
        return self._current_prices.get(code, 0)

    def on_tick(self, tick: Tick) -> None:
        """실시간 틱 수신 시 내부 가격 업데이트.

        KiwoomAPI의 on_tick_callback으로 등록하여 사용한다.

        Args:
            tick: 수신된 틱 데이터.
        """
        self._current_prices[tick.code] = tick.price

    @property
    def subscribed_codes(self) -> set[str]:
        """현재 구독 중인 종목코드 세트."""
        return self._subscribed_codes.copy()

    @property
    def current_prices(self) -> dict[str, int]:
        """전체 현재가 딕셔너리 복사본."""
        return self._current_prices.copy()
