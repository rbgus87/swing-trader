# 실전 운영 가이드

## 1. 사전 준비

### 환경변수 설정 (.env)

```bash
# 필수
IS_PAPER_TRADING=True          # True=모의, False=실거래
KIWOOM_ACCOUNT=계좌번호
KIWOOM_APPKEY=키움_앱키
KIWOOM_SECRETKEY=키움_시크릿키
TELEGRAM_BOT_TOKEN=텔레그램_봇토큰
TELEGRAM_CHAT_ID=텔레그램_채팅ID

# 선택 (데이터 안정성 향상)
KRX_API_KEY=KRX_인증키         # 미설정 시 pykrx 폴백
DART_API_KEY=DART_인증키       # 재무제표 조회용
```

### 의존성 설치

```bash
pip install -r requirements.txt
pip install opendartreader     # DART 재무제표 (선택)
```

---

## 2. 실행 모드

### Paper Trading (모의매매)

```bash
# GUI 모드
python gui.py

# 데몬 모드 (헤드리스)
python main.py
```

- `.env`의 `IS_PAPER_TRADING=True` 확인
- 주문은 시뮬레이션, 시세는 실서버에서 수신
- 텔레그램 알림 정상 발송

### Live Trading (실거래)

```bash
# 반드시 IS_PAPER_TRADING=False 확인 후 실행
python gui.py
```

- 실거래 전 Paper 모드에서 최소 1주일 검증 권장
- 텔레그램 알림이 정상 동작하는지 반드시 확인

---

## 3. 일일 운영 흐름

```
08:30  장전 스크리닝 (자동)
       ├── 시장 국면 판단 (KOSPI 200일선 + VKOSPI)
       ├── adaptive 모드: 국면별 전략 전환
       ├── 종목 스크리닝 (Light Pre-Filter → Pre-Screen → 전략 신호)
       └── 텔레그램: "당일 매수 후보: N종목"

08:50  WebSocket 연결 (자동)
       └── 후보 + 보유 종목 실시간 구독

09:00  장 시작 + 일일 리셋
       ├── 진입 조건 체크 (30초 간격 쓰로틀링)
       ├── 부분 매도 / 트레일링 / 손절 / 목표가 자동 실행
       └── 일일 손실 -3% 초과 시 자동 매매 중단

15:30  장 마감

15:35  미체결 주문 정리 (자동)
       └── 취소 + selling 포지션 복원

16:00  일간 리포트 (자동)
       └── 텔레그램: 매수/매도/손익/보유현황

18:10  WebSocket 종료 (자동)
```

---

## 4. 백테스트

### 단일 종목 백테스트

```bash
python -m src.backtest.engine \
  --strategy golden_cross \
  --codes 005930 \
  --start 20220101 --end 20250314 \
  --capital 3000000
```

### 포트폴리오 백테스트 (실전과 동일)

```bash
python -m src.backtest.engine --portfolio \
  --strategy adaptive \
  --codes 005930 000660 005380 000270 068270 035420 035720 105560 055550 066570 \
  --start 20190101 --end 20250314 \
  --capital 3000000 --max-positions 3
```

- `--portfolio`: 하나의 자본금으로 다종목 순차 매매
- `--strategy adaptive`: 시장 국면별 전략 자동 전환
- 결과: `reports/portfolio_*.html`

### 파라미터 최적화

```bash
python -m src.backtest.engine --optimize \
  --codes 005930 000660 005380 \
  --start 20200101 --end 20250314
```

---

## 5. Walk-Forward 검증

파라미터 오버피팅을 감지하는 검증 도구.

### 언제 실행하나?

| 시점 | 이유 |
|------|------|
| 전략 파라미터 변경 후 | 새 파라미터의 견고성 확인 |
| 새 전략 추가 후 | 실전 투입 전 검증 |
| 분기 1회 (선택) | 시장 구조 변화 감지 |

매일 돌릴 필요 없음. "이 설정을 실전에 써도 되나?" 판단할 때 1회 실행.

### 실행

