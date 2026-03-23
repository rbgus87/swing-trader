"""매매 신호 생성 — 지표 계산, 매수/매도 신호, 점수 산출.

pandas-ta 라이브러리를 사용하여 기술적 지표를 계산하고,
스윙 매매를 위한 AND(매수) / OR(매도) 조건 기반 신호를 생성.
"""

import sys
import types

import pandas as pd

# pandas_ta가 numba를 optional import하지만, PyInstaller exe에서는
# numba가 번들되지 않아 ImportError 발생. 더미 모듈로 우회.
if "numba" not in sys.modules:
    _noop_decorator = lambda *a, **kw: (lambda f: f)
    numba_mock = types.ModuleType("numba")
    numba_mock.jit = _noop_decorator
    numba_mock.njit = _noop_decorator
    numba_mock.vectorize = _noop_decorator
    numba_mock.prange = range
    sys.modules["numba"] = numba_mock

import pandas_ta as ta

from src.models import ExitReason, Position


def calculate_indicators(
    df: pd.DataFrame,
    *,
    macd_fast: int = 12,
    macd_slow: int = 26,
    macd_signal: int = 9,
    rsi_period: int = 14,
    bb_period: int = 20,
    bb_std: float = 2.0,
    stoch_k: int = 14,
    stoch_d: int = 3,
    stoch_smooth: int = 3,
    atr_period: int = 14,
    adx_period: int = 14,
    volume_sma_period: int = 20,
) -> pd.DataFrame:
    """OHLCV DataFrame에 기술적 지표를 추가.

    입력: 영문 컬럼명 (open, high, low, close, volume) DataFrame.

    추가 지표:
    - SMA: 5, 20, 60, 120일
    - MACD (12, 26, 9): macd, macd_signal, macd_hist
    - RSI (14): rsi
    - 볼린저밴드 (20, 2σ): bb_upper, bb_mid, bb_lower
    - 스토캐스틱 (14, 3, 3): stoch_k, stoch_d
    - ATR (14): atr
    - ADX (14): adx
    - 거래량 이동평균 (20): volume_sma20

    NaN 행 제거 후 반환 (dropna).

    Args:
        df: open, high, low, close, volume 컬럼을 가진 DataFrame.
        macd_fast: MACD 단기 기간.
        macd_slow: MACD 장기 기간.
        macd_signal: MACD 시그널 기간.
        rsi_period: RSI 기간.
        bb_period: 볼린저밴드 기간.
        bb_std: 볼린저밴드 표준편차.
        stoch_k: 스토캐스틱 %K 기간.
        stoch_d: 스토캐스틱 %D 기간.
        stoch_smooth: 스토캐스틱 스무딩.
        atr_period: ATR 기간.
        adx_period: ADX 기간.
        volume_sma_period: 거래량 이동평균 기간.

    Returns:
        지표가 추가된 DataFrame (NaN 행 제거됨).
    """
    result = df.copy()

    # SMA
    result["sma5"] = ta.sma(result["close"], length=5)
    result["sma20"] = ta.sma(result["close"], length=20)
    result["sma60"] = ta.sma(result["close"], length=60)
    result["sma120"] = ta.sma(result["close"], length=120)

    # MACD
    macd_df = ta.macd(
        result["close"], fast=macd_fast, slow=macd_slow, signal=macd_signal
    )
    macd_col = f"MACD_{macd_fast}_{macd_slow}_{macd_signal}"
    macds_col = f"MACDs_{macd_fast}_{macd_slow}_{macd_signal}"
    macdh_col = f"MACDh_{macd_fast}_{macd_slow}_{macd_signal}"
    result["macd"] = macd_df[macd_col]
    result["macd_signal"] = macd_df[macds_col]
    result["macd_hist"] = macd_df[macdh_col]

    # RSI
    result["rsi"] = ta.rsi(result["close"], length=rsi_period)

    # 볼린저밴드
    bb_df = ta.bbands(result["close"], length=bb_period, std=bb_std)
    result["bb_upper"] = bb_df[f"BBU_{bb_period}_{bb_std}_{bb_std}"]
    result["bb_mid"] = bb_df[f"BBM_{bb_period}_{bb_std}_{bb_std}"]
    result["bb_lower"] = bb_df[f"BBL_{bb_period}_{bb_std}_{bb_std}"]

    # 스토캐스틱
    stoch_df = ta.stoch(
        result["high"], result["low"], result["close"],
        k=stoch_k, d=stoch_d, smooth_k=stoch_smooth,
    )
    result["stoch_k"] = stoch_df[f"STOCHk_{stoch_k}_{stoch_d}_{stoch_smooth}"]
    result["stoch_d"] = stoch_df[f"STOCHd_{stoch_k}_{stoch_d}_{stoch_smooth}"]

    # ATR
    result["atr"] = ta.atr(
        result["high"], result["low"], result["close"], length=atr_period
    )

    # ADX
    adx_df = ta.adx(
        result["high"], result["low"], result["close"], length=adx_period
    )
    result["adx"] = adx_df[f"ADX_{adx_period}"]

    # 거래량 이동평균
    result["volume_sma20"] = ta.sma(result["volume"], length=volume_sma_period)

    # OBV (On-Balance Volume)
    obv = ta.obv(result["close"], result["volume"])
    if obv is not None:
        result["obv"] = obv
        result["obv_sma20"] = ta.sma(obv, length=20)

    result.dropna(inplace=True)

    return result


