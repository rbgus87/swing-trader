"""진입 파이프라인 진단 스크립트.

스크리닝 → 진입 체크까지의 모든 관문을 시뮬레이션하고
각 단계별 통과/탈락 원인을 상세하게 출력합니다.

Usage:
    python scripts/diagnose_entry.py                # 오늘 기준
    python scripts/diagnose_entry.py --date 20260324  # 특정일 기준
    python scripts/diagnose_entry.py --code 005930    # 특정 종목만 진단
"""

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

# 프로젝트 루트를 sys.path에 추가
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd
from loguru import logger

# 로거 설정 (콘솔만)
logger.remove()
logger.add(sys.stderr, level="DEBUG", format="{message}")


def main():
    parser = argparse.ArgumentParser(description="진입 파이프라인 진단")
    parser.add_argument("--date", type=str, default=None, help="기준일 YYYYMMDD")
    parser.add_argument("--code", type=str, default=None, help="특정 종목 코드 (없으면 스크리닝 결과 사용)")
    parser.add_argument("--top", type=int, default=5, help="진단할 최대 종목 수")
    args = parser.parse_args()

    from src.utils.config import config

    date = args.date or datetime.now().strftime("%Y%m%d")
    start_date = (datetime.strptime(date, "%Y%m%d") - timedelta(days=200)).strftime("%Y%m%d")
    strategy_config = config.data.get("strategy", {})
    strategy_type = strategy_config.get("type", "golden_cross")
    is_adaptive = strategy_type == "adaptive"

    print("=" * 70)
    print(f"  진입 파이프라인 진단 — 기준일: {date}")
    print("=" * 70)

    # ──────────────────────────────────────────────────
    # 1단계: 시장 국면 판단
    # ──────────────────────────────────────────────────
    print("\n[1/5] 시장 국면 판단 (MarketRegime)")
    print("-" * 50)

    from src.strategy.market_regime import MarketRegime
    regime = MarketRegime()
    is_bullish = regime.check(date)

    print(f"  is_bullish    = {is_bullish}")
    print(f"  regime_type   = {regime.regime_type}")
    print(f"  KOSPI 종가    = {regime.kospi_close:,}")
    print(f"  200일선       = {regime.kospi_sma200:,.0f}")
    print(f"  ADX           = {regime.kospi_adx:.1f}")
    print(f"  VKOSPI        = {regime.vkospi:.1f}")
    print(f"  차단 사유     = {regime.block_reason or '없음'}")

    if not is_bullish:
        print("\n  ⚠️  시장 방어 모드 — 이 상태에서는 모든 매수가 차단됩니다!")
        print("  → on_price_update() line 306에서 return")

    # 국면별 전략 결정
    if is_adaptive:
        regime_map = strategy_config.get("regime_strategy", {})
        regime_key = regime.regime_type
        if regime_key == "bearish":
            print(f"\n  ⚠️  국면=bearish → 매수 차단 (is_bullish=False 게이트)")
            strategy_names = regime_map.get("sideways", ["bb_bounce"])
        else:
            strategy_names = regime_map.get(regime_key, ["bb_bounce"])
        if isinstance(strategy_names, str):
            strategy_names = [strategy_names]
        print(f"  활성 전략     = {strategy_names}")
    else:
        strategy_names = [strategy_type]
        print(f"  단일 전략     = {strategy_type}")

    from src.strategy import get_strategy
    strategies = [get_strategy(n, strategy_config) for n in strategy_names]

    # ──────────────────────────────────────────────────
    # 2단계: 스크리닝 (후보 종목 선별)
    # ──────────────────────────────────────────────────
    print(f"\n[2/5] 종목 스크리닝")
    print("-" * 50)

    if args.code:
        codes = [args.code]
        print(f"  지정 종목: {args.code}")
    else:
        from src.strategy.screener import Screener
        screener = Screener(config.data)
        screening_regime = regime.regime_type if is_adaptive else None
        if screening_regime == "bearish":
            screening_regime = None
        candidates = screener.run_daily_screening(date, screening_regime)
        codes = candidates[:args.top]
        print(f"  스크리닝 결과: {len(candidates)}종목 후보")
        if not candidates:
            print("\n  ❌ 스크리닝 통과 종목이 0개입니다!")
            print("  → 가능한 원인:")
            print("    1) Pre-screening 조건 (시총, 거래량비율, RSI, 이격도 등)이 모두 탈락")
            print("    2) 전략 스크리닝 (check_screening_entry)에서 모든 종목 탈락")
            print("    3) 유동성 필터 (거래대금 < 10억) 탈락")
            print(f"\n  진단 대상: 상위 {args.top}종목 = 없음")
            print("\n  💡 TIP: 특정 종목을 지정하여 진단하세요:")
            print(f"    python scripts/diagnose_entry.py --code 005930 --date {date}")
            return
        print(f"  진단 대상: 상위 {len(codes)}종목 = {codes}")

    # ──────────────────────────────────────────────────
    # 3~5단계: 종목별 진입 관문 진단
    # ──────────────────────────────────────────────────
    from data.provider import get_provider
    from src.strategy.signals import calculate_indicators, calculate_signal_score

    provider = get_provider()

    for i, code in enumerate(codes, 1):
        print(f"\n{'=' * 70}")
        print(f"  종목 [{i}/{len(codes)}]: {code}")
        print(f"{'=' * 70}")

        # ── OHLCV 로드 ──
        print(f"\n[3/5] OHLCV 데이터 (Gate 8)")
        print("-" * 50)
        df = provider.get_ohlcv_by_date_range(code, start_date, date)
        if df.empty:
            print(f"  ❌ OHLCV 로드 실패 — 데이터 없음")
            continue

        print(f"  데이터 행 수  = {len(df)} (최소 30 필요)")
        if len(df) < 30:
            print(f"  ❌ OHLCV 데이터 부족 (30행 미만) → 진입체크 탈락")
            continue
        print(f"  ✅ OHLCV 충분")

        # ── 지표 계산 ──
        print(f"\n[3/5] 지표 계산 (Gate 9)")
        print("-" * 50)
        df_ind = calculate_indicators(
            df,
            macd_fast=strategy_config.get("macd_fast", 12),
            macd_slow=strategy_config.get("macd_slow", 26),
            macd_signal=strategy_config.get("macd_signal", 9),
            rsi_period=strategy_config.get("rsi_period", 14),
            bb_period=strategy_config.get("bb_period", 20),
            bb_std=strategy_config.get("bb_std", 2.0),
        )
        if df_ind.empty:
            print(f"  ❌ 지표 계산 실패 → 진입체크 탈락")
            continue
        print(f"  ✅ 지표 계산 성공 ({len(df_ind)}행)")

        # 마지막 행 주요 지표 출력
        latest = df_ind.iloc[-1]
        print(f"\n  최신 지표 값:")
        for col in ["close", "sma5", "sma20", "sma60", "rsi", "adx",
                     "macd", "macd_signal", "macd_hist",
                     "bb_upper", "bb_lower", "bb_mid",
                     "volume", "volume_sma20", "obv", "obv_sma20",
                     "stoch_k", "stoch_d"]:
            val = latest.get(col)
            if val is not None and not pd.isna(val):
                if col in ("close", "sma5", "sma20", "sma60", "bb_upper", "bb_lower", "bb_mid"):
                    print(f"    {col:18s} = {val:>12,.0f}")
                elif col in ("volume", "volume_sma20"):
                    print(f"    {col:18s} = {val:>12,.0f}")
                elif col in ("obv", "obv_sma20"):
                    print(f"    {col:18s} = {val:>14,.0f}")
                else:
                    print(f"    {col:18s} = {val:>12.2f}")

        # ── Signal Score ──
        print(f"\n[4/5] 신호 점수 (Gate 10)")
        print("-" * 50)
        score = calculate_signal_score(df_ind)
        min_score_trend = config.get("strategy.min_signal_score", 1.5)
        min_score_mr = config.get("strategy.min_signal_score_mr", 0.5)

        print(f"  signal_score  = {score:.2f}")
        print(f"  임계값(trend) = {min_score_trend}")
        print(f"  임계값(MR)    = {min_score_mr}")

        # Score 구성요소 상세 분해
        _print_score_breakdown(df_ind)

        # ── 주봉 SMA20 필터 ──
        print(f"\n[4/5] 주봉 SMA20 필터 (Gate 11) — trend 전략만 적용")
        print("-" * 50)
        weekly_ok = _check_weekly_trend(df_ind)
        print(f"  주봉 추세 통과 = {weekly_ok}")
        if not weekly_ok:
            print(f"  ⚠️  주봉 종가 < 주봉 SMA20 → trend 전략 진입 차단")

        # ── 전략별 스크리닝 + 실시간 진입 체크 ──
        print(f"\n[5/5] 전략별 진입 조건 (Gate 12)")
        print("-" * 50)

        for strategy in strategies:
            is_mr = strategy.category == "mean_reversion"
            stype = "MR" if is_mr else "Trend"
            min_s = min_score_mr if is_mr else min_score_trend

            print(f"\n  [{strategy.name}] (카테고리: {stype})")

            # Score 게이트
            if score < min_s:
                print(f"    ❌ score {score:.2f} < {min_s} → 점수 미달 탈락")
                continue
            else:
                print(f"    ✅ score {score:.2f} >= {min_s}")

            # 주봉 게이트 (trend만)
            if not is_mr and not weekly_ok:
                print(f"    ❌ 주봉 SMA20 미달 → trend 전략 탈락")
                continue
            elif not is_mr:
                print(f"    ✅ 주봉 SMA20 통과")

            # 스크리닝 진입 체크
            try:
                screening_ok = strategy.check_screening_entry(df_ind)
                print(f"    check_screening_entry = {screening_ok}")
                if not screening_ok:
                    _diagnose_screening_fail(strategy, df_ind, strategy_config)
            except Exception as e:
                print(f"    ❌ check_screening_entry 예외: {e}")

            # 실시간 진입 체크
            try:
                realtime_ok = strategy.check_realtime_entry(df_ind, None)
                print(f"    check_realtime_entry  = {realtime_ok}  (60분봉=None)")
                if not realtime_ok:
                    _diagnose_realtime_fail(strategy, df_ind, strategy_config)
            except Exception as e:
                print(f"    ❌ check_realtime_entry 예외: {e}")

    # ── 종합 요약 ──
    print(f"\n{'=' * 70}")
    print("  종합 진단 요약")
    print(f"{'=' * 70}")
    print(f"  기준일: {date}")
    print(f"  시장국면: {regime.regime_type} (is_bullish={is_bullish})")
    print(f"  활성전략: {strategy_names}")
    print(f"  후보종목: {len(codes)}종목")
    print(f"\n  💡 다음 단계:")
    print(f"    - score가 낮으면: config.yaml의 strategy.min_signal_score 완화")
    print(f"    - 전략 조건이 안 맞으면: 해당 전략 파라미터 조정")
    print(f"    - 주봉 추세 실패면: 하락장이므로 정상 (MR 전략만 활성)")


