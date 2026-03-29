# SURGERY_GUIDE.md — swing-trader 수술적 정밀 수정

> 작성일: 2026-03-28
> 목적: 실전 투입 전 크리티컬 버그 4건 + 전략 파라미터 정합성 1건 + 안전성 개선 2건 수정
> 방법론: quant-system v2.0 수술과 동일 — 최소 변경, 최대 안전

## 수술 철학

swing-trader는 아키텍처가 건강함. 전략 패턴, adaptive 국면 전환, 리스크 관리 구조가 잘 돼 있음.
**전면 재설계가 아니라 버그 수정 + 비용 모델/전략 파라미터 정확도 향상**이 목표.

---

## Phase 1: Critical Fixes (실전 투입 전 필수)

### FIX-1: 거래세 0.2% → 0.15% 일괄 변경

**문제**: 2025년 기준 증권거래세는 0.15%인데, 코드 전반에 0.2%(0.002)가 하드코딩.
매도마다 0.05% 비용 오차 → 백테스트 수익률 왜곡, 실거래 손익 계산 부정확.

**수정 대상 (7곳)**:

| # | 파일 | 행 | 현재 | 수정 |
|---|------|-----|------|------|
| 1 | `src/backtest/engine.py` | 21 | `TAX_RATE = 0.002` | `TAX_RATE = 0.0015` |
| 2 | `src/engine.py` | 639 | `* 0.002  # 매도세` | `* 0.0015  # 거래세 0.15% (2025년)` |
| 3 | `config.yaml` | 140 | `tax: 0.002` | `tax: 0.0015` |
| 4 | `docs/CLAUDE.md` | 139 | `거래세 0.2%` | `거래세 0.15% (2025년 기준)` |
| 5 | `docs/PRD.md` | 121 | `거래세 0.2%` | `거래세 0.15%` |
| 6 | `docs/BACKTEST_SPEC.md` | 24 | `TAX_RATE = 0.002  # 거래세 0.2%` | `TAX_RATE = 0.0015  # 거래세 0.15%` |
| 7 | `docs/ARCHITECTURE.md` | 262 | `tax: 0.002  # 0.2%` | `tax: 0.0015  # 0.15%` |

**검증**: `grep -rn "0\.002\|0\.2%" --include="*.py" --include="*.yaml" --include="*.md" .` → 0건

---

### FIX-2: 백테스트 비용 모델을 config.yaml에서 읽도록 변경

**문제**: `config.yaml`에 `backtest.commission/tax/slippage` 섹션이 있지만,
`BacktestEngine`은 모듈 상수(`COMMISSION_RATE`, `TAX_RATE`, `SLIPPAGE_RATE`)를 사용.
config를 바꿔도 백테스트 결과가 변하지 않는 함정.

**수정 범위**: `src/backtest/engine.py`

**변경 1 — 모듈 상수를 기본값(fallback)으로 변환**:
```python
# 기존 (19-22행):
COMMISSION_RATE = 0.00015
TAX_RATE = 0.002
SLIPPAGE_RATE = 0.001

# 수정:
_DEFAULT_COMMISSION = 0.00015   # 수수료 0.015% (편도)
_DEFAULT_TAX = 0.0015           # 거래세 0.15% (2025년, 매도만)
_DEFAULT_SLIPPAGE = 0.001       # 슬리피지 0.1%
```

**변경 2 — `__init__`에서 config 로드**:
```python
def __init__(self, initial_capital: int = 10_000_000, cost_config: dict | None = None):
    self.initial_capital = initial_capital
    self._price_cache: dict[str, pd.DataFrame] = {}

    # 비용 모델: cost_config > config.yaml > 기본값
    cc = cost_config or {}
    try:
        from src.utils.config import config as app_config
        bt = app_config.data.get("backtest", {})
    except Exception:
        bt = {}

    self.commission = cc.get("commission", bt.get("commission", _DEFAULT_COMMISSION))
    self.tax = cc.get("tax", bt.get("tax", _DEFAULT_TAX))
    self.slippage = cc.get("slippage", bt.get("slippage", _DEFAULT_SLIPPAGE))
```

**변경 3 — `_simulate_portfolio` 내 상수 참조를 인스턴스 변수로 교체**:
- `COMMISSION_RATE` → `self.commission`
- `TAX_RATE` → `self.tax`
- `SLIPPAGE_RATE` → `self.slippage`

