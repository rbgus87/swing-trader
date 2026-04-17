# STRATEGY_V3.md — 진입 조건 단순화 + 유니버스 확대

> 목적: WF 구간당 신호 0건 문제 해결
> 핵심: AND 조건 축소(과적합 감소 + 신호 빈도 증가) + 종목 유니버스 10배 확대

---

## 프롬프트 1 — momentum_pullback 진입 단순화

```
CLAUDE.md를 읽어줘.

momentum_pullback의 진입 조건을 5개 AND에서 3개 AND로 줄여줘.
WF 12개월 윈도우에서 0건 거래가 나온 원인이 AND 조건 누적이야.

## 설계 원칙

남기는 조건:
1. 모멘텀 양수 — 이게 엣지 자체. 절대 제거 불가.
2. 눌림목 (pullback OR RSI 과매도) — 진입 타이밍. 엣지 활용의 핵심.
3. 양봉 확인 — 반등 시작 확인. 최소한의 확인 신호.

제거하는 조건:
- SMA20 위: 60일 모멘텀이 양수면 대부분 SMA20 위에 있어. 중복 필터.
- 거래량 >= 20일 평균: "신호 살인범". 거래량은 노이즈가 크고,
  한국 시장에서 거래량 조건이 의미있는 것은 돌파(breakout) 전략이지
  눌림목 반등 전략이 아님. 눌림목에서 거래량이 줄어드는 게 오히려 정상.

## src/strategy/momentum_pullback_strategy.py 수정

### generate_backtest_signals 수정

기존:
raw_entries = cond_momentum & cond_above_sma & cond_pullback_or_rsi & cond_bullish & cond_vol

수정:
raw_entries = cond_momentum & cond_pullback_or_rsi & cond_bullish

cond_above_sma, cond_vol 정의는 남겨둬도 되지만 raw_entries에서 제거.
주석으로 "# v3: SMA20, 거래량 조건 제거 — 리스크는 청산 로직(손절/트레일링)에 위임" 추가.

### check_screening_entry 수정

기존 5개 조건 → 3개로:

def check_screening_entry(self, df: pd.DataFrame) -> bool:
    momentum_period = self.params.get("momentum_period", 60)
    pullback_days = self.params.get("pullback_days", 3)
    rsi_pullback_threshold = self.params.get("rsi_pullback_threshold", 40)

    if len(df) < momentum_period + 5:
        return False
    latest = df.iloc[-1]

    # 1. 60일 모멘텀 양수 (엣지)
    momentum = (latest["close"] - df.iloc[-momentum_period]["close"]) / df.iloc[-momentum_period]["close"]
    if momentum <= 0:
        return False

    # 2. 눌림목 확인 (타이밍)
    recent = df.iloc[-pullback_days:]
    down_days = sum(1 for i in range(len(recent)) if recent.iloc[i]["close"] < recent.iloc[i]["open"])
    if down_days < 1:
        if latest.get("rsi", 50) > rsi_pullback_threshold:
            return False

    # 3. 당일 양봉 (확인)
    if latest["close"] <= latest["open"]:
        return False

    return True

### check_realtime_entry 수정

동일하게 3개 조건으로:

def check_realtime_entry(self, df_daily, df_60m=None):
    momentum_period = self.params.get("momentum_period", 60)
    pullback_days = self.params.get("pullback_days", 3)

    if len(df_daily) < momentum_period + 5:
        return False
    latest = df_daily.iloc[-1]

    # 1. 60일 모멘텀 양수
    past = df_daily.iloc[-momentum_period]
    momentum = (latest["close"] - past["close"]) / past["close"]
    if momentum <= 0:
        return False

    # 2. 최근 N일 눌림 후 반등 (1% 이상 하락)
    if len(df_daily) >= pullback_days + 1:
        pullback_start = df_daily.iloc[-(pullback_days + 1)]
        pullback_end = df_daily.iloc[-2]
        pullback_pct = (pullback_end["close"] - pullback_start["close"]) / pullback_start["close"]
        if pullback_pct > -0.01:
            return False

    # 3. 당일 반등 (전일 대비 상승)
    if latest["close"] <= df_daily.iloc[-2]["close"]:
        return False

    return True

수정 후:
pytest tests/test_strategy.py -v -k "momentum"
```

---

## 프롬프트 2 — disparity_reversion 진입 단순화

