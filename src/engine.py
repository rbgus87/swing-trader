"""TradingEngine — 전체 모듈 조율자.

장전 스크리닝 -> 장중 실시간 파이프라인 -> 체결 이벤트 -> 장마감 리포트.
asyncio 기반으로 동작한다.
"""

import asyncio
from datetime import datetime
from typing import Literal

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from loguru import logger

from src.broker.kiwoom_api import KiwoomAPI
from src.broker.order_manager import OrderManager
from src.broker.realtime_data import RealtimeDataManager
from src.datastore import DataStore
from src.models import ExitReason, Position, Signal, Tick, TradeRecord
from src.notification.telegram_bot import TelegramBot
from src.risk.position_sizer import PositionSizer
from src.risk.risk_manager import RiskManager
from src.risk.stop_manager import StopManager
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

        # 키움 API (REST + WebSocket) — IS_PAPER_TRADING으로 서버 자동 선택
        is_paper = self.mode == "paper"
        if is_paper:
            base_url = config.get("broker.mock_base_url", "https://mockapi.kiwoom.com")
            ws_url = config.get(
                "broker.mock_ws_url",
                "wss://mockapi.kiwoom.com:10000/api/dostk/websocket",
            )
        else:
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
            stop_atr_mult=config.get("risk.stop_atr_multiplier", 1.5),
            max_stop_pct=config.get("risk.max_stop_pct", 0.07),
            trailing_atr_mult=config.get("risk.trailing_atr_multiplier", 2.0),
            trailing_activate_pct=config.get("risk.trailing_activate_pct", 0.03),
        )
        self._telegram = TelegramBot()

        # 상태
        self._candidates: list[str] = []  # 당일 매수 후보
        self._reconnect_count = 0
        self._max_reconnect = 5
        self._initial_capital = config.get("backtest.initial_capital", 1_000_000)
        self._positions_cache: list[dict] | None = None  # 포지션 메모리 캐시
        self._sell_retry_counts: dict[int, int] = {}  # 매도 재시도 카운터
        self._latest_prices: dict[str, int] = {}  # 종목별 최신 가격 캐시
        self._atr_cache: dict[str, float] = {}  # 종목별 ATR 캐시

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
        reconnect_time = config.get("schedule.reconnect_time", "08:45")

        h, m = screening_time.split(":")
        self._scheduler.add_job(
            self._pre_market_screening, "cron", hour=int(h), minute=int(m)
        )

        h, m = report_time.split(":")
        self._scheduler.add_job(
            self._daily_report, "cron", hour=int(h), minute=int(m)
        )

        h, m = reconnect_time.split(":")
        self._scheduler.add_job(
            self._ensure_connection, "cron", hour=int(h), minute=int(m)
        )

        # 일일 리셋 (09:00)
        self._scheduler.add_job(self._daily_reset, "cron", hour=9, minute=0)

        self._scheduler.start()

        # 키움 연결 (live 모드에서만 WebSocket 사용)
        use_ws = self.mode == "live"
        await self._kiwoom.connect(use_websocket=use_ws)

        # 서비스 시작 알림
        self._telegram.send_startup(self.mode)

    async def stop(self):
        """시스템 중지."""
        self._telegram.send_shutdown(self.mode)
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
        """장전 스크리닝 (08:30). 실패 시 최대 3회 재시도."""
        max_retries = 3
        try:
            today = datetime.now().strftime("%Y%m%d")
            # Screener는 pykrx 기반 sync — to_thread로 래핑
            self._candidates = await asyncio.to_thread(
                self._screener.run_daily_screening, today
            )
            if self._candidates:
                await self._realtime.subscribe_list(self._candidates)
                self._telegram.send(
                    f"📊 당일 매수 후보: {len(self._candidates)}종목"
                )
            logger.info(f"스크리닝 완료: {len(self._candidates)}종목")
        except Exception as e:
            logger.error(f"스크리닝 실패 (시도 {_retry + 1}/{max_retries + 1}): {e}")
            if _retry < max_retries:
                delay = 60 * (_retry + 1)  # 1분, 2분, 3분 후 재시도
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

        # 최신 가격 캐시 갱신
        self._latest_prices[tick.code] = tick.price

        # 1. 보유 종목 손절/트레일링/목표가 체크
        await self._check_exit_conditions(tick)

        # 2. 일일 손익 업데이트 및 한도 체크
        self._update_daily_pnl(tick)

        # 3. 후보 종목 진입 조건 체크
        if tick.code in self._candidates:
            await self._check_entry_conditions(tick)

    async def _check_exit_conditions(self, tick: Tick):
        """보유 종목 청산 조건 체크 — signals.check_exit_signal 통합."""
        positions = self._get_cached_positions()
        for pos_dict in positions:
            if pos_dict["code"] != tick.code:
                continue

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
        """OHLCV 기반 종합 청산 판단 — signals.check_exit_signal 활용."""
        from src.strategy.signals import check_exit_signal
        import pandas as pd

        max_hold = config.get("trading.max_hold_days", 15)

        # OHLCV 캐시에서 최근 일봉 조회
        try:
            from datetime import timedelta
            end = datetime.now().strftime("%Y-%m-%d")
            start = (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d")
            ohlcv = self._ds.get_cached_ohlcv(pos.code, start, end)
            if ohlcv and len(ohlcv) >= 1:
                latest_row = ohlcv[-1]
                latest = pd.Series(latest_row)
                return check_exit_signal(
                    pos, current_price, latest, max_hold_days=max_hold
                )
        except Exception:
            pass

        # OHLCV 없으면 기본 손절/목표가만 체크
        if self._stop_mgr.is_stopped(pos, current_price):
            return ExitReason.STOP_LOSS
        if pos.target_price > 0 and current_price >= pos.target_price:
            return ExitReason.TARGET_REACHED
        if pos.hold_days >= max_hold:
            return ExitReason.MAX_HOLD
        return None

    async def _check_entry_conditions(self, tick: Tick):
        """후보 종목 진입 조건 체크 — signals.py 통합."""
        from src.strategy.signals import (
            calculate_indicators,
            calculate_signal_score,
            check_entry_signal,
        )
        import pandas as pd

        # 1. OHLCV 캐시에서 일봉 데이터 조회 + 기술적 지표 계산
        try:
            from datetime import timedelta
            end = datetime.now().strftime("%Y-%m-%d")
            start = (datetime.now() - timedelta(days=130)).strftime("%Y-%m-%d")
            ohlcv = self._ds.get_cached_ohlcv(tick.code, start, end)
            if not ohlcv or len(ohlcv) < 30:
                return  # 데이터 부족

            df = pd.DataFrame(ohlcv)
            df = calculate_indicators(df)

            # 2. 매수 신호 체크 (60분봉 미사용)
            if not check_entry_signal(df, use_60m=False):
                return

            # 3. 신호 강도 점수 계산
            score = calculate_signal_score(df)
            min_score = config.get("strategy.min_signal_score", 1.5)
            if score < min_score:
                return
        except Exception:
            return  # 지표 계산 실패 시 진입 안 함

        # 4. 리스크 사전 체크
        name = self._get_stock_name(tick.code)
        signal = Signal(
            code=tick.code,
            name=name,
            signal_type="buy",
            price=tick.price,
            score=score,
        )
        risk_result = self._risk_mgr.pre_check(signal)
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
            from src.broker.tr_codes import ORDER_BUY, PRICE_MARKET

            result = await self._order_mgr.execute_order(
                tick.code, qty, tick.price, ORDER_BUY, PRICE_MARKET
            )
            if result.success:
                await self._record_buy(tick, qty)
        elif self.mode == "paper":
            await self._record_buy(tick, qty)

    async def _execute_sell(self, position: Position, price: int, reason: ExitReason):
        """매도 실행."""
        if self.mode == "live":
            from src.broker.tr_codes import ORDER_SELL, PRICE_MARKET

            # 매도 재시도 제한 (최대 3회)
            retry_count = self._sell_retry_counts.get(position.id, 0)
            if retry_count >= 3:
                logger.error(f"매도 재시도 한도 초과: {position.code} (id={position.id})")
                self._telegram.send_system_error(
                    f"매도 실패 3회 초과: {position.code}",
                    "engine._execute_sell",
                )
                return

            result = await self._order_mgr.execute_order(
                position.code,
                position.quantity,
                price,
                ORDER_SELL,
                PRICE_MARKET,
            )
            if not result.success:
                self._sell_retry_counts[position.id] = retry_count + 1
                logger.warning(f"매도 실패 ({retry_count + 1}/3): {position.code}")
                return

        # 포지션 종료
        self._ds.update_position(position.id, status="closed")
        self._invalidate_positions_cache()
        self._sell_retry_counts.pop(position.id, None)

        # 손익 계산
        pnl = (price - position.entry_price) * position.quantity
        pnl_pct = (price - position.entry_price) / position.entry_price
        fee = price * position.quantity * 0.00015
        tax = price * position.quantity * 0.002  # 매도세

        trade = TradeRecord(
            code=position.code,
            name=position.name,
            side="sell",
            price=price,
            quantity=position.quantity,
            amount=price * position.quantity,
            fee=fee,
            tax=tax,
            pnl=float(pnl),
            pnl_pct=pnl_pct,
            reason=reason.value,
            executed_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )
        self._ds.record_trade(trade)

        # 텔레그램 알림
        if pnl >= 0:
            net_pnl = pnl - fee - tax
            self._telegram.send_sell_executed_profit(
                position.code,
                position.name,
                price,
                position.hold_days,
                int(pnl),
                pnl_pct,
                int(net_pnl),
                net_pnl / (position.entry_price * position.quantity),
            )
        else:
            self._telegram.send_sell_executed_loss(
                position.code,
                position.name,
                price,
                position.hold_days,
                int(pnl),
                pnl_pct,
                reason.value,
            )

        logger.log(
            "TRADE",
            f"매도 {position.code} @ {price:,} ({reason.value}), PnL: {pnl:+,}",
        )

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
        """종목명 조회 (pykrx 캐시)."""
        try:
            from pykrx import stock
            name = stock.get_market_ticker_name(code)
            return name or code
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

        available = self._get_available_capital()
        capital_pct = (tick.price * qty) / available if available > 0 else 0
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

        # 당일 실현 손익
        today = datetime.now().strftime("%Y-%m-%d")
        trades = self._ds.get_trades_by_date(today)
        realized_pnl = sum(t.get("pnl", 0) for t in trades if t["side"] == "sell")

        total_pnl = realized_pnl + unrealized_pnl
        pnl_pct = total_pnl / self._initial_capital if self._initial_capital > 0 else 0.0

        self._risk_mgr.update_daily_pnl(pnl_pct)

        # MDD 업데이트
        current_capital = self._initial_capital + total_pnl
        self._risk_mgr.update_mdd(float(current_capital))

        # 일일 한도 체크
        if pnl_pct <= self._risk_mgr._daily_loss_limit:
            self.halt()

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
            updated_at=d.get("updated_at", ""),
        )

    # ── 장마감 ──

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

    async def _ensure_connection(self):
        """키움 API 연결 확인/재연결 (08:45)."""
        if not self._kiwoom._connected:
            self._reconnect_count += 1
            if self._reconnect_count <= self._max_reconnect:
                logger.warning(
                    f"키움 재연결 시도 ({self._reconnect_count}/{self._max_reconnect})"
                )
                await self._kiwoom.connect()
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
        """체결 이벤트 수신."""
        logger.info(f"체결 이벤트: {data}")