대상 행: 239, 292, 324, 337, 933, 980, 1158, 1169

**변경 4 — 포트폴리오 백테스트(`run_portfolio_backtest`) 내 동일 교체**:
대상 행: 933, 980, 1158, 1169 (동일 패턴)

**검증**: `grep -n "COMMISSION_RATE\|TAX_RATE\|SLIPPAGE_RATE" src/backtest/engine.py` → 기본값 정의만 남아야 함

---

### FIX-3: 슬리피지 모델 정확도 개선

**문제**: 현재 매도 proceeds 계산:
```python
proceeds = exit_price * (1 - COMMISSION_RATE - SLIPPAGE_RATE - TAX_RATE)
```
슬리피지는 "체결가가 불리하게 밀리는 것"이지 수수료처럼 차감하는 게 아님.
현재 방식은 슬리피지가 수수료·세금 계산 기반에도 영향을 줘서 이중 적용 효과.

**수정 — 매도 (4곳)**:
```python
# 기존:
proceeds = exit_price * (1 - COMMISSION_RATE - SLIPPAGE_RATE - TAX_RATE)

# 수정:
actual_exit = int(exit_price * (1 - self.slippage))
proceeds = actual_exit * (1 - self.commission - self.tax)
```

대상: `_simulate_portfolio` 내 매도 proceeds 계산 (239행, 292행)
및 `run_portfolio_backtest` 내 (933행, 980행)

**수정 — 매수 (2곳)**:
```python
# 기존:
cost_per_share = price * (1 + COMMISSION_RATE + SLIPPAGE_RATE)

# 수정:
actual_entry = int(price * (1 + self.slippage))
cost_per_share = actual_entry * (1 + self.commission)
```

대상: `_simulate_portfolio` 내 매수 (324행)
및 `run_portfolio_backtest` 내 (1158행)

**주의**: `int()` 변환은 한국 주식 호가 단위(원) 반영. 실제 체결가는 정수.

**검증**: 기존 백테스트 결과와 비교 — 비용 구조 변경이므로 수익률 소폭 변동 예상 (정상)

---

### FIX-4: Walk-Forward 종목 동적화

**문제**: `scripts/run_walk_forward.py:38-43`에 대형주 20종목 하드코딩.
실전 스크리너가 선별하는 종목군(유동성+기술적 필터)과 괴리.

**수정**:

1. 기존 `CODES` → `DEFAULT_CODES`로 리네임
2. `--codes` 인자 추가 (쉼표 구분)
3. `--use-screener` 옵션 추가: 지정 기간의 스크리너 출력 사용
4. 인자 없으면 `DEFAULT_CODES` 사용 (하위 호환)

```python
# 기존:
CODES = ["005930", "000660", ...]

# 수정:
DEFAULT_CODES = ["005930", "000660", ...]

# argparse에 추가:
parser.add_argument("--codes", type=str, default=None,
                    help="종목코드 쉼표 구분 (예: 005930,000660)")
parser.add_argument("--use-default", action="store_true",
                    help="DEFAULT_CODES 20종목 사용 (기본값)")

# 코드 결정 로직:
if args.codes:
    codes = [c.strip() for c in args.codes.split(",")]
else:
    codes = DEFAULT_CODES
```

**검증**: `python scripts/run_walk_forward.py --strategy golden_cross --codes 005930,000660 --train 12 --test 3` 실행 확인

---

### STRAT-1: 파라미터 기본값 4곳 불일치 해소 + config 경로 버그

**문제**: 동일 파라미터의 기본값이 코드 위치마다 다름. 백테스트 결과가 실전에서 재현되지 않는 근본 원인.

| 파라미터 | config.yaml | 실전 엔진 | 백테스트(단일) | 백테스트(포트폴리오) |
|---------|-------------|----------|-------------|-----------------|
| `trailing_activate_pct` | **0.10** | 0.10 | **0.05** | **0.07** |
| `stop_atr_mult` | **1.5** | 1.5 | **2.0** | **2.5** |
| `max_stop_pct` | **0.07** | 0.07 | **0.10** | **0.07** |
| `max_hold_days` | **10** (strategy.) | **15** ← 경로 버그 | **20** | **10** |

