# swing-trader

KOSPI/KOSDAQ 국내 주식 스윙매매 자동화 시스템. 키움증권 OpenAPI+를 통해 실시간 시세 수신, MACD-RSI 기반 매매 신호 생성, 자동 주문 실행까지 전 과정을 자동화합니다.

Paper(모의) 모드와 Live(실거래) 모드를 지원하며, vectorbt 기반 백테스트로 전략 파라미터를 최적화할 수 있습니다.

## 주요 기능

- **자동 스크리닝** - 거래대금/시가총액 기준 유니버스 자동 필터링 (pykrx)
- **MACD-RSI 스윙 전략** - 매수 AND 조건, 매도 OR 조건으로 신호 생성
- **리스크 관리** - 하프켈리 포지션 사이징, ATR 기반 손절, 트레일링 스탑
- **안전 장치** - 일일 손실 한도(-3%), 장 시간 외 주문 차단, 요청 제한(5건/초)
- **백테스트** - vectorbt 기반 그리드서치 + Walk-Forward 검증
- **텔레그램 알림** - 매수/매도/손절/일일리포트 등 8종 메시지 실시간 발송
- **Paper/Live 모드** - 모의매매로 검증 후 실거래 전환

## 시스템 아키텍처

```
┌─────────────────────────────────────────────────────────────┐
│                        main.py                              │
│                     (PyQt5 EventLoop)                       │
├─────────────────────────────────────────────────────────────┤
│                      TradingEngine                          │
│              (Paper/Live 모드 통합 관리)                      │
├──────────┬──────────┬──────────┬──────────┬─────────────────┤
│ Strategy │   Risk   │  Broker  │ Notifier │   Backtest      │
│  Layer   │  Layer   │  Layer   │  Layer   │   Engine        │
│          │          │          │          │                 │
│ Screener │ Sizer    │ KiwoomAPI│ Telegram │ vectorbt        │
│ Signals  │ StopMgr  │ OrderMgr │          │ Optimizer       │
│          │ RiskMgr  │ Realtime │          │ Report          │
├──────────┴──────────┴──────────┴──────────┴─────────────────┤
│                    DataStore (SQLite)                        │
├─────────────────────────────────────────────────────────────┤
│               Utils (Config, Logger, Calendar)              │
└─────────────────────────────────────────────────────────────┘
```

## 기술 스택

| 구분 | 기술 |
|------|------|
| 언어 | Python 3.10+ |
| 이벤트루프 | asyncio (REST/WebSocket 비동기 처리) |
| 브로커 API | 키움 REST API (httpx + websockets) |
| 시장 데이터 | pykrx |
| 기술 지표 | pandas-ta |
| 백테스트 | vectorbt |
| 데이터베이스 | SQLite |
| 스케줄러 | APScheduler |
| 알림 | Telegram Bot API |
| 로깅 | loguru |
| 린터/포매터 | ruff, black |

## 설치 방법

### 사전 요구사항

- Windows 10/11
- Python 3.10+ (32bit 또는 64bit)
- 키움증권 OpenAPI+ 설치 및 인증서 등록

### 설치

```bash
# 저장소 클론
git clone https://github.com/your-org/swing-trader.git
cd swing-trader

# 가상환경 생성 및 활성화
python -m venv venv
venv\Scripts\activate

# 의존성 설치
pip install -r requirements.txt
```

### 환경변수 설정

```bash
# .env 파일 생성
copy .env.example .env
```

`.env` 파일을 열어 실제 값을 입력합니다:

```dotenv
KIWOOM_ACCOUNT=your_account_number    # 키움 계좌번호 (필수)
TELEGRAM_BOT_TOKEN=your_bot_token     # 텔레그램 봇 토큰 (Live 모드 필수)
TELEGRAM_CHAT_ID=your_chat_id         # 텔레그램 채팅 ID (Live 모드 필수)
```

## 사용 방법

### Paper 모드 (모의매매)

```bash
python main.py --mode paper
```

텔레그램 없이도 실행 가능합니다. 주문은 실제로 전송되지 않고 내부 시뮬레이션으로 처리됩니다.

### Live 모드 (실거래)

```bash
python main.py --mode live
```

Live 모드는 반드시 `TELEGRAM_BOT_TOKEN`이 설정되어 있어야 합니다. 장 시간(09:00~15:30)에만 주문이 실행됩니다.

### 백테스트

```python
from src.backtest.engine import BacktestEngine

engine = BacktestEngine()
result = engine.run(
    symbol="005930",
    start_date="2025-01-01",
    end_date="2025-12-31",
)
```

### 설정 변경

`config.yaml` 파일에서 전략 파라미터, 리스크 한도, 스케줄 등을 조정할 수 있습니다.

## 디렉터리 구조

```
swing-trader/
├── main.py                    # 진입점
├── config.yaml                # 시스템 설정
├── requirements.txt           # Python 의존성
├── pyproject.toml             # ruff/black/pytest 설정
├── .env.example               # 환경변수 템플릿
├── src/
│   ├── engine.py              # TradingEngine (통합 관리)
│   ├── models.py              # 데이터 모델
│   ├── datastore.py           # SQLite 저장소
│   ├── broker/                # 키움 OpenAPI+ 브로커
│   │   ├── kiwoom_api.py      # REST/WS 래퍼
│   │   ├── order_manager.py   # 주문 관리
│   │   ├── realtime_data.py   # 실시간 시세
│   │   ├── rate_limiter.py    # API 요청 제한
│   │   └── tr_codes.py        # TR 코드 정의
│   ├── strategy/              # 매매 전략
│   │   ├── signals.py         # MACD-RSI 신호 생성
│   │   ├── screener.py        # 유니버스 스크리닝
│   │   └── base_strategy.py   # 전략 베이스 클래스
│   ├── risk/                  # 리스크 관리
│   │   ├── risk_manager.py    # 리스크 매니저
│   │   ├── position_sizer.py  # 포지션 사이징
│   │   └── stop_manager.py    # 손절/트레일링 관리
│   ├── backtest/              # 백테스트 엔진
│   │   ├── engine.py          # vectorbt 기반 백테스트
│   │   ├── optimizer.py       # 그리드서치 최적화
│   │   └── report.py          # 성과 리포트
│   ├── notification/          # 알림
│   │   └── telegram_bot.py    # 텔레그램 봇
│   └── utils/                 # 유틸리티
│       ├── config.py          # 설정 로더
│       ├── logger.py          # 로그 설정
│       └── market_calendar.py # 장 운영일/시간 판단
├── data/
│   ├── column_mapper.py       # 컬럼 매핑
│   └── cache/                 # 시세 캐시
├── tests/                     # 테스트
├── docs/                      # 설계 문서
├── logs/                      # 로그 파일
└── reports/                   # 백테스트 리포트
```

## 안전 규칙

| 규칙 | 설명 |
|------|------|
| 리스크 체크 필수 | 모든 주문은 `RiskManager.pre_check()` 통과 후 실행 |
| 일일 손실 한도 | -3% 초과 시 자동 매매 중단 (halt) |
| 텔레그램 필수 | Live 모드에서 텔레그램 미설정 시 실행 불가 |
| 장 시간 제한 | 09:00~15:30 외 자동 주문 차단 |
| API 제한 | TR 5건/초, 주문 5건/초 rate limiting 적용 |

## 라이선스

MIT License
