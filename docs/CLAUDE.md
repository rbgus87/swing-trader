# CLAUDE.md — 스윙 자동매매 시스템

Claude Code가 이 프로젝트 작업 시 가장 먼저 읽어야 하는 파일.

## 프로젝트 개요

국내 주식(KOSPI/KOSDAQ) 스윙매매 자동화 시스템.
키움 REST API(httpx)로 주문 실행, WebSocket(websockets)으로 실시간 시세 수신, pandas 기반 자체 백테스트 엔진, 텔레그램으로 알림 발송.

## 핵심 제약사항 (절대 위반 금지)

### 실거래 안전 규칙
- `order_manager.py`의 `execute_order()` 함수는 반드시 `RiskManager.pre_check()` 통과 후에만 호출
- 포지션 사이징은 `risk/position_sizer.py`의 `PositionSizer` 클래스만 사용 — 임의 계산 금지
- 일일 최대 손실 -3% 초과 시 `TradingEngine.halt()` 자동 호출 — 이 로직 절대 제거 금지
- 실거래(`LIVE=True`) 모드에서는 텔레그램 알림 없이 주문 불가

### 키움 API 제약
- REST API 기반 → OS 무관, asyncio 이벤트루프 사용
- 초당 요청 제한: TR 조회 5건/초, 주문 5건/초 — `AsyncRateLimiter` 반드시 사용
- base_url은 반드시 https:// 사용 (rest_client.py에서 강제 검증)
- 장 시간 외 자동 주문 시도 금지 (09:00~15:30 체크 필수)

### 데이터 처리
- pykrx 데이터의 컬럼명은 한글 반환 → `data/column_mapper.py`로 영문 변환 후 사용
- ROE는 pykrx에서 직접 제공 안 함 → `EPS / BPS * 100` 수동 계산
- BPS ≤ 0 종목은 퀄리티 팩터 계산에서 제외

## 디렉터리 구조

```
swing-trader/
├── CLAUDE.md               ← 지금 이 파일
├── README.md               ← 프로젝트 소개
├── docs/
│   ├── PRD.md              ← 제품 요구사항
│   ├── ARCHITECTURE.md     ← 시스템 아키텍처
│   ├── KIWOOM_SPEC.md      ← 키움 API 연동 명세
│   ├── STRATEGY_SPEC.md    ← 전략 명세
│   ├── RISK_SPEC.md        ← 리스크 관리 명세
│   ├── BACKTEST_SPEC.md    ← 백테스트 명세
│   └── TELEGRAM_SPEC.md    ← 알림 명세
├── src/
│   ├── broker/             ← 키움 API 연동
│   │   ├── kiwoom_api.py   ← REST/WS 래퍼
│   │   ├── rest_client.py  ← REST 클라이언트 (httpx)
│   │   ├── ws_client.py    ← WebSocket 클라이언트 (websockets)
│   │   ├── order_manager.py
│   │   └── realtime_data.py
│   ├── strategy/           ← 매매 전략
│   │   ├── base_strategy.py
│   │   ├── signals.py      ← 지표 & 신호 생성
│   │   └── screener.py     ← 종목 스크리닝
│   ├── risk/               ← 리스크 관리
│   │   ├── risk_manager.py
│   │   ├── position_sizer.py
│   │   └── stop_manager.py
│   ├── backtest/           ← pandas 기반 백테스트
│   │   ├── engine.py
│   │   ├── optimizer.py
│   │   └── report.py
│   ├── notification/       ← 텔레그램
│   │   └── telegram_bot.py
│   └── utils/
│       ├── logger.py
│       ├── config.py
│       └── market_calendar.py
├── data/
│   ├── column_mapper.py    ← pykrx 컬럼 한→영 변환
│   └── cache/              ← 일봉 캐시 (gitignore)
├── tests/
├── logs/                   ← 매매 로그 (gitignore)
├── config.yaml             ← 설정 파일 (secrets 제외)
└── .env                    ← API 키: KIWOOM_APPKEY, KIWOOM_SECRETKEY 등 (gitignore 필수)
```

## 기술 스택

| 구분 | 라이브러리 | 비고 |
|------|-----------|------|
| 브로커 | 키움 REST API (httpx + websockets) | OS 무관, asyncio 기반 |
| 이벤트루프 | asyncio | REST/WebSocket 비동기 처리 |
| 데이터 | pykrx, pandas | 일봉/재무 데이터 |
| 지표 | pandas-ta | TA-Lib 대체 (설치 간편) |
| 백테스트 | pandas (자체 구현) | 파라미터 최적화 포함 |
| 스케줄링 | APScheduler | 장 시작/마감 작업 |
| DB | SQLite | 매매일지, 포지션 상태 |
| 알림 | requests (Telegram Bot API) | 동기 직접 호출 |
| 설정 | python-dotenv + PyYAML | .env + config.yaml |
| 로깅 | loguru | 파일 + 콘솔 동시 출력 |

## 개발 명령어

```bash
# 환경 설정
pip install -r requirements.txt

# 백테스트 실행 (장 외 가능)
python -m src.backtest.engine --strategy macd_rsi --period 2y

# 파라미터 최적화
python -m src.backtest.optimizer --strategy macd_rsi --grid

# 시뮬레이션 (Paper Trading)
python main.py --mode paper

# 실거래 (신중하게)
python main.py --mode live

# 테스트
pytest tests/ -v
```

## 코딩 컨벤션

- 모든 주문 함수에는 `# RISK_CHECK_REQUIRED` 주석 필수
- 키움 TR 코드는 상수로 정의 (`src/broker/tr_codes.py`)
- 금액 단위: 원화는 항상 `int`, 비율은 `float` (0.0~1.0)
- 로그: 주문 실행은 반드시 `logger.trade()` (별도 핸들러)
- 타임존: `Asia/Seoul` 명시 (`pytz` 또는 `zoneinfo`)

## 알려진 이슈 & 주의사항

- pykrx `get_market_ohlcv_by_ticker()` 반환 컬럼: `['시가','고가','저가','종가','거래량','거래대금','등락률']`
- 키움 API 연결은 장 시작 전(08:30~08:50) 사이에 초기화 권장
- REST API 인증 토큰은 23시간 후 자동 갱신 (만료 5분 전 선제 갱신)
- 거래세 0.2% + 수수료 0.015% 백테스트에 반드시 반영
