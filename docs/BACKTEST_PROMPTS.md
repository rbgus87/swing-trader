# BACKTEST_PROMPTS.md — 신규 전략 백테스트 및 최적화

> 목적: 신규 3개 전략의 WF 그리드 설정 + 백테스트 인프라 구축
> 사용법: 프롬프트 1을 CLI에서 실행 → 로컬에서 백테스트 명령어 실행

---

## 프롬프트 1 — WF 그리드 + 최적화 인프라 업데이트

```
CLAUDE.md를 읽어줘.

새 전략 3개(momentum_pullback, institutional_flow, disparity_reversion)의
Walk-Forward 파라미터 그리드와 최적화 인프라를 구축해줘.

## 1. scripts/run_walk_forward.py — 신규 전략 그리드 추가

기존 그리드(WF_GRID_GOLDEN_CROSS, WF_GRID_BB_BOUNCE, WF_GRID_ADAPTIVE)는
주석 처리하지 말고 유지해. 새 그리드를 추가하고 STRATEGY_GRIDS 매핑을 갱신해.

기존 그리드 정의 뒤에 추가:

# ============================================================================
# 신규 전략 파라미터 그리드 (v2)
# ============================================================================

# momentum_pullback: 모멘텀 + 눌림목 (72조합)
WF_GRID_MOMENTUM_PULLBACK = {
    # 진입 파라미터
    "momentum_period": [40, 60],          # 모멘텀 측정 기간
    "pullback_days": [3, 5],              # 눌림목 확인 기간
    "rsi_pullback_threshold": [25, 30],   # 눌림 RSI 기준
    # 청산 파라미터
    "stop_atr_mult": [1.5, 2.0],
    "target_return": [0.08, 0.10],
    "max_hold_days": [7, 10, 15],
}

# institutional_flow: 수급 기반 (48조합)
WF_GRID_INSTITUTIONAL_FLOW = {
    # 진입 파라미터
    "adx_threshold": [15, 20, 25],
    "volume_multiplier": [0.8, 1.0],
    # 청산 파라미터
    "stop_atr_mult": [1.5, 2.0],
    "target_return": [0.08, 0.10],
    "max_hold_days": [10, 15],
}

# disparity_reversion: 이격도 평균회귀 (72조합)
WF_GRID_DISPARITY_REVERSION = {
    # 진입 파라미터
    "disparity_entry": [91, 93, 95],      # 진입 이격도 기준
    "rsi_oversold": [20, 25, 30],         # RSI 과매도 기준
    # 청산 파라미터
    "stop_atr_mult": [1.5, 2.0],
    "target_return": [0.05, 0.08],
    "max_hold_days": [5, 7],
}

# STRATEGY_GRIDS 매핑 갱신:
STRATEGY_GRIDS = {
    # 기존 (비활성 전략, 비교용 유지)
    "golden_cross": WF_GRID_GOLDEN_CROSS,
    "bb_bounce": WF_GRID_BB_BOUNCE,
    "adaptive": WF_GRID_ADAPTIVE,
    # 신규 (활성 전략)
    "momentum_pullback": WF_GRID_MOMENTUM_PULLBACK,
    "institutional_flow": WF_GRID_INSTITUTIONAL_FLOW,
    "disparity_reversion": WF_GRID_DISPARITY_REVERSION,
}

# 기본 그리드도 신규로 변경:
WF_GRID = WF_GRID_MOMENTUM_PULLBACK

## 2. src/backtest/optimizer.py — 기본 그리드 업데이트

PARAM_GRID를 신규 전략 공통 파라미터로 교체:

PARAM_GRID = {
    "stop_atr_mult": [1.5, 2.0, 2.5],
    "target_return": [0.05, 0.08, 0.10],
    "max_hold_days": [7, 10, 15],
    "volume_multiplier": [0.8, 1.0],
}

## 3. src/backtest/engine.py — CLI 기본값 업데이트

CLI 섹션의 --optimize 그리드를 신규 전략에 맞게:

    if args.optimize:
        from src.backtest.optimizer import ParameterOptimizer

        optimizer = ParameterOptimizer(engine)
        # 전략별 그리드 자동 선택
        strategy_grids = {
            "momentum_pullback": {
                "momentum_period": [40, 60],
                "pullback_days": [3, 5],
                "rsi_pullback_threshold": [25, 30, 35],
                "stop_atr_mult": [1.5, 2.0],
                "target_return": [0.08, 0.10],
                "max_hold_days": [7, 10, 15],
            },
            "institutional_flow": {
                "adx_threshold": [15, 20, 25],
                "volume_multiplier": [0.8, 1.0],
                "stop_atr_mult": [1.5, 2.0],
                "target_return": [0.08, 0.10],
                "max_hold_days": [10, 15],
            },
            "disparity_reversion": {
                "disparity_entry": [91, 93, 95],
                "rsi_oversold": [20, 25, 30],
                "stop_atr_mult": [1.5, 2.0],
                "target_return": [0.05, 0.08],
                "max_hold_days": [5, 7],
            },
        }
        grid = strategy_grids.get(args.strategy, {
            "stop_atr_mult": [1.5, 2.0],
            "target_return": [0.08, 0.10],
            "max_hold_days": [7, 10, 15],
        })
        results = optimizer.run_grid_search(
            args.codes, start_date, end_date, grid,
            strategy_name=args.strategy,
        )

## 4. scripts/run_walk_forward.py — --all 옵션에 신규 전략 반영

기존 --all 옵션이 golden_cross → bb_bounce → adaptive 순서로 돌린다면,
신규 전략도 추가하거나 교체해줘:

if args.all:
    strategies = ["momentum_pullback", "institutional_flow", "disparity_reversion"]
elif args.strategy:
    strategies = [args.strategy]

기존 전략도 비교용으로 남겨두고 싶으면:
    strategies = [
        "momentum_pullback", "institutional_flow", "disparity_reversion",
        # "golden_cross", "bb_bounce",  # 비교용 (주석 해제하여 실행)
    ]

수정 후 관련 테스트만:
pytest tests/test_backtest.py -v

커밋:
git add -A
git commit -m "feat: 신규 전략 WF 그리드 + 최적화 인프라 구축

- momentum_pullback WF 그리드 (72조합)
- institutional_flow WF 그리드 (48조합)
- disparity_reversion WF 그리드 (72조합)
- CLI --optimize 전략별 그리드 자동 선택
- --all 옵션 신규 전략 반영"

git push
```