**`max_hold_days` 경로 버그 (Critical)**:
- config.yaml: `strategy.max_hold_days: 10`
- 실전 엔진 (`src/engine.py:366`): `config.get("trading.max_hold_days", 15)` ← **잘못된 경로**
- `_validate()`가 `trading.max_hold_days`가 없으므로 기본값 15 생성 → 항상 15일 보유

**수정 1 — max_hold_days 경로 수정**:
```python
# src/engine.py:366
# 기존:
max_hold = config.get("trading.max_hold_days", 15)
# 수정:
max_hold = config.get("strategy.max_hold_days", 10)
```

**수정 2 — 백테스트 기본값을 config.yaml 값과 통일**:
```python
# src/backtest/engine.py _simulate_portfolio (175-179행):
# 기존:
stop_atr_mult = p.get("stop_atr_mult", 2.0)
trailing_activate_pct = p.get("trailing_activate_pct", 0.05)
max_hold_days = p.get("max_hold_days", 20)
max_stop_pct = p.get("max_stop_pct", 0.10)

# 수정 (config.yaml 값과 일치):
stop_atr_mult = p.get("stop_atr_mult", 1.5)
trailing_activate_pct = p.get("trailing_activate_pct", 0.10)
max_hold_days = p.get("max_hold_days", 10)
max_stop_pct = p.get("max_stop_pct", 0.07)
```

```python
# src/backtest/engine.py run_portfolio_backtest (720-724행):
# 기존:
stop_atr_mult = p.get("stop_atr_mult", 2.5)
trailing_activate_pct = p.get("trailing_activate_pct", 0.07)

# 수정:
stop_atr_mult = p.get("stop_atr_mult", 1.5)
trailing_activate_pct = p.get("trailing_activate_pct", 0.10)
```

**수정 3 — StopManager 기본값 통일**:
```python
# src/risk/stop_manager.py:18
# 기존:
trailing_activate_pct: float = 0.03,
# 수정:
trailing_activate_pct: float = 0.10,
```

**검증**:
```bash
grep -rn "trailing_activate_pct.*0\.\|stop_atr_mult.*[0-9]\.\|max_hold_days.*[0-9]" \
  src/engine.py src/backtest/engine.py src/risk/stop_manager.py
```
→ 모든 기본값이 config.yaml과 일치해야 함

---

### STRAT-2: 백테스트 ↔ 실전 청산 로직 불일치 (Phase 2에서 처리)

**문제**: 청산 경로가 다름.
- 백테스트: 손절 → 부분매도 → 목표가 → 트레일링 → 최대보유 → **전략별 exit 신호**
- 실전: 손절 → 부분매도 → 목표가 → **MACD 데드크로스(일괄)** → 최대보유

전략별 exit 신호가 실전에서 미적용. 트레일링도 코드 경로가 다름.

**수정 방향** (Phase 2):
- 백테스트와 실전의 청산 로직을 일치시킴
- `_evaluate_exit()`에 전략별 exit 체크 추가, 또는 백테스트에서 MACD 데드크로스로 통일

---

### STRAT-3: Adaptive 모드 진입 전략 미추적 (Phase 2에서 처리)

**문제**: Position 모델·DB에 `entry_strategy` 필드 없음.
국면 전환 시 잘못된 전략의 exit 신호가 적용될 수 있음.

**수정 방향** (Phase 2):
1. Position에 `entry_strategy: str` 추가
2. DB positions 테이블에 `entry_strategy` 컬럼 추가
3. `_record_buy()`에서 전략명 기록
4. `_evaluate_exit()`에서 진입 전략 기반 청산 로직 분기

---

## Phase 2: Safety Improvements (실전 운용 안정성)

### FIX-5: CLAUDE.md 루트 레벨 생성 ✅ 완료

**문제**: Claude Code CLI는 루트의 `CLAUDE.md`만 자동 인식. 현재 `docs/CLAUDE.md`에만 있음.

**수정**: 루트에 CLI 특화 CLAUDE.md 신규 생성 (완료).
- `./CLAUDE.md` — CLI 컨텍스트 브리지 (수술 상태, 핵심 아키텍처, 비용 모델 목표 상태)
- `docs/CLAUDE.md` — 상세 레퍼런스 (유지)
- 두 파일은 역할이 다르므로 내용 동기화 불필요. 루트는 CLI용, docs/는 사람용.

