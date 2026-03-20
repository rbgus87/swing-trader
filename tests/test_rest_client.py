"""REST 클라이언트 및 AsyncRateLimiter 테스트."""

import pytest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch, MagicMock
from src.broker.rest_client import KiwoomRestClient
from src.broker.rate_limiter import AsyncRateLimiter


class TestAsyncRateLimiter:
    """AsyncRateLimiter 단위 테스트."""

    @pytest.mark.asyncio
    async def test_can_call_within_limit(self):
        limiter = AsyncRateLimiter(max_calls=3)
        assert limiter.can_call() is True

    @pytest.mark.asyncio
    async def test_wait_records_call(self):
        limiter = AsyncRateLimiter(max_calls=5)
        await limiter.wait()
        assert len(limiter._calls) == 1


class TestKiwoomRestClient:
    """KiwoomRestClient 단위 테스트."""

    @pytest.fixture
    def client(self):
        return KiwoomRestClient("https://test.api.com", "test_key", "test_secret")

    @pytest.mark.asyncio
    async def test_authenticate(self, client):
        """토큰 발급 성공."""
        mock_response = MagicMock()
        mock_response.json.return_value = {"token": "test_token_123", "expires_dt": "20260318120000"}
        mock_response.raise_for_status = MagicMock()

        with patch.object(client._client, "post", new_callable=AsyncMock, return_value=mock_response):
            token = await client.authenticate()

        assert token == "test_token_123"
        assert client._access_token == "test_token_123"
        assert client._token_expires is not None
        assert client._token_expires == datetime(2026, 3, 18, 12, 0, 0)

    @pytest.mark.asyncio
    async def test_ensure_token_refreshes_expired(self, client):
        """만료된 토큰 자동 갱신."""
        client._token_expires = datetime.now() - timedelta(hours=1)

        mock_response = MagicMock()
        mock_response.json.return_value = {"token": "new_token"}
        mock_response.raise_for_status = MagicMock()

        with patch.object(client._client, "post", new_callable=AsyncMock, return_value=mock_response):
            await client._ensure_token()

        assert client._access_token == "new_token"

    @pytest.mark.asyncio
    async def test_request_includes_auth_header(self, client):
        """request에 인증 헤더 포함."""
        client._access_token = "my_token"
        client._token_expires = datetime.now() + timedelta(hours=23)

        mock_response = MagicMock()
        mock_response.json.return_value = {"result": "ok"}
        mock_response.raise_for_status = MagicMock()

        with patch.object(client._client, "post", new_callable=AsyncMock, return_value=mock_response) as mock_post:
            result = await client.request("POST", "/test", "api001", data={"key": "val"})

        call_headers = mock_post.call_args.kwargs["headers"]
        assert "Bearer my_token" in call_headers["Authorization"]
        assert result == {"result": "ok"}

    @pytest.mark.asyncio
    async def test_send_order(self, client):
        """주문 전송."""
        client._access_token = "token"
        client._token_expires = datetime.now() + timedelta(hours=23)

        mock_response = MagicMock()
        mock_response.json.return_value = {"return_code": 0, "ord_no": "ORD001"}
        mock_response.raise_for_status = MagicMock()

        with patch.object(client._client, "post", new_callable=AsyncMock, return_value=mock_response):
            result = await client.send_order("005930", 10, 50000, 1, "03", "1234567890")

        assert result["return_code"] == 0
        assert result["ord_no"] == "ORD001"

    @pytest.mark.asyncio
    async def test_get_daily_ohlcv(self, client):
        """일봉 데이터 조회."""
        client._access_token = "token"
        client._token_expires = datetime.now() + timedelta(hours=23)

        mock_response = MagicMock()
        mock_response.json.return_value = {"chart": [{"date": "20240101", "close": 50000}]}
        mock_response.raise_for_status = MagicMock()

        with patch.object(client._client, "post", new_callable=AsyncMock, return_value=mock_response):
            result = await client.get_daily_ohlcv("005930", "20240101", "20241231")

        assert len(result) == 1
        assert result[0]["close"] == 50000

    @pytest.mark.asyncio
    async def test_close(self, client):
        """클라이언트 종료."""
        with patch.object(client._client, "aclose", new_callable=AsyncMock) as mock_close:
            await client.close()
        mock_close.assert_called_once()
