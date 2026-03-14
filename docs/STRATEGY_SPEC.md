# STRATEGY_SPEC.md — 전략 명세

## 1. 전략 개요

- 전략명: MACD-RSI 스윙 전략 (기본 전략)
- 타임프레임: 일봉 (기준) + 60분봉 (진입 타이밍)
- 보유 기간: 2일 ~ 15 영업일
- 목표 수익: 진입가 대비 +8% (기본값, 파라미터화)
- 손절 기준: ATR × 1.5 (진입가 기준 하방)

---

## 2. 신호 생성 로직

### 2.1 매수 신호 (Entry) — AND 조건

```python
def check_entry_signal(df: pd.DataFrame, df_60m: pd.DataFrame) -> bool:
    """
    df      : 일봉 DataFrame (close, macd, macd_signal, rsi, bb_upper, bb_lower, volume, sma20)
    df_60m  : 60분봉 DataFrame (close, sma5, sma20)
    모든 조건 동시 충족 시 True
    """
    latest = df.iloc[-1]
    prev   = df.iloc[-2]

    # 조건 1: 20일 이평선 위 종가 (추세 확인)
    cond_trend = latest['close'] > latest['sma20']

    # 조건 2: MACD 히스토그램 양전환 (전일 음수 → 당일 양수)
    cond_macd = (prev['macd_hist'] < 0) and (latest['macd_hist'] > 0)

    # 조건 3: RSI 40~65 구간 (과매수 아닌 모멘텀 구간)
    cond_rsi = 40 <= latest['rsi'] <= 65

    # 조건 4: 거래량 20일 평균 대비 1.5배 이상
    cond_volume = latest['volume'] >= latest['volume_sma20'] * 1.5

    # 조건 5: 60분봉 단기 추세 상향 (5분봉 > 20분봉 이평)
    cond_60m = df_60m.iloc[-1]['sma5'] > df_60m.iloc[-1]['sma20']

    return all([cond_trend, cond_macd, cond_rsi, cond_volume, cond_60m])
```

### 2.2 매도 신호 (Exit) — OR 조건

```python
def check_exit_signal(position: Position, current_price: int, latest: pd.Series) -> ExitReason | None:
    """우선순위 순서로 체크"""

    # 1순위: 손절가 이탈 (절대 우선)
    if current_price <= position.stop_price:
        return ExitReason.STOP_LOSS

    # 2순위: 트레일링스탑 발동
    if current_price <= position.trailing_stop:
        return ExitReason.TRAILING_STOP

    # 3순위: 목표가 도달
    if current_price >= position.target_price:
        return ExitReason.TARGET_REACHED

    # 4순위: MACD 데드크로스 (수익 구간에서만)
    pnl_pct = (current_price - position.entry_price) / position.entry_price
    if pnl_pct > 0.02 and latest['macd_hist'] < 0 and position.prev_macd_hist >= 0:
        return ExitReason.MACD_DEAD

    # 5순위: 최대 보유 기간 초과
    if position.hold_days >= MAX_HOLD_DAYS:
        return ExitReason.MAX_HOLD

    return None  # 보유 유지
```

---

## 3. 지표 계산

```python
# src/strategy/signals.py
import pandas_ta as ta

def calculate_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    입력: OHLCV DataFrame (컬럼명 영문 변환 후)
    출력: 지표 추가된 DataFrame
    """
    # 이동평균선
    df['sma5']   = ta.sma(df['close'], length=5)
    df['sma20']  = ta.sma(df['close'], length=20)
    df['sma60']  = ta.sma(df['close'], length=60)
    df['sma120'] = ta.sma(df['close'], length=120)

    # MACD
    macd = ta.macd(df['close'], fast=12, slow=26, signal=9)
    df['macd']        = macd['MACD_12_26_9']
    df['macd_signal'] = macd['MACDs_12_26_9']
    df['macd_hist']   = macd['MACDh_12_26_9']

    # RSI
    df['rsi'] = ta.rsi(df['close'], length=14)

    # 볼린저밴드
    bb = ta.bbands(df['close'], length=20, std=2)
    df['bb_upper'] = bb['BBU_20_2.0']
    df['bb_mid']   = bb['BBM_20_2.0']
    df['bb_lower'] = bb['BBL_20_2.0']

    # 스토캐스틱
    stoch = ta.stoch(df['high'], df['low'], df['close'], k=14, d=3, smooth_k=3)
    df['stoch_k'] = stoch['STOCHk_14_3_3']
    df['stoch_d'] = stoch['STOCHd_14_3_3']

    # ATR (손절 계산용)
    df['atr'] = ta.atr(df['high'], df['low'], df['close'], length=14)

    # ADX (추세 강도 — 국면 판별)
    adx = ta.adx(df['high'], df['low'], df['close'], length=14)
    df['adx'] = adx['ADX_14']

    # 거래량 이동평균
    df['volume_sma20'] = ta.sma(df['volume'], length=20)

    return df.dropna()
```