---

### FIX-6: 포지션 사이징 하드코딩 제거

**문제**: `src/engine.py:547-549`
```python
invest_amount = self._sizer.calculate(
    capital=capital, win_rate=0.5, avg_win=0.08, avg_loss=0.04
)
```

**수정**: config.yaml에 기본값 추가 + DB 이력 기반 동적 계산 옵션
```yaml
# config.yaml risk 섹션에 추가:
risk:
  default_win_rate: 0.5
  default_avg_win: 0.08
  default_avg_loss: 0.04
  use_historical_stats: false  # true면 최근 N거래 이력에서 자동 계산
```

```python
# engine.py 수정:
win_rate = config.get("risk.default_win_rate", 0.5)
avg_win = config.get("risk.default_avg_win", 0.08)
avg_loss = config.get("risk.default_avg_loss", 0.04)

if config.get("risk.use_historical_stats", False):
    stats = self._ds.get_trade_statistics(limit=50)
    if stats and stats["count"] >= 10:
        win_rate = stats["win_rate"]
        avg_win = stats["avg_win"]
        avg_loss = stats["avg_loss"]

invest_amount = self._sizer.calculate(
    capital=capital, win_rate=win_rate, avg_win=avg_win, avg_loss=avg_loss
)
```

---

### FIX-7: 실험 스크립트 정리

**문제**: `scripts/` 폴더에 실험용 스크립트 8개가 혼재.

**수정**:
- `scripts/run_walk_forward.py` — 유지 (핵심 검증 도구)
- 나머지 7개 → `scripts/archived/`로 이동
- `scripts/README.md` 생성: "archived/는 개발 과정에서 사용한 실험 스크립트. 실전 운용에 불필요."

---

## 수정 순서

```
사전 작업 (완료):
  FIX-5: CLAUDE.md 루트 레벨 생성 ✅

Phase 1 (완료 — 검증 인프라 교정):
  FIX-1 → FIX-2 → FIX-3 → FIX-4 → STRAT-1 ✅

Phase 2 (완료 — 안전성):
  FIX-6 → FIX-7 ✅

Phase 3 (완료 — 전략 정합성):
  STRAT-2 → STRAT-3 ✅ (단, INFRA-2 미완성 — Phase A에서 보완)

Phase A (인프라 잔여 — Phase B 전 선행):
  INFRA-1: StopManager fallback 통일
  INFRA-2: _evaluate_exit → _check_strategy_exit 분리 (전략별 exit 골격)
  INFRA-3: Paper 모드 OrderManager 이중 안전장치
  INFRA-5: DataStore.get_trade_statistics() 구현

Phase B (전략 재설계):
  B-1: 기존 전략 비활성화 + momentum_pullback 구현
  B-2: institutional_flow 구현
  B-3: disparity_reversion 구현
  B-4: config.yaml 전략 매핑 교체 + regime_position_scale
  B-5: 전체 검증 + 커밋

Phase C (검증):
  Paper trading 1~2개월 → 데이터 기반 판단

Phase D (실전):
  확정된 전략 셋으로 live 전환
```

프롬프트: `docs/SURGERY_PROMPTS_V2.md` 참조.

각 Phase 끝에 `pytest tests/ -v` 전체 통과 확인 후 커밋.

---

## Phase A: 인프라 잔여 수정

### INFRA-1: StopManager 초기화 fallback 기본값 불일치

**문제**: `src/engine.py:67-72`의 StopManager 초기화 fallback이 config.yaml과 불일치.
config 정상 로드 시 문제없지만, 파일 누락 시 STRAT-1에서 통일한 값과 다른 기본값 적용.

| 파라미터 | engine.py fallback | config.yaml | 수정 |
|---------|-------------------|-------------|------|
| stop_atr_mult | **2.5** | 1.5 | 1.5 |
| max_stop_pct | **0.10** | 0.07 | 0.07 |
| trailing_atr_mult | **2.5** | 2.0 | 2.0 |
| trailing_activate_pct | **0.07** | 0.10 | 0.10 |

### INFRA-2: _evaluate_exit 전략별 분기 미구현