def _print_score_breakdown(df: pd.DataFrame):
    """signal_score 구성요소 분해 출력."""
    latest = df.iloc[-1]
    rsi = latest.get("rsi", 50)
    macd_hist = latest.get("macd_hist", 0)
    close = latest.get("close", 1)
    volume = latest.get("volume", 0)
    vol_sma20 = latest.get("volume_sma20", 1)
    adx = latest.get("adx", 0)
    bb_upper = latest.get("bb_upper", close)
    bb_lower = latest.get("bb_lower", close)
    obv = latest.get("obv", 0)
    obv_sma20 = latest.get("obv_sma20", 0)

    print(f"\n  Score 구성요소 분해:")

    # 1. RSI 포지션
    rsi_score = max(0, 1.0 - abs(rsi - 52.5) / 22.5) if not pd.isna(rsi) else 0
    print(f"    RSI 포지션       = {rsi_score:.2f}  (RSI={rsi:.1f}, 최적=52.5)")

    # 2. MACD 히스토그램
    if close > 0 and not pd.isna(macd_hist):
        norm = abs(macd_hist) / close * 100
        macd_s = min(1.0, norm / 0.5) if macd_hist > 0 else 0
    else:
        macd_s = 0
    print(f"    MACD 히스토그램   = {macd_s:.2f}  (hist={macd_hist:.2f})")

    # 3. 거래량 배수
    vol_ratio = volume / vol_sma20 if vol_sma20 > 0 else 0
    vol_s = max(0, min(1.0, (vol_ratio - 1.0) / 2.0))
    print(f"    거래량 배수      = {vol_s:.2f}  (ratio={vol_ratio:.2f}x)")

    # 4. ADX 강도
    adx_s = max(0, min(1.0, (adx - 25) / 25)) if adx >= 25 else 0
    print(f"    ADX 강도         = {adx_s:.2f}  (ADX={adx:.1f})")

    # 5. BB 포지션
    bb_range = bb_upper - bb_lower if bb_upper > bb_lower else 1
    bb_pos = (close - bb_lower) / bb_range if bb_range > 0 else 0.5
    bb_s = max(0, 1.0 - abs(bb_pos - 0.65) / 0.25) if 0.4 <= bb_pos <= 0.9 else 0
    print(f"    BB 포지션        = {bb_s:.2f}  (pos={bb_pos:.2f}, 최적=0.65)")

    # 6. OBV 추세
    close_5d_up = df["close"].iloc[-1] > df["close"].iloc[-5] if len(df) >= 5 else False
    obv_5d_up = df["obv"].iloc[-1] > df["obv"].iloc[-5] if len(df) >= 5 and "obv" in df.columns else False
    obv_s = 0
    if close_5d_up and obv_5d_up:
        obv_s = 0.7
        if not pd.isna(obv) and not pd.isna(obv_sma20) and obv > obv_sma20:
            obv_s = 1.0
    print(f"    OBV 추세         = {obv_s:.2f}  (5일종가↑={close_5d_up}, OBV↑={obv_5d_up})")

    # 7. 기관/외국인 (오프라인에서는 0)
    print(f"    기관/외국인      = 0.00  (오프라인 — 데이터 없음)")

    # 8. 모멘텀
    if len(df) >= 60:
        mom_ret = (df["close"].iloc[-1] - df["close"].iloc[-60]) / df["close"].iloc[-60]
        mom_s = min(1.0, mom_ret / 0.30) if mom_ret > 0 else 0
    else:
        mom_ret = 0
        mom_s = 0
    print(f"    모멘텀(60일)     = {mom_s:.2f}  (수익률={mom_ret:.1%})")

    # 9. 상대 강도
    if len(df) >= 20:
        ret_20 = (df["close"].iloc[-1] - df["close"].iloc[-20]) / df["close"].iloc[-20]
        ret_10 = (df["close"].iloc[-1] - df["close"].iloc[-10]) / df["close"].iloc[-10] if len(df) >= 10 else 0
        rs_s = min(1.0, ret_20 / 0.10) if ret_20 > 0 and ret_10 > ret_20 * 0.5 else 0
    else:
        rs_s = 0
    print(f"    상대 강도        = {rs_s:.2f}")

    total = rsi_score + macd_s + vol_s + adx_s + bb_s + obv_s + 0 + mom_s + rs_s
    print(f"    ─────────────────────────")
    print(f"    합계 (기관외인 제외) = {total:.2f} / 9.00")


