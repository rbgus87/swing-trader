# SURGERY_PROMPTS.md — Claude Code CLI 복사-붙여넣기 프롬프트

> 사용법: 각 프롬프트를 Claude Code CLI에 그대로 복사하여 실행.
> 순서: Phase 1 (FIX-1 → FIX-4) → 테스트 → 커밋 → Phase 2 (FIX-5 → FIX-7) → 테스트 → 커밋

---

## Phase 1: Critical Fixes

### 프롬프트 1 — FIX-1 + FIX-2 + FIX-3 (비용 모델 전면 교정)

```
docs/SURGERY_GUIDE.md를 읽어줘.

FIX-1, FIX-2, FIX-3을 한 번에 처리해줘. 세 개가 모두 비용 모델 관련이라 같이 수정하는 게 효율적이야.

## FIX-1: 거래세 0.2% → 0.15% 일괄 변경

아래 7곳을 수정해줘:

1. src/backtest/engine.py 21행: TAX_RATE = 0.002 → 0.0015, 주석도 "거래세 0.15% (2025년, 매도만)"으로 변경
2. src/engine.py 639행: price * sell_qty * 0.002 → * 0.0015, 주석 "# 거래세 0.15% (2025년)"
3. config.yaml 140행: tax: 0.002 → tax: 0.0015, 주석 "거래세 (0.15%, 매도 시에만 적용, 2025년 기준)"
4. docs/CLAUDE.md 139행: "거래세 0.2%" → "거래세 0.15% (2025년 기준)"
5. docs/PRD.md 121행: "거래세 0.2%" → "거래세 0.15%"
6. docs/BACKTEST_SPEC.md 24행: "TAX_RATE = 0.002 # 거래세 0.2%" → "TAX_RATE = 0.0015 # 거래세 0.15%"
7. docs/ARCHITECTURE.md 262행: "tax: 0.002 # 0.2%" → "tax: 0.0015 # 0.15%"

수정 후 grep -rn "0\.002" --include="*.py" --include="*.yaml" --include="*.md" . 로 누락 확인해줘.
거래세와 관련 없는 0.002 (예: 다른 비율)는 무시해도 돼.

## FIX-2: 백테스트 비용 모델을 config에서 읽도록 변경

src/backtest/engine.py를 수정해줘:

1. 모듈 상수를 기본값(fallback)으로 변환:
   COMMISSION_RATE → _DEFAULT_COMMISSION = 0.00015
   TAX_RATE → _DEFAULT_TAX = 0.0015
   SLIPPAGE_RATE → _DEFAULT_SLIPPAGE = 0.001

2. BacktestEngine.__init__에 cost_config 파라미터 추가:
   def __init__(self, initial_capital: int = 10_000_000, cost_config: dict | None = None):
   
   cost_config가 있으면 그 값 사용, 없으면 config.yaml의 backtest 섹션 읽기, 
   그것도 없으면 _DEFAULT_* 기본값 사용.
   인스턴스 변수: self.commission, self.tax, self.slippage

3. _simulate_portfolio와 run_portfolio_backtest 내에서:
   COMMISSION_RATE → self.commission
   TAX_RATE → self.tax  
   SLIPPAGE_RATE → self.slippage
   모든 참조를 인스턴스 변수로 교체.

4. config.yaml import 실패 시 (테스트 환경 등) graceful하게 기본값 사용.

## FIX-3: 슬리피지 모델 정확도 개선

FIX-2에서 인스턴스 변수 교체할 때, 슬리피지 적용 방식도 같이 바꿔줘:

매도 시 (기존):
  proceeds = exit_price * (1 - COMMISSION_RATE - SLIPPAGE_RATE - TAX_RATE)

매도 시 (수정):
  actual_exit = int(exit_price * (1 - self.slippage))
  proceeds = actual_exit * (1 - self.commission - self.tax)

매수 시 (기존):
  cost_per_share = price * (1 + COMMISSION_RATE + SLIPPAGE_RATE)

매수 시 (수정):
  actual_entry = int(price * (1 + self.slippage))
  cost_per_share = actual_entry * (1 + self.commission)

이 패턴을 _simulate_portfolio와 run_portfolio_backtest 양쪽에 동일 적용.
부분 매도(partial sell) proceeds 계산에도 동일 적용.

수정 완료 후:
1. grep -n "COMMISSION_RATE\|TAX_RATE\|SLIPPAGE_RATE" src/backtest/engine.py 로 기본값 정의 외 잔여 참조 확인
2. pytest tests/test_backtest.py -v 실행
3. 실패하는 테스트가 있으면 비용 모델 변경에 맞게 expected 값 업데이트 (세율/슬리피지 모델 변경이니까 정상)
```