def check_golden_cross_entry(
    df: pd.DataFrame,
    adx_threshold: int = 20,
    volume_multiplier: float = 1.0,
) -> bool:
    """골든크로스 매수 신호 — AND 조건.

    1. SMA5 > SMA20 크로스 (전일 SMA5 <= SMA20, 당일 SMA5 > SMA20)
    2. RSI >= 50
    3. ADX >= adx_threshold
    4. 거래량 >= 20일 평균 × volume_multiplier

    Args:
        df: 지표 계산 완료된 DataFrame (최소 2행 이상).
        adx_threshold: ADX 추세 강도 기준.
        volume_multiplier: 거래량 배수 기준.

    Returns:
        모든 조건 충족 시 True.
    """
    if len(df) < 2:
        return False
    latest = df.iloc[-1]
    prev = df.iloc[-2]

    cond_cross = (latest["sma5"] > latest["sma20"]) and (
        prev["sma5"] <= prev["sma20"]
    )
    cond_rsi = latest["rsi"] >= 50
    cond_adx = latest["adx"] >= adx_threshold
    cond_vol = latest["volume"] >= latest["volume_sma20"] * volume_multiplier

    return all([cond_cross, cond_rsi, cond_adx, cond_vol])


def check_golden_cross_exit(df: pd.DataFrame) -> bool:
    """골든크로스 매도 신호 — 데드크로스.

    SMA5 < SMA20 크로스 (전일 SMA5 >= SMA20, 당일 SMA5 < SMA20)

    Args:
        df: 지표 계산 완료된 DataFrame (최소 2행 이상).

    Returns:
        데드크로스 발생 시 True.
    """
    if len(df) < 2:
        return False
    latest = df.iloc[-1]
    prev = df.iloc[-2]
    return (latest["sma5"] < latest["sma20"]) and (prev["sma5"] >= prev["sma20"])


def check_entry_signal(
    df: pd.DataFrame,
    df_60m: pd.DataFrame | None = None,
    *,
    use_60m: bool = True,
    ma_trend: int = 20,
    rsi_entry_min: float = 40,
    rsi_entry_max: float = 65,
    volume_multiplier: float = 1.5,
) -> bool:
    """매수 신호 — AND 조건 (모두 충족 시 True).

    조건:
    1. 20일 이평선 위 종가 (추세)
    2. MACD 히스토그램 양전환 (전일 음 → 당일 양)
    3. RSI 40~65 (모멘텀 구간)
    4. 거래량 >= 20일 평균 × 1.5배
    5. 60분봉 SMA5 > SMA20 (단기 상향) — use_60m=True이고 df_60m 유효 시만

    Args:
        df: 일봉 지표 계산 완료된 DataFrame (최소 2행 이상).
        df_60m: 60분봉 DataFrame (sma5, sma20 컬럼 포함). None이면 조건 스킵.
        use_60m: 60분봉 조건 사용 여부 (기본 True).
        ma_trend: 추세 판단용 이평 기간 (sma{N} 컬럼 필요).
        rsi_entry_min: RSI 하한.
        rsi_entry_max: RSI 상한.
        volume_multiplier: 거래량 배수 기준.

    Returns:
        모든 조건 충족 시 True.
    """
    if len(df) < 2:
        return False

    latest = df.iloc[-1]
    prev = df.iloc[-2]

    sma_col = f"sma{ma_trend}"

    # 1. 종가 > 20일 이평선
    if latest["close"] <= latest[sma_col]:
        return False

    # 2. MACD 히스토그램 양전환 (전일 음 → 당일 양)
    if not (prev["macd_hist"] < 0 and latest["macd_hist"] > 0):
        return False

    # 3. RSI 40~65
    if not (rsi_entry_min <= latest["rsi"] <= rsi_entry_max):
        return False

    # 4. 거래량 >= 20일 평균 × volume_multiplier
    if latest["volume"] < latest["volume_sma20"] * volume_multiplier:
        return False

    # 5. 60분봉 SMA5 > SMA20 (옵션)
    if use_60m and df_60m is not None and len(df_60m) >= 1:
        latest_60m = df_60m.iloc[-1]
        if latest_60m["sma5"] <= latest_60m["sma20"]:
            return False

    return True