```bash
# 기본 (10종목, train 24개월, test 6개월, step 12개월 → ~3구간, ~2분)
python scripts/run_walk_forward.py --strategy golden_cross

# 빠른 검증 (5종목, ~1분)
python scripts/run_walk_forward.py \
  --strategy golden_cross \
  --codes 005930 000660 005380 035420 066570

# 전체 종목 (20종목, ~10분)
python scripts/run_walk_forward.py \
  --strategy golden_cross \
  --codes 005930 000660 005380 000270 068270 035420 035720 105560 055550 066570 006400 003670 012330 028260 096770 003550 034730 032830 030200 017670

# 독립 종목별 (포트폴리오 아닌 종목 개별)
python scripts/run_walk_forward.py --strategy golden_cross --independent
```

### 결과 해석

```
  최종 판정: 견고 (Robust) — 파라미터 신뢰 가능        → 실전 투입 OK
  최종 판정: 주의 — 일부 지표 열화, 파라미터 범위 축소 권장  → 범위 좁혀서 재실행
  최종 판정: 오버피팅 — 파라미터 재검토 필요             → 파라미터 원복
```

핵심 지표:
- **OOS 수익 양(+) 구간**: 50% 이상이면 양호
- **수익률 열화율**: IS 대비 OOS 성과 감소율, 30% 미만이면 양호
- **OOS 거래 수**: 0건이면 test 기간이 짧거나 종목이 부족 → 기간/종목 늘리기

---

## 6. 전략 운영

### adaptive 모드 (기본, 권장)

config.yaml:
```yaml
strategy:
  type: adaptive
  regime_strategy:
    trending: golden_cross    # 추세장: 골든크로스
    sideways: bb_bounce       # 횡보장: 볼린저밴드 반등
    # bearish: 매수 차단 (MarketRegime 게이트)
```

시장 국면 판단 기준:
- KOSPI > 200일선 AND VKOSPI <= 30 → 매수 허용
- ADX >= 25 → trending (golden_cross)
- ADX < 25 → sideways (bb_bounce)
- KOSPI < 200일선 OR VKOSPI > 30 → bearish (매수 차단)

### 부분 매도

config.yaml:
```yaml
strategy:
  partial_sell_enabled: true    # 활성화
  partial_target_pct: 0.5       # 목표가의 50% 도달 시 트리거
  partial_sell_ratio: 0.5       # 보유 수량의 50% 매도
```

예: 목표 수익률 6% → 3% 도달 시 절반 매도, 나머지는 트레일링

### 재진입 쿨다운

config.yaml:
```yaml
trading:
  reentry_cooldown_days: 3      # 기본 쿨다운
  reentry_cooldown_trend_days: 1 # 추세 유지 시 (SMA5>SMA20, ADX>임계) 단축
```

---

## 7. 리스크 관리

### 자동 안전 장치

| 항목 | 조건 | 동작 |
|------|------|------|
| 일일 손실 한도 | -3% | 당일 매매 중단 |
| 일일 손실 경고 | -2% | 텔레그램 경고 |
| 최대 낙폭 | -20% | 전체 매매 중단 |
| 장 시간 외 | 09:00 이전/15:30 이후 | 주문 차단 |
| 매도 재시도 | 3회 초과 | 매도 중단 + 텔레그램 알림 |

### 포지션 사이징

```yaml
risk:
  sizing_method: half_kelly     # 켈리 공식의 절반
  max_position_ratio: 0.15      # 종목당 최대 자본의 15%
  stop_atr_multiplier: 2.5      # 손절가 = 진입가 - ATR * 2.5
  trailing_activate_pct: 0.07   # +7% 수익 시 트레일링 활성화
```

---

## 8. 데이터 소스 (DataProvider)

시스템은 DataProvider를 통해 데이터를 조회합니다.

```
호출부 → DataProvider → KRX API (1순위, 공식)
                      → pykrx (2순위, 폴백)
                      → KODEX200 ETF (3순위, 인덱스 폴백)
```

- **KRX_API_KEY** 설정 시: 공식 API 사용 (안정적)
- **미설정 시**: pykrx 스크래핑 (KRX 사이트 변경 시 중단 위험)
- **KOSPI/VKOSPI**: KRX API → pykrx → KODEX200 ETF 순 폴백

---

## 9. 데이터 갱신 절차

### 일일 갱신 (자동 — daily_run.sh)

```bash
# 매일 장 마감 후 (16:00 이후) 자동 실행
bash scripts/daily_run.sh
```

내용: 일봉 OHLCV + 시총 + 신규 상장 감지 + 시그널 생성. GUI의 "🔄 일일 실행" 버튼과 동일.

