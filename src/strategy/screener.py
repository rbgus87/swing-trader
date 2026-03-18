"""종목 스크리닝 — 장 시작 전(08:30) 실행.

pykrx를 사용하여 전종목 OHLCV를 수집하고,
유동성 필터 + 기술적 지표 기반 매수 후보를 선별.
"""

from datetime import datetime, timedelta

import pandas as pd
from loguru import logger
from pykrx import stock

from data.column_mapper import OHLCV_MAP, map_columns
from src.strategy.signals import (
    calculate_indicators,
    calculate_signal_score,
    check_entry_signal,
    check_golden_cross_entry,
)


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
        self.strategy_type = self.strategy_config.get("type", "golden_cross")
        self._ds = datastore

    def get_all_codes(self, market: str | None = None) -> list[str]:
        """전종목 코드 수집.

        Args:
            market: "KOSPI", "KOSDAQ", 또는 None (universe 설정 기반).

        Returns:
            종목 코드 리스트.
        """
        if market:
            return stock.get_market_ticker_list(market=market)

        if self.universe == "kospi":
            return stock.get_market_ticker_list(market="KOSPI")
        elif self.universe == "kosdaq":
            return stock.get_market_ticker_list(market="KOSDAQ")
        else:
            # kospi_kosdaq
            kospi = stock.get_market_ticker_list(market="KOSPI")
            kosdaq = stock.get_market_ticker_list(market="KOSDAQ")
            return kospi + kosdaq

    def run_daily_screening(self, date: str | None = None) -> list[str]:
        """당일 매수 후보 종목 스크리닝.

        Steps:
        1. 전종목 코드 수집 (pykrx)
        2. 유동성 필터 (거래대금, 주가 범위)
        3. 지표 계산
        4. 매수 신호 체크
        5. 점수 기반 상위 N종목 반환

        Args:
            date: 기준 날짜 ("YYYYMMDD"). None이면 오늘.

        Returns:
            매수 후보 종목 코드 리스트 (점수 내림차순).
        """
        if date is None:
            date = datetime.now().strftime("%Y%m%d")

        start_date = (
            datetime.strptime(date, "%Y%m%d") - timedelta(days=200)
        ).strftime("%Y%m%d")

        # 고정 종목 리스트가 있으면 watchlist 우선 사용
        if self.watchlist:
            codes = self.watchlist
            logger.info(f"스크리닝 시작: watchlist {len(codes)}종목, 기준일 {date}")
        else:
            codes = self.get_all_codes()
            logger.info(f"스크리닝 시작: 전체 {len(codes)}종목, 기준일 {date}")

        candidates: list[tuple[str, float]] = []

        for code in codes:
            try:
                # 일봉 OHLCV 수집 (캐시 우선)
                df = self._get_ohlcv(code, start_date, date)
                if df.empty or len(df) < 130:
                    continue

                # 유동성 필터
                if not self._apply_liquidity_filter(code, df):
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
                    continue

                # 매수 신호 체크 (전략 타입에 따라 분기)
                has_signal = False
                if self.strategy_type == "golden_cross":
                    has_signal = check_golden_cross_entry(
                        df_with_ind,
                        adx_threshold=self.strategy_config.get("adx_threshold", 20),
                        volume_multiplier=self.strategy_config.get(
                            "volume_multiplier", 1.0
                        ),
                    )
                else:
                    # MACD-RSI 전략 (레거시)
                    df_60m = pd.DataFrame(
                        {
                            "sma5": [df_with_ind.iloc[-1]["sma5"]],
                            "sma20": [df_with_ind.iloc[-1]["sma20"]],
                        }
                    )
                    has_signal = check_entry_signal(
                        df_with_ind,
                        df_60m,
                        rsi_entry_min=self.strategy_config.get("rsi_entry_min", 40),
                        rsi_entry_max=self.strategy_config.get("rsi_entry_max", 65),
                        volume_multiplier=self.strategy_config.get(
                            "volume_multiplier", 1.5
                        ),
                    )

                if has_signal:
                    score = calculate_signal_score(df_with_ind)
                    candidates.append((code, score))

            except Exception as e:
                logger.warning(f"종목 {code} 스크리닝 실패: {e}")
                continue

        # 점수 내림차순 정렬, 상위 N종목
        candidates.sort(key=lambda x: x[1], reverse=True)
        result = [code for code, _ in candidates[: self.top_n]]

        logger.info(f"스크리닝 완료: {len(candidates)}종목 신호 발생, 상위 {len(result)}종목 선정")
        return result

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

        # pykrx에서 조회
        df = stock.get_market_ohlcv_by_date(start_date, end_date, code)
        if df.empty:
            return df

        df = map_columns(df, OHLCV_MAP)

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
        if "amount" in df.columns:
            recent_amount = df["amount"].tail(5).mean()
            if recent_amount < self.min_daily_amount:
                return False
        else:
            # amount 컬럼이 없으면 close * volume 으로 근사
            recent_amount = (df["close"] * df["volume"]).tail(5).mean()
            if recent_amount < self.min_daily_amount:
                return False

        return True
