# REST API 전환 + 전략 튜닝 구현 계획

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 키움 OCX 브로커를 REST API로 전환하고, 매수 조건을 완화하여 백테스트 거래 수를 50건 이상으로 늘리며, asyncio 기반 엔진으로 재구성

**Architecture:** 3단계 진행 — (1) 전략 튜닝으로 거래 수 확보, (2) 브로커 REST API 전환 (httpx + websockets 직접 구현), (3) 엔진을 PyQt5→asyncio로 전환. 각 단계는 독립적으로 테스트 가능.

**Tech Stack:** Python 3.14, httpx, websockets, asyncio, APScheduler (AsyncIOScheduler), pandas, pandas-ta

---

## Context

### 현재 상태
- 10개 세션 코드 완성 (253 tests passed)
- 키움 OCX 기반 (PyQt5 + QAxWidget) → Python 3.14 비호환
- 백테스트 3건 거래 (매수 AND 조건 5개가 너무 엄격)
- 60분봉 조건이 백테스트에 미반영
- vectorbt 제거 완료 → pandas 기반 백테스트

### 키움 REST API 정보
- **Base**: `api.kiwoom.com` (HTTPS)
- **WebSocket**: `wss://api.kiwoom.com:10000/api/dostk/websocket`
- **인증**: appkey/secretkey → Bearer token
- **주문**: `/api/dostk/ordr` (api_id: `kt10000`)
- **실시간 타입**: `0B`(체결), `0D`(호가), `00`(주문체결)
- **참고 라이브러리**: `kiwoom-restful` (API 구조 참고용, 직접 구현)

### 변경 대상 파일 맵

```
변경:
  src/broker/kiwoom_api.py      → REST/WebSocket 클라이언트로 교체
  src/broker/order_manager.py   → async 주문 호출로 변경
  src/broker/realtime_data.py   → WebSocket 구독으로 변경
  src/broker/tr_codes.py        → REST API ID 상수 추가
  src/broker/rate_limiter.py    → AsyncRateLimiter 추가 (asyncio.sleep)
  src/engine.py                 → PyQt5 → asyncio 전환
  src/strategy/signals.py       → 매수 조건 완화 옵션 추가
  src/strategy/screener.py      → async 데이터 조회로 변경
  src/backtest/engine.py        → 완화된 조건 반영
  main.py                       → asyncio.run() 진입점
  requirements.txt              → PyQt5 제거, httpx/websockets 추가
  config.yaml                   → REST API 설정 추가
  .env.example                  → appkey/secretkey 추가
  pyproject.toml                → asyncio_mode = "auto" 추가
  tests/test_broker.py          → REST mock 테스트로 교체
  tests/test_engine.py          → asyncio mock 테스트로 교체
  tests/test_strategy.py        → 완화 조건 테스트 추가
  tests/test_backtest.py        → 거래 수 검증 테스트 추가
  tests/conftest.py             → async fixture로 재구성
  tests/e2e/test_buy_flow.py    → async 전환
  tests/e2e/test_stop_loss_flow.py → async 전환
  tests/e2e/test_halt_flow.py   → async 전환
  tests/e2e/test_paper_trading.py → async 전환

신규:
  src/broker/rest_client.py     → httpx 기반 REST 클라이언트
  src/broker/ws_client.py       → WebSocket 클라이언트
```

### 리뷰 반영 사항
- AsyncRateLimiter 추가 (time.sleep → asyncio.sleep, 이벤트루프 블로킹 방지)
- WebSocket 접속키 발급 플로우 명시
- kiwoom-restful 직접 사용 안함 (구조 참고만)
- Screener async 전환 포함
- pytest-asyncio `asyncio_mode = "auto"` 설정
- Task 5를 5a/5b/5c로 분할
- E2E 테스트 파일 명시적 나열

---

## Chunk 1: 전략 튜닝 (거래 수 확보)

### Task 1: 매수 조건 완화 옵션 추가

**Files:**
- Modify: `src/strategy/signals.py`
- Modify: `src/backtest/engine.py`
- Modify: `tests/test_strategy.py`
- Modify: `tests/test_backtest.py`

**목표:** 백테스트 기본 파라미터에서 거래 수 50건 이상 확보

- [ ] **Step 1: signals.py의 check_entry_signal에 조건별 on/off 파라미터 추가**

`check_entry_signal()` 함수에 `use_60m=True` 파라미터 추가. 백테스트에서는 `use_60m=False`로 호출.