def check_exit_signal(
    position: Position,
    current_price: int,
    latest: pd.Series,
    *,
    max_hold_days: int = 15,
    target_return: float = 0.08,
) -> ExitReason | None:
    """매도 신호 — OR 조건 (하나라도 충족 시 반환).

    우선순위순:
    1. 손절가 이탈 → STOP_LOSS
    2. 트레일링스탑 발동 → TRAILING_STOP
    3. 목표가 도달 → TARGET_REACHED
    4. MACD 데드크로스 (수익 +2% 이상이고, macd_hist 음전환) → MACD_DEAD
    5. 최대 보유기간 초과 → MAX_HOLD

    Args:
        position: Position 객체.
        current_price: 현재가 (int).
        latest: 일봉 최신 행 (macd_hist 포함).
        max_hold_days: 최대 보유 기간(일).
        target_return: 목표 수익률 (미사용, target_price 기준 판단).

    Returns:
        ExitReason 또는 None (보유 유지).
    """
    # 1. 손절가 이탈
    if position.stop_price > 0 and current_price <= position.stop_price:
        return ExitReason.STOP_LOSS

    # 2. 트레일링스탑 발동 (stop_price에 통합됨, 하위 호환용 유지)
    trailing = getattr(position, "trailing_stop", 0)
    if trailing > 0 and current_price <= trailing:
        return ExitReason.TRAILING_STOP

    # 3. 목표가 도달
    if position.target_price > 0 and current_price >= position.target_price:
        return ExitReason.TARGET_REACHED

    # 4. MACD 데드크로스 (수익 +2% 이상이고, macd_hist 음전환)
    pnl_pct = (current_price - position.entry_price) / position.entry_price
    if pnl_pct >= 0.02:
        macd_hist = latest["macd_hist"] if "macd_hist" in latest.index else None
        if macd_hist is not None:
            if getattr(position, "prev_macd_hist", 0) > 0 and macd_hist < 0:
                return ExitReason.MACD_DEAD

    # 5. 최대 보유기간 초과
    if position.hold_days >= max_hold_days:
        return ExitReason.MAX_HOLD

    return None