**문제**: STRAT-2 커밋에서 entry_strategy 기록 인프라만 추가됨.
`_evaluate_exit()`는 여전히 모든 포지션에 MACD 데드크로스 일괄 적용.
전략별 exit 분기가 없음.

**수정**: `_evaluate_exit()`에서 전략별 고유 청산을 `_check_strategy_exit()` 메서드로 분리.
Phase B에서 새 전략 추가 시 이 메서드에 분기만 추가하면 됨.

### INFRA-3: Paper 모드 안전장치 부족

**문제**: Paper 모드의 주문 차단이 engine.py의 mode 분기에만 의존.
OrderManager 레벨에 이중 안전장치 없음.

**수정**: OrderManager에 `is_paper` 플래그 추가.
execute_order() 최상단에서 paper 모드면 즉시 시뮬레이션 결과 반환.

### INFRA-5: get_trade_statistics() 미구현

**문제**: FIX-6에서 config에 `use_historical_stats` 옵션 추가했지만,
DataStore.get_trade_statistics() 메서드가 존재하지 않아 런타임 에러.

**수정**: 최근 N건 매도 거래의 승률/평균손익 통계를 반환하는 메서드 구현.

---

## Phase 4: 전략 재설계 로드맵 (수술 완료 후 실행)

> 이 섹션은 Phase 1~3 수술이 완료된 후 실행할 계획.
> 기존 전략을 삭제하지 않고, BaseStrategy를 상속하는 새 전략을 추가하여 병렬 비교.

### 현재 전략의 문제 진단

**핵심 이슈: 엣지(edge)의 정의가 불명확**

| 문제 | 설명 |
|------|------|
| 후행 지표 의존 | SMA5/20 크로스는 이미 5~10일 전에 시작된 움직임을 확인. 보유기간 10일에서 절반이 이미 지남 |
| 인디케이터 수프 | Signal Score 9개 구성요소 중 대부분이 높은 상관. 사실상 2~3개 팩터를 중복 측정 |
| 파라미터 폭발 | 5전략 × 30+ 파라미터 → 과적합 구조적 위험 |
| 엣지 미정의 | "왜 돈을 버는가?"에 대한 경제학적 한 문장 답변 부재 |

### 신규 전략 설계 — 3개

#### 전략 A: `momentum_pullback` (추세 + 눌림목)

**엣지: "모멘텀 프리미엄 — 최근 잘 간 종목이 계속 간다"**
학술적으로 가장 견고한 시장 이상 현상. 행동경제학(underreaction, herding)으로 설명.

```
카테고리: trend
국면: trending

스크리닝:
  - 60일 수익률 상위 20% (모멘텀 필터)
  - 시가총액 3,000억 이상
  - 일 거래대금 10억 이상

진입 (AND):
  - 종가 > 20일선 (추세 유지)
  - 최근 3~5일 하락 또는 5일 RSI < 30 (눌림목)
  - 당일 양봉 마감 (반등 확인)
  - 거래량 > 20일 평균

청산 (OR):
  - +8~10% 목표가
  - -5% 손절 또는 ATR×1.5
  - 최대 보유 10일
  - 트레일링: +5% 수익 시 활성화

파라미터: ~6개
```

현재 `macd_pullback`과 유사하지만 핵심 차이:
MACD 크로스(후행) 대신 **가격 자체의 pullback(N일 하락폭)**을 사용.
후행 지표 의존 제거, 가격 행동(price action)에 직접 반응.

---

#### 전략 B: `institutional_flow` (수급 기반)

**엣지: "외국인/기관의 정보 우위 — 대형 자금이 먼저 움직인다"**
한국 시장에서 특히 강력. 정보 비대칭 + 대형 자금의 분할 체결로 설명.

```
카테고리: trend
국면: trending

스크리닝:
  - 외국인 5일 누적 순매수 > 0
  - 기관 3일 이상 순매수
  - 시가총액 5,000억 이상 (수급 데이터 신뢰성)

진입 (AND):
  - 종가 > 20일선
  - 외국인 당일 순매수 지속
  - 거래량 > 20일 평균
  - ADX > 20 (방향성 확인)

청산 (OR):
  - 외국인 2일 연속 순매도 전환 ← 핵심 exit
  - +10% 목표가
  - -5% 손절
  - 최대 보유 15일

파라미터: ~6개
```

