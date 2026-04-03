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

    # 토큰 재발급 최소 간격 (연속 호출 방지)
    TOKEN_REISSUE_MIN_INTERVAL = 60  # seconds

    def __init__(self, base_url: str, appkey: str, secretkey: str):
        if base_url and not base_url.startswith("https://"):
            raise ValueError("base_url은 https://로 시작해야 합니다 (보안)")
        self._base_url = base_url
        self._appkey = appkey
        self._secretkey = secretkey
        self._access_token: str | None = None
        self._token_expires: datetime | None = None
        self._last_reissue: datetime | None = None
        self._client = httpx.AsyncClient(
            base_url=base_url, timeout=10.0,
            limits=httpx.Limits(max_connections=10)
        )
        self._rate_limiter = AsyncRateLimiter(max_calls=5, period=1.0)

    async def authenticate(self) -> str:
        """접근토큰 발급.

        POST /oauth2/token
        Body: {"grant_type": "client_credentials", "appkey": ..., "secretkey": ...}
        Returns: token string
        """
        await self._rate_limiter.wait()
        response = await self._client.post(
            "/oauth2/token",
            json={
                "grant_type": "client_credentials",
                "appkey": self._appkey,
                "secretkey": self._secretkey,
            }
        )
        response.raise_for_status()
        data = response.json()
        self._access_token = data.get("token", "")

        if not self._access_token:
            logger.error(f"토큰 발급 실패 — 응답: {data}")
            raise ValueError(f"토큰이 비어있음. 응답: {data}")

        # Parse expiry from "expires_dt" (format: YYYYMMDDHHmmss)
        expires_str = data.get("expires_dt", "")
        if expires_str:
            self._token_expires = datetime.strptime(expires_str, "%Y%m%d%H%M%S")
        else:
            self._token_expires = datetime.now() + timedelta(hours=23)

        logger.info(f"접근토큰 발급 완료 (만료: {self._token_expires})")
        return self._access_token

    async def _ensure_token(self):
        """토큰 만료 시 자동 갱신 (만료 10분 전 선제 갱신)."""
        if (self._token_expires is None or
            datetime.now() >= self._token_expires - timedelta(minutes=10)):
            await self.authenticate()

    @property
    def access_token(self) -> str:
        """현재 접근토큰 반환."""
        return self._access_token or ""

    def _auth_headers(self) -> dict:
        """인증 헤더 생성."""
        return {
            "Content-Type": "application/json;charset=UTF-8",
            "Authorization": f"Bearer {self._access_token}",
        }

    def _is_token_invalid_response(self, result: dict) -> bool:
        """응답이 토큰 무효화 에러(8005)인지 확인."""
        return_code = result.get("return_code")
        return_msg = str(result.get("return_msg", ""))
        if return_code == 3:
            return True
        if "8005" in return_msg or "Token" in return_msg:
            return True
        return False

    async def _reissue_token(self) -> bool:
        """토큰 재발급 (최소 간격 60초 제한).

        Returns:
            True: 재발급 성공, False: 실패 또는 간격 미달
        """
        now = datetime.now()
        if (self._last_reissue and
                (now - self._last_reissue).total_seconds() < self.TOKEN_REISSUE_MIN_INTERVAL):
            logger.warning("토큰 재발급 간격 미달 (60초 제한)")
            return False
        try:
            await self.authenticate()
            self._last_reissue = datetime.now()
            logger.info("토큰 만료 감지 → 자동 재발급 완료")
            return True
        except Exception as e:
            logger.error(f"토큰 재발급 실패: {e}")
            return False

    async def request(self, method: str, endpoint: str, api_id: str,
                      data: dict | None = None, params: dict | None = None) -> dict:
        """API 요청 실행.

        인증 헤더 자동 포함, 토큰 자동 갱신, rate limit 준수.
        8005 토큰 무효화 에러 시 자동 재발급 후 1회 재시도.
        """
        await self._ensure_token()
        await self._rate_limiter.wait()

        headers = self._auth_headers()
        headers["api-id"] = api_id
        headers["cont-yn"] = "N"
        headers["next-key"] = ""

        max_retries = 3
        for attempt in range(max_retries):
            try:
                if method.upper() == "GET":
                    response = await self._client.get(endpoint, headers=headers, params=params)
                else:
                    response = await self._client.post(endpoint, headers=headers, json=data)

                response.raise_for_status()
                result = response.json()

                # 8005 토큰 무효화 감지 → 재발급 후 1회 재시도
                if self._is_token_invalid_response(result):
                    logger.warning(f"토큰 무효 응답 감지: {result.get('return_msg', '')}")
                    if await self._reissue_token():
                        headers = self._auth_headers()
                        headers["api-id"] = api_id
                        headers["cont-yn"] = "N"
                        headers["next-key"] = ""
                        await self._rate_limiter.wait()
                        if method.upper() == "GET":
                            retry_resp = await self._client.get(
                                endpoint, headers=headers, params=params)
                        else:
                            retry_resp = await self._client.post(
                                endpoint, headers=headers, json=data)
                        retry_resp.raise_for_status()
                        return retry_resp.json()
                    else:
                        # 재발급 실패 — 텔레그램 알림은 호출자에서 처리
                        from src.notification.telegram_bot import TelegramBot
                        try:
                            TelegramBot().send("⚠️ 토큰 재발급 실패 — 수동 확인 필요")
                        except Exception:
                            pass
                        return result

                return result
            except httpx.HTTPStatusError as e:
                logger.error(f"API 요청 실패: {endpoint} ({e.response.status_code})")
                if e.response.status_code >= 500 and attempt < max_retries - 1:
                    await asyncio.sleep(1 * (attempt + 1))
                    continue
                raise
            except httpx.RequestError as e:
                logger.error(f"API 연결 실패: {endpoint} ({e})")
                if attempt < max_retries - 1:
                    await asyncio.sleep(1 * (attempt + 1))
                    continue
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
