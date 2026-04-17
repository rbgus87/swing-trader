# INTRADAY_ENTRY.md — 장중 진입 판단 (오늘 가상 일봉)

> 현재가를 오늘 일봉의 종가로 가정하여 SMA/RSI/ADX 재계산.
> "임박" 조건식과 자연스럽게 결합하여 장중 실제 골든크로스 발생 즉시 매수.

## 배경

기존 `check_realtime_entry`는 어제까지의 일봉만 사용:
- 오늘 장중 가격이 SMA 계산에 반영 안 됨
- "임박" 조건식으로 잡힌 종목(SMA5<SMA20)은 영원히 진입 조건(SMA5>SMA20) 통과 못 함
- 결과: 매수 0건 데드락

해결: 오늘 현재가를 가상 일봉 추가 → SMA 재계산 → 장중 크로스 발생 시 매수

## 트레이드오프 (수용함)

- 백테스트(어제 종가 기준)와 미세하게 어긋남 → 수용
- 장중 변동 노이즈 가능 (오전 돌파→오후 무너짐) → 수용
- 진입 시간대 09:30~15:00 그대로 → 수용

---

## 프롬프트

```
CLAUDE.md를 읽어줘.
그리고 docs/INTRADAY_ENTRY.md를 읽어줘.

장중 진입 판단을 "어제 일봉 기준"에서 "오늘 현재가를 종가로 가정한 가상 일봉"
기준으로 변경해줘. 임박 조건식으로 잡힌 종목이 장중 실제 골든크로스를
발생시키는 순간 매수가 일어나도록 하는 것이 목적.

**중요:**
- 백테스트 로직(generate_backtest_signals)은 절대 변경하지 마. 일봉 종가 기준 그대로.
- 변경은 check_realtime_entry (실전 진입)만.
- adaptive 전략(disparity_reversion 등)은 건드리지 마. golden_cross만 적용.

## 1. src/strategy/golden_cross_strategy.py — check_realtime_entry 수정

기존 시그니처:
def check_realtime_entry(self, df_daily, df_60m=None) -> bool:

새 시그니처:
def check_realtime_entry(
    self, df_daily, df_60m=None,
    current_price: int | None = None,
    today_volume: int | None = None,
) -> bool:
    """장중 진입 — 오늘 현재가를 가상 일봉으로 추가하여 SMA 재계산.

    Args:
        df_daily: 어제까지의 일봉 OHLCV+지표 (calculate_indicators 적용된 것)
        df_60m: 사용 안 함 (호환성 유지)
        current_price: 오늘 현재가 (가상 일봉의 close)
        today_volume: 오늘 누적 거래량 (없으면 어제 거래량으로 fallback)

    Returns:
        진입 조건 5개 모두 통과 시 True.
    """
    p = self.params
    adx_threshold = p.get("adx_threshold", 20)
    volume_multiplier = p.get("volume_multiplier", 1.0)
    rsi_entry_min = p.get("rsi_entry_min", 35)
    screening_lookback = p.get("screening_lookback", 5)

    if len(df_daily) < screening_lookback + 1:
        self._last_reject = "데이터부족"
        return False

    # current_price가 없으면 기존 동작 (어제 종가 기준)
    if current_price is None:
        df_for_check = df_daily
    else:
        # 오늘 가상 일봉 추가 → 지표 재계산
        df_for_check = self._build_with_today_candle(
            df_daily, current_price, today_volume
        )
        if df_for_check is None or df_for_check.empty:
            self._last_reject = "가상일봉생성실패"
            return False

    latest = df_for_check.iloc[-1]

    # 1. SMA5 > SMA20 유지 중
    sma5 = latest["sma5"]
    sma20 = latest["sma20"]
    if sma5 <= sma20:
        self._last_reject = f"SMA5({sma5:,.0f})<=SMA20({sma20:,.0f})"
        return False

    # 2. 최근 N일 내 크로스 발생
    recent = df_for_check.iloc[-(screening_lookback + 1):]
    cross_found = False
    for i in range(1, len(recent)):
        if (recent.iloc[i]["sma5"] > recent.iloc[i]["sma20"] and
                recent.iloc[i - 1]["sma5"] <= recent.iloc[i - 1]["sma20"]):
            cross_found = True
            break
    if not cross_found:
        self._last_reject = f"크로스없음(최근{screening_lookback}일)"
        return False

    # 3. RSI >= 하한
    rsi = latest.get("rsi", 50)
    if rsi < rsi_entry_min:
        self._last_reject = f"RSI({rsi:.1f})<{rsi_entry_min}"
        return False

    # 4. ADX >= 임계값
    adx = latest.get("adx", 0)
    if adx < adx_threshold:
        self._last_reject = f"ADX({adx:.1f})<{adx_threshold}"
        return False

    # 5. 거래량 >= 20일 평균
    volume = latest["volume"]
    volume_sma20 = latest.get("volume_sma20", 0)
    if volume < volume_sma20 * volume_multiplier:
        self._last_reject = (
            f"거래량({volume:,})<평균×{volume_multiplier}({volume_sma20:,.0f})"
        )
        return False

    return True


def _build_with_today_candle(
    self, df_daily, current_price, today_volume
):
    """어제까지 일봉에 오늘 가상 일봉을 추가하고 지표 재계산.

    오늘 가상 일봉:
        open: 어제 종가 (시가 추적 안 함, 갭 영향 무시)
        high: max(어제 종가, 현재가)
        low: min(어제 종가, 현재가)
        close: 현재가
        volume: today_volume 또는 어제 거래량 (fallback)

    Returns:
        오늘 일봉 추가 + 지표 재계산된 DataFrame.
    """
    import pandas as pd
    from src.strategy.signals import calculate_indicators

    if df_daily is None or df_daily.empty:
        return None

    yesterday = df_daily.iloc[-1]
    yesterday_close = float(yesterday.get("close", current_price))

    # fallback: today_volume 없으면 어제 거래량 사용
    vol = today_volume if today_volume and today_volume > 0 else int(yesterday.get("volume", 0))

    today_row = {
        "open": yesterday_close,
        "high": max(yesterday_close, current_price),
        "low": min(yesterday_close, current_price),
        "close": float(current_price),
        "volume": int(vol),
    }
    
    # date 컬럼이 있으면 채워줌 (없으면 무시)
    if "date" in df_daily.columns:
        from datetime import datetime
        today_row["date"] = datetime.now().strftime("%Y-%m-%d")

    # OHLCV만 떼어서 새 row 추가 → 지표 재계산
    base_cols = [c for c in ["date", "open", "high", "low", "close", "volume"] if c in df_daily.columns]
    df_ohlcv = df_daily[base_cols].copy()
    new_row_df = pd.DataFrame([{c: today_row.get(c) for c in base_cols}])
    df_combined = pd.concat([df_ohlcv, new_row_df], ignore_index=True)

    # 지표 재계산 (SMA, RSI, ADX, volume_sma20 등)
    df_with_indicators = calculate_indicators(df_combined)
    return df_with_indicators


## 2. src/engine.py — _check_entry_conditions에서 current_price 전달

기존 호출부 (line ~711):
if strategy.check_realtime_entry(df_daily, df_60m):

변경:
# golden_cross 전략만 current_price 전달 (다른 전략은 시그니처 미변경)
if strategy.name == "golden_cross":
    if strategy.check_realtime_entry(
        df_daily, df_60m,
        current_price=tick.price,
        today_volume=getattr(tick, "volume", None),
    ):
        matched_strategy = strategy
        break
else:
    if strategy.check_realtime_entry(df_daily, df_60m):
        matched_strategy = strategy
        break

(주의: tick.volume이 "현재가 1틱의 거래량"인지 "오늘 누적 거래량"인지에 따라
효과 다름. 키움 0B 실시간 체결 데이터의 13번 항목이 누적 거래량이므로
WS_client에서 tick.volume에 누적 거래량을 담고 있을 가능성 높음. 만약
"틱 거래량"이라면 별도 누적 처리 필요. 일단 현재 구현 그대로 전달하고
로그로 확인.)

## 3. 검증

### 3-1. 단위 테스트 (수동 시뮬레이션)
python -c "
import pandas as pd
from src.strategy.golden_cross_strategy import GoldenCrossStrategy
from src.strategy.signals import calculate_indicators

# 가짜 일봉: 21일치 (SMA20 계산용)
# 마지막 5일 동안 SMA5 < SMA20인 임박 상태 만들기
import numpy as np
np.random.seed(42)
prices = list(np.linspace(10000, 9500, 15)) + list(np.linspace(9500, 9800, 6))
volumes = [100000] * 21

df = pd.DataFrame({
    'open': prices,
    'high': [p*1.01 for p in prices],
    'low': [p*0.99 for p in prices],
    'close': prices,
    'volume': volumes,
})
df_with_ind = calculate_indicators(df)
print('어제 SMA5/SMA20:', df_with_ind.iloc[-1]['sma5'], df_with_ind.iloc[-1]['sma20'])

strategy = GoldenCrossStrategy(params={
    'adx_threshold': 20,
    'volume_multiplier': 1.0,
    'rsi_entry_min': 35,
    'screening_lookback': 5,
})

# 어제 종가 기준 진입체크 (current_price=None) → 탈락 예상
result1 = strategy.check_realtime_entry(df_with_ind)
print(f'기존 방식: {result1} | 사유: {strategy._last_reject}')

# 오늘 현재가 11000으로 시뮬레이션 (큰 폭 상승)
result2 = strategy.check_realtime_entry(df_with_ind, current_price=11000, today_volume=200000)
print(f'장중 방식 (price=11000): {result2} | 사유: {strategy._last_reject}')
"

### 3-2. 실전 테스트 시나리오
1. 페이퍼 모드 시작
2. 09:30 이후 watchlist 종목 polling 시작
3. 로그 확인:
   - 'check_realtime_entry' 호출 시 current_price 반영되는지
   - 탈락 사유가 변하는지 (SMA5/SMA20 값이 어제와 다르게 찍혀야 함)
4. 장중 실제 진입 발생 여부 모니터링

### 3-3. 백테스트 회귀 확인
백테스트는 generate_backtest_signals를 사용하므로 변경 없음.
혹시 모르니 한 번 돌려서 결과 변하지 않았는지 확인:
python scripts/backtest_runner.py --strategy golden_cross --period 1y

## 4. 커밋

git add -A
git commit -m "feat: 장중 진입 판단 — 오늘 현재가를 가상 일봉으로 SMA 재계산

문제:
- 기존 check_realtime_entry는 어제 일봉만 사용
- 'swing_pre_cross' 임박 조건식 종목은 SMA5<SMA20 (정의상)
- 어제 일봉 기준이면 영원히 진입 조건(SMA5>SMA20) 미충족
- 매수 0건 데드락

해결:
- check_realtime_entry에 current_price/today_volume 파라미터 추가
- 오늘 가상 일봉 (close=current_price) 추가 후 SMA/RSI/ADX 재계산
- 임박 종목이 장중 실제 크로스하는 순간 매수 가능

변경:
- golden_cross_strategy.py: check_realtime_entry 시그니처 확장
- golden_cross_strategy.py: _build_with_today_candle 헬퍼 추가
- engine.py: golden_cross 호출 시 current_price 전달

미변경 (의도적):
- generate_backtest_signals (백테스트 로직, 일봉 종가 기준 유지)
- 다른 전략(disparity_reversion) check_realtime_entry
- 진입 시간대 (09:30~15:00 그대로)
- 조건검색 (swing_pre_cross 임박 조건 그대로)

트레이드오프 수용:
- 백테스트와 미세한 어긋남 (백테=종가, 실전=장중)
- 장중 변동 노이즈 가능 (오전 돌파→오후 무너짐)"
git push

## 5. 트러블슈팅

### 문제: 변경 후에도 매수 0건
- 로그에서 _last_reject 사유 확인
- SMA5/SMA20 값이 어제와 비슷한지 (가상 일봉이 효과 없는 경우)
- → 가상 일봉 close 값 확인, current_price가 정상 전달되는지 점검

### 문제: 진입 후 즉시 손절
- 장중 거짓 신호 (오전 급등→오후 하락)
- 대응: entry_start_time을 13:00으로 늦추는 것 검토 (config.yaml)

### 문제: 같은 종목 여러 번 진입 시도
- 30초 쓰로틀링 + held_codes 체크가 이미 있음
- 추가 방어 필요 시: _entry_logged 활용

### 문제: today_volume이 비현실적으로 작음
- tick.volume이 "틱 거래량"인 경우
- _build_with_today_candle에서 fallback으로 어제 거래량 사용 중
- 정확도 원하면 별도 누적 거래량 추적 필요 (별건)
```
