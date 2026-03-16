# 명령어 가이드

## 사전 준비

### 1. 의존성 설치

```bash
pip install -r requirements.txt
```

> Python 3.14에서 pandas-ta 설치 시:
> ```bash
> pip install pandas-ta --no-deps
> ```

### 2. 환경변수 설정

```bash
# .env.example을 복사하여 실제 값 입력
cp .env.example .env
```

`.env` 파일 내용:
```env
KIWOOM_APPKEY=your_appkey          # 키움 REST API 앱키
KIWOOM_SECRETKEY=your_secretkey    # 키움 REST API 시크릿키
KIWOOM_ACCOUNT=your_account        # 키움 계좌번호
TELEGRAM_BOT_TOKEN=your_bot_token  # 텔레그램 봇 토큰
TELEGRAM_CHAT_ID=your_chat_id      # 텔레그램 채팅 ID
CONFIG_PATH=./config.yaml          # 설정 파일 경로 (선택)
LOG_LEVEL=INFO                     # 로그 레벨 (선택)
```

---

## 매매 실행

### 모의매매 (Paper Trading)

실제 주문을 하지 않고 DB에 기록만 합니다. 키움 API 연결이 필요합니다.

```bash
python main.py --mode paper
```

### 실거래

키움 REST API를 통해 실제 주문을 실행합니다. 텔레그램 봇 설정이 필수입니다.

```bash
python main.py --mode live
```

---

## 백테스트

### 기본 백테스트

```bash
# 삼성전자 2년치 백테스트
python -m src.backtest.engine --codes 005930 --period 2y

# 다종목 백테스트
python -m src.backtest.engine --codes 005930 000660 035720 035420 --period 2y

# 날짜 범위 직접 지정
python -m src.backtest.engine --codes 005930 --start 20200101 --end 20250314

# 자본금 변경 (기본: 100만원)
python -m src.backtest.engine --codes 005930 --period 2y --capital 1000000
```

실행 완료 시 자동으로:
- 콘솔에 성과 요약 출력
- `reports/backtest_YYYYMMDD_HHMMSS.html` 리포트 생성 (에퀴티 커브 차트 + 거래 상세 포함)

### 파라미터 최적화 (그리드 서치)

```bash
python -m src.backtest.engine --codes 005930 000660 --period 2y --optimize
```

### 4개 전략 비교 백테스트

모멘텀 브레이크아웃, 평균회귀, 듀얼 모멘텀, 골든크로스를 동일 조건으로 비교합니다.

```bash
python -m src.backtest.strategy_compare
```

### 포트폴리오 백테스트 (현재 최적 설정)

100만원 자본금, 20종목, 동시 최대 3종목 보유로 포트폴리오 레벨 백테스트를 실행합니다.

```bash
python scripts/portfolio_backtest.py
```

### 골든크로스 파라미터 최적화

핵심 파라미터를 자동 탐색합니다.

```bash
python scripts/optimize_gc.py
```

---

## 테스트

### 전체 테스트 실행

```bash
pytest tests/ -v
```

### 특정 모듈만 테스트

```bash
pytest tests/test_risk.py -v          # 리스크 관리
pytest tests/test_broker.py -v        # 브로커 (REST API)
pytest tests/test_strategy.py -v      # 전략 (신호 생성)
pytest tests/test_engine.py -v        # TradingEngine
pytest tests/test_backtest.py -v      # 백테스트 엔진
pytest tests/test_datastore.py -v     # SQLite DB
pytest tests/test_telegram.py -v      # 텔레그램 알림
```

### E2E 통합 테스트만

```bash
pytest tests/e2e/ -v
```

### 특정 테스트 케이스만

```bash
pytest tests/test_risk.py::TestPositionSizer -v
pytest tests/e2e/test_buy_flow.py -v
```

### 커버리지 측정

```bash
pytest tests/ -v --cov=src --cov=data --cov-report=term-missing
```

---

## 코드 품질

### 포매터 (Black)

```bash
black src/ tests/ scripts/
```

### 린터 (Ruff)

```bash
ruff check src/ tests/ scripts/
```

---

## 데이터베이스 (SQLite)

### DB 파일 생성

프로그램 첫 실행 시 자동 생성되지만, 직접 생성할 수도 있습니다.

```bash
python -c "
from src.datastore import DataStore
ds = DataStore('trading.db')
ds.connect()
ds.create_tables()
print('DB 생성 완료: trading.db')
ds.close()
"
```

### DB 내용 확인

```bash
# 테이블 목록
sqlite3 trading.db ".tables"

# 보유 포지션 확인
sqlite3 trading.db "SELECT * FROM positions WHERE status='open';"

# 매매 이력 확인
sqlite3 trading.db "SELECT * FROM trades ORDER BY executed_at DESC LIMIT 10;"

# 일일 성과 확인
sqlite3 trading.db "SELECT * FROM daily_performance ORDER BY date DESC LIMIT 5;"
```

> SQLite CLI가 없으면 [DB Browser for SQLite](https://sqlitebrowser.org/)로 GUI에서 확인 가능

---

## 출력 파일 위치

| 파일/폴더 | 내용 |
|-----------|------|
| `trading.db` | SQLite 데이터베이스 (포지션, 매매기록, 성과) |
| `logs/trading_YYYYMMDD.log` | 일반 로그 (시스템 동작) |
| `logs/trades_YYYYMMDD.log` | 매매 전용 로그 (주문 실행 내역) |
| `reports/*.html` | 백테스트 HTML 리포트 (차트 포함) |
| `config.yaml` | 매매 전략/리스크 설정 |
| `.env` | API 키, 계좌번호 등 민감 정보 |

---

## 자주 쓰는 작업 순서

### 처음 시작할 때

```bash
pip install -r requirements.txt          # 1. 의존성 설치
cp .env.example .env                     # 2. 환경변수 파일 생성 (.env 편집)
pytest tests/ -v                         # 3. 테스트 확인 (전체 통과 확인)
python scripts/portfolio_backtest.py     # 4. 백테스트 실행
# reports/ 폴더에서 HTML 리포트 확인     # 5. 결과 확인
```

### 백테스트 후 실거래 전환 시

```bash
python scripts/portfolio_backtest.py     # 1. 최종 백테스트 확인
python main.py --mode paper              # 2. 모의매매 (2주 이상 검증)
# 텔레그램으로 일간 리포트 확인           # 3. 실시간 모니터링
python main.py --mode live               # 4. 실거래 전환 (소액부터)
```

### 전략 파라미터 변경 시

```bash
# config.yaml 수정 후
pytest tests/ -v                         # 1. 테스트 통과 확인
python scripts/portfolio_backtest.py     # 2. 변경된 파라미터로 백테스트
# reports/ 결과 비교                      # 3. 이전 결과와 비교
```