```python
def check_entry_signal(
    df: pd.DataFrame,
    df_60m: pd.DataFrame | None = None,
    rsi_entry_min: int = 40,
    rsi_entry_max: int = 65,
    volume_multiplier: float = 1.5,
    use_60m: bool = True,
) -> bool:
    # ... 기존 조건 1~4 ...

    # 조건 5: 60분봉 (옵션)
    if use_60m and df_60m is not None and len(df_60m) >= 2:
        cond_60m = df_60m.iloc[-1]["sma5"] > df_60m.iloc[-1]["sma20"]
    else:
        cond_60m = True  # 60분봉 미사용 시 항상 통과

    return all([cond_trend, cond_macd, cond_rsi, cond_volume, cond_60m])
```

- [ ] **Step 2: backtest/engine.py의 generate_signals 기본 파라미터 완화**

기존 백테스트 기본값을 완화:
- `rsi_min`: 40 → 35
- `rsi_max`: 65 → 70
- `volume_multiplier`: 1.5 → 1.2

```python
# generate_signals 내부
rsi_min = p.get("rsi_min", 35)      # 기존 40 → 35
rsi_max = p.get("rsi_max", 70)      # 기존 65 → 70
volume_multiplier = p.get("volume_multiplier", 1.2)  # 기존 1.5 → 1.2
```

- [ ] **Step 3: test_strategy.py에 완화 조건 테스트 추가**

```python
def test_entry_signal_relaxed_params():
    """완화된 파라미터로 더 많은 신호 발생 확인."""
    # rsi_entry_min=35, rsi_entry_max=70, volume_multiplier=1.2

def test_entry_signal_without_60m():
    """60분봉 미사용 시 정상 동작."""
    result = check_entry_signal(df, None, use_60m=False)
```

- [ ] **Step 4: test_backtest.py에 거래 수 검증 추가**

```python
def test_relaxed_params_generate_more_signals(engine, sample_ohlcv):
    """완화된 파라미터로 더 많은 entry 신호 생성."""
    relaxed = {"rsi_min": 35, "rsi_max": 70, "volume_multiplier": 1.2}
    entries_r, _ = engine.generate_signals(sample_ohlcv, relaxed)

    default = {}
    entries_d, _ = engine.generate_signals(sample_ohlcv, default)

    assert entries_r.sum() >= entries_d.sum()
```

- [ ] **Step 5: 테스트 실행 확인**

Run: `python -m pytest tests/test_strategy.py tests/test_backtest.py -v`
Expected: ALL PASS

- [ ] **Step 6: 커밋**

```bash
git add src/strategy/signals.py src/backtest/engine.py tests/test_strategy.py tests/test_backtest.py
git commit -m "feat: relax entry conditions for more trade signals"
```

---

## Chunk 2: 브로커 REST 클라이언트

### Task 2: REST 클라이언트 기반 클래스 구현

**Files:**
- Create: `src/broker/rest_client.py`
- Modify: `src/broker/tr_codes.py`
- Modify: `requirements.txt`
- Modify: `config.yaml`
- Modify: `.env.example`
- Create: `tests/test_rest_client.py`

- [ ] **Step 1: requirements.txt 업데이트**

PyQt5 제거, REST 관련 패키지 추가:
```
# ── Core ──
httpx>=0.27
websockets>=12.0
# PyQt5 제거
```

- [ ] **Step 2: config.yaml에 REST API 설정 추가**

```yaml
broker:
  type: rest                    # ocx | rest
  base_url: "https://api.kiwoom.com"
  ws_url: "wss://api.kiwoom.com:10000/api/dostk/websocket"
  environment: paper            # paper | live (모의투자 / 실거래)
```

- [ ] **Step 3: .env.example에 API 키 추가**

```
KIWOOM_APPKEY=your_appkey
KIWOOM_SECRETKEY=your_secretkey
KIWOOM_ACCOUNT=your_account_number
```

- [ ] **Step 4: tr_codes.py에 REST API ID 상수 추가**