def _check_weekly_trend(df_daily: pd.DataFrame) -> bool:
    """주봉 SMA20 필터 — engine.py 동일 로직."""
    try:
        if len(df_daily) < 60:
            return True
        if not hasattr(df_daily.index, "to_period"):
            return True

        weekly = df_daily.resample("W").agg({
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
        }).dropna()

        if len(weekly) < 20:
            return True

        weekly_sma20 = weekly["close"].rolling(20).mean().iloc[-1]
        weekly_close = weekly["close"].iloc[-1]
        print(f"  주봉 종가     = {weekly_close:,.0f}")
        print(f"  주봉 SMA20    = {weekly_sma20:,.0f}")
        return weekly_close > weekly_sma20
    except Exception as e:
        print(f"  주봉 필터 예외: {e} → True 반환 (필터 통과)")
        return True


def _diagnose_screening_fail(strategy, df: pd.DataFrame, cfg: dict):
    """전략별 check_screening_entry 실패 원인 진단."""
    latest = df.iloc[-1]
    name = strategy.name
    adx_thr = cfg.get("adx_threshold", 20)
    vol_mult = cfg.get("volume_multiplier", 1.0)
    rsi = latest.get("rsi", 0)
    adx = latest.get("adx", 0)
    vol = latest.get("volume", 0)
    vol_sma = latest.get("volume_sma20", 1)
    close = latest.get("close", 0)
    sma5 = latest.get("sma5", 0)
    sma20 = latest.get("sma20", 0)
    macd = latest.get("macd", 0)
    macd_sig = latest.get("macd_signal", 0)
    bb_lower = latest.get("bb_lower", 0)
    bb_upper = latest.get("bb_upper", 0)

    vol_ratio = vol / vol_sma if vol_sma > 0 else 0

    if name == "golden_cross":
        print(f"      진단: SMA5>SMA20={sma5>sma20}, RSI={rsi:.1f}(≥50?), ADX={adx:.1f}(≥{adx_thr}?), 거래량={vol_ratio:.2f}x(≥{vol_mult}?)")
        if sma5 <= sma20:
            print(f"      → SMA5({sma5:,.0f}) <= SMA20({sma20:,.0f}): 골든크로스 미발생")
        if rsi < 50:
            print(f"      → RSI {rsi:.1f} < 50: 모멘텀 부족")

    elif name == "macd_pullback":
        prev_rsi = df.iloc[-2].get("rsi", 0) if len(df) >= 2 else 0
        rsi_pullback = cfg.get("rsi_pullback", 45)
        print(f"      진단: MACD>Sig={macd>macd_sig}, 가격>SMA20={close>sma20}, ADX={adx:.1f}, 전일RSI={prev_rsi:.1f}(<{rsi_pullback}?), 금일RSI={rsi:.1f}(>전일?)")
        if prev_rsi >= rsi_pullback:
            print(f"      → 전일 RSI {prev_rsi:.1f} >= {rsi_pullback}: 눌림 미발생")
        if macd <= macd_sig:
            print(f"      → MACD({macd:.2f}) <= Signal({macd_sig:.2f}): 상승 추세 아님")

    elif name == "volume_breakout":
        vol_mult_bo = cfg.get("vol_breakout_multiplier", 1.5)
        lookback = cfg.get("vol_lookback", 20)
        obv = latest.get("obv", 0)
        obv_sma = latest.get("obv_sma20", 0)
        if len(df) > lookback:
            max_vol = df["volume"].iloc[-(lookback+1):-1].max()
        else:
            max_vol = vol_sma
        print(f"      진단: 거래량={vol:,.0f} > {lookback}일max×{vol_mult_bo}={max_vol*vol_mult_bo:,.0f}?, 가격>SMA20={close>sma20}, OBV>SMA={obv>obv_sma}")
        if vol < max_vol * vol_mult_bo:
            print(f"      → 거래량 돌파 미발생 (현재={vol:,.0f}, 기준={max_vol*vol_mult_bo:,.0f})")

    elif name == "bb_bounce":
        bb_range = bb_upper - bb_lower if bb_upper > bb_lower else 1
        touch_pct = cfg.get("bb_touch_pct", 0.15)
        rsi_oversold = cfg.get("rsi_oversold", 40)
        dist = (close - bb_lower) / bb_range if bb_range > 0 else 1.0
        print(f"      진단: BB거리={dist:.3f}(≤{touch_pct}?), RSI={rsi:.1f}(≤{rsi_oversold}?), 거래량={vol_ratio:.2f}x")
        if dist > touch_pct:
            print(f"      → BB 하단과 거리 {dist:.3f} > {touch_pct}: 터치 미발생")
        if rsi > rsi_oversold:
            print(f"      → RSI {rsi:.1f} > {rsi_oversold}: 과매도 아님")


