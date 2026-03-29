# CLAUDE.md — swing-trader

Claude Code CLI가 매 호출마다 자동으로 읽는 컨텍스트 파일.
상세 레퍼런스: `docs/CLAUDE.md`

## 프로젝트 한 줄 요약

KOSPI/KOSDAQ 스윙매매 자동화 시스템. 키움 REST API + pandas 백테스트 + 텔레그램 알림.

## 현재 상태

전략 검증 완료. Paper trading 준비 상태.

### 확정 전략 (2전략 adaptive)

| 전략 | 국면 | Sharpe | 승률 | 검증 파라미터 |
|------|------|--------|------|-------------|
| golden_cross | trending | 0.84 | 65.6% | adx=20, stop_atr=2.0, lookback=3 |
| disparity_reversion | sideways | 0.45 | 73.5% | disparity=96, stop=88, hold=7 |

운용: trending 100%, sideways 70%, bearish 0%
유니버스: 대형주 20종목 watchlist (분기 자동 갱신)
자본금: 300만원, max_positions: 4

### watchlist 기준

선정: 시가총액 5조+, 거래대금 100억+, 우선주 제외, ATR% 2~5%
갱신: 분기 자동(3/6/9/12월) + 수동(scripts/refresh_watchlist.py)

수술 상태: 모든 항목 완료 (FIX-1~7, STRAT-1~3, INFRA-1~5).

## 절대 위반 금지

- `order_manager.py`의 `execute_order()`는 반드시 `RiskManager.pre_check()` 통과 후에만 호출
- 포지션 사이징은 `risk/position_sizer.py`의 `PositionSizer`만 사용
- 일일 최대 손실 -3% 초과 시 `TradingEngine.halt()` 자동 호출 — 이 로직 제거 금지
- LIVE 모드에서 텔레그램 알림 없이 주문 불가

## 핵심 아키텍처

```
main.py → TradingEngine (src/engine.py — 핵심 조율자)
  ├─ Screener        2단계 스크리닝 (pre-screening → screening)
  ├─ Strategy        2전략 adaptive
  │   ├─ golden_cross       trending (SMA5/20)
  │   └─ disparity_reversion sideways (이격도)
  ├─ Watchlist       대형주 20종목 (분기 갱신)
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
  trending:  golden_cross (Sharpe 0.84)
  sideways:  disparity_reversion (Sharpe 0.45)
  bearish:   매수 차단

포지션 스케일: trending 100% / sideways 70% / bearish 0%
유니버스: 대형주 20종목 (중형주 확대 금지 — Sharpe 0.84→-0.33)
비활성 전략: src/strategy/에 파일 보존, __init__.py에서 주석
```

## 코딩 컨벤션

- 주문 함수에 `# RISK_CHECK_REQUIRED` 주석 필수
- 금액: `int` (원), 비율: `float` (0.0~1.0)
- 로그: 주문은 `logger.trade()`, 일반은 `logger.info()`
- 타임존: `Asia/Seoul`
- pykrx 컬럼: 한글 → `data/column_mapper.py`로 영문 변환 후 사용

## 테스트

```bash
pytest tests/ -v              # 전체
pytest tests/test_backtest.py  # 백테스트
pytest tests/test_engine.py    # 엔진
pytest tests/test_strategy.py  # 전략
```

## 파일 크기 상위 (수정 시 주의)

| 파일 | 역할 |
|------|------|
| `src/backtest/engine.py` | 백테스트 |
| `src/engine.py` | 실시간 매매 엔진 |
| `src/strategy/screener.py` | 종목 스크리닝 |
| `src/gui/main_window.py` | GUI |
| `src/datastore.py` | SQLite DB |