현재 시스템은 수급을 Signal Score 9점 중 1점으로만 사용.
이걸 독립 전략으로 승격. `get_institutional_net_buying()` 인프라 이미 존재.

---

#### 전략 C: `disparity_reversion` (이격도 평균회귀)

**엣지: "단기 과매도 반등 — 극단적 이탈은 평균으로 돌아온다"**
현재 `bb_bounce` 대체. BB 대신 이격도(disparity ratio) 사용.
"얼마나 이탈했는가"가 직관적으로 측정되고, 파라미터 감소.

```
카테고리: mean_reversion
국면: sideways

스크리닝:
  - 20일선 이격도 < 93% (7% 이상 이탈)
  - 5일 RSI < 25 (극단 과매도)
  - 60일 이동평균 상승 중 (장기 추세는 생존)

진입 (AND):
  - 당일 양봉 (바닥 확인)
  - 거래량 증가 (매수세 유입)

청산 (OR):
  - 이격도 100% 복귀 (20일선 터치) ← 핵심 exit
  - 이격도 < 88% (추가 하락 시 컷)
  - 최대 보유 7일 (평균회귀는 짧게)

파라미터: ~5개
```

---

### Adaptive 국면 전환 — 단순화

```yaml
# 현재 (5개 전략 전환):
regime_strategy:
  trending: [golden_cross, macd_pullback, volume_breakout]
  sideways: bb_bounce

# 변경 후 (3개 전략 + 포지션 사이즈 조절):
regime_strategy:
  trending: [momentum_pullback, institutional_flow]
  sideways: disparity_reversion
  bearish: []  # 매수 차단

# 신규: 국면별 포지션 사이즈 스케일링
regime_position_scale:
  trending: 1.0    # 100%
  sideways: 0.5    # 50% (리스크 축소)
  bearish: 0.0     # 매수 차단
```

전략 교체보다 **포지션 사이즈 조절**이 더 안정적.
sideways에서 전략을 완전히 바꾸면 승률 데이터가 리셋되지만,
사이즈만 줄이면 같은 전략으로 데이터가 계속 쌓임.

---

### 전략 재설계 전후 비교

| 항목 | 현재 | 재설계 후 |
|------|------|----------|
| 전략 수 | 5개 | 3개 |
| 핵심 엣지 | 불명확 (지표 조합) | 명확 (모멘텀, 수급, 평균회귀) |
| 파라미터 수 | 30+ | 15~17개 |
| 지표 상관성 | 높음 (9개 중복) | 낮음 (각 전략이 독립 팩터) |
| 진입 근거 | SMA 크로스 (후행) | 가격 pullback / 수급 선행 |
| 수급 활용 | 점수의 1/9 | 독립 전략 |
| 국면 적응 | 전략 교체 | 전략 교체 + 사이즈 조절 |

---

### 구현 순서 (Phase 4 내부)

```
4-1: momentum_pullback 구현
     → BaseStrategy 상속, generate_backtest_signals 구현
     → 단일 종목 백테스트 → 포트폴리오 백테스트 → WF 검증

4-2: institutional_flow 구현
     → 기존 get_institutional_net_buying() 활용
     → 수급 데이터 품질 검증 (pykrx 외국인/기관 데이터 지연 확인)
     → 백테스트 → WF 검증

4-3: disparity_reversion 구현
     → 이격도 계산 (close / sma20 * 100)
     → 백테스트 → WF 검증

4-4: regime_position_scale 구현
     → TradingEngine에 국면별 사이즈 스케일링 로직 추가
     → PositionSizer.calculate()에 scale 파라미터 추가

4-5: 기존 5전략 vs. 신규 3전략 비교 리포트
     → 동일 기간, 동일 종목 유니버스, 동일 비용 모델
```

### 실행 전 점검사항

Phase 4 시작 전 확인:
- [ ] Phase 1~3 수술 완료, 모든 테스트 통과
- [ ] 기존 전략으로 Paper trading 최소 1개월 데이터 확보
- [ ] 실전 승률/평균손익 → 하프켈리 실측값 반영 완료
- [ ] 백테스트 ↔ 실전 청산 로직 일치 (STRAT-2 완료)

