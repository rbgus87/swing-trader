# ARCHITECTURE.md — 시스템 아키텍처

## 1. 전체 구조

```
┌─────────────────────────────────────────────────────────┐
│                    main.py (진입점)                       │
│              asyncio 이벤트루프 기반 단일 프로세스           │
└───────────────┬─────────────────────────┬───────────────┘
                │                         │
       ┌────────▼────────┐      ┌─────────▼────────┐
       │  TradingEngine  │      │  APScheduler      │
       │  (핵심 조율자)   │      │  (장 시작/마감 작업) │
       └────────┬────────┘      └─────────┬────────┘
                │                         │
    ┌───────────┼──────────────────────────┤
    │           │                          │
┌───▼───┐  ┌───▼───────┐  ┌──────────▼──────────┐
│Broker │  │ Strategy  │  │    RiskManager        │
│Layer  │  │ Layer     │  │                       │
├───────┤  ├───────────┤  ├───────────────────────┤
│Kiwoom │  │Screener   │  │PositionSizer          │
│API    │  │Signals    │  │StopManager            │
│Order  │  │MTF 분석   │  │DailyLimitChecker      │
│Mgr    │  │           │  │                       │
└───┬───┘  └─────┬─────┘  └────────────┬──────────┘
    │             │                      │
    └─────────────▼──────────────────────┘
                  │
         ┌────────▼────────┐
         │  DataStore      │
         │  (SQLite)       │
         │  - positions    │
         │  - trades       │
         │  - ohlcv_cache  │
         └────────┬────────┘
                  │
         ┌────────▼────────┐
         │  Notification   │
         │  (Telegram)     │
         └─────────────────┘
```

## 2. 레이어별 책임

### TradingEngine (`src/engine.py`)
시스템의 핵심 조율자. 다른 모듈을 직접 인스턴스화하지 않고 인터페이스를 통해 호출.

```python
class TradingEngine:
    def __init__(self, mode: Literal['paper', 'live'])
    def start()          # 메인루프 시작
    def halt()           # 매매 중단 (일일 한도 초과 등)
    def on_signal(signal: Signal)    # 전략에서 신호 수신
    def on_price_update(tick: Tick)  # 실시간 시세 수신
```

### Broker Layer (`src/broker/`)

**kiwoom_api.py** — REST/WS 래퍼
```python
# 키움 API 이벤트 → Python 콜백 변환
OnReceiveTrData   → on_tr_data(tr_code, data)
OnReceiveChejanData → on_chejan(fid_dict)
OnReceiveRealData → on_realtime(code, data)
```

**order_manager.py** — 주문 실행
```python
# 반드시 RiskManager.pre_check() 통과 후 호출
def execute_order(code, qty, price, order_type) -> OrderResult
def cancel_order(order_no) -> bool
def get_pending_orders() -> list[Order]
```

**realtime_data.py** — 실시간 시세 관리
```python
def subscribe(code: str)    # 종목 실시간 등록
def unsubscribe(code: str)
def get_current_price(code) -> int
```

### Strategy Layer (`src/strategy/`)

**screener.py** — 장 마감 후 다음 날 후보 선정
```
pykrx 전종목 데이터 → 유동성 필터 → 지표 계산 → 신호 조건 체크 → 후보 리스트
```

**signals.py** — 실시간 신호 생성
```python
def generate_entry_signal(df_daily, df_60min) -> Signal | None
def generate_exit_signal(position, df_daily, current_price) -> ExitSignal | None
```

### Risk Layer (`src/risk/`)

**position_sizer.py** — 켈리/하프켈리 사이징
```python
def calculate(capital, win_rate, avg_win, avg_loss, method='half_kelly') -> float
# returns: 투자 비율 (0.0 ~ MAX_POSITION_RATIO)
```

**stop_manager.py** — 손절/트레일링스탑
```python
def get_initial_stop(entry_price, atr) -> int    # ATR × 1.5 기본
def update_trailing_stop(position, current_price, atr) -> int
def is_stopped(position, current_price) -> bool
```

**risk_manager.py** — 사전 리스크 체크
```python
def pre_check(signal: Signal) -> RiskCheckResult
# 체크 항목: 일일한도, 종목수, 종목당비율, 쿨다운
```

## 3. 데이터 흐름

### 장 시작 전 (08:30)
```
APScheduler 트리거
→ Screener.run_daily_screening()
  → pykrx 전종목 데이터 로드 (캐시 우선)
  → 유동성/필터 적용
  → 지표 계산 (pandas-ta)
  → 신호 조건 체크
  → 후보 종목 리스트 저장 (SQLite)
  → Telegram 알림 (당일 후보 N종목)
```

