"""TradingEngine — 전체 모듈 조율자.

장전 스크리닝 -> 장중 실시간 파이프라인 -> 체결 이벤트 -> 장마감 리포트.
asyncio 기반으로 동작한다.
"""

import asyncio
import time
from datetime import datetime
from typing import Literal

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from loguru import logger

from src.broker.kiwoom_api import KiwoomAPI
from src.broker.order_manager import OrderManager
from src.broker.realtime_data import RealtimeDataManager
from src.broker.tr_codes import ORDER_BUY, ORDER_SELL, PRICE_LIMIT, PRICE_MARKET
from src.datastore import DataStore
from src.models import ExitReason, Position, Signal, Tick, TradeRecord
from src.notification.telegram_bot import TelegramBot
from src.risk.position_sizer import PositionSizer
from src.risk.risk_manager import RiskManager
from src.risk.stop_manager import StopManager
from src.strategy import get_strategy
from src.strategy.market_regime import MarketRegime
from src.strategy.screener import Screener
from src.utils.config import config
from src.utils.market_calendar import is_market_open


class TradingEngine:
    """매매 시스템 핵심 조율자."""

    def __init__(self, mode: Literal["paper", "live"] | None = None):
        self.mode = mode or config.mode
        self._running = False

        # 모듈 초기화
        self._ds = DataStore()
        self._ds.connect()
        self._ds.create_tables()

        # 키움 API (REST + WebSocket) — paper/live 모두 실전 서버 사용
        # paper 모드는 주문만 시뮬레이션하고, 시세는 실서버에서 수신
        base_url = config.get("broker.base_url", "https://api.kiwoom.com")
        ws_url = config.get(
            "broker.ws_url",
            "wss://api.kiwoom.com:10000/api/dostk/websocket",
        )
        appkey = config.get_env("KIWOOM_APPKEY", "")
        secretkey = config.get_env("KIWOOM_SECRETKEY", "")

        self._kiwoom = KiwoomAPI(base_url, ws_url, appkey, secretkey)
        account = config.get_env("KIWOOM_ACCOUNT", "")
        self._order_mgr = OrderManager(self._kiwoom, account)
        self._realtime = RealtimeDataManager(self._kiwoom)

        self._screener = Screener(config.data, datastore=self._ds)
        self._risk_mgr = RiskManager(self._ds, config.data)
        self._sizer = PositionSizer()
        self._stop_mgr = StopManager(
            stop_atr_mult=config.get("risk.stop_atr_multiplier", 2.5),
            max_stop_pct=config.get("risk.max_stop_pct", 0.10),
            trailing_atr_mult=config.get("risk.trailing_atr_multiplier", 2.5),
            trailing_activate_pct=config.get("risk.trailing_activate_pct", 0.07),
        )
        self._telegram = TelegramBot()

        # 전략 인스턴스 (멀티전략 지원)
        self._strategy_config = config.data.get("strategy", {})
        self._strategy_type = self._strategy_config.get("type", "golden_cross")
        self._is_adaptive = self._strategy_type == "adaptive"

        if self._is_adaptive:
            # adaptive 모드: 기본 전략 리스트로 초기화, 장전 스크리닝에서 전환
            regime_map = self._strategy_config.get("regime_strategy", {})
            default_names = regime_map.get("sideways", "bb_bounce")
            if isinstance(default_names, str):
                default_names = [default_names]
            self._strategies = [
                get_strategy(n, self._strategy_config) for n in default_names
            ]
            self._strategy = self._strategies[0]  # 하위 호환
            names = ", ".join(s.name for s in self._strategies)
            logger.info(f"전략 로드: adaptive (기본 [{names}])")
        else:
            self._strategy = get_strategy(self._strategy_type, self._strategy_config)
            self._strategies = [self._strategy]
            logger.info(f"전략 로드: {self._strategy_type}")

        # 시장 국면 판단기
        self._market_regime = MarketRegime()

        # 상태
        self._candidates: list[str] = []  # 당일 매수 후보
        self._reconnect_count = 0
        self._max_reconnect = 5
        self._initial_capital = config.get(
            "trading.initial_capital",
            config.get("backtest.initial_capital", 1_000_000),
        )
        self._positions_cache: list[dict] | None = None  # 포지션 메모리 캐시
        self._sell_retry_counts: dict[int, int] = {}  # 매도 재시도 카운터
        self._latest_prices: dict[str, int] = {}  # 종목별 최신 가격 캐시
        self._atr_cache: dict[str, float] = {}  # 종목별 ATR 캐시
        self._last_entry_check: dict[str, float] = {}  # 종목별 마지막 진입 체크 시각 (throttle)
        self._minute_ohlcv_cache: dict[str, "pd.DataFrame"] = {}  # 종목별 60분봉 캐시
        # partial_sold는 DB positions.partial_sold 컬럼으로 관리
        self._daily_trades_cache: list[dict] | None = None  # 당일 매매 기록 캐시

        # MDD 초기 자본 설정
        self._risk_mgr.set_initial_capital(float(self._initial_capital))

        # 스케줄러
        self._scheduler = AsyncIOScheduler()

        # 콜백 등록
        self._kiwoom.on_tick_callback = self.on_price_update
        self._kiwoom.on_chejan_callback = self.on_chejan

    async def start(self):
        """메인루프 시작."""
        logger.info(f"TradingEngine 시작 (mode={self.mode})")
        self._running = True

        # 스케줄러 등록
        screening_time = config.get("schedule.screening_time", "08:30")
        report_time = config.get("schedule.daily_report_time", "16:00")
        ws_connect_time = config.get("schedule.ws_connect_time", "08:50")
        ws_disconnect_time = config.get("schedule.ws_disconnect_time", "18:10")

        h, m = screening_time.split(":")
        self._scheduler.add_job(
            self._pre_market_screening, "cron", hour=int(h), minute=int(m)
        )

        h, m = report_time.split(":")
        self._scheduler.add_job(
            self._daily_report, "cron", hour=int(h), minute=int(m)
        )

        h, m = ws_connect_time.split(":")
        self._scheduler.add_job(
            self._ws_connect, "cron", hour=int(h), minute=int(m)
        )

        h, m = ws_disconnect_time.split(":")
        self._scheduler.add_job(
            self._ws_disconnect, "cron", hour=int(h), minute=int(m)
        )

        # 미체결 주문 정리 (15:35 — 장 마감 5분 후)
        self._scheduler.add_job(
            self._post_market_cleanup, "cron", hour=15, minute=35
        )

        # 일일 리셋 (09:00)
        self._scheduler.add_job(self._daily_reset, "cron", hour=9, minute=0)

        # 60분봉 갱신 (장중 매시 정각: 10, 11, 12, 13, 14, 15시)
        self._scheduler.add_job(
            self._refresh_minute_ohlcv, "cron",
            hour="10-15", minute=1,  # 정각 1분 후 (캔들 확정 대기)
        )

        self._scheduler.start()

        # watchlist → 후보 등록 (WebSocket 연결 전에 준비)
        watchlist = config.get("watchlist", [])
        if watchlist:
            self._candidates = list(watchlist)
            logger.info(f"watchlist {len(watchlist)}종목 → 전체 후보 등록")
        else:
            logger.info("watchlist 미설정 — 스크리닝 기반 모드")

        # 장 시간대이면 즉시 WebSocket 연결 (프로그램이 장중에 시작된 경우)
        from src.utils.market_calendar import is_ws_active_hours
        if is_ws_active_hours():
            await self._ws_connect()

        # 서비스 시작 알림
        self._telegram.send_startup(self.mode)

    async def stop(self):
        """시스템 중지."""
        try:
            self._telegram.send_shutdown(self.mode)
        except Exception as e:
            logger.warning(f"종료 알림 전송 실패 (무시): {e}")
        self._running = False
        self._scheduler.shutdown(wait=False)
        await self._kiwoom.disconnect()
        self._ds.close()
        logger.info("TradingEngine 중지")

    def halt(self):
        """매매 중단 (일일 한도 초과 등)."""
        self._risk_mgr.halt()
        self._telegram.send_halt_alert(self._risk_mgr.daily_pnl_pct)
        logger.warning("매매 중단 - 일일 한도 초과")

    # ── 장전 ──

    async def _pre_market_screening(self, _retry: int = 0):
        """장전 스크리닝 (08:30).

        watchlist 있음: OHLCV 캐시 갱신 + ATR 사전계산 (데이터 프리로드)
        watchlist 없음: 전체 시장 스캔 → 후보 선정 → WebSocket 구독
        """
        max_retries = 3
        watchlist = config.get("watchlist", [])

        try:
            today = datetime.now().strftime("%Y%m%d")

            # 시장 국면 판단 (장전 갱신)
            is_bullish = self._market_regime.check(today)
            if not is_bullish:
                logger.info("시장 방어 모드 — 스크리닝 수행하지만 장중 매수는 차단됨")
                reason = self._market_regime.block_reason or "조건 미충족"
                self._telegram.send(f"시장 방어 모드: {reason}")

            # adaptive 모드: 국면별 전략 전환
            if self._is_adaptive and is_bullish:
                self._switch_strategy_by_regime()

            if watchlist:
                # 모드 A: watchlist 전체가 후보, OHLCV 캐시만 갱신
                await asyncio.to_thread(
                    self._screener.preload_ohlcv, watchlist, today
                )
                self._candidates = list(watchlist)
                # ATR 캐시 리프레시
                self._atr_cache.clear()
                for code in watchlist:
                    self._get_atr(code)
                logger.info(
                    f"장전 데이터 프리로드 완료: {len(watchlist)}종목 OHLCV + ATR 갱신"
                )
            else:
                # 모드 B: pre-screening + screening 2단계 선별
                regime = self._market_regime.regime_type if is_bullish else None
                self._candidates = await asyncio.to_thread(
                    self._screener.run_daily_screening, today, regime
                )
                # 보유 포지션도 WebSocket 구독 보장
                open_positions = self._ds.get_open_positions()
                subscribe_codes = set(self._candidates)
                if open_positions:
                    subscribe_codes.update(p["code"] for p in open_positions)
                if subscribe_codes:
                    # WebSocket이 닫혀있으면 재연결 후 구독
                    if not (self._kiwoom._ws and self._kiwoom._ws.connected):
                        try:
                            logger.info("WebSocket 재연결 (스크리닝 구독용)")
                            await self._kiwoom.connect(use_websocket=True)
                        except Exception as e:
                            logger.warning(f"WebSocket 재연결 실패: {e}")
                    try:
                        await self._realtime.subscribe_list(list(subscribe_codes))
                    except Exception as ws_err:
                        logger.warning(f"WebSocket 구독 실패 (스크리닝 결과는 유지): {ws_err}")
                logger.info(
                    f"스크리닝 완료: {len(self._candidates)}종목 후보 선정"
                    f" (국면: {regime or 'bearish'})"
                )

            self._telegram.send(
                f"📊 당일 매수 후보: {len(self._candidates)}종목"
            )
        except Exception as e:
            logger.error(f"스크리닝 실패 (시도 {_retry + 1}/{max_retries + 1}): {e}")
            if _retry < max_retries:
                delay = 60 * (_retry + 1)
                logger.info(f"스크리닝 재시도 예약: {delay}초 후")
                await asyncio.sleep(delay)
                await self._pre_market_screening(_retry=_retry + 1)
            else:
                self._telegram.send_system_error(
                    str(e), "screener.run_daily_screening",
                    f"최대 재시도({max_retries}회) 초과"
                )

    # ── 장중 실시간 ──

    async def on_price_update(self, tick: Tick):
        """실시간 시세 수신 콜백."""
        if not self._running or self._risk_mgr.is_halted:
            return
        if not is_market_open():
            return

        # 첫 틱 수신 로깅 (종목별 1회)
        if tick.code not in self._latest_prices:
            logger.debug(f"첫 틱 수신: {tick.code} = {tick.price:,}원")

        # 최신 가격 캐시 갱신
        self._latest_prices[tick.code] = tick.price

        # 1. 보유 종목 손절/트레일링/목표가 체크
        await self._check_exit_conditions(tick)

        # 2. 일일 손익 업데이트 및 한도 체크
        self._update_daily_pnl(tick)

        # 3. 후보 종목 진입 조건 체크
        # 시장 국면 게이트: 방어 모드이면 매수 차단
        if not self._market_regime.is_bullish:
            return
        # 시간대 진입 제한: 장 초반 변동성/장 마감 갭 리스크 회피
        now_hm = datetime.now().strftime("%H:%M")
        entry_start = config.get("trading.entry_start_time", "09:30")
        entry_end = config.get("trading.entry_end_time", "14:30")
        if not (entry_start <= now_hm <= entry_end):
            return
        # 쓰로틀링: 같은 종목은 30초 간격으로만 진입 판단 (지표 계산 부하 방지)
        if tick.code in self._candidates:
            now = time.monotonic()
            last_check = self._last_entry_check.get(tick.code, 0)
            if now - last_check < 30:
                return
            held_codes = {p["code"] for p in self._get_cached_positions()}
            if tick.code not in held_codes:
                self._last_entry_check[tick.code] = now
                await self._check_entry_conditions(tick)

    async def _check_exit_conditions(self, tick: Tick):
        """보유 종목 청산 조건 체크 — signals.check_exit_signal 통합."""
        positions = self._get_cached_positions()
        for pos_dict in positions:
            if pos_dict["code"] != tick.code:
                continue
            if pos_dict.get("status") == "selling":
                continue  # 매도 주문 중인 포지션은 스킵

            pos = self._dict_to_position(pos_dict)

            # 트레일링스탑 업데이트 (OHLCV 캐시 기반 ATR)
            atr = self._get_atr(tick.code, pos.entry_price)
            new_stop = self._stop_mgr.update_trailing_stop(pos, tick.price, atr)
            if new_stop != pos.stop_price:
                self._ds.update_position(pos.id, stop_price=new_stop)
                pos.stop_price = new_stop

            # OHLCV 기반 종합 청산 판단
            exit_reason = self._evaluate_exit(pos, tick.price)
            if exit_reason:
                await self._execute_sell(pos, tick.price, exit_reason)

    def _evaluate_exit(self, pos: Position, current_price: int) -> ExitReason | None:
        """종합 청산 판단 — 손절/목표가/트레일링/MACD 데드크로스/최대보유."""
        from src.strategy.signals import calculate_indicators
        import pandas as pd

        max_hold = config.get("trading.max_hold_days", 15)

        # 1. 손절가 이탈
        if self._stop_mgr.is_stopped(pos, current_price):
            return ExitReason.STOP_LOSS

        # 2a. 부분 매도: 목표가의 N% 도달 시 (아직 부분 매도 안 한 포지션만)
        partial_enabled = config.get("strategy.partial_sell_enabled", False)
        if (
            partial_enabled
            and not getattr(pos, "partial_sold", False)
            and pos.target_price > 0
        ):
            partial_pct = config.get("strategy.partial_target_pct", 0.5)
            target_return_val = (pos.target_price - pos.entry_price) / pos.entry_price
            partial_trigger = pos.entry_price * (1 + target_return_val * partial_pct)
            if current_price >= partial_trigger:
                return ExitReason.PARTIAL_TARGET

        # 2b. 목표가 도달 (전량 매도)
        if pos.target_price > 0 and current_price >= pos.target_price:
            return ExitReason.TARGET_REACHED

        # 3. MACD 데드크로스 (수익 +2% 이상이고, macd_hist 음전환)
        pnl_pct = (current_price - pos.entry_price) / pos.entry_price
        if pnl_pct >= 0.02:
            try:
                from datetime import timedelta
                end = datetime.now().strftime("%Y-%m-%d")
                start = (datetime.now() - timedelta(days=40)).strftime("%Y-%m-%d")
                ohlcv = self._ds.get_cached_ohlcv(pos.code, start, end)
                if ohlcv and len(ohlcv) >= 30:
                    df = pd.DataFrame(ohlcv)
                    df = calculate_indicators(df)
                    if len(df) >= 2:
                        prev_hist = df.iloc[-2].get("macd_hist", 0)
                        curr_hist = df.iloc[-1].get("macd_hist", 0)
                        if prev_hist > 0 and curr_hist < 0:
                            return ExitReason.MACD_DEAD
            except Exception:
                pass

        # 4. 최대 보유기간 초과
        if pos.hold_days >= max_hold:
            return ExitReason.MAX_HOLD

        return None

    async def _check_entry_conditions(self, tick: Tick):
        """후보 종목 진입 조건 체크 — 멀티전략 + 카테고리별 게이트."""
        from src.strategy.signals import (
            calculate_indicators,
            calculate_signal_score,
            get_institutional_net_buying,
        )
        import pandas as pd

        score = 0.0
        matched_strategy = None
        try:
            from datetime import timedelta

            # 일봉 데이터 준비
            end = datetime.now().strftime("%Y-%m-%d")
            start = (datetime.now() - timedelta(days=130)).strftime("%Y-%m-%d")
            ohlcv = self._ds.get_cached_ohlcv(tick.code, start, end)
            if not ohlcv or len(ohlcv) < 30:
                return

            df_daily = pd.DataFrame(ohlcv)
            df_daily = calculate_indicators(df_daily)
            if df_daily.empty:
                return

            # 기관/외국인 수급 데이터 (graceful: 실패 시 0)
            inst_net, foreign_net = get_institutional_net_buying(tick.code)

            # 신호 강도 점수 (OBV + 수급 포함)
            score = calculate_signal_score(
                df_daily,
                institutional_net=inst_net,
                foreign_net=foreign_net,
            )

            df_60m = self._minute_ohlcv_cache.get(tick.code)

            # 멀티전략 순회: 카테고리별 게이트 분기
            for strategy in self._strategies:
                is_mr = strategy.category == "mean_reversion"

                # Signal Score 게이트: trend 전략만 적용
                if not is_mr:
                    min_score = config.get("strategy.min_signal_score", 1.5)
                    if score < min_score:
                        continue
                else:
                    # 평균회귀: 완화된 최소 점수 (0.5)
                    min_score_mr = config.get("strategy.min_signal_score_mr", 0.5)
                    if score < min_score_mr:
                        continue

                # 주봉 SMA20 필터: trend 전략만 적용
                if not is_mr and not self._check_weekly_trend(df_daily):
                    continue

                # 전략별 실시간 진입 판단
                if strategy.check_realtime_entry(df_daily, df_60m):
                    matched_strategy = strategy
                    break

            if not matched_strategy:
                return
            logger.info(
                f"진입 신호: {tick.code} by {matched_strategy.name} "
                f"(score={score:.1f})"
            )
        except Exception as e:
            logger.warning(f"진입 조건 체크 실패 ({tick.code}): {e}")
            return

        # 4. 리스크 사전 체크 (추세 유지 시 재진입 쿨다운 단축)
        trend_intact = False
        try:
            latest = df_daily.iloc[-1]
            adx_thr = config.get("strategy.adx_threshold", 20)
            if (
                latest.get("sma5", 0) > latest.get("sma20", 0)
                and latest.get("adx", 0) > adx_thr
            ):
                trend_intact = True
        except Exception:
            pass

        name = self._get_stock_name(tick.code)
        signal = Signal(
            code=tick.code,
            name=name,
            signal_type="buy",
            price=tick.price,
            score=score,
        )
        risk_result = self._risk_mgr.pre_check(signal, trend_intact=trend_intact)
        if not risk_result.approved:
            return

        # 5. 포지션 사이징
        capital = self._get_available_capital()
        invest_amount = self._sizer.calculate(
            capital=capital, win_rate=0.5, avg_win=0.08, avg_loss=0.04
        )
        if invest_amount <= 0:
            return

        qty = invest_amount // tick.price
        if qty <= 0:
            return

        # 6. 주문 실행  # RISK_CHECK_REQUIRED
        if self.mode == "live":
            hoga = self._get_hoga_type()
            order_price = tick.price if hoga == PRICE_LIMIT else 0

            result = await self._order_mgr.execute_order(
                tick.code, qty, order_price, ORDER_BUY, hoga
            )
            if result.success:
                await self._record_buy(tick, qty)
        elif self.mode == "paper":
            await self._record_buy(tick, qty)

    async def _execute_sell(self, position: Position, price: int, reason: ExitReason):
        """매도 실행. 부분 매도(PARTIAL_TARGET) 시 절반만 매도하고 포지션 유지."""
        # 부분 매도 수량 결정
        is_partial = reason == ExitReason.PARTIAL_TARGET
        if is_partial:
            sell_ratio = config.get("strategy.partial_sell_ratio", 0.5)
            sell_qty = max(1, int(position.quantity * sell_ratio))
            # 잔여 수량이 0이 되면 전량 매도로 전환
            if sell_qty >= position.quantity:
                is_partial = False
                sell_qty = position.quantity
        else:
            sell_qty = position.quantity

        if self.mode == "live":
            # 매도 재시도 제한 (최대 3회)
            retry_count = self._sell_retry_counts.get(position.id, 0)
            if retry_count >= 3:
                logger.error(f"매도 재시도 한도 초과: {position.code} (id={position.id})")
                self._telegram.send_system_error(
                    f"매도 실패 3회 초과: {position.code}",
                    "engine._execute_sell",
                )
                return

            # selling 상태로 변경 (중복 매도 방지)
            self._ds.update_position(position.id, status="selling")
            self._invalidate_positions_cache()

            hoga = self._get_hoga_type()
            order_price = price if hoga == PRICE_LIMIT else 0

            result = await self._order_mgr.execute_order(
                position.code,
                sell_qty,
                order_price,
                ORDER_SELL,
                hoga,
            )
            if not result.success:
                # 주문 실패 → open으로 복원
                self._ds.update_position(position.id, status="open")
                self._invalidate_positions_cache()
                self._sell_retry_counts[position.id] = retry_count + 1
                logger.warning(f"매도 실패 ({retry_count + 1}/3): {position.code}")
                return

            logger.info(f"매도 주문 접수: {position.code} ({'부분' if is_partial else '전량'}, 체결 대기 중)")

        # 포지션 상태 업데이트
        if is_partial:
            # 부분 매도: 수량 감소, 포지션 유지
            remaining_qty = position.quantity - sell_qty
            self._ds.update_position(
                position.id, quantity=remaining_qty, status="open", partial_sold=1
            )
            logger.info(f"부분 매도 완료: {position.code} {sell_qty}주 매도, {remaining_qty}주 잔여 (트레일링 계속)")
        else:
            if self.mode == "paper":
                self._ds.update_position(position.id, status="closed")
        self._invalidate_positions_cache()
        self._sell_retry_counts.pop(position.id, None)

        # 손익 계산 (매도 수량 기준)
        pnl = (price - position.entry_price) * sell_qty
        pnl_pct = (price - position.entry_price) / position.entry_price
        fee = price * sell_qty * 0.00015
        tax = price * sell_qty * 0.002  # 매도세

        trade = TradeRecord(
            code=position.code,
            name=position.name,
            side="sell",
            price=price,
            quantity=sell_qty,
            amount=price * sell_qty,
            fee=fee,
            tax=tax,
            pnl=float(pnl),
            pnl_pct=pnl_pct,
            reason=reason.value,
            executed_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )
        self._ds.record_trade(trade)
        self._daily_trades_cache = None  # 매매 발생 → 당일 trades 캐시 갱신

        # 텔레그램 알림
        sell_label = f"부분매도 {sell_qty}주" if is_partial else "전량매도"
        if pnl >= 0:
            net_pnl = pnl - fee - tax
            self._telegram.send_sell_executed_profit(
                position.code,
                position.name,
                price,
                position.hold_days,
                int(pnl),
                pnl_pct * 100,
                int(net_pnl),
                net_pnl / (position.entry_price * sell_qty) * 100,
            )
        else:
            self._telegram.send_sell_executed_loss(
                position.code,
                position.name,
                price,
                position.hold_days,
                int(pnl),
                pnl_pct * 100,
                reason.value,
            )

        logger.log(
            "TRADE",
            f"{sell_label} {position.code} @ {price:,} ({reason.value}), PnL: {pnl:+,}",
        )

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

    def _get_atr(self, code: str, fallback_price: int = 0) -> float:
        """종목의 ATR 조회 — OHLCV 캐시 우선, 없으면 가격 기반 추정.

        Args:
            code: 종목코드.
            fallback_price: OHLCV 없을 때 기준 가격.

        Returns:
            ATR 값 (float).
        """
        if code in self._atr_cache:
            return self._atr_cache[code]

        # OHLCV 캐시에서 최근 20일 데이터로 ATR 계산
        try:
            from datetime import timedelta
            end = datetime.now().strftime("%Y-%m-%d")
            start = (datetime.now() - timedelta(days=40)).strftime("%Y-%m-%d")
            ohlcv = self._ds.get_cached_ohlcv(code, start, end)

            if len(ohlcv) >= 14:
                trs = []
                for i in range(1, len(ohlcv)):
                    high = ohlcv[i]["high"]
                    low = ohlcv[i]["low"]
                    prev_close = ohlcv[i - 1]["close"]
                    tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
                    trs.append(tr)
                atr = sum(trs[-14:]) / 14
                self._atr_cache[code] = atr
                return atr
        except Exception:
            pass

        # 폴백: 가격의 2%
        atr = fallback_price * 0.02 if fallback_price > 0 else 0.0
        return atr

    def _get_stock_name(self, code: str) -> str:
        """종목명 조회 (DataProvider 경유, 캐시)."""
        try:
            from data.provider import get_provider
            return get_provider().get_stock_name(code)
        except Exception:
            return code

    async def _record_buy(self, tick: Tick, qty: int):
        """매수 기록."""
        atr = self._get_atr(tick.code, tick.price)
        stop_price = self._stop_mgr.get_initial_stop(tick.price, atr)
        target_return = config.get("strategy.target_return", 0.08)
        target_price = int(tick.price * (1 + target_return))
        name = self._get_stock_name(tick.code)

        pos = Position(
            id=0,
            code=tick.code,
            name=name,
            entry_date=datetime.now().strftime("%Y-%m-%d"),
            entry_price=tick.price,
            quantity=qty,
            stop_price=stop_price,
            target_price=target_price,
            high_since_entry=tick.price,
        )
        self._ds.insert_position(pos)
        self._invalidate_positions_cache()

        # 매수 종목 실시간 구독 보장 (watchlist에 없는 종목일 경우)
        await self._realtime.subscribe(tick.code)

        trade = TradeRecord(
            code=tick.code,
            name=name,
            side="buy",
            price=tick.price,
            quantity=qty,
            amount=tick.price * qty,
            fee=tick.price * qty * 0.00015,
            tax=0.0,
            pnl=0.0,
            pnl_pct=0.0,
            reason="signal",
            executed_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )
        self._ds.record_trade(trade)
        self._daily_trades_cache = None  # 매매 발생 → 당일 trades 캐시 갱신

        capital_pct = (tick.price * qty) / self._initial_capital if self._initial_capital > 0 else 0
        self._telegram.send_buy_executed(
            tick.code,
            name,
            tick.price,
            qty,
            tick.price * qty,
            capital_pct,
            stop_price,
            target_price,
        )
        logger.log("TRADE", f"매수 {tick.code} @ {tick.price:,} × {qty}주")

    def _update_daily_pnl(self, tick: Tick):
        """일일 손익 업데이트 — 보유 포지션 기반 미실현 손익."""
        positions = self._get_cached_positions()
        if not positions:
            return

        unrealized_pnl = 0
        for pos_dict in positions:
            code = pos_dict["code"]
            entry_price = pos_dict["entry_price"]
            qty = pos_dict["quantity"]
            # 최신 가격 캐시에서 조회 — 없으면 매입가 사용 (변동 0)
            current = self._latest_prices.get(code, entry_price)
            unrealized_pnl += (current - entry_price) * qty

        # 당일 실현 손익 (메모리 캐시 — 매매 발생 시만 갱신)
        if self._daily_trades_cache is None:
            today = datetime.now().strftime("%Y-%m-%d")
            self._daily_trades_cache = self._ds.get_trades_by_date(today)
        realized_pnl = sum(
            t.get("pnl", 0) for t in self._daily_trades_cache if t["side"] == "sell"
        )

        total_pnl = realized_pnl + unrealized_pnl
        pnl_pct = total_pnl / self._initial_capital if self._initial_capital > 0 else 0.0

        self._risk_mgr.update_daily_pnl(pnl_pct)

        # MDD 업데이트
        current_capital = self._initial_capital + total_pnl
        self._risk_mgr.update_mdd(float(current_capital))

        # 일일 한도 체크
        if pnl_pct <= self._risk_mgr._daily_loss_limit and not self._risk_mgr.is_halted:
            self.halt()

    def _switch_strategy_by_regime(self):
        """시장 국면에 따라 전략 인스턴스 전환 (adaptive 모드, 멀티전략 지원)."""
        regime = self._market_regime.regime_type
        regime_map = self._strategy_config.get("regime_strategy", {})
        new_names = regime_map.get(regime)

        if not new_names:
            return  # 매핑 없으면 현재 전략 유지

        # str → list 하위 호환
        if isinstance(new_names, str):
            new_names = [new_names]

        current_names = sorted(s.name for s in self._strategies)
        target_names = sorted(new_names)

        if current_names == target_names:
            logger.info(f"전략 유지: {current_names} (국면: {regime})")
            return

        self._strategies = [
            get_strategy(n, self._strategy_config) for n in new_names
        ]
        self._strategy = self._strategies[0]  # 하위 호환
        names_str = ", ".join(new_names)
        logger.info(
            f"전략 전환: {current_names} → [{names_str}] (국면: {regime})"
        )
        self._telegram.send(
            f"전략 전환: [{names_str}] "
            f"(국면: {regime}, ADX {self._market_regime.kospi_adx:.1f})"
        )

    def _get_hoga_type(self) -> str:
        """config 기반 호가 유형 반환."""
        order_type = config.get("trading.order_type", "market")
        return PRICE_LIMIT if order_type == "limit" else PRICE_MARKET

    def _get_available_capital(self) -> int:
        """가용 자본 조회 — 초기자본 - 투자중 금액."""
        positions = self._get_cached_positions()
        invested = sum(p["entry_price"] * p["quantity"] for p in positions)
        return max(0, self._initial_capital - invested)

    def _get_cached_positions(self) -> list[dict]:
        """포지션 메모리 캐시 반환. 없으면 DB 조회."""
        if self._positions_cache is None:
            self._positions_cache = self._ds.get_open_positions()
        return self._positions_cache

    def _invalidate_positions_cache(self):
        """포지션 캐시 무효화 (매수/매도 후)."""
        self._positions_cache = None

    def _dict_to_position(self, d: dict) -> Position:
        """dict -> Position 변환."""
        # hold_days 계산: entry_date 기준 경과일
        hold_days = d.get("hold_days", 0)
        if hold_days == 0 and d.get("entry_date"):
            try:
                entry = datetime.strptime(d["entry_date"], "%Y-%m-%d").date()
                hold_days = (datetime.now().date() - entry).days
            except ValueError:
                hold_days = 0

        return Position(
            id=d["id"],
            code=d["code"],
            name=d.get("name", ""),
            entry_date=d["entry_date"],
            entry_price=d["entry_price"],
            quantity=d["quantity"],
            stop_price=d["stop_price"],
            target_price=d.get("target_price", 0),
            status=d.get("status", "open"),
            high_since_entry=d.get("high_since_entry", d["entry_price"]),
            hold_days=hold_days,
            partial_sold=bool(d.get("partial_sold", 0)),
            updated_at=d.get("updated_at", ""),
        )

    # ── 장마감 ──

    async def _post_market_cleanup(self):
        """장 마감 후 미체결 주문 정리 (15:35 스케줄).

        1. 미체결 주문 전량 취소
        2. "selling" 상태인데 체결 안 된 포지션을 "open"으로 복원
        """
        if self.mode != "live":
            return

        logger.info("장마감 미체결 정리 시작")

        # 1. 미체결 주문 전량 취소
        cancel_results = await self._order_mgr.cancel_all_pending()

        # 2. "selling" 상태 포지션 복원 (체결 안 된 매도 주문)
        restored_count = 0
        try:
            selling_positions = self._ds.get_positions_by_status("selling")

            for pos_dict in selling_positions:
                self._ds.update_position(pos_dict["id"], status="open")
                restored_count += 1
                logger.warning(
                    f"미체결 매도 포지션 복원: {pos_dict['code']} (id={pos_dict['id']})"
                )
        except Exception as e:
            logger.error(f"selling 포지션 복원 실패: {e}")

        if selling_positions:
            self._invalidate_positions_cache()
            self._sell_retry_counts.clear()

        # 텔레그램 알림
        cancelled = sum(1 for v in cancel_results.values() if v)
        failed = sum(1 for v in cancel_results.values() if not v)
        if cancel_results or restored_count > 0:
            msg_parts = []
            if cancel_results:
                msg_parts.append(f"미체결 취소 {cancelled}건")
                if failed > 0:
                    msg_parts.append(f"(실패 {failed}건)")
            if restored_count > 0:
                msg_parts.append(f"매도 미체결 복원 {restored_count}건")
            self._telegram.send(f"🔄 장마감 정리: {', '.join(msg_parts)}")

        logger.info(
            f"장마감 미체결 정리 완료: 취소 {cancelled}건, 복원 {restored_count}건"
        )

    def _daily_report(self):
        """일간 리포트 (16:00)."""
        today = datetime.now().strftime("%Y-%m-%d")
        trades = self._ds.get_trades_by_date(today)
        positions = self._ds.get_open_positions()

        buy_count = sum(1 for t in trades if t["side"] == "buy")
        sell_count = sum(1 for t in trades if t["side"] == "sell")
        realized_pnl = sum(t.get("pnl", 0) for t in trades if t["side"] == "sell")

        # 미실현 손익 (보유 포지션 기준 — 종가 미반영, 매입가 기준)
        unrealized_pnl = 0  # 장마감 시 실시간 가격 없으므로 0

        pnl_pct = realized_pnl / self._initial_capital * 100 if self._initial_capital > 0 else 0.0
        current_capital = self._initial_capital + int(realized_pnl)

        self._telegram.send_daily_report(
            date=today,
            buy_count=buy_count,
            sell_count=sell_count,
            realized_pnl=int(realized_pnl),
            realized_pnl_pct=pnl_pct,
            position_count=len(positions),
            unrealized_pnl=unrealized_pnl,
            initial_capital=self._initial_capital,
            current_capital=current_capital,
            total_return_pct=pnl_pct,
            current_mdd=self._risk_mgr.current_mdd,
        )

        # 일일 성과 DB 저장
        self._ds.save_daily_performance(
            date=today,
            realized_pnl=realized_pnl,
            unrealized_pnl=float(unrealized_pnl),
            total_capital=float(current_capital),
            daily_return=pnl_pct,
            mdd_current=self._risk_mgr.current_mdd,
            trade_count=buy_count + sell_count,
        )

        logger.info(
            f"일간 리포트 발송: 매수{buy_count}/매도{sell_count}/PnL{int(realized_pnl):+,}"
        )

    def _daily_reset(self):
        """일일 리셋 (09:00)."""
        self._risk_mgr.reset_daily()
        self._invalidate_positions_cache()
        self._sell_retry_counts.clear()
        self._atr_cache.clear()  # ATR 캐시 리프레시
        self._last_entry_check.clear()  # 진입 체크 쓰로틀 초기화
        self._minute_ohlcv_cache.clear()  # 60분봉 캐시 초기화
        # partial_sold 초기화 불필요 (DB 기반)
        self._daily_trades_cache = None  # 당일 trades 캐시 초기화

        # OHLCV 캐시 정리 (400일 이상 된 데이터 삭제)
        try:
            self._ds.cleanup_ohlcv_cache(400)
        except Exception as e:
            logger.warning(f"OHLCV 캐시 정리 실패 (무시): {e}")

        # 보유 포지션 hold_days 갱신
        positions = self._ds.get_open_positions()
        today = datetime.now().date()
        for pos in positions:
            try:
                entry = datetime.strptime(pos["entry_date"], "%Y-%m-%d").date()
                hold_days = (today - entry).days
                self._ds.update_position(pos["id"], hold_days=hold_days)
            except (ValueError, KeyError):
                pass

        logger.info("일일 리셋 완료")

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

    async def _ws_connect(self):
        """WebSocket 연결 + 구독 (08:50 스케줄)."""
        from src.utils.market_calendar import is_trading_day, now_kst
        if not is_trading_day(now_kst().date()):
            logger.info("비거래일 — WebSocket 연결 생략")
            return

        try:
            await self._kiwoom.connect(use_websocket=True)
        except Exception as e:
            logger.error(f"WebSocket 연결 실패: {e}")
            self._telegram.send_system_error(str(e), "ws_connect")
            return

        # 구독 대상: watchlist + 보유 포지션
        subscribe_codes: set[str] = set()
        if self._candidates:
            subscribe_codes.update(self._candidates)
        open_positions = self._ds.get_open_positions()
        if open_positions:
            subscribe_codes.update(p["code"] for p in open_positions)
        if subscribe_codes:
            await self._realtime.subscribe_list(list(subscribe_codes))
            logger.info(f"WebSocket 연결 + 구독 완료: {len(subscribe_codes)}종목")

            # 구독 후 연결 안정성 확인 (서버 close 타이밍 이슈 대응)
            await asyncio.sleep(3)
            if self._kiwoom._ws and not self._kiwoom._ws.connected:
                logger.warning("WebSocket 구독 직후 연결 끊김 — 즉시 재연결")
                try:
                    await self._kiwoom.connect(use_websocket=True)
                    await self._realtime.subscribe_list(list(subscribe_codes))
                    logger.info("WebSocket 재연결 + 재구독 성공")
                except Exception as e:
                    logger.error(f"WebSocket 재연결 실패: {e}")
                    self._telegram.send_system_error(str(e), "ws_reconnect")

    async def _ws_disconnect(self):
        """WebSocket 명시적 종료 (18:10 스케줄)."""
        if self._kiwoom._ws and self._kiwoom._ws.connected:
            await self._kiwoom.disconnect()
            logger.info("WebSocket 장마감 종료 (18:10)")
        else:
            logger.debug("WebSocket 이미 종료 상태")

    async def _ensure_connection(self):
        """키움 API 연결 확인/재연결 — _ws_connect에 통합, 레거시 호환용."""
        if self._kiwoom._connected:
            logger.info("키움 API 연결 상태: 정상")
            return

        self._reconnect_count += 1
        if self._reconnect_count <= self._max_reconnect:
            logger.warning(
                f"키움 재연결 시도 ({self._reconnect_count}/{self._max_reconnect})"
            )
            try:
                await self._kiwoom.connect()
                logger.info("키움 API 재연결 성공")
            except Exception as e:
                logger.error(f"키움 API 재연결 실패: {e}")
                self._telegram.send_system_error(
                    "ConnectionLost",
                    "kiwoom_api",
                    f"재연결 시도 ({self._reconnect_count}/{self._max_reconnect})",
                )
        else:
            logger.error("최대 재연결 횟수 초과")
            self._telegram.send_system_error(
                "MaxReconnectExceeded", "kiwoom_api"
            )

    async def on_chejan(self, data: dict):
        """체결 이벤트 수신 — selling 포지션의 최종 종료 처리."""
        logger.info(f"체결 이벤트: {data}")

        # selling 상태 포지션 중 체결된 종목 찾아서 closed 처리
        try:
            code = data.get("item", "") or data.get("code", "")
            if not code:
                return

            positions = self._ds.get_open_positions()
            # selling 상태인 포지션도 별도 조회
            selling_positions = self._ds.get_positions_by_code_and_status(
                code, "selling"
            )

            for pos_dict in selling_positions:
                self._ds.update_position(pos_dict["id"], status="closed")
                logger.info(f"체결 확인 → 포지션 종료: {code} (id={pos_dict['id']})")

            if selling_positions:
                self._invalidate_positions_cache()
                self._sell_retry_counts = {
                    k: v for k, v in self._sell_retry_counts.items()
                    if k not in {p["id"] for p in selling_positions}
                }
        except Exception as e:
            logger.error(f"체결 이벤트 처리 실패: {e}")