```python
# REST API IDs
API_AUTH_TOKEN = "au10001"          # 접근토큰 발급
API_STOCK_ORDER = "kt10000"        # 주식 매수/매도
API_STOCK_CANCEL = "kt10001"       # 주문 취소
API_STOCK_PRICE = "ka10001"        # 현재가 조회
API_STOCK_DAILY = "ka10002"        # 일봉 조회
API_STOCK_MINUTE = "ka10003"       # 분봉 조회
API_ACCOUNT_BALANCE = "ka10070"    # 잔고 조회
API_STOCK_LIST = "ka10100"         # 종목 리스트

# REST Endpoints
EP_AUTH = "/api/auth/token"
EP_ORDER = "/api/dostk/ordr"
EP_STOCK = "/api/dostk/stkinfo"
EP_CHART = "/api/dostk/chart"
EP_ACCOUNT = "/api/dostk/acnt"

# WebSocket 실시간 타입
WS_TYPE_TICK = "0B"                # 주식 체결
WS_TYPE_ORDERBOOK = "0D"          # 호가
WS_TYPE_ORDER = "00"              # 주문 체결

# 기존 상수 유지 (ORDER_BUY, ORDER_SELL, PRICE_LIMIT, PRICE_MARKET 등)
```

- [ ] **Step 5: rate_limiter.py에 AsyncRateLimiter 추가**

기존 동기 RateLimiter 유지 + 비동기 버전 추가 (asyncio.sleep 사용):

```python
class AsyncRateLimiter:
    """비동기 슬라이딩 윈도우 rate limiter.

    asyncio 이벤트루프를 블로킹하지 않음.
    """
    def __init__(self, max_calls: int = 5, period: float = 1.0):
        self.max_calls = max_calls
        self.period = period
        self._calls = deque()

    async def wait(self):
        """호출 가능할 때까지 비동기 대기."""
        now = time.monotonic()
        while self._calls and now - self._calls[0] > self.period:
            self._calls.popleft()
        if len(self._calls) >= self.max_calls:
            sleep_time = self.period - (now - self._calls[0])
            if sleep_time > 0:
                await asyncio.sleep(sleep_time)
        self._calls.append(time.monotonic())
```

- [ ] **Step 6: rest_client.py 구현**

```python
"""키움 REST API 클라이언트.

httpx 기반 비동기 HTTP 클라이언트.
인증 토큰 자동 갱신, 요청 재시도, 에러 처리 포함.
"""
import httpx
from datetime import datetime, timedelta
from loguru import logger
from src.broker.rate_limiter import AsyncRateLimiter

class KiwoomRestClient:
    """키움 REST API HTTP 클라이언트."""

    def __init__(self, base_url: str, appkey: str, secretkey: str):
        self._base_url = base_url
        self._appkey = appkey
        self._secretkey = secretkey
        self._access_token: str | None = None
        self._token_expires: datetime | None = None
        self._ws_key: str | None = None  # WebSocket 접속키
        self._client = httpx.AsyncClient(
            base_url=base_url, timeout=10.0,
            limits=httpx.Limits(max_connections=10)  # 커넥션 풀
        )
        self._rate_limiter = AsyncRateLimiter(max_calls=5, period=1.0)

    async def authenticate(self) -> str:
        """접근토큰 발급/갱신.

        POST /api/auth/token
        Body: {"appkey": ..., "secretkey": ...}
        Returns: access_token (self._access_token에 저장)
        """

    async def get_ws_key(self) -> str:
        """WebSocket 접속키 발급.

        POST /api/auth/websocket
        Returns: ws_key (self._ws_key에 저장)
        connect() 전에 호출 필요.
        """

    async def _ensure_token(self):
        """토큰 만료 시 자동 갱신.

        모든 request() 호출 전에 체크.
        토큰 만료 5분 전에 선제적 갱신.
        """
        if (self._token_expires is None or
            datetime.now() >= self._token_expires - timedelta(minutes=5)):
            await self.authenticate()

    async def request(self, method: str, endpoint: str, api_id: str,
                      data: dict = None, params: dict = None) -> dict:
        """API 요청 실행 (인증 헤더 자동 포함, 토큰 자동 갱신)."""
        await self._ensure_token()
        await self._rate_limiter.wait()

    async def get_daily_ohlcv(self, code: str, start_date: str,
                               end_date: str) -> list[dict]:
        """일봉 데이터 조회."""

    async def get_minute_ohlcv(self, code: str, tick_range: int = 60,
                                count: int = 100) -> list[dict]:
        """분봉 데이터 조회."""

    async def get_current_price(self, code: str) -> dict:
        """현재가 조회."""

    async def send_order(self, code: str, qty: int, price: int,
                         order_type: int, hoga_type: str,
                         account: str) -> dict:
        """주문 전송."""

    async def get_account_balance(self, account: str) -> dict:
        """계좌 잔고 조회."""

    async def close(self):
        """클라이언트 종료."""
        await self._client.aclose()
```