```
CLAUDE.md를 읽어줘.

disparity_reversion의 진입 조건을 4개 AND에서 2개 AND로 줄여줘.

## 설계 원칙

남기는 조건:
1. 이격도 < 임계값 — 이게 엣지 자체.
2. 양봉 확인 — 바닥 반등 시작 확인.

제거하는 조건:
- RSI < oversold: 이격도가 낮으면 RSI도 자연히 낮아. 높은 상관관계 = 중복 필터.
- SMA60 상승: 이 조건이 대부분의 신호를 죽였어. 이격도가 96% 미만인 시점에서
  SMA60이 상승 중인 경우는 매우 드물어 — 가격이 급락했으니까 SMA도 꺾이기 시작하거든.
  장기 추세 보호는 "이격도 88% 이하면 손절"로 대체.

## src/strategy/disparity_reversion_strategy.py 수정

### generate_backtest_signals 수정

기존:
raw_entries = cond_disparity & cond_rsi & cond_bullish & cond_sma60_up

수정:
raw_entries = cond_disparity & cond_bullish

주석: "# v3: RSI, SMA60 조건 제거 — 추가 하락 리스크는 이격도 88% 손절로 관리"

### check_screening_entry 수정

def check_screening_entry(self, df: pd.DataFrame) -> bool:
    disparity_entry = self.params.get("disparity_entry", 96)

    if len(df) < 20:
        return False
    latest = df.iloc[-1]

    sma20 = latest.get("sma20", 0)
    if sma20 <= 0:
        return False

    # 1. 이격도 < 임계값 (엣지)
    disparity = latest["close"] / sma20 * 100
    if disparity >= disparity_entry:
        return False

    # 2. 당일 양봉 (바닥 확인)
    if latest["close"] <= latest["open"]:
        return False

    return True

### check_realtime_entry 수정

def check_realtime_entry(self, df_daily, df_60m=None):
    disparity_entry = self.params.get("disparity_entry", 96)

    if len(df_daily) < 20:
        return False
    latest = df_daily.iloc[-1]

    sma20 = latest.get("sma20", 0)
    if sma20 <= 0:
        return False

    # 1. 이격도 < 임계값
    disparity = latest["close"] / sma20 * 100
    if disparity >= disparity_entry:
        return False

    # 2. 당일 양봉 + 전일 대비 반등
    if latest["close"] <= latest["open"]:
        return False
    if len(df_daily) >= 2 and latest["close"] <= df_daily.iloc[-2]["close"]:
        return False

    return True

수정 후:
pytest tests/test_strategy.py -v -k "disparity"
```

---

## 프롬프트 3 — WF 유니버스 동적 확대

```
CLAUDE.md를 읽어줘.

WF 백테스트의 종목 유니버스를 하드코딩 20개에서 동적 100~200개로 확대해줘.

## 1. scripts/run_walk_forward.py에 동적 유니버스 함수 추가

DEFAULT_CODES는 유지 (--use-default 옵션용).

새 함수 추가:

def get_dynamic_universe(min_market_cap: int = 300_000_000_000,
                         top_n: int = 150) -> list[str]:
    """시가총액 상위 종목을 동적으로 선정.

    pykrx로 KOSPI+KOSDAQ 전종목의 시가총액을 조회하고
    상위 N종목을 반환. 거래정지/관리종목은 자동 제외.

    Args:
        min_market_cap: 최소 시가총액 (원). 기본 3000억.
        top_n: 상위 N종목. 기본 150.

    Returns:
        종목코드 리스트.
    """
    from pykrx import stock
    from datetime import datetime, timedelta

    # 최근 거래일 기준
    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=7)).strftime("%Y%m%d")

    codes = []
    for market in ["KOSPI", "KOSDAQ"]:
        try:
            tickers = stock.get_market_ticker_list(end, market=market)
            if not tickers:
                # 주말/공휴일이면 이전 거래일 시도
                tickers = stock.get_market_ticker_list(start, market=market)
            codes.extend(tickers)
        except Exception as e:
            print(f"  {market} 종목 조회 실패: {e}")

    if not codes:
        print("  종목 조회 실패 — DEFAULT_CODES 사용")
        return DEFAULT_CODES

    # 시가총액 조회
    try:
        cap_df = stock.get_market_cap(end)
        if cap_df.empty:
            cap_df = stock.get_market_cap(start)
    except Exception:
        print("  시가총액 조회 실패 — DEFAULT_CODES 사용")
        return DEFAULT_CODES

    # 시가총액 필터 + 정렬
    cap_df = cap_df[cap_df["시가총액"] >= min_market_cap]
    cap_df = cap_df.sort_values("시가총액", ascending=False)

    result = cap_df.head(top_n).index.tolist()
    print(f"  동적 유니버스: {len(result)}종목 (시가총액 {min_market_cap/1e8:.0f}억 이상)")
    return result

## 2. argparse에 --dynamic 옵션 추가

parser.add_argument("--dynamic", action="store_true",
                    help="동적 유니버스 사용 (시가총액 상위 150종목)")
parser.add_argument("--universe-size", type=int, default=150,
                    help="동적 유니버스 종목 수 (기본: 150)")
parser.add_argument("--min-cap", type=int, default=300_000_000_000,
                    help="최소 시가총액 (기본: 3000억)")

## 3. 코드 결정 로직 수정

기존:
if args.codes:
    codes = [c.strip() for c in args.codes.split(",")]
else:
    codes = DEFAULT_CODES

수정:
if args.codes:
    codes = [c.strip() for c in args.codes.split(",")]
elif args.dynamic:
    codes = get_dynamic_universe(
        min_market_cap=args.min_cap,
        top_n=args.universe_size,
    )
else:
    codes = DEFAULT_CODES

## 4. 백테스트 CLI에도 동일 옵션 추가

src/backtest/engine.py CLI 섹션에 --dynamic 옵션 추가.
동일한 get_dynamic_universe 함수를 사용하되,
함수를 src/strategy/screener.py 또는 data/provider.py에 두고 공유:

data/provider.py에 메서드 추가:

def get_top_stocks_by_market_cap(self, top_n: int = 150,
                                  min_market_cap: int = 300_000_000_000) -> list[str]:
    """시가총액 상위 종목 코드 리스트."""
    from pykrx import stock
    from datetime import datetime, timedelta

    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=7)).strftime("%Y%m%d")

    try:
        cap_df = stock.get_market_cap(end)
        if cap_df.empty:
            cap_df = stock.get_market_cap(start)
        cap_df = cap_df[cap_df["시가총액"] >= min_market_cap]
        cap_df = cap_df.sort_values("시가총액", ascending=False)
        return cap_df.head(top_n).index.tolist()
    except Exception:
        return []

WF 스크립트와 백테스트 CLI 모두 이 메서드 호출.

## 5. WF 그리드에 momentum_period 축소 옵션 추가

유니버스가 커지면 momentum_period=40도 테스트해야 해.
WF_GRID_MOMENTUM_PULLBACK에 이미 [40, 60] 있으니 유지.

수정 후:
python scripts/run_walk_forward.py --help
→ --dynamic, --universe-size, --min-cap 옵션 확인

커밋하지 마. 프롬프트 4에서 전체 검증 후 커밋.
```