### 월간 갱신 — 상장폐지 종목 (수동, 월 1회)

KRX OpenAPI는 폐지 엔드포인트를 제공하지 않아 KRX 정보데이터시스템에서 xls를
수동 다운로드 후 import해야 합니다. **월 1회 정도가 권장**.

**절차**:

1. 브라우저에서 `data.krx.co.kr` 접속 → "상장폐지현황" 검색 → xls 다운로드
2. 받은 파일을 `data/raw/delisting/상장폐지현황.xls`에 **덮어쓰기**
3. 실행 (cmd / PowerShell / Git Bash / WSL 모두 동일):

   ```bash
   python scripts/update_delisting.py
   ```

   옵션:
   ```bash
   python scripts/update_delisting.py --skip-candles  # xls 파싱만
   python scripts/update_delisting.py --skip-infer    # 추론 생략
   ```

**스크립트가 하는 일** (3단계, 모두 멱등):

| 단계 | 모듈 | 역할 |
|------|------|------|
| 1/3 | `import_delisted_list.main()`  | xls 파싱 → `stocks` 테이블에 INSERT/UPDATE. cutoff 2014-01-01 이후만 |
| 2/3 | `collect_daily_candles.main()` | 신규 폐지 종목의 일봉 OHLCV를 FDR로 수집 (생존편향 제거용) |
| 3/3 | `infer_delisted.infer_delisted()` | 일봉 마지막 거래일 + 임계값으로 `delisted_date` 역산 (KRX 폐지일 미제공 fallback) |

**ticker 재사용 처리**:
- 같은 ticker가 다른 종목으로 재발행된 경우 자동 감지 → `ticker_reuse_events` + `TICKER_REUSE_POLLUTED` anomaly 기록
- 기존 stocks 행은 보존, 새 폐지 정보는 무시 (충돌 방지)

**검증** (선택):

```bash
# 새 폐지 종목 정상 import 확인
python -c "
from src.data_pipeline.db import get_connection
with get_connection() as c:
    n = c.execute('SELECT COUNT(*) FROM stocks WHERE delisted_date IS NOT NULL').fetchone()[0]
    last = c.execute('SELECT ticker, name, delisted_date FROM stocks WHERE delisted_date IS NOT NULL ORDER BY delisted_date DESC LIMIT 5').fetchall()
print(f'폐지 종목 총 {n}건. 최근 5건:')
for r in last: print(f'  {r[\"ticker\"]} {r[\"name\"]} → {r[\"delisted_date\"]}')
"
```

**알려진 한계**:
- `delisted_date`는 일봉 마지막 거래일 + 임계값으로 추정 → **월 단위 정확도**. Universe 필터링에는 충분.
- KRX market 정보는 xls에 없어 폐지 종목은 `market='UNKNOWN'`으로 import. SQLite ALTER COLUMN 미지원으로 보정 불가.
- 일부 소형주(약 2%)는 KRX historical snapshot에 미반환 → 누락 가능.

---

## 10. 모니터링

### 텔레그램 알림 종류

| 알림 | 내용 |
|------|------|
| 시작/종료 | 엔진 시작/종료 |
| 매수 체결 | 종목, 가격, 수량, 손절가, 목표가 |
| 매도 체결 (수익) | 종목, 가격, 보유일, PnL, 순이익 |
| 매도 체결 (손실) | 종목, 가격, 보유일, PnL, 사유 |
| 일간 리포트 | 매수/매도 건수, 실현손익, 보유현황, MDD |
| 시장 방어 모드 | KOSPI < 200일선 또는 VKOSPI > 30 |
| 전략 전환 | adaptive 모드에서 국면별 전략 변경 |
| 시스템 오류 | API 연결 실패, 매도 재시도 초과 등 |

### 로그 파일

- `logs/` 디렉토리에 일별 로그 파일 생성
- GUI의 로그 탭에서 실시간 확인 가능 (레벨 필터)

---

## 11. WF 검증 최종 결과 (2026-03-22)

### 설정
- 전략: **adaptive** (실전 사용 전략, 국면별 자동 전환)
- 기간: 2019.01~2026.02 / Train 24개월, Test 3개월, Step 6개월 → 11구간
- 종목: 10개 대형주 / 자본금 300만원 / 최대 동시 3종목
- 그리드: 32조합 (랜덤 50% 샘플링, 최소 24개)

