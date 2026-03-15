"""키움 REST API 클라이언트.

httpx 기반 비동기 HTTP 클라이언트.
인증 토큰 자동 갱신, 요청 재시도, 에러 처리 포함.
"""

import httpx
import asyncio
from datetime import datetime, timedelta
from loguru import logger
from src.broker.rate_limiter import AsyncRateLimiter


class KiwoomRestClient:
    """키움 REST API HTTP 클라이언트."""

    def __init__(self, base_url: str, appkey: str, secretkey: str):
        if base_url and not base_url.startswith("https://"):
            raise ValueError("base_url은 https://로 시작해야 합니다 (보안)")
        self._base_url = base_url
        self._appkey = appkey
        self._secretkey = secretkey
        self._access_token: str | None = None
        self._token_expires: datetime | None = None
        self._ws_key: str | None = None
        self._client = httpx.AsyncClient(
            base_url=base_url, timeout=10.0,
            limits=httpx.Limits(max_connections=10)
        )
        self._rate_limiter = AsyncRateLimiter(max_calls=5, period=1.0)

    async def authenticate(self) -> str:
        """접근토큰 발급.

        POST /api/auth/token
        Body: {"appkey": ..., "secretkey": ...}
        Returns: access_token string
        """
        await self._rate_limiter.wait()
        response = await self._client.post(
            "/api/auth/token",
            json={"appkey": self._appkey, "secretkey": self._secretkey}
        )
        response.raise_for_status()
        data = response.json()
        self._access_token = data.get("access_token", "")
        # 토큰 만료: 보통 24시간, 안전하게 23시간으로 설정
        self._token_expires = datetime.now() + timedelta(hours=23)
        logger.info("접근토큰 발급 완료")
        return self._access_token

    async def get_ws_key(self) -> str:
        """WebSocket 접속키 발급.

        POST /api/auth/websocket
        Returns: ws_key string
        """
        await self._ensure_token()
        await self._rate_limiter.wait()
        response = await self._client.post(
            "/api/auth/websocket",
            headers=self._auth_headers()
        )
        response.raise_for_status()
        data = response.json()
        self._ws_key = data.get("ws_key", "")
        logger.info("WebSocket 접속키 발급 완료")
        return self._ws_key

    async def _ensure_token(self):
        """토큰 만료 시 자동 갱신 (만료 5분 전 선제 갱신)."""
        if (self._token_expires is None or
            datetime.now() >= self._token_expires - timedelta(minutes=5)):
            await self.authenticate()

    def _auth_headers(self) -> dict:
        """인증 헤더 생성."""
        return {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
            "appkey": self._appkey,
            "secretkey": self._secretkey,
        }

    async def request(self, method: str, endpoint: str, api_id: str,
                      data: dict | None = None, params: dict | None = None) -> dict:
        """API 요청 실행.

        인증 헤더 자동 포함, 토큰 자동 갱신, rate limit 준수.
        """
        await self._ensure_token()
        await self._rate_limiter.wait()

        headers = self._auth_headers()
        headers["api-id"] = api_id

        try:
            if method.upper() == "GET":
                response = await self._client.get(endpoint, headers=headers, params=params)
            else:
                response = await self._client.post(endpoint, headers=headers, json=data)

            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            logger.error(f"API 요청 실패: {endpoint} ({e.response.status_code})")
            raise
        except httpx.RequestError as e:
            logger.error(f"API 연결 실패: {endpoint} ({e})")
            raise

    async def get_daily_ohlcv(self, code: str, start_date: str,
                               end_date: str) -> list[dict]:
        """일봉 데이터 조회."""
        from src.broker.tr_codes import API_STOCK_DAILY, EP_CHART
        data = {"stk_cd": code, "start_date": start_date, "end_date": end_date, "period": "D"}
        result = await self.request("POST", EP_CHART, API_STOCK_DAILY, data=data)
        return result.get("chart", [])

    async def get_minute_ohlcv(self, code: str, tick_range: int = 60,
                                count: int = 100) -> list[dict]:
        """분봉 데이터 조회."""
        from src.broker.tr_codes import API_STOCK_MINUTE, EP_CHART
        data = {"stk_cd": code, "tick_range": str(tick_range), "count": count}
        result = await self.request("POST", EP_CHART, API_STOCK_MINUTE, data=data)
        return result.get("chart", [])

    async def get_current_price(self, code: str) -> dict:
        """현재가 조회."""
        from src.broker.tr_codes import API_STOCK_PRICE, EP_STOCK
        data = {"stk_cd": code}
        return await self.request("POST", EP_STOCK, API_STOCK_PRICE, data=data)

    async def send_order(self, code: str, qty: int, price: int,
                         order_type: int, hoga_type: str,
                         account: str) -> dict:
        """주문 전송."""
        from src.broker.tr_codes import API_STOCK_ORDER, EP_ORDER
        data = {
            "stk_cd": code,
            "ord_qty": qty,
            "ord_uv": price,
            "trde_tp": hoga_type,
            "ord_tp": order_type,
            "acnt_no": account,
        }
        return await self.request("POST", EP_ORDER, API_STOCK_ORDER, data=data)

    async def cancel_order(self, order_no: str, code: str, qty: int,
                           account: str) -> dict:
        """주문 취소."""
        from src.broker.tr_codes import API_STOCK_CANCEL, EP_ORDER
        data = {
            "org_ord_no": order_no,
            "stk_cd": code,
            "ord_qty": qty,
            "acnt_no": account,
        }
        return await self.request("POST", EP_ORDER, API_STOCK_CANCEL, data=data)

    async def get_account_balance(self, account: str) -> dict:
        """계좌 잔고 조회."""
        from src.broker.tr_codes import API_ACCOUNT_BALANCE, EP_ACCOUNT
        data = {"acnt_no": account}
        return await self.request("POST", EP_ACCOUNT, API_ACCOUNT_BALANCE, data=data)

    async def close(self):
        """클라이언트 종료."""
        await self._client.aclose()