### 장 중 (09:00~15:30)
```
키움 OnReceiveRealData (체결가)
→ TradingEngine.on_price_update(tick)
  → StopManager.is_stopped() 체크
    → [손절 발동] OrderManager.execute_order(SELL)
  → 보유 종목 목표가 체크
    → [목표가 도달] OrderManager.execute_order(SELL)
  → 후보 종목 진입 조건 재확인
    → [진입 조건 충족] RiskManager.pre_check()
      → [통과] PositionSizer.calculate()
        → OrderManager.execute_order(BUY)
```

### 체결 이벤트
```
키움 OnReceiveChejanData
→ TradingEngine.on_chejan(data)
  → DataStore.record_trade()
  → Telegram.send_execution_alert()
  → 포지션 상태 업데이트
```

### 장 마감 (15:35)
```
APScheduler 트리거
→ DailyReport.generate()
  → 당일 매매 내역 집계
  → 수익률/MDD 업데이트
  → Telegram 일간 리포트 발송
→ Screener.prepare_next_day() (익일 준비)
```

## 4. SQLite 스키마

```sql
-- 포지션 현황
CREATE TABLE positions (
    id          INTEGER PRIMARY KEY,
    code        TEXT NOT NULL,
    name        TEXT,
    entry_date  TEXT NOT NULL,
    entry_price INTEGER NOT NULL,
    quantity    INTEGER NOT NULL,
    stop_price  INTEGER NOT NULL,
    target_price INTEGER,
    status      TEXT DEFAULT 'open',  -- open | closed
    updated_at  TEXT
);

-- 매매 이력
CREATE TABLE trades (
    id          INTEGER PRIMARY KEY,
    code        TEXT NOT NULL,
    name        TEXT,
    side        TEXT NOT NULL,  -- buy | sell
    price       INTEGER NOT NULL,
    quantity    INTEGER NOT NULL,
    amount      INTEGER NOT NULL,
    fee         REAL,
    tax         REAL,
    pnl         REAL,           -- 매도 시 손익
    pnl_pct     REAL,
    reason      TEXT,           -- signal | stop | target | trailing | max_hold
    executed_at TEXT NOT NULL
);

-- 일간 성과
CREATE TABLE daily_performance (
    date        TEXT PRIMARY KEY,
    realized_pnl REAL,
    unrealized_pnl REAL,
    total_capital REAL,
    daily_return REAL,
    mdd_current  REAL,
    trade_count  INTEGER
);

-- 일봉 캐시
CREATE TABLE ohlcv_cache (
    code   TEXT NOT NULL,
    date   TEXT NOT NULL,
    open   INTEGER, high INTEGER, low INTEGER, close INTEGER,
    volume INTEGER, amount INTEGER,
    PRIMARY KEY (code, date)
);
```

## 5. 설정 파일 구조 (`config.yaml`)

```yaml
trading:
  mode: paper                    # paper | live
  universe: kospi_kosdaq         # kospi | kosdaq | kospi_kosdaq
  max_positions: 5
  reentry_cooldown_days: 3

screening:
  min_daily_amount: 5_000_000_000    # 50억
  min_market_cap: 30_000_000_000     # 300억
  min_price: 1000
  max_price: 500000
  top_n: 30

strategy:
  timeframe_primary: D           # 일봉
  timeframe_entry: 60            # 60분봉
  macd_fast: 12
  macd_slow: 26
  macd_signal: 9
  rsi_period: 14
  rsi_entry_min: 40
  rsi_entry_max: 65
  bb_period: 20
  bb_std: 2.0
  volume_multiplier: 1.5
  ma_trend: 20
  max_hold_days: 15
  target_return: 0.08            # 8%

risk:
  max_position_ratio: 0.15       # 종목당 15%
  sizing_method: half_kelly
  stop_atr_multiplier: 1.5
  trailing_atr_multiplier: 2.0
  daily_loss_limit: -0.03        # -3%
  daily_loss_warning: -0.02      # -2% 경고
  max_mdd: -0.20                 # -20%

backtest:
  commission: 0.00015            # 0.015%
  tax: 0.0015                    # 0.15%
  slippage: 0.001                # 0.1%
  initial_capital: 10_000_000

schedule:
  screening_time: "08:30"
  daily_report_time: "16:00"
  reconnect_time: "08:45"
```

## 6. 에러 처리 전략

| 오류 유형 | 처리 방법 |
|---------|---------|
| 키움 API 연결 끊김 | 자동 재연결 (최대 5회, 30초 간격) + 텔레그램 알림 |
| TR 데이터 조회 실패 | 3회 재시도 후 캐시 데이터 사용 |
| 주문 미체결 | 3회 재주문 후 취소 + 알림 |
| 예외 발생 (미처리) | 로깅 + 텔레그램 알림 + 안전 중단 |
| 일일 한도 초과 | halt() 호출 + 알림 (당일 재개 불가) |
| pykrx 데이터 오류 | 전일 캐시 데이터 사용 + 경고 로그 |
