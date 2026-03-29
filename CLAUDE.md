# CLAUDE.md — swing-trader

Claude Code CLI가 매 호출마다 자동으로 읽는 컨텍스트 파일.
상세 레퍼런스: `docs/CLAUDE.md`

## 프로젝트 한 줄 요약

KOSPI/KOSDAQ 스윙매매 자동화 시스템. 키움 REST API + pandas 백테스트 + 텔레그램 알림.

## 현재 상태

수술적 정밀 수정(surgical fix) + 전략 재설계 완료.
수술 문서: `docs/SURGERY_GUIDE.md`, `docs/SURGERY_PROMPTS_V2.md`

### 수술 + 인프라 (전체 완료)

| ID | 상태 | 내용 |
|----|------|------|
| FIX-1~4 | 완료 | 비용 모델 교정, WF 종목 동적화 |
| STRAT-1~3 | 완료 | 파라미터 통일, 청산 로직 일치, entry_strategy 추적 |
| FIX-6~7 | 완료 | 포지션 사이징 config 연동, 실험 스크립트 정리 |
| INFRA-1~5 | 완료 | StopManager fallback, _check_strategy_exit 분리, Paper 안전장치, 거래 통계 |

### 전략 재설계 v2 (완료)

| 전략 | 엣지 | 카테고리 | 국면 |
|------|------|---------|------|
| `momentum_pullback` | 모멘텀 프리미엄 — 60일 상위 종목의 눌림목 반등 | trend | trending |
| `institutional_flow` | 외국인/기관 정보 우위 — 수급 선행 | trend | trending |
| `disparity_reversion` | 단기 과매도 평균회귀 — 이격도 < 93% 반등 | mean_reversion | sideways |

## 절대 위반 금지

- `order_manager.py`의 `execute_order()`는 반드시 `RiskManager.pre_check()` 통과 후에만 호출
- 포지션 사이징은 `risk/position_sizer.py`의 `PositionSizer`만 사용
- 일일 최대 손실 -3% 초과 시 `TradingEngine.halt()` 자동 호출 — 이 로직 제거 금지
- LIVE 모드에서 텔레그램 알림 없이 주문 불가

## 핵심 아키텍처

```
main.py → TradingEngine (src/engine.py, 1253줄 — 핵심 조율자)
  ├─ Screener        2단계 스크리닝 (pre-screening → screening)
  ├─ Strategy        BaseStrategy + register_strategy 데코레이터 패턴
  │   ├─ momentum_pullback   모멘텀 + 눌림목 반등 (trend)
  │   ├─ institutional_flow  수급 기반 진입 (trend)
  │   └─ disparity_reversion 이격도 평균회귀 (mean_reversion)
  ├─ MarketRegime    KOSPI 200일선 + ADX + VKOSPI → trending/sideways/bearish
  ├─ RiskManager     pre_check, 일일 한도, MDD
  ├─ PositionSizer   하프켈리 기반
  ├─ StopManager     ATR 손절 + 트레일링스탑
  ├─ KiwoomAPI       REST polling (30초 간격)
  └─ TelegramBot     알림
```

## 비용 모델 (현재 적용 중)

```
수수료: 0.015% (편도) — config.yaml backtest.commission
거래세: 0.15% (2025년, 매도만) — config.yaml backtest.tax
슬리피지: 0.1% (체결가 조정) — config.yaml backtest.slippage

매수: actual_entry = int(price * (1 + slippage))
      cost = actual_entry * (1 + commission)
매도: actual_exit = int(price * (1 - slippage))
      proceeds = actual_exit * (1 - commission - tax)
```

## 전략 구조

```
adaptive 모드 (config.yaml strategy.type: adaptive)
  trending:  momentum_pullback + institutional_flow
  sideways:  disparity_reversion
  bearish:   매수 차단

포지션 스케일: trending 100% / sideways 50% / bearish 0%
기존 7전략은 src/strategy/에 파일 보존, __init__.py에서 비활성
```

## 코딩 컨벤션

- 주문 함수에 `# RISK_CHECK_REQUIRED` 주석 필수
- 금액: `int` (원), 비율: `float` (0.0~1.0)
- 로그: 주문은 `logger.trade()`, 일반은 `logger.info()`
- 타임존: `Asia/Seoul`
- pykrx 컬럼: 한글 → `data/column_mapper.py`로 영문 변환 후 사용

## 테스트

```bash
pytest tests/ -v              # 전체 (352개)
pytest tests/test_backtest.py  # 백테스트 (49개) — 비용 모델 수정 시 여기 집중
pytest tests/test_engine.py    # 엔진 (40개)
pytest tests/test_strategy.py  # 전략 (62개)
```

## 파일 크기 상위 (수정 시 주의)

| 파일 | 줄 수 | 역할 |
|------|-------|------|
| `src/backtest/engine.py` | 1,357 | 백테스트 — FIX-1,2,3 수정 대상 |
| `src/engine.py` | 1,253 | 실시간 매매 엔진 — FIX-1,5 수정 대상 |
| `src/strategy/screener.py` | 792 | 종목 스크리닝 |
| `src/gui/main_window.py` | 652 | GUI |
| `src/datastore.py` | 519 | SQLite DB |
