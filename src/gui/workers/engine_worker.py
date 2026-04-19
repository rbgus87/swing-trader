"""Worker 스레드 모음.

- EngineWorker: v2.3 Orchestrator(EOD 배치) 1회 실행.
- LegacyEngineWorker: engine_legacy.TradingEngine(실시간 asyncio 루프) 구동.
"""

import asyncio
from datetime import datetime

from PyQt5.QtCore import QThread
from loguru import logger

from src.gui.workers.signals import EngineSignals


class EngineWorker(QThread):
    """Orchestrator(EOD 배치)를 1회 실행."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.signals = EngineSignals()
        self.setTerminationEnabled(True)

    def run(self):
        self.signals.started.emit()
        try:
            from src.engine.orchestrator import Orchestrator
            orch = Orchestrator()
            orch.run()
        except Exception as e:
            logger.error(f"Orchestrator 실행 오류: {e}")
            self.signals.error.emit(str(e))
        finally:
            self.signals.stopped.emit()


class LegacyEngineWorker(QThread):
    """engine_legacy.TradingEngine(실시간)을 asyncio 루프에서 실행.

    2초 간격 폴링으로 상태/포지션/체결/후보를 시그널로 emit.
    """

    def __init__(self, mode: str = "paper", parent=None):
        super().__init__(parent)
        self._mode = mode
        self._engine = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self.signals = EngineSignals()

        self.signals.request_stop.connect(self._on_request_stop)
        self.signals.request_halt.connect(self._on_request_halt)
        self.signals.request_resume.connect(self._on_request_resume)

        self.setTerminationEnabled(True)

    def run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._run_engine())
        except Exception as e:
            logger.error(f"LegacyEngineWorker 오류: {e}")
            self.signals.error.emit(str(e))
        finally:
            if self._engine and getattr(self._engine, '_running', False):
                try:
                    self._loop.run_until_complete(self._engine.stop())
                except Exception:
                    pass
            try:
                pending = asyncio.all_tasks(self._loop)
                for t in pending:
                    t.cancel()
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
        from src.engine_legacy import TradingEngine
        self._engine = TradingEngine(mode=self._mode)
        await self._engine.start()
        self.signals.started.emit()

        while self._engine._running:
            self._emit_status()
            self._emit_positions()
            self._emit_trades()
            self._emit_candidates()
            await asyncio.sleep(2)

    def _on_request_stop(self):
        if self._engine and self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._engine.stop(), self._loop)

    def _on_request_halt(self):
        if self._engine and self._loop and self._loop.is_running():
            def _halt():
                self._engine.halt()
            asyncio.run_coroutine_threadsafe(
                asyncio.to_thread(_halt), self._loop
            )

    def _on_request_resume(self):
        if self._engine and self._loop and self._loop.is_running():
            def _resume():
                self._engine._risk_mgr.resume()
            asyncio.run_coroutine_threadsafe(
                asyncio.to_thread(_resume), self._loop
            )

    def _emit_status(self):
        if not self._engine:
            return
        self.signals.status_updated.emit({
            "mode": self._engine.mode,
            "running": self._engine._running,
            "halted": self._engine._risk_mgr.is_halted,
            "daily_pnl_pct": self._engine._risk_mgr.daily_pnl_pct,
            "mdd": self._engine._risk_mgr.current_mdd,
            "candidates": len(self._engine._candidates),
            "capital": self._engine._get_available_capital(),
            "max_positions": 4,
            "breadth": self._engine._breadth_value,
            "gate_open": self._engine._breadth_ok,
        })

    def _emit_positions(self):
        if not self._engine:
            return
        try:
            positions = self._engine._ds.get_open_positions()
            for pos in positions:
                code = pos.get("code", "")
                latest = self._engine._latest_prices.get(code)
                pos["current_price"] = latest if latest else pos.get("entry_price", 0)
                pos["entry_strategy"] = "TF"
                pos["quantity"] = pos.get("quantity", 0)
                pos["tp1_price"] = pos.get("target_price", 0)
                pos["tp1_triggered"] = bool(pos.get("partial_sold", 0))
                pos["atr_at_entry"] = self._engine._get_atr(code, pos.get("entry_price", 0))
                pos["highest_since_entry"] = pos.get("high_since_entry", 0)
            self.signals.positions_updated.emit(positions)
        except Exception:
            pass

    def _emit_trades(self):
        if not self._engine:
            return
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            trades = self._engine._ds.get_trades_by_date(today)
            self.signals.trades_updated.emit(trades)
        except Exception:
            pass

    def _emit_candidates(self):
        if not self._engine:
            return
        try:
            candidates = []
            for code in self._engine._candidates:
                c = self._engine._v23_entry_cache.get(code, {})
                candidates.append({
                    "code": code,
                    "name": c.get("name", code),
                    "price": int(c.get("close", 0)),
                    "date": datetime.now().strftime("%Y-%m-%d"),
                    "reason": (
                        f"trend state: ADX={c.get('adx', 0):.1f}, "
                        f"MA60_dist={c.get('ma60_dist', 0):+.1%}"
                    ),
                })
            self.signals.candidates_updated.emit(candidates)
        except Exception:
            pass

    @property
    def engine(self):
        return self._engine