def _diagnose_realtime_fail(strategy, df: pd.DataFrame, cfg: dict):
    """전략별 check_realtime_entry 실패 원인 진단."""
    # 대부분 screening과 유사하지만 약간 다른 조건
    latest = df.iloc[-1]
    name = strategy.name

    if name == "golden_cross":
        close = latest.get("close", 0)
        sma20 = latest.get("sma20", 0)
        sma5 = latest.get("sma5", 0)
        rsi = latest.get("rsi", 0)
        rsi_min = cfg.get("rsi_entry_min", 35)
        rsi_max = cfg.get("rsi_entry_max", 65)
        adx = latest.get("adx", 0)
        adx_thr = cfg.get("adx_threshold", 15)

        print(f"      RT진단: 가격({close:,.0f})>SMA20({sma20:,.0f})={close>sma20}, "
              f"SMA5>SMA20={sma5>sma20}, RSI={rsi:.1f}({rsi_min}-{rsi_max}?), "
              f"ADX={adx:.1f}(≥{adx_thr}?)")
        if close <= sma20:
            print(f"      → 가격이 SMA20 아래: 추세 미확인")
        if not (rsi_min <= rsi <= rsi_max):
            print(f"      → RSI {rsi:.1f} 범위 밖 ({rsi_min}-{rsi_max})")

    elif name == "bb_bounce":
        if len(df) >= 2:
            prev = df.iloc[-2]
            curr = df.iloc[-1]
            bb_lower_prev = prev.get("bb_lower", 0)
            close_prev = prev.get("close", 0)
            close_curr = curr.get("close", 0)
            rsi_prev = prev.get("rsi", 50)
            rsi_curr = curr.get("rsi", 50)
            rsi_oversold = cfg.get("rsi_oversold", 40)
            rsi_recovery = 45  # 기본값

            touch_prev = close_prev <= bb_lower_prev * 1.02
            bounce = close_curr > close_prev
            print(f"      RT진단: 전일BB터치={touch_prev}(종가{close_prev:,.0f}≤BB*1.02={bb_lower_prev*1.02:,.0f}?), "
                  f"반등={bounce}(금일{close_curr:,.0f}>전일{close_prev:,.0f}?), "
                  f"전일RSI={rsi_prev:.1f}(≤{rsi_oversold}?), 금일RSI={rsi_curr:.1f}(≥{rsi_recovery}?)")
            if not touch_prev:
                print(f"      → 전일 BB 하단 터치 안됨")
            if not bounce:
                print(f"      → 금일 반등 미발생")


if __name__ == "__main__":
    main()
