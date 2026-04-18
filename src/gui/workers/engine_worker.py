"""TradingEngine을 별도 스레드에서 asyncio로 실행하는 QThread 래퍼.

모든 cross-thread 호출은 Qt signal 또는 asyncio.run_coroutine_threadsafe로 처리.
"""

import asyncio
from datetime import datetime

from PyQt5.QtCore import QThread
from loguru import logger

from src.gui.workers.signals import EngineSignals


class EngineWorker(QThread):
    """TradingEngine을 asyncio 이벤트 루프에서 실행."""

    def __init__(self, mode: str = "paper", parent=None):
        super().__init__(parent)
        self._mode = mode
        self._engine = None
        self._loop = None

        self.signals = EngineSignals()

        # UI → Worker 시그널 연결 (slot은 worker 스레드에서 실행)
        self.signals.request_stop.connect(self._on_request_stop)
        self.signals.request_halt.connect(self._on_request_halt)
        self.signals.request_resume.connect(self._on_request_resume)
        self.signals.request_screening.connect(self._on_request_screening)
        self.signals.request_report.connect(self._on_request_report)
        self.signals.request_reconnect.connect(self._on_request_reconnect)
        self.signals.request_daily_reset.connect(self._on_request_daily_reset)
        self.signals.request_refresh_60m.connect(self._on_request_refresh_60m)

        # daemon 스레드 — 메인 프로세스 종료 시 자동 정리
        self.setTerminationEnabled(True)

    def run(self):
        """QThread 메인 — asyncio 루프 실행."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        try:
            self._loop.run_until_complete(self._run_engine())
        except Exception as e:
            logger.error(f"EngineWorker 오류: {e}")
            self.signals.error.emit(str(e))
        finally:
            # 엔진 정리 — 루프가 아직 열려있으면 stop 호출
            if self._engine and self._engine._running:
                try:
                    self._loop.run_until_complete(self._engine.stop())
                except Exception:
                    pass
            try:
                # 잔여 태스크 정리
                pending = asyncio.all_tasks(self._loop)
                for task in pending:
                    task.cancel()
                if pending:
                    self._loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
            except Exception:
                pass
            self._loop.close()
            self._loop = None
            self._engine = None
            self.signals.stopped.emit()

    async def _run_engine(self):
        """엔진 시작 및 폴링 루프."""
        from src.engine_legacy import TradingEngine

        self._engine = TradingEngine(mode=self._mode)

        await self._engine.start()
        self.signals.started.emit()

        # 폴링 루프 — 2초 간격
        while self._engine._running:
            self._emit_status()
            self._emit_positions()
            self._emit_trades()
            self._emit_candidates()
            await asyncio.sleep(2)

    # ── UI → Worker 명령 처리 (thread-safe) ──

    def _on_request_stop(self):
        """엔진 중지 — asyncio 안전 호출."""
        if self._engine and self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._engine.stop(), self._loop)

    def _on_request_halt(self):
        """매매 중단 — asyncio 안전 호출."""
        if self._engine and self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._async_halt(), self._loop)

    def _on_request_resume(self):
        """매매 재개 — asyncio 안전 호출."""
        if self._engine and self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._async_resume(), self._loop)

    async def _async_halt(self):
        """halt()를 asyncio 루프 내에서 실행."""
        self._engine.halt()
        self._emit_status()

    async def _async_resume(self):
        """resume()을 asyncio 루프 내에서 실행."""
        self._engine._risk_mgr.resume()
        self._emit_status()

    def _on_request_screening(self):
        """수동 스크리닝 — asyncio 안전 호출."""
        if self._engine and self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(
                self._engine._pre_market_screening(), self._loop
            )

    def _on_request_report(self):
        """수동 일간 리포트 — asyncio 안전 호출."""
        if self._engine and self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._async_report(), self._loop)

    async def _async_report(self):
        """daily_report()를 asyncio 루프 내에서 실행."""
        self._engine._daily_report()
        self._emit_status()

    def _on_request_reconnect(self):
        """수동 연결 확인 — asyncio 안전 호출."""
        if self._engine and self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(
                self._engine._ensure_connection(), self._loop
            )

    def _on_request_daily_reset(self):
        """수동 일일 리셋 — asyncio 안전 호출."""
        if self._engine and self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._async_daily_reset(), self._loop)

    async def _async_daily_reset(self):
        """daily_reset()을 asyncio 루프 내에서 실행."""
        self._engine._daily_reset()
        self._emit_status()

    def _on_request_refresh_60m(self):
        """수동 60분봉 갱신 — asyncio 안전 호출."""
        if self._engine and self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(
                self._engine._refresh_minute_ohlcv(), self._loop
            )

    # ── 데이터 Emission ──

    def _emit_status(self):
        """현재 상태를 시그널로 전송."""
        if not self._engine:
            return

        from src.utils.config import config
        max_positions = config.get("trading.max_positions", 3)

        self.signals.status_updated.emit({
            "mode": self._engine.mode,
            "running": self._engine._running,
            "halted": self._engine._risk_mgr.is_halted,
            "daily_pnl_pct": self._engine._risk_mgr.daily_pnl_pct,
            "mdd": self._engine._risk_mgr.current_mdd,
            "candidates": len(self._engine._candidates),
            "capital": self._engine._get_available_capital(),
            "max_positions": max_positions,
        })

    def _emit_positions(self):
        """포지션 목록을 시그널로 전송 (현재가 주입)."""
        if not self._engine:
            return
        try:
            positions = self._engine._ds.get_open_positions()
            # 엔진의 최신 가격 캐시에서 current_price 주입
            for pos in positions:
                code = pos.get("code", "")
                latest = self._engine._latest_prices.get(code)
                if latest:
                    pos["current_price"] = latest
                else:
                    pos["current_price"] = pos.get("entry_price", 0)
            self.signals.positions_updated.emit(positions)
        except Exception:
            pass

    def _emit_trades(self):
        """당일 체결 내역을 시그널로 전송."""
        if not self._engine:
            return
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            trades = self._engine._ds.get_trades_by_date(today)
            self.signals.trades_updated.emit(trades)
        except Exception:
            pass

    def _emit_candidates(self):
        """매수 후보 목록을 시그널로 전송."""
        if not self._engine:
            return
        try:
            candidates = []
            for code in self._engine._candidates:
                name = self._engine._get_stock_name(code)
                candidates.append({"code": code, "name": name})
            self.signals.candidates_updated.emit(candidates)
        except Exception:
            pass

    @property
    def engine(self):
        return self._engine