def calculate_signal_score(
    df: pd.DataFrame,
    institutional_net: int = 0,
    foreign_net: int = 0,
) -> float:
    """신호 강도 점수 계산 (0.0 ~ 9.0).

    점수 항목 (각 최대 1.0):
    1. RSI 위치 점수: 50 근처가 가장 높음 (매수 초기 추세 진입)
    2. MACD 히스토그램 크기: 양수이고 클수록 높음
    3. 거래량 배수: volume / volume_sma20 비율
    4. ADX 추세 강도: 25 이상이면 점수 부여
    5. 볼린저밴드 위치: 중간~상단 범위
    6. OBV 추세 일치: 가격 상승 + OBV 상승 동시 충족
    7. 기관/외국인 수급: 순매수 시 가점
    8. 모멘텀 팩터: 60일(~3개월) 수익률 기반 추세 강도
    9. 업종 RS(상대강도): 종목 수익률 vs 시장 수익률 비교

    Args:
        df: 지표 계산 완료된 DataFrame.
        institutional_net: 기관 순매수 금액 (원). 양수=순매수, 음수=순매도.
        foreign_net: 외국인 순매수 금액 (원).

    Returns:
        0.0 ~ 9.0 범위의 점수.
    """
    if len(df) < 1:
        return 0.0

    latest = df.iloc[-1]
    score = 0.0

    # 1. RSI 위치 점수 (50 근처가 최고, 30~70 범위에서 점수 부여)
    rsi = latest.get("rsi", 50)
    rsi_score = max(0.0, 1.0 - abs(rsi - 52.5) / 22.5)
    score += rsi_score

    # 2. MACD 히스토그램 크기
    macd_hist = latest.get("macd_hist", 0)
    if macd_hist > 0:
        # 가격 대비 정규화 (close 기준)
        close = latest.get("close", 1)
        normalized = macd_hist / close * 100 if close > 0 else 0
        macd_score = min(1.0, normalized / 0.5)
        score += macd_score

    # 3. 거래량 배수
    volume = latest.get("volume", 0)
    volume_sma = latest.get("volume_sma20", 1)
    if volume_sma > 0:
        vol_ratio = volume / volume_sma
        vol_score = min(1.0, max(0.0, (vol_ratio - 1.0) / 2.0))
        score += vol_score

    # 4. ADX 추세 강도
    adx = latest.get("adx", 0)
    if adx >= 25:
        adx_score = min(1.0, (adx - 25) / 25)
        score += adx_score

    # 5. 볼린저밴드 위치 (중간~상단: 높은 점수)
    close = latest.get("close", 0)
    bb_lower = latest.get("bb_lower", 0)
    bb_upper = latest.get("bb_upper", 0)
    bb_range = bb_upper - bb_lower
    if bb_range > 0:
        bb_position = (close - bb_lower) / bb_range
        # 0.5~0.8 범위가 이상적 (중간~상단)
        if 0.4 <= bb_position <= 0.9:
            bb_score = 1.0 - abs(bb_position - 0.65) / 0.35
            score += max(0.0, bb_score)

    # 6. OBV 추세 일치 (가격 5일 상승 + OBV 5일 상승 = 추세 건강)
    if len(df) >= 6 and "obv" in df.columns and "obv_sma20" in df.columns:
        price_up = df.iloc[-1]["close"] > df.iloc[-6]["close"]
        obv_up = df.iloc[-1]["obv"] > df.iloc[-6]["obv"]
        obv_above_sma = df.iloc[-1]["obv"] > df.iloc[-1]["obv_sma20"]
        if price_up and obv_up:
            obv_score = 0.7
            if obv_above_sma:
                obv_score = 1.0
            score += obv_score

    # 7. 기관/외국인 수급 가점 (데이터 있을 때만)
    supply_score = 0.0
    if institutional_net > 0:
        supply_score += 0.5  # 기관 순매수
    if foreign_net > 0:
        supply_score += 0.5  # 외국인 순매수
    score += supply_score

    # 8. 모멘텀 팩터: 60일(~3개월) 수익률 기반
    if len(df) >= 60:
        momentum_return = (df["close"].iloc[-1] - df["close"].iloc[-60]) / df["close"].iloc[-60]
        if momentum_return > 0:
            # 양의 모멘텀: 0~30% → 0~1.0 점수
            momentum_score = min(1.0, momentum_return / 0.30)
            score += momentum_score
        # 음의 모멘텀은 감점하지 않음 (BB 전략에서 필요)

    # 9. 상대강도(RS): 20일 수익률이 양수이고 가속 중이면 가점
    if len(df) >= 20:
        ret_20d = (df["close"].iloc[-1] - df["close"].iloc[-20]) / df["close"].iloc[-20]
        ret_10d = (df["close"].iloc[-1] - df["close"].iloc[-10]) / df["close"].iloc[-10] if len(df) >= 10 else 0
        # 20일 수익률 양수 + 최근 10일이 더 강함 = 가속 추세
        if ret_20d > 0 and ret_10d > ret_20d / 2:
            rs_score = min(1.0, ret_20d / 0.15)  # 15% 수익이면 만점
            score += rs_score

    return round(min(9.0, max(0.0, score)), 2)


def get_institutional_net_buying(code: str, days: int = 5) -> tuple[int, int]:
    """기관/외국인 최근 N일 누적 순매수 금액 조회.

    pykrx를 사용하여 KRX 데이터를 조회한다.
    데이터 조회 실패 시 (0, 0) 반환 (graceful degradation).

    Args:
        code: 종목코드 (6자리).
        days: 조회 기간 (영업일 수).

    Returns:
        (기관_순매수, 외국인_순매수) 튜플 (원 단위).
    """
    try:
        from data.provider import get_provider
        return get_provider().get_institutional_net_buying(code, days)
    except Exception:
        return (0, 0)