- [ ] **Step 6: test_rest_client.py 작성**

httpx mock으로 테스트:
```python
import pytest
import httpx
from unittest.mock import AsyncMock, patch
from src.broker.rest_client import KiwoomRestClient

@pytest.fixture
async def client():
    c = KiwoomRestClient("https://api.kiwoom.com", "test_key", "test_secret")
    yield c
    await c.close()

class TestAuthentication:
    async def test_authenticate_returns_token(self, client):
        """토큰 발급 성공."""
    async def test_auto_refresh_expired_token(self, client):
        """만료된 토큰 자동 갱신."""

class TestDataQueries:
    async def test_get_daily_ohlcv(self, client):
        """일봉 데이터 조회."""
    async def test_get_current_price(self, client):
        """현재가 조회."""

class TestOrders:
    async def test_send_buy_order(self, client):
        """매수 주문 전송."""
    async def test_send_sell_order(self, client):
        """매도 주문 전송."""
```

- [ ] **Step 7: 테스트 실행**

Run: `python -m pytest tests/test_rest_client.py -v`
Expected: ALL PASS

- [ ] **Step 8: 커밋**

```bash
git add src/broker/rest_client.py src/broker/tr_codes.py tests/test_rest_client.py requirements.txt config.yaml .env.example
git commit -m "feat: add REST API client for Kiwoom"
```

### Task 3: WebSocket 클라이언트 구현

**Files:**
- Create: `src/broker/ws_client.py`
- Create: `tests/test_ws_client.py`

- [ ] **Step 1: ws_client.py 구현**

```python
"""키움 REST API WebSocket 클라이언트.

실시간 시세(체결/호가) 및 주문 체결 이벤트 수신.
"""
import asyncio
import json
from loguru import logger
import websockets
from src.models import Tick

class KiwoomWebSocketClient:
    """WebSocket 기반 실시간 데이터 클라이언트."""

    def __init__(self, ws_url: str, ws_key: str):
        self._ws_url = ws_url
        self._ws_key = ws_key
        self._ws = None
        self._running = False
        self.on_tick_callback = None       # Tick 수신 콜백
        self.on_order_callback = None      # 체결 수신 콜백

    async def connect(self):
        """WebSocket 연결 (접속키 포함)."""

    async def disconnect(self):
        """WebSocket 종료."""

    async def _reconnect(self, max_retries: int = 5, delay: float = 5.0):
        """자동 재연결 (최대 5회, 5초 간격).

        장중 연결 끊김 시 자동 복구.
        """
        for attempt in range(1, max_retries + 1):
            try:
                logger.warning(f"WebSocket 재연결 시도 ({attempt}/{max_retries})")
                await self.connect()
                logger.info("WebSocket 재연결 성공")
                return
            except Exception as e:
                logger.error(f"재연결 실패: {e}")
                if attempt < max_retries:
                    await asyncio.sleep(delay)
        logger.critical("WebSocket 최대 재연결 횟수 초과")

    async def subscribe(self, codes: list[str], real_type: str = "0B"):
        """실시간 시세 등록.

        Args:
            codes: 종목 코드 리스트
            real_type: "0B"(체결), "0D"(호가), "00"(주문체결)
        """
        msg = {
            "trnm": "REG",
            "grp_no": "1",
            "refresh": "1",
            "data": [{"item": codes, "type": [real_type]}]
        }
        await self._ws.send(json.dumps(msg))

    async def unsubscribe(self, codes: list[str], real_type: str = "0B"):
        """실시간 시세 해지."""
        msg = {
            "trnm": "REMOVE",
            "grp_no": "1",
            "data": [{"item": codes, "type": [real_type]}]
        }
        await self._ws.send(json.dumps(msg))

    async def _listen(self):
        """메시지 수신 루프."""
        async for message in self._ws:
            data = json.loads(message)
            await self._dispatch(data)

    async def _dispatch(self, data: dict):
        """수신 데이터 타입별 콜백 라우팅."""
```

- [ ] **Step 2: test_ws_client.py 작성**

WebSocket mock 테스트.

- [ ] **Step 3: 테스트 실행 및 커밋**