---

## 로컬 실행 가이드 — 백테스트 + WF 검증

> CLI 프롬프트로 인프라 세팅이 끝나면, 아래 명령어를 로컬에서 순서대로 실행.
> pykrx가 KRX 서버에서 데이터를 가져오므로 인터넷 연결 필요.

### Step 1: 단일 종목 백테스트 — 전략 동작 확인

각 전략이 신호를 제대로 생성하는지 먼저 확인:

```bash
# momentum_pullback — 삼성전자 2년
python -m src.backtest.engine --strategy momentum_pullback --period 2y --codes 005930

# institutional_flow — SK하이닉스 2년
python -m src.backtest.engine --strategy institutional_flow --period 2y --codes 000660

# disparity_reversion — 현대차 2년
python -m src.backtest.engine --strategy disparity_reversion --period 2y --codes 005380
```

**확인 포인트:**
- 거래가 발생하는가? (0건이면 진입 조건이 너무 까다로움)
- 승률이 합리적인가? (30~60% 범위)
- Sharpe가 양수인가?

### Step 2: 포트폴리오 백테스트 — 다종목 실전 시뮬레이션

```bash
# momentum_pullback — 20종목/8포지션
python -m src.backtest.engine --strategy momentum_pullback --period 2y \
  --codes 005930 000660 005380 000270 068270 035420 035720 105560 055550 066570 \
         006400 003670 012330 028260 096770 003550 034730 032830 030200 017670 \
  --portfolio --max-positions 8 --capital 3000000

# institutional_flow
python -m src.backtest.engine --strategy institutional_flow --period 2y \
  --codes 005930 000660 005380 000270 068270 035420 035720 105560 055550 066570 \
         006400 003670 012330 028260 096770 003550 034730 032830 030200 017670 \
  --portfolio --max-positions 8 --capital 3000000

# disparity_reversion
python -m src.backtest.engine --strategy disparity_reversion --period 2y \
  --codes 005930 000660 005380 000270 068270 035420 035720 105560 055550 066570 \
         006400 003670 012330 028260 096770 003550 034730 032830 030200 017670 \
  --portfolio --max-positions 8 --capital 3000000
```

### Step 3: 파라미터 최적화 — 각 전략 개별

```bash
# momentum_pullback 최적화
python -m src.backtest.engine --strategy momentum_pullback --period 2y \
  --codes 005930 000660 005380 000270 068270 \
  --optimize

# institutional_flow 최적화
python -m src.backtest.engine --strategy institutional_flow --period 2y \
  --codes 005930 000660 005380 000270 068270 \
  --optimize

# disparity_reversion 최적화
python -m src.backtest.engine --strategy disparity_reversion --period 2y \
  --codes 005930 000660 005380 000270 068270 \
  --optimize
```

**주의**: 최적화는 시간이 걸려. 종목 수를 5개로 줄여서 먼저 돌리고,
유망한 파라미터 범위를 좁힌 후 전체 20종목으로 재검증.

### Step 4: Walk-Forward 검증 — 과적합 판별

```bash
# 전략별 개별 WF
python scripts/run_walk_forward.py --strategy momentum_pullback --train 12 --test 3
python scripts/run_walk_forward.py --strategy institutional_flow --train 12 --test 3
python scripts/run_walk_forward.py --strategy disparity_reversion --train 12 --test 3

# 3개 전략 순차 실행
python scripts/run_walk_forward.py --all
```

**핵심 지표:**
- OOS Sharpe ≥ IS Sharpe × 0.5 → 과적합 아님 (합격)
- OOS Sharpe < IS Sharpe × 0.3 → 과적합 의심 (파라미터 축소 필요)
- OOS Sharpe < 0 → 전략 자체 재검토 필요

### Step 5: 결과 분석 후 판단

백테스트 결과를 나한테 공유해줘. 같이 볼 것:
1. 전략별 CAGR, MDD, Sharpe, 승률
2. WF IS vs OOS 열화율
3. 거래 빈도 (너무 적으면 실전에서 의미 없음)
4. 국면별 성과 차이

이 데이터를 보고:
- 파라미터 확정 (config.yaml 반영)
- 전략 제외 여부 판단 (3개 중 성과 미달 전략 비활성화)
- Paper trading 시작 결정

---

## 파라미터 설계 원칙

최적화할 때 주의할 점:

1. **파라미터 수를 최소화**: 전략당 5~6개가 한계. 그 이상은 과적합.
2. **안정 구간(plateau) 찾기**: 최고 Sharpe 단일 값이 아니라,
   주변 값에서도 성과가 비슷한 구간이 안정적.
3. **OOS 기준으로 판단**: IS에서 Sharpe 3.0이어도 OOS에서 0.5면 과적합.
4. **거래 빈도 확인**: 연 10건 미만이면 통계적으로 무의미.
5. **비용 감응도**: 슬리피지를 0.1% → 0.2%로 올려도 양수 수익이면 현실적.