### OOS(미래 검증) 구간별 성과

| # | Test 기간 | 수익 | Sharpe | 승률 | 비고 |
|---|----------|------|--------|------|------|
| 1 | 2020.12~2021.03 | **+5.77%** | 3.52 | 100% | |
| 2 | 2021.06~2021.09 | -11.97% | -4.69 | 37.5% | KOSPI 하락 전환기 |
| 3 | 2021.12~2022.03 | +0.62% | 0.25 | 40.0% | |
| 4 | 2022.06~2022.09 | **+4.87%** | 1.96 | 100% | bearish bb_bounce |
| 5 | 2022.12~2023.03 | -3.99% | -0.99 | 57.1% | |
| 6 | 2023.06~2023.09 | -1.99% | -1.22 | 62.5% | |
| 7 | 2023.12~2024.03 | **+1.87%** | 2.57 | 83.3% | |
| 8 | 2024.06~2024.09 | **+4.07%** | 2.16 | 77.8% | |
| 9 | 2024.12~2025.03 | -4.89% | -1.62 | 0% | |
| 10 | 2025.05~2025.08 | **+4.69%** | 3.41 | 85.7% | |
| 11 | 2025.11~2026.02 | **+9.17%** | 3.76 | 85.0% | |

### 종합

| 지표 | 값 | 판정 |
|------|-----|------|
| OOS 평균 수익률 | **+0.75%** | 양(+) |
| OOS Sharpe | **0.83** | 양호 |
| OOS 양(+) 구간 | **7/11 (64%)** | 과반수 |
| OOS 승률 | **66.3%** | 양호 |
| Sharpe 열화 | **-31.8%** (OOS > IS) | 오버피팅 없음 |

### 적용된 개선 (효과 있음)

| 개선 | 효과 |
|------|------|
| 시장 필터 완화 — bearish에서 bb_bounce 허용 (포지션 1/3) | 거래 0건 구간 해소 |
| 연속 손실 쿨다운 — 3연패 시 5일간 진입 차단 | 급락 억제, Sharpe 열화 개선 |
| 거래량 필터 — 20일 평균 대비 0.8배 미만 차단 | OOS Sharpe 0.40→0.83 |

### 시도 후 롤백 (효과 없음)

| 시도 | 이유 |
|------|------|
| ATR 포지션 사이징 | 수익 기회도 축소 |
| 청산 로직 변경 (트레일링4%, 보유15일) | OOS 악화 |
| 앙상블 파라미터 (상위3 중앙값) | 엣지도 평활화 |

### 알려진 한계

- 수익률 열화 90%: IS 과적합은 그리드 서치의 구조적 한계이나 OOS가 양(+)이므로 실용적 문제 없음
- 구간2 (-11.97%): KOSPI 하락 전환 초기 연속 손실. 쿨다운으로 일부 억제
- 구간9 (-4.89%): bearish bb_bounce 역효과. 포지션 축소(1/3)로 제한 중

---

## 12. 인프라 개선 이력

| 항목 | 이전 | 현재 |
|------|------|------|
| WF 실행 시간 | ~1시간 | **~9분** (adaptive 단독) |
| 그리드 서치 | 전수 순차 + run() | 랜덤 50% + run_portfolio() + 프리컴퓨팅 |
| 데이터 로딩 | 매번 pykrx 호출 | 가격/KOSPI 캐싱 |
| CLI 자본금 | 1000만원 | 300만원 (WF와 통일) |

---

## 13. 트러블슈팅

| 증상 | 원인 | 해결 |
|------|------|------|
| "Unknown strategy 'adaptive'" | screener에서 adaptive 직접 호출 | 최신 코드로 업데이트 |
| KOSPI 데이터 실패 | pykrx Python 3.14 인코딩 | KRX_API_KEY 설정 또는 KODEX200 자동 폴백 |
| 매수 0건 (하락장) | MarketRegime 방어 모드 | 정상 동작 — KOSPI < 200일선이면 매수 차단 |
| Walk-Forward OOS 0건 | test 기간 부족 또는 종목 부족 | `--test 6 --codes 10종목 이상` |
| 텔레그램 미발송 | BOT_TOKEN/CHAT_ID 미설정 | .env 확인 |
| DB 잠금 | 동시 실행 | gui.py와 main.py 동시 실행 금지 |