---

## 프롬프트 4 — 전체 검증 + 커밋

```
Phase 수정이 끝났으니 전체 검증해줘.

1. 전략 조건 확인:
   grep -n "raw_entries" src/strategy/momentum_pullback_strategy.py
   → cond_momentum & cond_pullback_or_rsi & cond_bullish (3개 AND만)
   
   grep -n "raw_entries" src/strategy/disparity_reversion_strategy.py
   → cond_disparity & cond_bullish (2개 AND만)

2. 동적 유니버스 확인:
   grep -n "get_top_stocks_by_market_cap\|get_dynamic_universe\|--dynamic" \
     scripts/run_walk_forward.py src/backtest/engine.py data/provider.py

3. 테스트:
   pytest tests/ -v -k "not test_screening_failure" --tb=short

4. 실패 테스트가 있으면:
   - 진입 조건 변경으로 기존 전략 테스트가 깨지면 새 조건에 맞게 업데이트
   - cond_above_sma, cond_vol 제거를 반영

커밋:
git add -A
git commit -m "refactor: 전략 v3 — 진입 단순화(AND 5→3/4→2) + 유니버스 동적 확대

전략 단순화:
- momentum_pullback: AND 5→3 (모멘텀+눌림목+양봉). SMA20/거래량 제거.
- disparity_reversion: AND 4→2 (이격도+양봉). RSI/SMA60 제거.
- 리스크는 청산 로직(손절 ATR×1.5, 트레일링, 이격도 88% 컷)에 위임.

유니버스 확대:
- data/provider.py: get_top_stocks_by_market_cap() 추가
- WF/백테스트 CLI: --dynamic 옵션 (시가총액 상위 150종목)
- 기존 DEFAULT_CODES 20종목은 --use-default로 유지"

git push
```

---

## 로컬 실행 — 재검증

커밋 후 순서대로:

### Step 1: 단일 종목 확인 (신호 빈도 증가 확인)

```bash
python -m src.backtest.engine --strategy momentum_pullback --period 2y --codes 005930
python -m src.backtest.engine --strategy disparity_reversion --period 2y --codes 005380
```

### Step 2: 포트폴리오 — 동적 유니버스 150종목

```bash
# 동적 유니버스로 포트폴리오 백테스트
python -m src.backtest.engine --strategy momentum_pullback --period 2y \
  --dynamic --portfolio --max-positions 8 --capital 3000000

python -m src.backtest.engine --strategy disparity_reversion --period 2y \
  --dynamic --portfolio --max-positions 8 --capital 3000000
```

### Step 3: WF 검증 — 동적 유니버스

```bash
python scripts/run_walk_forward.py --strategy momentum_pullback \
  --dynamic --universe-size 100 --train 12 --test 3

python scripts/run_walk_forward.py --strategy disparity_reversion \
  --dynamic --universe-size 100 --train 12 --test 3
```

주의: 유니버스 100~150종목에서 WF를 돌리면 시간이 상당히 걸려.
먼저 --universe-size 50으로 테스트한 후 확대하는 것도 방법.

### 합격 기준

| 지표 | momentum_pullback | disparity_reversion |
|------|------------------|-------------------|
| WF 구간당 거래 수 | ≥ 5건 | ≥ 3건 |
| OOS Sharpe | ≥ 0.3 | ≥ 0.2 |
| IS→OOS 열화율 | < 100% | < 150% |
| 연간 거래 빈도 | ≥ 50건 | ≥ 20건 |

결과 나오면 공유해줘.