```bash
git add src/broker/ws_client.py tests/test_ws_client.py
git commit -m "feat: add WebSocket client for real-time data"
```

### Task 4: kiwoom_api.py를 REST 기반으로 교체

**Files:**
- Modify: `src/broker/kiwoom_api.py` (전면 재작성)
- Modify: `src/broker/order_manager.py`
- Modify: `src/broker/realtime_data.py`
- Modify: `tests/test_broker.py`

- [ ] **Step 1: kiwoom_api.py를 REST 래퍼로 교체**

OCX QAxWidget 상속 제거. `KiwoomRestClient` + `KiwoomWebSocketClient`를 조합한 통합 인터페이스:

```python
"""키움 REST API 통합 인터페이스.

REST 클라이언트 + WebSocket 클라이언트를 래핑하여
기존 engine.py 호환 인터페이스 제공.
"""
class KiwoomAPI:
    """키움 REST/WebSocket 기반 API 래퍼."""

    def __init__(self, base_url: str, ws_url: str,
                 appkey: str, secretkey: str):
        self._rest = KiwoomRestClient(base_url, appkey, secretkey)
        self._ws: KiwoomWebSocketClient | None = None
        self._ws_url = ws_url
        self._connected = False
        self.on_tick_callback = None
        self.on_chejan_callback = None

    async def connect(self):
        """인증 + WebSocket 연결."""
        await self._rest.authenticate()
        # WebSocket 접속키 발급 후 연결
        self._connected = True

    async def disconnect(self):
        """연결 종료."""

    # 데이터 조회 (REST)
    async def get_daily_ohlcv(self, code, start_date, adj_price=True):
        return await self._rest.get_daily_ohlcv(code, start_date, ...)

    async def get_minute_ohlcv(self, code, tick_range=60, count=100):
        return await self._rest.get_minute_ohlcv(code, tick_range, count)

    # 주문 (REST)
    async def send_order(self, code, qty, price, order_type, hoga_type, account):
        return await self._rest.send_order(...)

    # 실시간 (WebSocket)
    async def subscribe_realtime(self, codes):
        await self._ws.subscribe(codes, "0B")

    async def unsubscribe_realtime(self, codes):
        await self._ws.unsubscribe(codes, "0B")

    @property
    def connected(self):
        return self._connected
```

- [ ] **Step 2: order_manager.py 업데이트**

`execute_order`를 async로 변경:
```python
async def execute_order(self, code, qty, price, order_type, hoga_type) -> OrderResult:
    # 입력 검증 (기존 유지)
    self._rate_limiter.wait()
    result = await self._kiwoom.send_order(
        code, qty, price, order_type, hoga_type, self._account
    )
    ...
```

- [ ] **Step 3: realtime_data.py 업데이트**

WebSocket 기반으로 변경:
```python
async def subscribe(self, code: str):
    if code not in self._subscribed_codes:
        self._subscribed_codes.add(code)
        await self._kiwoom.subscribe_realtime([code])

async def subscribe_list(self, codes: list[str]):
    new_codes = [c for c in codes if c not in self._subscribed_codes]
    if new_codes:
        self._subscribed_codes.update(new_codes)
        await self._kiwoom.subscribe_realtime(new_codes)
```

- [ ] **Step 4: test_broker.py 교체**

OCX mock → REST/WebSocket mock:
```python
@pytest.fixture
def mock_rest_client():
    """REST 클라이언트 mock."""
    mock = AsyncMock()
    mock.authenticate.return_value = "test_token"
    mock.send_order.return_value = {"return_code": 0, "ord_no": "ORD001"}
    return mock
```

- [ ] **Step 5: 테스트 실행**

Run: `python -m pytest tests/test_broker.py -v`
Expected: ALL PASS

- [ ] **Step 6: 커밋**

```bash
git add src/broker/ tests/test_broker.py
git commit -m "refactor: replace OCX broker with REST API"
```

---

## Chunk 3: 엔진 asyncio 전환

### Task 5a: engine.py + main.py asyncio 전환

**Files:**
- Modify: `src/engine.py`
- Modify: `src/strategy/screener.py` (async 데이터 조회)
- Modify: `main.py`

- [ ] **Step 1: engine.py asyncio 전환**

핵심 변경:
- `QtScheduler` → `AsyncIOScheduler`
- 모든 broker 호출을 `await`로 변경
- PyQt5 import 완전 제거

