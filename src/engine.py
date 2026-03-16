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

    def __init__(self, mode: Literal["paper", "simulate", "live"] = "paper"):
        self.mode = mode
        self._running = False

        # 모듈 초기화
        self._ds = DataStore()
        self._ds.connect()
        self._ds.create_tables()

        # 키움 API (REST + WebSocket)
        if mode == "simulate":
            base_url = config.get("broker.mock_base_url", "https://mockapi.kiwoom.com")
            ws_url = config.get(
                "broker.mock_ws_url",
                "wss://mockapi.kiwoom.com:10000/api/dostk/websocket",
            )
        elif mode == "live":
            base_url = config.get("broker.base_url", "https://api.kiwoom.com")
            ws_url = config.get(
                "broker.ws_url",
                "wss://api.kiwoom.com:10000/api/dostk/websocket",
            )
        else:  # paper
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

        self._screener = Screener(config.data)
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

        # 키움 연결 (simulate/live에서만 WebSocket 사용)
        use_ws = self.mode in ("simulate", "live")
        await self._kiwoom.connect(use_websocket=use_ws)

    async def stop(self):
        """시스템 중지."""
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

    async def _pre_market_screening(self):
        """장전 스크리닝 (08:30)."""
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
            logger.error(f"스크리닝 실패: {e}")
            self._telegram.send_system_error(
                str(e), "screener.run_daily_screening"
            )

    # ── 장중 실시간 ──

    async def on_price_update(self, tick: Tick):
        """실시간 시세 수신 콜백."""
        if not self._running or self._risk_mgr.is_halted:
            return
        if not is_market_open():
            return

        # 1. 보유 종목 손절/트레일링/목표가 체크
        await self._check_exit_conditions(tick)

        # 2. 일일 손익 업데이트 및 한도 체크
        self._update_daily_pnl(tick)

        # 3. 후보 종목 진입 조건 체크
        if tick.code in self._candidates:
            await self._check_entry_conditions(tick)

    async def _check_exit_conditions(self, tick: Tick):
        """보유 종목 청산 조건 체크."""
        positions = self._ds.get_open_positions()
        for pos_dict in positions:
            if pos_dict["code"] != tick.code:
                continue

            pos = self._dict_to_position(pos_dict)

            # 트레일링스탑 업데이트
            # (ATR은 캐시된 값 사용, 여기서는 간이 계산)
            new_stop = self._stop_mgr.update_trailing_stop(
                pos, tick.price, pos.entry_price * 0.02
            )
            if new_stop != pos.stop_price:
                self._ds.update_position(pos.id, stop_price=new_stop)

            # 청산 체크 (간이 — 실제로는 latest 일봉 필요)
            if self._stop_mgr.is_stopped(pos, tick.price):
                await self._execute_sell(pos, tick.price, ExitReason.STOP_LOSS)
            elif tick.price >= pos.target_price:
                await self._execute_sell(pos, tick.price, ExitReason.TARGET_REACHED)

    async def _check_entry_conditions(self, tick: Tick):
        """후보 종목 진입 조건 체크 — 간이 버전."""
        # 리스크 사전 체크
        signal = Signal(
            code=tick.code,
            name="",
            signal_type="buy",
            price=tick.price,
            score=0.0,
        )
        risk_result = self._risk_mgr.pre_check(signal)
        if not risk_result.approved:
            return

        # 포지션 사이징
        capital = self._get_available_capital()
        invest_amount = self._sizer.calculate(
            capital=capital, win_rate=0.5, avg_win=0.08, avg_loss=0.04
        )
        if invest_amount <= 0:
            return

        qty = invest_amount // tick.price
        if qty <= 0:
            return

        # 주문 실행  # RISK_CHECK_REQUIRED
        if self.mode in ("live", "simulate"):
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
        if self.mode in ("live", "simulate"):
            from src.broker.tr_codes import ORDER_SELL, PRICE_MARKET

            result = await self._order_mgr.execute_order(
                position.code,
                position.quantity,
                price,
                ORDER_SELL,
                PRICE_MARKET,
            )
            if not result.success:
                return

        # 포지션 종료
        self._ds.update_position(position.id, status="closed")

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

    async def _record_buy(self, tick: Tick, qty: int):
        """매수 기록."""
        atr_estimate = tick.price * 0.02  # 간이 ATR
        stop_price = self._stop_mgr.get_initial_stop(tick.price, atr_estimate)
        target_return = config.get("strategy.target_return", 0.08)
        target_price = int(tick.price * (1 + target_return))

        pos = Position(
            id=0,
            code=tick.code,
            name="",
            entry_date=datetime.now().strftime("%Y-%m-%d"),
            entry_price=tick.price,
            quantity=qty,
            stop_price=stop_price,
            target_price=target_price,
            high_since_entry=tick.price,
        )
        self._ds.insert_position(pos)

        trade = TradeRecord(
            code=tick.code,
            name="",
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
            "",
            tick.price,
            qty,
            tick.price * qty,
            capital_pct,
            stop_price,
            target_price,
        )
        logger.log("TRADE", f"매수 {tick.code} @ {tick.price:,} × {qty}주")

    def _update_daily_pnl(self, tick: Tick):
        """일일 손익 업데이트."""
        # 간이 구현: 보유 포지션 기반 미실현 손익
        pass

    def _get_available_capital(self) -> int:
        """가용 자본 조회."""
        initial = config.get("backtest.initial_capital", 10_000_000)
        return initial  # 간이 구현

    def _dict_to_position(self, d: dict) -> Position:
        """dict -> Position 변환."""
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
        total_pnl = sum(t.get("pnl", 0) for t in trades if t["side"] == "sell")

        initial_capital = config.get("backtest.initial_capital", 1_000_000)
        pnl_pct = total_pnl / initial_capital * 100 if initial_capital > 0 else 0.0

        self._telegram.send_daily_report(
            date=today,
            buy_count=buy_count,
            sell_count=sell_count,
            realized_pnl=int(total_pnl),
            realized_pnl_pct=pnl_pct,
            position_count=len(positions),
            unrealized_pnl=0,
            initial_capital=initial_capital,
            current_capital=initial_capital + int(total_pnl),
            total_return_pct=pnl_pct,
            current_mdd=0.0,
        )
        logger.info(
            f"일간 리포트 발송: 매수{buy_count}/매도{sell_count}/PnL{int(total_pnl):+,}"
        )

    def _daily_reset(self):
        """일일 리셋 (09:00)."""
        self._risk_mgr.reset_daily()
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
