"""PollingMixin — REST 폴링 루프."""
from __future__ import annotations
import asyncio
from datetime import datetime
from loguru import logger
from src.models import Tick
from src.utils.config import config
from src.utils.market_calendar import is_trading_day, now_kst


class PollingMixin:
    """REST 폴링 관련 메서드."""

    @staticmethod
    def _check_weekly_trend(df_daily: "pd.DataFrame") -> bool:
        """주봉 SMA20 필터 — 일봉 데이터를 주봉으로 리샘플링하여 추세 확인.

        Args:
            df_daily: 지표 계산 완료된 일봉 DataFrame (최소 100행 권장).

        Returns:
            주간 종가 > 주봉 SMA20이면 True.
        """
        import pandas as pd
        try:
            if len(df_daily) < 60:
                return True  # 데이터 부족 시 필터 통과

            # 날짜 인덱스가 없으면 리샘플링 불가 → 필터 통과
            if not hasattr(df_daily.index, 'to_period'):
                return True

            weekly = df_daily.resample("W").agg({
                "open": "first",
                "high": "max",
                "low": "min",
                "close": "last",
                "volume": "sum",
            }).dropna()

            if len(weekly) < 20:
                return True  # 주봉 데이터 부족 시 필터 통과

            weekly_sma20 = weekly["close"].rolling(20).mean().iloc[-1]
            weekly_close = weekly["close"].iloc[-1]
            return weekly_close > weekly_sma20
        except Exception:
            return True  # 오류 시 필터 통과 (보수적)

    async def _refresh_minute_ohlcv(self):
        """60분봉 캐시 갱신 (장중 매시 정각+1분).

        watchlist/후보 종목의 60분봉을 키움 REST API로 조회하여
        메모리 캐시에 저장. 진입 판단의 2층(타이밍) 데이터로 사용.
        """
        import pandas as pd
        from data.column_mapper import OHLCV_MAP, map_columns

        codes = self._candidates or config.get("watchlist", [])
        if not codes:
            return

        tick_range = config.get("strategy.timeframe_entry", 60)
        success = 0

        for code in codes:
            for attempt in range(2):  # 최대 1회 재시도
                try:
                    raw = await self._kiwoom.get_minute_ohlcv(
                        code, tick_range=tick_range, count=30
                    )
                    if raw and isinstance(raw, list) and len(raw) > 0:
                        df = pd.DataFrame(raw)
                        if not df.empty:
                            df = map_columns(df, OHLCV_MAP)
                            self._minute_ohlcv_cache[code] = df
                            success += 1
                    else:
                        logger.debug(f"60분봉 빈 응답 ({code}): raw={type(raw).__name__}, len={len(raw) if isinstance(raw, list) else 'N/A'}")
                    break  # 성공 시 재시도 루프 탈출
                except Exception as e:
                    if "429" in str(e) and attempt == 0:
                        await asyncio.sleep(2)  # 429 시 2초 대기 후 재시도
                        continue
                    logger.warning(f"60분봉 조회 실패 ({code}): {e}")
                    break
            # API rate limit 준수: 종목 간 1초 대기
            await asyncio.sleep(1.0)

        logger.info(f"60분봉 갱신 완료: {success}/{len(codes)}종목")

    async def _ensure_connection(self):
        """REST API 연결 확인 및 재연결."""
        if not self._kiwoom._connected:
            try:
                await self._kiwoom.connect(use_websocket=False)
                logger.info("REST API 연결 성공")
                self._telegram.send("REST API 연결 확인 완료")
            except Exception as e:
                logger.error(f"REST API 연결 실패: {e}")
                self._telegram.send_system_error(str(e), "ensure_connection")
        else:
            logger.info("REST API 이미 연결됨")

    async def _start_polling(self):
        """REST polling 시작 (09:25 스케줄)."""
        from src.utils.market_calendar import is_trading_day, now_kst
        if not is_trading_day(now_kst().date()):
            logger.info("비거래일 — REST polling 생략")
            return

        if self._polling_task and not self._polling_task.done():
            logger.debug("polling 이미 실행 중")
            return

        # REST 인증 확인
        if not self._kiwoom._connected:
            try:
                await self._kiwoom.connect(use_websocket=False)
            except Exception as e:
                logger.error(f"REST 인증 실패: {e}")
                self._telegram.send_system_error(str(e), "polling_start")
                return

        self._polling_task = asyncio.create_task(self._polling_loop())
        try:
            self._health.status.polling_active = True
        except AttributeError:
            pass
        logger.info(
            f"REST polling 시작 (간격: {self._polling_interval}초)"
        )

    async def _stop_polling(self):
        """REST polling 중지 (15:35 스케줄)."""
        if self._polling_task and not self._polling_task.done():
            self._polling_task.cancel()
            try:
                await self._polling_task
            except asyncio.CancelledError:
                pass
            self._polling_task = None
            try:
                self._health.status.polling_active = False
            except AttributeError:
                pass
            logger.info("REST polling 중지")
        else:
            logger.debug("polling 이미 중지 상태")

    async def _polling_loop(self):
        """REST polling 메인 루프 — 보유 종목 우선, 만석 시 후보 제외."""
        logger.info("polling 루프 진입")
        try:
            while self._running:
                cycle_start = asyncio.get_event_loop().time()

                # 폴링 루프 생존 확인 — 보유 유무 무관하게 매 사이클 호출
                try:
                    self._health.beat()
                except AttributeError:
                    pass

                # polling 대상 결정: 보유 종목 + (여유 있을 때만) 후보 종목
                open_positions = self._get_cached_positions()
                held_codes = {p["code"] for p in open_positions} if open_positions else set()
                max_pos = config.get("trading.max_positions", 8)
                positions_full = len(held_codes) >= max_pos

                poll_codes: set[str] = set(held_codes)
                if not positions_full and self._candidates:
                    poll_codes.update(self._candidates)
                # 감시 목록 상위 N종목 폴링 (0포지션 시에도 heartbeat 유지)
                _wl = getattr(self, "_scored_watchlist", [])
                if _wl:
                    _poll_top_n = int(
                        config.get("watchlist.scorer.poll_top_n", 20)
                    )
                    poll_codes.update(item.ticker for item in _wl[:_poll_top_n])

                # 틱 수신 중단 경고 조건 갱신
                try:
                    self._health.status.has_positions = len(held_codes) > 0
                except AttributeError:
                    pass

                if not poll_codes:
                    logger.debug("polling 대상 없음 — 다음 주기 대기")
                    await asyncio.sleep(self._polling_interval)
                    continue

                # 종목별 현재가 REST 조회 (rate limit: 5 TR/sec → 0.2초 간격)
                success_count = 0
                fail_count = 0
                for code in poll_codes:
                    if not self._running:
                        break
                    try:
                        data = await self._kiwoom.get_current_price(code)
                        # 키움 REST 응답: cur_prc (부호 포함 문자열, 예: "+4685", "-61200")
                        raw_price = data.get("cur_prc") or data.get("cur_pr") or data.get("stk_pr") or "0"
                        price = abs(int(str(raw_price).replace(",", "").replace("+", "")))
                        raw_vol = data.get("trde_qty") or data.get("tr_vol") or data.get("acc_vol") or "0"
                        volume = abs(int(str(raw_vol).replace(",", "")))
                        if price > 0:
                            tick = Tick(
                                code=code,
                                price=price,
                                volume=volume,
                                timestamp=datetime.now(),
                            )
                            # 종목명 캐시 (첫 틱 로그용)
                            stk_nm = data.get("stk_nm", "")
                            if stk_nm:
                                self._poll_stock_names[code] = stk_nm
                            await self.on_price_update(tick)
                            success_count += 1
                        else:
                            fail_count += 1
                            logger.debug(f"가격 0원 ({code}): data={data}")
                    except Exception as e:
                        fail_count += 1
                        logger.warning(f"현재가 조회 실패 ({code}): {e}")
                    # rate limit 준수: 5 TR/sec → 0.2초 간격
                    await asyncio.sleep(0.2)

                # poll_failure 기록 (beat()는 루프 최상단에서 처리)
                try:
                    if fail_count > 0 and success_count == 0:
                        self._health.record_poll_failure()
                except AttributeError:
                    pass

                # 실패 시에만 로깅
                if fail_count > 0:
                    mode_label = f"보유{len(held_codes)}종목" if positions_full else f"보유{len(held_codes)}+후보{len(poll_codes)-len(held_codes)}"
                    logger.info(
                        f"polling 주기: {success_count}/{len(poll_codes)}종목 "
                        f"가격 수신 ({mode_label}, 실패: {fail_count})"
                    )

                # 주기 맞춤 대기 (polling_interval - 소요시간)
                elapsed = asyncio.get_event_loop().time() - cycle_start
                wait = max(0, self._polling_interval - elapsed)
                if wait > 0:
                    await asyncio.sleep(wait)
        except asyncio.CancelledError:
            logger.info("polling 루프 취소됨")
            raise
        except Exception as e:
            logger.error(f"polling 루프 오류: {e}")
            self._telegram.send_system_error(str(e), "polling_loop")