```python
"""TradingEngine — asyncio 기반 전체 모듈 조율자."""
import asyncio
from apscheduler.schedulers.asyncio import AsyncIOScheduler

class TradingEngine:
    def __init__(self, mode="paper"):
        self.mode = mode
        self._running = False

        # 모듈 초기화
        self._ds = DataStore()
        self._ds.connect()
        self._ds.create_tables()

        # REST API 설정
        base_url = config.get("broker.base_url", "https://api.kiwoom.com")
        ws_url = config.get("broker.ws_url", "wss://api.kiwoom.com:10000/api/dostk/websocket")
        appkey = config.get_env("KIWOOM_APPKEY", "")
        secretkey = config.get_env("KIWOOM_SECRETKEY", "")

        self._kiwoom = KiwoomAPI(base_url, ws_url, appkey, secretkey)
        # ... 나머지 초기화 ...

        self._scheduler = AsyncIOScheduler()

    async def start(self):
        """메인루프 시작."""
        self._running = True
        # 스케줄러 등록
        self._scheduler.add_job(self._pre_market_screening, "cron", ...)
        self._scheduler.start()
        # 키움 연결
        await self._kiwoom.connect()

    async def stop(self):
        """시스템 중지."""
        self._running = False
        self._scheduler.shutdown(wait=False)
        await self._kiwoom.disconnect()
        self._ds.close()

    async def on_price_update(self, tick: Tick):
        """실시간 시세 수신 콜백 (async)."""
        if not self._running or self._risk_mgr.is_halted:
            return
        await self._check_exit_conditions(tick)
        if tick.code in self._candidates:
            await self._check_entry_conditions(tick)

    async def _execute_sell(self, position, price, reason):
        """매도 실행 (async)."""
        if self.mode == "live":
            result = await self._order_mgr.execute_order(...)
            if not result.success:
                return
        # ... 기존 로직 유지 ...
```

- [ ] **Step 2: main.py asyncio 전환**

```python
"""메인 진입점 — asyncio 기반."""
import argparse
import asyncio
import sys
from loguru import logger

async def main():
    parser = argparse.ArgumentParser(description="스윙 자동매매 시스템")
    parser.add_argument("--mode", choices=["paper", "live"], default="paper")
    args = parser.parse_args()

    from src.utils.logger import setup_logger
    from src.utils.config import config
    setup_logger(log_level=config.get_env("LOG_LEVEL", "INFO"))

    if args.mode == "live":
        if not config.get_env("TELEGRAM_BOT_TOKEN"):
            logger.error("LIVE 모드: TELEGRAM_BOT_TOKEN 필수")
            sys.exit(1)

    from src.engine import TradingEngine
    engine = TradingEngine(mode=args.mode)

    try:
        await engine.start()
        # 종료 시그널 대기
        while engine._running:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        logger.info("사용자 중단")
    finally:
        await engine.stop()

if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 3: screener.py async 전환 (pykrx 호출은 유지, REST 조회는 향후)**

Screener에서 KiwoomAPI 대신 pykrx를 계속 사용하되, 필요한 경우 async wrapper 적용.

- [ ] **Step 4: 커밋**

```bash
git add src/engine.py src/strategy/screener.py main.py
git commit -m "refactor: convert engine and main to asyncio"
```

### Task 5b: conftest.py + 테스트 fixture 재구성

**Files:**
- Modify: `tests/conftest.py`
- Modify: `pyproject.toml` (asyncio_mode = "auto")

- [ ] **Step 1: pyproject.toml에 pytest-asyncio 설정 추가**

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
```

- [ ] **Step 2: conftest.py 업데이트**

AsyncMock 기반 fixture로 교체:
```python
@pytest.fixture
def mock_kiwoom():
    """KiwoomAPI async mock."""
    mock = AsyncMock()
    mock._connected = True
    mock.connect = AsyncMock()
    mock.send_order = AsyncMock(return_value={"return_code": 0, "ord_no": "ORD001"})
    mock.subscribe_realtime = AsyncMock()
    mock.unsubscribe_realtime = AsyncMock()
    mock.disconnect = AsyncMock()
    return mock
```

- [ ] **Step 3: 커밋**

```bash
git add tests/conftest.py pyproject.toml
git commit -m "refactor: async test fixtures and pytest-asyncio config"
```

### Task 5c: test_engine.py + E2E 테스트 async 전환

