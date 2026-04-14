"""종목 스크리닝 — 장 시작 전(08:30) 실행.

pykrx를 사용하여 전종목 OHLCV를 수집하고,
2단계 필터링(pre-screening → screening)으로 매수 후보를 선별.

흐름:
  1차 Pre-Screening: 국면별 조건식으로 후보군 축소 (2,500 → 30~50종목)
  2차 Screening: 전략별 매수 신호 + 점수 기반 최종 선정 (30~50 → 10~20종목)
"""

from datetime import datetime, timedelta

import pandas as pd
from loguru import logger

from data.column_mapper import OHLCV_MAP, map_columns
from data.provider import get_provider
# Phase 1: 전략 레이어 무력화 — Phase 3에서 4-레이어 재구축
# from src.strategy.base_strategy import get_strategy
from src.strategy.signals import (  # signals는 인프라급 유지
    calculate_indicators,
    calculate_signal_score,
)


def get_strategy(*args, **kwargs):  # noqa: ARG001
    raise NotImplementedError("Phase 1: strategy layer disabled")


class Screener:
    """종목 스크리닝 — 장 시작 전(08:30) 실행."""

    def __init__(self, config: dict, datastore=None):
        """스크리너 초기화.

        Args:
            config: 전체 설정 딕셔너리.
            datastore: DataStore 인스턴스 (OHLCV 캐싱용, 선택).
        """
        screening = config.get("screening", {})
        self.min_daily_amount = screening.get("min_daily_amount", 5_000_000_000)
        self.min_price = screening.get("min_price", 1000)
        self.max_price = screening.get("max_price", 500000)
        self.top_n = screening.get("top_n", 30)
        self.universe = config.get("trading", {}).get("universe", "kospi_kosdaq")
        self.strategy_config = config.get("strategy", {})
        self.watchlist = config.get("watchlist", [])
        self.strategy_type = self.strategy_config.get("type", "adaptive")
        self._is_adaptive = self.strategy_type == "adaptive"

        # adaptive 모드: 기본 전략 리스트로 초기화 (국면별 전환은 run_daily_screening에서)
        if self._is_adaptive:
            regime_map = self.strategy_config.get("regime_strategy", {})
            default_names = regime_map.get("sideways", "disparity_reversion")
            if isinstance(default_names, str):
                default_names = [default_names]
            self._strategies = [
                get_strategy(n, self.strategy_config) for n in default_names
            ]
            self._strategy = self._strategies[0]  # 하위 호환
        else:
            self._strategy = get_strategy(self.strategy_type, self.strategy_config)
            self._strategies = [self._strategy]
        self._ds = datastore

        # Pre-screening 설정
        pre = config.get("pre_screening", {})
        self._pre_min_market_cap = pre.get("min_market_cap", 100_000_000_000)
        self._pre_volume_ratio = pre.get("volume_ratio_threshold", 1.5)

        # 조건식 1: 눌림목 (trending)
        pullback = pre.get("pullback", {})
        self._pb_week52_min = pullback.get("week52_high_min", -0.15)
        self._pb_week52_max = pullback.get("week52_high_max", 0.0)
        self._pb_rsi_min = pullback.get("rsi_min", 45)
        self._pb_rsi_max = pullback.get("rsi_max", 65)
        self._pb_disp20_min = pullback.get("disparity_sma20_min", 100)
        self._pb_disp20_max = pullback.get("disparity_sma20_max", 200)
        self._pb_disp60_min = pullback.get("disparity_sma60_min", 100)
        self._pb_disp60_max = pullback.get("disparity_sma60_max", 200)

        # 조건식 2: 변동성 수축 (sideways)
        squeeze = pre.get("squeeze", {})
        self._sq_range_period = squeeze.get("price_range_period", 20)
        self._sq_range_max = squeeze.get("price_range_max", 0.15)
        self._sq_min_price = squeeze.get("min_price", 3000)
        self._sq_max_price = squeeze.get("max_price", 200000)
        # 평균회귀 전략용 거래량 기준 (기본 0.8x — BB 하단 터치 시 거래량 급증 불필요)
        self._sq_volume_ratio = squeeze.get("volume_ratio_threshold", 0.8)

    def get_all_codes(self, market: str | None = None) -> list[str]:
        """전종목 코드 수집.

        Args:
            market: "KOSPI", "KOSDAQ", 또는 None (universe 설정 기반).

        Returns:
            종목 코드 리스트.
        """
        provider = get_provider()
        if market:
            m = market.lower()
            return provider.get_ticker_list(market=m)

        return provider.get_ticker_list(market=self.universe)

    # ── Light Pre-Filter (0차 필터) ──

    def _light_pre_filter(self, date: str) -> tuple[list[str], dict[str, int]]:
        """전종목 데이터를 1콜로 가져와 시총+가격으로 빠르게 걸러냄.

        KRX API get_all_stocks_today 사용 → 1콜로 전종목 종가/시총/거래량 확보.
        시가총액, 주가범위 기본 필터를 OHLCV 개별 조회 전에 적용.

        Returns:
            (필터 통과 종목코드 리스트, {코드: 시총} dict).
        """
        provider = get_provider()
        market_caps: dict[str, int] = {}
        passed_codes: list[str] = []

        try:
            for market in self._get_markets():
                df = provider.get_all_stocks_today(date, market.lower())
                if df.empty:
                    continue

                for _, row in df.iterrows():
                    code = str(row.get("code", ""))
                    if not code:
                        continue

                    cap = int(row.get("market_cap", 0))
                    close = int(row.get("close", 0))

                    # 시가총액 필터
                    if cap < self._pre_min_market_cap:
                        continue

                    # 주가 범위 필터 (양 조건식의 합집합 범위)
                    price_min = min(self.min_price, self._sq_min_price)
                    price_max = max(self.max_price, self._sq_max_price)
                    if close < price_min or close > price_max:
                        continue

                    market_caps[code] = cap
                    passed_codes.append(code)

            logger.info(
                f"[Light Pre-Filter] 시총+가격 필터: "
                f"{len(passed_codes)}종목 통과"
            )
        except Exception as e:
            logger.warning(f"Light pre-filter 실패 (전종목 대상으로 진행): {e}")

        return passed_codes, market_caps

    def _get_markets(self) -> list[str]:
        """universe 설정에 따른 시장 리스트."""
        if self.universe == "kospi":
            return ["KOSPI"]
        elif self.universe == "kosdaq":
            return ["KOSDAQ"]
        return ["KOSPI", "KOSDAQ"]

    def pre_screen_pullback(self, codes: list[str], date: str,
                            market_caps: dict[str, int]) -> list[str]:
        """조건식 1: 52주 고가 눌림목 필터 (trending 국면용).

        조건:
        - 52주 최고가 대비 -15% ~ 0%
        - RSI(14) 45 ~ 65
        - 이격도(종가/SMA20) 100% ~ 200%
        - 이격도(종가/SMA60) 100% ~ 200%
        - 시가총액 1000억+
        - 거래량비율 20일 대비 150%+

        Args:
            codes: 전종목 코드 리스트.
            date: 기준일 (YYYYMMDD).
            market_caps: {코드: 시가총액} 딕셔너리.

        Returns:
            조건 통과 종목 코드 리스트.
        """
        passed = []
        start_date = (
            datetime.strptime(date, "%Y%m%d") - timedelta(days=370)
        ).strftime("%Y%m%d")

        for code in codes:
            try:
                # 시가총액 필터 (데이터 있을 때만 적용)
                if market_caps:
                    cap = market_caps.get(code, 0)
                    if cap < self._pre_min_market_cap:
                        continue

                # OHLCV 조회 (52주 = ~250거래일, 여유분 포함 370일)
                df = self._get_ohlcv(code, start_date, date)
                if df.empty or len(df) < 60:
                    continue

                close = df["close"].iloc[-1]
                high_col = df["high"]
                volume = df["volume"]

                # 52주 최고가 대비 비율
                week52_high = high_col.tail(250).max()
                if week52_high == 0:
                    continue
                ratio_from_high = (close - week52_high) / week52_high
                if not (self._pb_week52_min <= ratio_from_high <= self._pb_week52_max):
                    continue

                # RSI(14) 계산
                delta = df["close"].diff()
                gain = delta.clip(lower=0).rolling(14).mean()
                loss = (-delta.clip(upper=0)).rolling(14).mean()
                rs = gain / loss.replace(0, float("nan"))
                rsi = 100 - (100 / (1 + rs))
                current_rsi = rsi.iloc[-1]
                if pd.isna(current_rsi):
                    continue
                if not (self._pb_rsi_min <= current_rsi <= self._pb_rsi_max):
                    continue

                # 이격도: 종가 / SMA20 × 100
                sma20 = df["close"].rolling(20).mean().iloc[-1]
                if pd.isna(sma20) or sma20 == 0:
                    continue
                disp20 = (close / sma20) * 100
                if not (self._pb_disp20_min <= disp20 <= self._pb_disp20_max):
                    continue

                # 이격도: 종가 / SMA60 × 100
                sma60 = df["close"].rolling(60).mean().iloc[-1]
                if pd.isna(sma60) or sma60 == 0:
                    continue
                disp60 = (close / sma60) * 100
                if not (self._pb_disp60_min <= disp60 <= self._pb_disp60_max):
                    continue

                # 거래량비율: 당일 거래량 / 20일 평균 거래량
                vol_sma20 = volume.tail(20).mean()
                if vol_sma20 == 0:
                    continue
                vol_ratio = volume.iloc[-1] / vol_sma20
                if vol_ratio < self._pre_volume_ratio:
                    continue

                passed.append(code)

            except Exception as e:
                logger.debug(f"pullback pre-screen 실패 ({code}): {e}")
                continue

        logger.info(f"[Pre-Screen] 눌림목(pullback): {len(passed)}/{len(codes)}종목 통과")
        return passed

    def pre_screen_squeeze(self, codes: list[str], date: str,
                           market_caps: dict[str, int]) -> list[str]:
        """조건식 2: 변동성 수축 필터 (sideways 국면용).

        조건:
        - 20일간 최고최저폭 0% ~ 15%
        - 거래량비율 20일 대비 150%+
        - 주가 3,000 ~ 200,000원
        - 시가총액 1000억+

        Args:
            codes: 전종목 코드 리스트.
            date: 기준일 (YYYYMMDD).
            market_caps: {코드: 시가총액} 딕셔너리.

        Returns:
            조건 통과 종목 코드 리스트.
        """
        passed = []
        start_date = (
            datetime.strptime(date, "%Y%m%d") - timedelta(days=200)
        ).strftime("%Y%m%d")
        period = self._sq_range_period

        for code in codes:
            try:
                # 시가총액 필터 (데이터 있을 때만 적용)
                if market_caps:
                    cap = market_caps.get(code, 0)
                    if cap < self._pre_min_market_cap:
                        continue

                # OHLCV 조회
                df = self._get_ohlcv(code, start_date, date)
                if df.empty or len(df) < period:
                    continue

                close = df["close"].iloc[-1]
                volume = df["volume"]

                # 주가 범위 필터
                if not (self._sq_min_price <= close <= self._sq_max_price):
                    continue

                # 20일간 최고최저폭: (고가 max - 저가 min) / 저가 min
                recent = df.tail(period)
                high_max = recent["high"].max()
                low_min = recent["low"].min()
                if low_min == 0:
                    continue
                price_range = (high_max - low_min) / low_min
                if price_range > self._sq_range_max:
                    continue

                # 거래량비율 (squeeze는 완화된 기준 적용)
                vol_sma20 = volume.tail(20).mean()
                if vol_sma20 == 0:
                    continue
                vol_ratio = volume.iloc[-1] / vol_sma20
                if vol_ratio < self._sq_volume_ratio:
                    continue

                passed.append(code)

            except Exception as e:
                logger.debug(f"squeeze pre-screen 실패 ({code}): {e}")
                continue

        logger.info(f"[Pre-Screen] 수축돌파(squeeze): {len(passed)}/{len(codes)}종목 통과")
        return passed

    # ── Screening (2차 필터) ──

    def run_daily_screening(self, date: str | None = None,
                            regime: str | None = None) -> list[str]:
        """당일 매수 후보 종목 스크리닝 (2단계).

        watchlist가 있으면 기존 모드(하위 호환).
        watchlist가 비어있으면: pre-screening → screening 2단계.

        Args:
            date: 기준 날짜 ("YYYYMMDD"). None이면 오늘.
            regime: 시장 국면 ("trending" / "sideways" / None).
                    None이면 두 조건식의 합집합 사용.

        Returns:
            매수 후보 종목 코드 리스트 (점수 내림차순).
        """
        import time as _time

        if date is None:
            date = datetime.now().strftime("%Y%m%d")

        screening_start = _time.monotonic()

        start_date = (
            datetime.strptime(date, "%Y%m%d") - timedelta(days=200)
        ).strftime("%Y%m%d")

        # 고정 종목 리스트가 있으면 watchlist 우선 사용 (하위 호환)
        if self.watchlist:
            codes = self.watchlist
            logger.info(f"[1/3] Light Filter: watchlist {len(codes)}종목, 기준일 {date}")
        else:
            # 0차: Light Pre-Filter (1콜로 시총+가격 사전 필터)
            step_start = _time.monotonic()
            light_codes, market_caps = self._light_pre_filter(date)

            if light_codes:
                all_codes = light_codes
            else:
                all_codes = self.get_all_codes()

            elapsed = _time.monotonic() - step_start
            logger.info(
                f"[1/3] Light Filter 완료 ({elapsed:.0f}초) — "
                f"{len(all_codes)}종목, 국면 {regime or 'all'}, 기준일 {date}"
            )

            # 1차: Pre-Screening (기술적 조건 필터)
            step_start = _time.monotonic()
            codes = self._run_pre_screening(all_codes, date, regime, market_caps)
            elapsed = _time.monotonic() - step_start

            if not codes:
                logger.warning(
                    f"[2/3] Pre-Screen 완료 ({elapsed:.0f}초) — "
                    f"0종목 통과, light filter 종목으로 fallback"
                )
                codes = all_codes
            else:
                logger.info(
                    f"[2/3] Pre-Screen 완료 ({elapsed:.0f}초) — {len(codes)}종목 통과"
                )

        # adaptive 모드: 국면에 맞는 전략 리스트로 전환
        if self._is_adaptive and regime:
            regime_map = self.strategy_config.get("regime_strategy", {})
            target_names = regime_map.get(regime)
            if target_names:
                if isinstance(target_names, str):
                    target_names = [target_names]
                current_names = sorted(s.name for s in self._strategies)
                if current_names != sorted(target_names):
                    self._strategies = [
                        get_strategy(n, self.strategy_config)
                        for n in target_names
                    ]
                    self._strategy = self._strategies[0]
                    names_str = ", ".join(target_names)
                    logger.info(f"  전략 전환: [{names_str}] (국면: {regime})")

        # 2차: 전략 기반 매수 신호 + 점수 (멀티전략 OR 로직)
        step_start = _time.monotonic()
        strategy_names = ", ".join(s.name for s in self._strategies)
        logger.info(f"[3/3] 전략 스크리닝: {len(codes)}종목 ([{strategy_names}])")
        candidates: list[tuple[str, float]] = []
        drop_data = 0      # OHLCV 부족
        drop_liquidity = 0  # 유동성 탈락
        drop_indicator = 0  # 지표 계산 실패
        drop_signal = 0     # 전략 신호 없음

        for code in codes:
            try:
                # 일봉 OHLCV 수집 (캐시 우선)
                df = self._get_ohlcv(code, start_date, date)
                if df.empty or len(df) < 130:
                    drop_data += 1
                    continue

                # 유동성 필터
                if not self._apply_liquidity_filter(code, df):
                    if drop_liquidity == 0:
                        # 첫 탈락 시 원인 로깅
                        has_amount = "amount" in df.columns and df["amount"].tail(5).sum() > 0
                        if has_amount:
                            amt = df["amount"].tail(5).mean()
                        else:
                            amt = (df["close"] * df["volume"]).tail(5).mean()
                        logger.info(
                            f"  유동성 탈락 샘플 ({code}): "
                            f"거래대금 {amt:,.0f}원, "
                            f"기준 {self.min_daily_amount:,.0f}원, "
                            f"amount컬럼={'있음' if has_amount else '없음/0'}"
                        )
                    drop_liquidity += 1
                    continue

                # 지표 계산
                df_with_ind = calculate_indicators(
                    df,
                    macd_fast=self.strategy_config.get("macd_fast", 12),
                    macd_slow=self.strategy_config.get("macd_slow", 26),
                    macd_signal=self.strategy_config.get("macd_signal", 9),
                    rsi_period=self.strategy_config.get("rsi_period", 14),
                    bb_period=self.strategy_config.get("bb_period", 20),
                    bb_std=self.strategy_config.get("bb_std", 2.0),
                )

                if df_with_ind.empty:
                    drop_indicator += 1
                    continue

                # 매수 신호 체크 — 멀티전략 OR (하나라도 신호 시 통과)
                has_signal = any(
                    s.check_screening_entry(df_with_ind)
                    for s in self._strategies
                )

                if has_signal:
                    score = calculate_signal_score(df_with_ind)
                    candidates.append((code, score))
                else:
                    drop_signal += 1

            except Exception as e:
                logger.warning(f"종목 {code} 스크리닝 실패: {e}")
                continue

        elapsed = _time.monotonic() - step_start

        # 탈락 통계 로깅
        total_dropped = drop_data + drop_liquidity + drop_indicator + drop_signal
        if total_dropped > 0:
            logger.info(
                f"[3/3] 탈락 상세: 데이터부족 {drop_data}, "
                f"유동성 {drop_liquidity}, 지표실패 {drop_indicator}, "
                f"신호없음 {drop_signal}"
            )

        # 수급 가산점 (trending 국면에서만)
        if regime == "trending" and candidates:
            flow_bonus_enabled = self.strategy_config.get("flow_filter_enabled", True)
            if flow_bonus_enabled:
                from src.strategy.signals import get_institutional_net_buying
                scored = []
                for code, score in candidates:
                    try:
                        inst_net, foreign_net = get_institutional_net_buying(code, days=5)
                        flow_bonus = 0.0
                        if foreign_net > 0:
                            flow_bonus += 1.5
                        if inst_net > 0:
                            flow_bonus += 1.0
                        scored.append((code, score + flow_bonus))
                    except Exception:
                        scored.append((code, score))
                candidates = scored
                logger.info(f"  수급 가산점 적용: {sum(1 for _, s in candidates if s > 0)}종목")

        # 점수 내림차순 정렬, 상위 N종목
        candidates.sort(key=lambda x: x[1], reverse=True)
        result = [code for code, _ in candidates[: self.top_n]]

        total_elapsed = _time.monotonic() - screening_start
        minutes = int(total_elapsed // 60)
        seconds = int(total_elapsed % 60)
        time_str = f"{minutes}분 {seconds}초" if minutes > 0 else f"{seconds}초"
        logger.info(
            f"[3/3] 전략 스크리닝 완료 ({elapsed:.0f}초) — "
            f"{len(candidates)}종목 신호, 상위 {len(result)}종목 선정"
        )
        logger.info(f"스크리닝 완료 (총 {time_str})")
        return result

    def _run_pre_screening(self, codes: list[str], date: str,
                           regime: str | None,
                           market_caps: dict[str, int]) -> list[str]:
        """국면에 따라 적절한 pre-screening 조건식 실행.

        Args:
            codes: 전종목 코드 리스트.
            date: 기준일.
            regime: "trending" / "sideways" / None.
            market_caps: 시가총액 딕셔너리.

        Returns:
            pre-screening 통과 종목 리스트.
        """
        if regime == "trending":
            return self.pre_screen_pullback(codes, date, market_caps)
        elif regime == "sideways":
            return self.pre_screen_squeeze(codes, date, market_caps)
        else:
            # 국면 미지정: OHLCV를 한번만 조회하고 두 조건식에 공유
            ohlcv_cache = self._batch_load_ohlcv(codes, date)
            pullback = set(
                self._pre_screen_pullback_with_cache(codes, date, market_caps, ohlcv_cache)
            )
            squeeze = set(
                self._pre_screen_squeeze_with_cache(codes, date, market_caps, ohlcv_cache)
            )
            combined = list(pullback | squeeze)
            logger.info(
                f"[Pre-Screen] 합집합: {len(combined)}종목 "
                f"(눌림목 {len(pullback)} + 수축 {len(squeeze)}, "
                f"중복 {len(pullback & squeeze)})"
            )
            return combined

    def _batch_load_ohlcv(self, codes: list[str], date: str) -> dict[str, pd.DataFrame]:
        """여러 종목의 OHLCV를 한번에 로드하여 캐시 dict 반환.

        pullback(370일)이 더 긴 기간을 필요로 하므로 370일로 통일.
        """
        start_date = (
            datetime.strptime(date, "%Y%m%d") - timedelta(days=370)
        ).strftime("%Y%m%d")

        cache: dict[str, pd.DataFrame] = {}
        total = len(codes)
        # GUI 프로그레스 바 시작
        logger.log("PROGRESS", f"OHLCV 로드|0|{total}")

        # 콘솔: tqdm 프로그레스 바
        try:
            from tqdm import tqdm
            code_iter = tqdm(codes, desc="Batch OHLCV", unit="종목", leave=False, ncols=60)
        except ImportError:
            code_iter = codes

        # GUI 프로그레스 바: 5% 간격
        progress_interval = max(1, total // 20)
        for i, code in enumerate(code_iter, 1):
            try:
                df = self._get_ohlcv(code, start_date, date)
                if not df.empty:
                    cache[code] = df
            except Exception:
                pass
            if i % progress_interval == 0 or i == total:
                logger.log("PROGRESS", f"OHLCV 로드|{i}|{total}")
        logger.info(f"[Batch OHLCV] {len(cache)}/{total}종목 로드 완료")
        return cache

    def _pre_screen_pullback_with_cache(
        self, codes: list[str], date: str,
        market_caps: dict[str, int],
        ohlcv_cache: dict[str, pd.DataFrame],
    ) -> list[str]:
        """pullback pre-screen (OHLCV 캐시 사용 버전)."""
        passed = []
        for code in codes:
            try:
                if market_caps:
                    if market_caps.get(code, 0) < self._pre_min_market_cap:
                        continue

                df = ohlcv_cache.get(code)
                if df is None or len(df) < 60:
                    continue

                close = df["close"].iloc[-1]

                week52_high = df["high"].tail(250).max()
                if week52_high == 0:
                    continue
                ratio = (close - week52_high) / week52_high
                if not (self._pb_week52_min <= ratio <= self._pb_week52_max):
                    continue

                delta = df["close"].diff()
                gain = delta.clip(lower=0).rolling(14).mean()
                loss = (-delta.clip(upper=0)).rolling(14).mean()
                rs = gain / loss.replace(0, float("nan"))
                rsi = 100 - (100 / (1 + rs))
                cur_rsi = rsi.iloc[-1]
                if pd.isna(cur_rsi) or not (self._pb_rsi_min <= cur_rsi <= self._pb_rsi_max):
                    continue

                sma20 = df["close"].rolling(20).mean().iloc[-1]
                if pd.isna(sma20) or sma20 == 0:
                    continue
                disp20 = (close / sma20) * 100
                if not (self._pb_disp20_min <= disp20 <= self._pb_disp20_max):
                    continue

                sma60 = df["close"].rolling(60).mean().iloc[-1]
                if pd.isna(sma60) or sma60 == 0:
                    continue
                disp60 = (close / sma60) * 100
                if not (self._pb_disp60_min <= disp60 <= self._pb_disp60_max):
                    continue

                vol_sma20 = df["volume"].tail(20).mean()
                if vol_sma20 == 0:
                    continue
                if df["volume"].iloc[-1] / vol_sma20 < self._pre_volume_ratio:
                    continue

                passed.append(code)
            except Exception:
                continue

        logger.info(f"[Pre-Screen] 눌림목(pullback): {len(passed)}/{len(codes)}종목 통과")
        return passed

    def _pre_screen_squeeze_with_cache(
        self, codes: list[str], date: str,
        market_caps: dict[str, int],
        ohlcv_cache: dict[str, pd.DataFrame],
    ) -> list[str]:
        """squeeze pre-screen (OHLCV 캐시 사용 버전)."""
        passed = []
        period = self._sq_range_period
        for code in codes:
            try:
                if market_caps:
                    if market_caps.get(code, 0) < self._pre_min_market_cap:
                        continue

                df = ohlcv_cache.get(code)
                if df is None or len(df) < period:
                    continue

                close = df["close"].iloc[-1]
                if not (self._sq_min_price <= close <= self._sq_max_price):
                    continue

                recent = df.tail(period)
                high_max = recent["high"].max()
                low_min = recent["low"].min()
                if low_min == 0:
                    continue
                if (high_max - low_min) / low_min > self._sq_range_max:
                    continue

                vol_sma20 = df["volume"].tail(20).mean()
                if vol_sma20 == 0:
                    continue
                if df["volume"].iloc[-1] / vol_sma20 < self._sq_volume_ratio:
                    continue

                passed.append(code)
            except Exception:
                continue

        logger.info(f"[Pre-Screen] 수축돌파(squeeze): {len(passed)}/{len(codes)}종목 통과")
        return passed

    def preload_ohlcv(self, codes: list[str], date: str | None = None) -> None:
        """watchlist 종목의 OHLCV 캐시를 갱신 (데이터 프리로드).

        장전에 pykrx에서 최신 일봉을 가져와 DataStore에 캐시한다.
        진입 판단 시 캐시된 데이터를 사용하므로 장중 지연 없음.

        Args:
            codes: 종목 코드 리스트.
            date: 기준 날짜 ("YYYYMMDD"). None이면 오늘.
        """
        if date is None:
            date = datetime.now().strftime("%Y%m%d")

        start_date = (
            datetime.strptime(date, "%Y%m%d") - timedelta(days=200)
        ).strftime("%Y%m%d")

        logger.info(f"OHLCV 프리로드 시작: {len(codes)}종목, 기준일 {date}")

        success = 0
        for code in codes:
            try:
                df = self._get_ohlcv(code, start_date, date)
                if not df.empty:
                    success += 1
            except Exception as e:
                logger.warning(f"OHLCV 프리로드 실패 ({code}): {e}")

        logger.info(f"OHLCV 프리로드 완료: {success}/{len(codes)}종목 성공")

    def _get_ohlcv(self, code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """OHLCV 조회 — DataStore 캐시 우선, 없으면 pykrx 조회 후 캐시 저장.

        Args:
            code: 종목 코드.
            start_date: 시작일 "YYYYMMDD".
            end_date: 종료일 "YYYYMMDD".

        Returns:
            영문 컬럼명의 OHLCV DataFrame.
        """
        # 캐시에서 조회 시도
        if self._ds is not None:
            start_fmt = f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:]}"
            end_fmt = f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:]}"
            cached = self._ds.get_cached_ohlcv(code, start_fmt, end_fmt)
            if len(cached) >= 130:
                df = pd.DataFrame(cached)
                df = df.rename(columns={
                    "open": "open", "high": "high", "low": "low",
                    "close": "close", "volume": "volume", "amount": "amount",
                })
                return df

        # DataProvider 경유 조회 (KRX API → pykrx 폴백)
        df = get_provider().get_ohlcv_by_date_range(code, start_date, end_date)
        if df.empty:
            return df

        # 캐시에 저장
        if self._ds is not None and not df.empty:
            records = []
            for idx, row in df.iterrows():
                date_str = idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx)
                records.append({
                    "date": date_str,
                    "open": int(row.get("open", 0)),
                    "high": int(row.get("high", 0)),
                    "low": int(row.get("low", 0)),
                    "close": int(row.get("close", 0)),
                    "volume": int(row.get("volume", 0)),
                    "amount": int(row.get("amount", 0)) if "amount" in row else 0,
                })
            try:
                self._ds.cache_ohlcv(code, records)
            except Exception as e:
                logger.warning(f"OHLCV 캐시 저장 실패 ({code}): {e}")

        return df

    def _apply_liquidity_filter(self, code: str, df: pd.DataFrame) -> bool:
        """유동성 필터 적용.

        최근 종가와 최근 5일 평균 거래대금으로 필터링.

        Args:
            code: 종목 코드.
            df: OHLCV DataFrame (영문 컬럼명).

        Returns:
            필터 통과 시 True.
        """
        if df.empty:
            return False

        latest = df.iloc[-1]

        # 주가 범위 필터
        price = latest["close"]
        if price < self.min_price or price > self.max_price:
            return False

        # 거래대금 필터 (최근 5일 평균)
        if "amount" in df.columns and df["amount"].tail(5).sum() > 0:
            recent_amount = df["amount"].tail(5).mean()
        else:
            # amount 컬럼이 없거나 0이면 close * volume 으로 근사
            recent_amount = (df["close"] * df["volume"]).tail(5).mean()

        if recent_amount < self.min_daily_amount:
            logger.debug(
                f"유동성 탈락 ({code}): 거래대금 {recent_amount:,.0f} "
                f"< 기준 {self.min_daily_amount:,.0f}"
            )
            return False

        return True