---

### 프롬프트 2 — FIX-4 (Walk-Forward 종목 동적화)

```
docs/SURGERY_GUIDE.md의 FIX-4를 처리해줘.

scripts/run_walk_forward.py를 수정해줘:

1. 기존 CODES 리스트 → DEFAULT_CODES로 리네임 (38-43행)

2. argparse에 --codes 인자 추가:
   parser.add_argument("--codes", type=str, default=None,
                       help="종목코드 쉼표 구분 (예: 005930,000660,005380)")

3. 코드 결정 로직:
   if args.codes:
       codes = [c.strip() for c in args.codes.split(",")]
   else:
       codes = DEFAULT_CODES
   
   기존 CODES를 참조하던 모든 곳을 codes 로컬 변수로 교체.

4. 스크립트 상단 docstring에 사용법 추가:
   # 커스텀 종목으로 실행
   python scripts/run_walk_forward.py --strategy golden_cross --codes 005930,000660,005380

하위 호환성: 인자 없이 실행하면 기존과 동일하게 DEFAULT_CODES 20종목 사용.

수정 후 python scripts/run_walk_forward.py --help 로 인자 확인.
```

---

### 프롬프트 3 — Phase 1 검증 및 커밋

```
Phase 1 수정이 끝났으니 전체 검증해줘:

1. 전체 테스트 실행:
   pytest tests/ -v

2. 비용 모델 검증:
   - grep -rn "0\.002" --include="*.py" --include="*.yaml" . | grep -v "__pycache__\|archived"
     → 거래세 관련 0.002가 없어야 함
   - grep -n "COMMISSION_RATE\|TAX_RATE\|SLIPPAGE_RATE" src/backtest/engine.py
     → _DEFAULT_* 정의와 주석만 남아야 함

3. 실패한 테스트가 있으면:
   - 비용 모델 변경(세율 0.002→0.0015, 슬리피지 분리)으로 인한 expected 값 차이는 정상
   - 테스트의 expected 값을 새 비용 모델에 맞게 업데이트
   - 로직 버그로 인한 실패와 구분할 것

4. 모든 테스트 통과 확인 후 커밋:
   git add -A
   git commit -m "fix: 비용 모델 교정 — 거래세 0.15%, config 연동, 슬리피지 분리, WF 종목 동적화

   - FIX-1: 거래세 0.2% → 0.15% (2025년 기준) 전체 일괄 변경
   - FIX-2: 백테스트 비용 모델을 config.yaml에서 읽도록 변경 (하드코딩 제거)
   - FIX-3: 슬리피지를 체결가 조정 방식으로 분리 (이중 적용 제거)
   - FIX-4: Walk-Forward --codes 인자 추가 (DEFAULT_CODES 하위호환)"
```

---

## Phase 2: Safety Improvements

### 프롬프트 4 — FIX-6 + FIX-7 (안전성 개선)