**Files:**
- Modify: `tests/test_engine.py`
- Modify: `tests/e2e/test_buy_flow.py`
- Modify: `tests/e2e/test_stop_loss_flow.py`
- Modify: `tests/e2e/test_halt_flow.py`
- Modify: `tests/e2e/test_paper_trading.py`

- [ ] **Step 1: test_engine.py asyncio 테스트로 교체**

`@pytest.mark.asyncio` 데코레이터 적용:
```python
import pytest

class TestOnPriceUpdate:
    @pytest.mark.asyncio
    async def test_ignored_when_halted(self, trading_engine):
        """halt 상태에서 무시."""
        trading_engine._risk_mgr._is_halted = True
        tick = Tick(code="005930", price=50000, volume=100, ...)
        await trading_engine.on_price_update(tick)
        # 아무 동작 없음 확인
```

- [ ] **Step 5: E2E 테스트 asyncio 전환**

모든 E2E 테스트에 `@pytest.mark.asyncio` 적용.

- [ ] **Step 6: requirements.txt에 pytest-asyncio 추가**

```
pytest-asyncio>=0.23
```

- [ ] **Step 7: 전체 테스트 실행**

Run: `python -m pytest tests/ -v`
Expected: ALL PASS

- [ ] **Step 8: 커밋**

```bash
git add src/engine.py main.py tests/ requirements.txt
git commit -m "refactor: migrate engine from PyQt5 to asyncio"
```

---

## Chunk 4: 통합 및 검증

### Task 6: 전체 통합 테스트 + 보안 검토

**Files:**
- Modify: `tests/e2e/test_buy_flow.py`
- Modify: `tests/e2e/test_stop_loss_flow.py`
- Modify: `tests/e2e/test_halt_flow.py`
- Modify: `tests/e2e/test_paper_trading.py`

- [ ] **Step 1: E2E 테스트 async 전환 완료 확인**

모든 E2E 테스트가 asyncio mock으로 동작하는지 확인.

- [ ] **Step 2: 보안 검토**

체크리스트:
- [ ] appkey/secretkey가 .gitignore에 포함되는지 확인
- [ ] 로그에 토큰/키가 출력되지 않는지 확인
- [ ] REST 요청에 HTTPS 사용 확인
- [ ] pre_check 우회 불가 확인
- [ ] rate_limiter 정상 동작 확인

- [ ] **Step 3: 전체 테스트 실행**

Run: `python -m pytest tests/ -v --tb=short`
Expected: ALL PASS

- [ ] **Step 4: 커밋**

```bash
git add -A
git commit -m "test: update E2E tests for async REST API architecture"
```

### Task 7: 문서 업데이트

**Files:**
- Modify: `docs/ARCHITECTURE.md`
- Modify: `docs/KIWOOM_SPEC.md`

- [ ] **Step 1: ARCHITECTURE.md 업데이트**

OCX 관련 내용을 REST API로 수정.

- [ ] **Step 2: KIWOOM_SPEC.md 업데이트**

REST API 엔드포인트, WebSocket 연결 방법 추가.

- [ ] **Step 3: 커밋**

```bash
git add docs/
git commit -m "docs: update specs for REST API architecture"
```

---

## 검증 방법

1. 각 Task 완료 후: `pytest tests/ -v` 전체 통과
2. Task 1 완료 후: `python -m src.backtest.engine --codes 005930 --period 2y` → 거래 수 10건 이상
3. Task 4 완료 후: `python -m src.backtest.engine --codes 005930 000660 035720 035420 373220 --period 2y` → 거래 수 50건 이상
4. Task 5 완료 후: `python main.py --mode paper` → asyncio 이벤트루프 정상 시작
5. 전체 완료 후: 모든 테스트 통과 + 보안 체크리스트 충족

## 핵심 참조 파일

- `docs/ARCHITECTURE.md` — 시스템 구조, 데이터 흐름
- `docs/KIWOOM_SPEC.md` — 키움 API 스펙 (OCX → REST 참조)
- `docs/STRATEGY_SPEC.md` — 매수 AND, 매도 OR 조건
- `docs/RISK_SPEC.md` — pre_check, 포지션 사이징
- `config.yaml` — 전략/리스크 파라미터
- [키움 REST API 공식](https://openapi.kiwoom.com) — REST 엔드포인트 레퍼런스
- [kiwoom-restful PyPI](https://pypi.org/project/kiwoom-restful/) — Python 라이브러리 참고