---

## 4. 종목 스크리닝 (일간 배치)

```python
# src/strategy/screener.py
# 실행 시점: 매일 08:30 (장 시작 전)

def run_daily_screening(date: str = None) -> list[str]:
    """
    반환: 당일 매수 후보 종목코드 리스트 (최대 30종목)
    """
    # Step 1: 전종목 일봉 데이터 수집
    from pykrx import stock
    today = date or datetime.now().strftime('%Y%m%d')

    all_codes = (
        stock.get_market_ticker_list(market='KOSPI') +
        stock.get_market_ticker_list(market='KOSDAQ')
    )

    # Step 2: 유동성 필터 (빠른 필터링 먼저)
    candidates = []
    for code in all_codes:
        try:
            df = get_cached_ohlcv(code, period='3m')   # 캐시 우선
            if df is None or len(df) < 60:
                continue

            latest = df.iloc[-1]

            # 유동성 필터
            if latest['amount'] < MIN_DAILY_AMOUNT:        # 거래대금 50억
                continue
            if latest['close'] < MIN_PRICE:                # 최소 주가 1,000
                continue
            if latest['close'] > MAX_PRICE:                # 최대 주가 500,000
                continue

            candidates.append(code)
        except Exception:
            continue

    # Step 3: 지표 계산 및 신호 체크
    result = []
    for code in candidates:
        df = get_cached_ohlcv(code, period='1y')
        df = calculate_indicators(df)
        df_60m = get_60min_ohlcv(code, period='20d')

        if check_entry_signal(df, df_60m):
            score = calculate_signal_score(df)  # 신호 강도 점수
            result.append((code, score))

    # Step 4: 상위 N종목 반환
    result.sort(key=lambda x: x[1], reverse=True)
    return [code for code, _ in result[:TOP_N]]
```

---

## 5. 멀티 타임프레임 분석

| 타임프레임 | 역할 | 확인 내용 |
|-----------|------|---------|
| 일봉 | 전략 기준 | 추세, 신호 생성 |
| 60분봉 | 진입 타이밍 | 단기 방향성 확인 |

60분봉은 일봉 신호 발생 후 진입 타이밍 정밀화에만 사용. 60분봉이 단기 상향 추세가 아니라면 당일 진입 보류, 다음 신호까지 대기.

---

## 6. 파라미터 최적화 대상

vectorbt 그리드 서치로 아래 파라미터 최적화.

```python
PARAM_GRID = {
    'macd_fast':         [8, 10, 12],
    'macd_slow':         [22, 24, 26],
    'macd_signal':       [7, 9],
    'rsi_period':        [12, 14],
    'rsi_min':           [35, 40, 45],
    'rsi_max':           [60, 65, 70],
    'volume_multiplier': [1.2, 1.5, 2.0],
    'stop_atr_mult':     [1.0, 1.5, 2.0],
    'target_return':     [0.06, 0.08, 0.10],
}
# 총 조합: 3×3×2×2×3×3×3×3×3 = 8,748가지 → vectorbt 수초 완료
```

최적화 기준 지표 (우선순위 순):
1. 샤프지수 ≥ 1.0
2. MDD ≤ -15%
3. 승률 ≥ 45%
4. 손익비 ≥ 1.8