```
docs/SURGERY_GUIDE.md의 Phase 2를 처리해줘. FIX-6, FIX-7을 한 번에.
(FIX-5 CLAUDE.md 루트 생성은 이미 완료됨)

## FIX-6: 포지션 사이징 하드코딩 제거

1. config.yaml의 risk 섹션에 추가:
   # 포지션 사이징 기본 파라미터
   default_win_rate: 0.5
   default_avg_win: 0.08
   default_avg_loss: 0.04

2. src/engine.py의 _check_entry_conditions에서:
   기존 (547-549행):
     invest_amount = self._sizer.calculate(
         capital=capital, win_rate=0.5, avg_win=0.08, avg_loss=0.04
     )
   
   수정:
     win_rate = config.get("risk.default_win_rate", 0.5)
     avg_win = config.get("risk.default_avg_win", 0.08)
     avg_loss = config.get("risk.default_avg_loss", 0.04)
     invest_amount = self._sizer.calculate(
         capital=capital, win_rate=win_rate, avg_win=avg_win, avg_loss=avg_loss
     )

## FIX-7: 실험 스크립트 정리

1. scripts/ 폴더에서 run_walk_forward.py를 제외한 나머지 7개 파일을 scripts/archived/로 이동:
   mkdir -p scripts/archived
   mv scripts/ab_compare.py scripts/archived/
   mv scripts/detailed_report.py scripts/archived/
   mv scripts/diagnose_entry.py scripts/archived/
   mv scripts/exit_optimize.py scripts/archived/
   mv scripts/gate_compare.py scripts/archived/
   mv scripts/param_compare.py scripts/archived/
   mv scripts/universe_compare.py scripts/archived/

2. scripts/README.md 생성:
   # scripts/
   
   ## 실행 스크립트
   - `run_walk_forward.py` — Walk-Forward 검증 (핵심 도구)
   
   ## archived/
   개발 과정에서 사용한 실험·비교 스크립트. 실전 운용에 불필요.
   파라미터 튜닝이나 전략 비교가 필요하면 참고용으로 사용 가능.

검증:
1. pytest tests/ -v
2. ls scripts/ (run_walk_forward.py + archived/ + README.md만 있어야 함)

커밋:
git add -A
git commit -m "chore: 안전성 개선 — 포지션 사이징 config 연동, 실험 스크립트 정리

- FIX-6: 포지션 사이징 win_rate/avg_win/avg_loss를 config.yaml에서 로드
- FIX-7: 실험 스크립트 7개 → scripts/archived/ 이동"
```

---

## (선택) Phase 3: CLAUDE.md 내용 업데이트

### 프롬프트 5 — CLAUDE.md 수술 완료 반영

```
수술이 완료되었으니 CLAUDE.md 문서를 갱신해줘.

1. 루트 CLAUDE.md의 수술 상태 표:
   모든 "미완"을 "완료"로 변경

2. docs/CLAUDE.md의 "알려진 이슈 & 주의사항" 섹션 (139행 부근):
   기존: "거래세 0.2% + 수수료 0.015% 백테스트에 반드시 반영"
   수정: "거래세 0.15% (2025년) + 수수료 0.015% — config.yaml backtest 섹션에서 관리"
   
   그리고 해당 섹션 마지막에 추가:
   - 백테스트 비용 모델은 config.yaml의 backtest 섹션에서 관리 (BacktestEngine이 자동 로드)
   - 슬리피지는 체결가 조정 방식 (proceeds = int(price * (1-slippage)) * (1-commission-tax))
   - Walk-Forward는 --codes 인자로 커스텀 종목 지정 가능 (미지정 시 DEFAULT_CODES 20종목)

커밋:
git add -A
git commit -m "docs: CLAUDE.md 수술 완료 반영 — 비용 모델, WF 사용법"
```

---

## 최종 확인 체크리스트

수술 완료 후 아래를 확인:

- [ ] `pytest tests/ -v` 전체 통과
- [ ] `grep -rn "0\.002" --include="*.py" --include="*.yaml"` → 거래세 관련 없음
- [ ] `grep -n "COMMISSION_RATE\|TAX_RATE\|SLIPPAGE_RATE" src/backtest/engine.py` → _DEFAULT_* 만
- [ ] `python scripts/run_walk_forward.py --help` → --codes 인자 표시
- [ ] `cat CLAUDE.md` → 루트에 수술 상태 표 존재, 모두 "완료"
- [ ] `ls scripts/` → run_walk_forward.py + archived/ + README.md
- [ ] `git log --oneline -4` → 커밋 확인
