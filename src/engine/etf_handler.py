"""ETF 포지션 관리 — 스크리닝 시 판단, 동시호가에 체결.

스크리닝(08:30) 시 어제 KOSPI OHLCV로 IBS 판단
→ 동시호가(08:50)에 주문 제출 → 09:00 시초가 체결.
주식 진입(09:30~)과 시간대가 분리되어 현금 충돌 없음.
"""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from loguru import logger

from src.strategy.etf_mean_reversion import (
    ETFStrategyParams,
    check_ibs_entry,
    check_ibs_exit,
    compute_ibs,
)

if TYPE_CHECKING:
    from src.datastore import DataStore
    from src.broker.order_manager import OrderManager
    from src.notification.telegram_bot import TelegramBot


class ETFHandler:
    """ETF IBS 평균회귀 관리자."""

    def __init__(
        self,
        ds: "DataStore",
        order_mgr: "OrderManager",
        telegram: "TelegramBot",
        params: ETFStrategyParams,
        mode: str = "paper",
    ) -> None:
        self._ds = ds
        self._order_mgr = order_mgr
        self._telegram = telegram
        self._params = params
        self._mode = mode
        self._position: dict | None = None
        self._queued_action: str | None = None  # "buy" | "sell" | None
        self._queued_qty: int = 0

    @property
    def is_holding(self) -> bool:
        return self._position is not None

    @property
    def invested_amount(self) -> int:
        if not self._position:
            return 0
        return self._position["entry_price"] * self._position["qty"]

    @property
    def has_queued_order(self) -> bool:
        return self._queued_action is not None

    # ── 08:30 스크리닝 시 호출 ──

    def evaluate_at_screening(
        self, yesterday_ohlcv: dict, idle_cash: int
    ) -> None:
        """어제 KOSPI OHLCV로 IBS 판단 → 큐잉."""
        if not self._params.enabled:
            return

        self._queued_action = None
        self._queued_qty = 0

        h = float(yesterday_ohlcv.get("high", 0))
        lo = float(yesterday_ohlcv.get("low", 0))
        c = float(yesterday_ohlcv.get("close", 0))
        if h == 0 or lo == 0 or c == 0:
            logger.warning("ETF 스크리닝: KOSPI OHLCV 데이터 없음 — 스킵")
            return

        ibs = compute_ibs(h, lo, c)

        if self._position:
            self._position["hold_days"] += 1
            if check_ibs_exit(
                h, lo, c,
                threshold=self._params.ibs_exit,
                hold_days=self._position["hold_days"],
                max_hold=self._params.max_hold_days,
            ):
                self._queued_action = "sell"
                logger.info(
                    f"ETF 매도 큐: IBS={ibs:.3f}, "
                    f"보유 {self._position['hold_days']}일"
                )
            return

        # 미보유 → 진입 체크
        if idle_cash >= self._params.min_idle_cash:
            if check_ibs_entry(h, lo, c, threshold=self._params.ibs_entry):
                est_price = self._get_last_etf_close() or 1
                qty = int(idle_cash * 0.99 / max(1, est_price))
                if qty > 0:
                    self._queued_action = "buy"
                    self._queued_qty = qty
                    logger.info(
                        f"ETF 매수 큐: IBS={ibs:.3f}, "
                        f"{qty}주, 유휴 {idle_cash:,}원"
                    )

    # ── 08:50 동시호가 제출 ──

    async def submit_queued_order(self) -> None:
        """동시호가 주문 제출."""
        if self._queued_action is None:
            return

        code = self._params.etf_code

        try:
            if self._queued_action == "buy":
                await self._submit_buy(code)
            elif self._queued_action == "sell":
                await self._submit_sell(code)
        finally:
            self._queued_action = None
            self._queued_qty = 0

    async def _submit_buy(self, code: str) -> None:
        qty = self._queued_qty
        if qty <= 0:
            return

        est_price = self._get_last_etf_close() or 0

        if self._mode == "live":
            from src.broker.tr_codes import ORDER_BUY, PRICE_MARKET
            result = await self._order_mgr.execute_order(
                code, qty, 0, ORDER_BUY, PRICE_MARKET
            )
            if not result.success:
                logger.warning(f"ETF 매수 실패: {result.message}")
                return
            fill_price = est_price or 0
        else:
            from src.engine.paper_fill import simulate_fill_price
            fill_price = simulate_fill_price(
                est_price or 0,
                float((est_price or 0) * qty),
                1e12,  # ETF는 유동성 매우 높음
                side="buy",
            )

        today = datetime.now().strftime("%Y-%m-%d")
        self._position = {
            "code": code,
            "entry_price": fill_price,
            "qty": qty,
            "entry_date": today,
            "hold_days": 0,
        }
        self._ds.insert_etf_position(code, fill_price, qty, today)

        logger.bind(
            event="ETF_BUY",
            data={"code": code, "price": fill_price, "qty": qty},
        ).info("ETF 매수: KODEX200 {}주 @{:,}", qty, fill_price)
        self._telegram.send(f"📈 ETF 매수: KODEX200 {qty}주 @{fill_price:,}")

    async def _submit_sell(self, code: str) -> None:
        if not self._position:
            return

        pos = self._position
        qty = pos["qty"]
        est_price = self._get_last_etf_close() or pos["entry_price"]

        if self._mode == "live":
            from src.broker.tr_codes import ORDER_SELL, PRICE_MARKET
            result = await self._order_mgr.execute_order(
                code, qty, 0, ORDER_SELL, PRICE_MARKET
            )
            if not result.success:
                logger.warning(f"ETF 매도 실패: {result.message}")
                return
            fill_price = est_price
        else:
            from src.engine.paper_fill import simulate_fill_price
            fill_price = simulate_fill_price(
                est_price,
                float(est_price * qty),
                1e12,
                side="sell",
            )

        entry_p = pos["entry_price"]
        cost = (entry_p * qty + fill_price * qty) * self._params.cost_pct / 2
        pnl = (fill_price - entry_p) * qty - cost
        pnl_pct = (fill_price / entry_p - 1) if entry_p > 0 else 0.0
        today = datetime.now().strftime("%Y-%m-%d")

        self._ds.close_etf_position(
            code, fill_price, int(pnl), today, pos["hold_days"], "ibs_exit"
        )
        self._position = None

        logger.bind(
            event="ETF_SELL",
            data={
                "code": code,
                "price": fill_price,
                "pnl": int(pnl),
                "hold_days": pos["hold_days"],
            },
        ).info(
            "ETF 매도: KODEX200 {}주 @{:,} (PnL {:+,}원, {}일)",
            qty, fill_price, int(pnl), pos["hold_days"],
        )
        self._telegram.send(
            f"📉 ETF 매도: KODEX200 {qty}주 @{fill_price:,}\n"
            f"PnL: {int(pnl):+,}원 ({pnl_pct:+.1%})"
        )

    # ── 유틸 ──

    def _get_last_etf_close(self) -> int | None:
        """ETF 또는 KOSPI 지수 최근 종가."""
        from src.data_pipeline.db import get_data_db

        try:
            with get_data_db() as conn:
                row = conn.execute(
                    "SELECT close FROM daily_candles "
                    "WHERE ticker = ? ORDER BY date DESC LIMIT 1",
                    (self._params.etf_code,),
                ).fetchone()
            if row and row["close"]:
                return int(row["close"])
        except Exception:
            pass

        try:
            with get_data_db() as conn:
                row = conn.execute(
                    "SELECT close FROM index_daily "
                    "WHERE index_code = ? ORDER BY date DESC LIMIT 1",
                    (self._params.index_code,),
                ).fetchone()
            if row and row[0]:
                return int(row[0])
        except Exception:
            pass

        return None

    def restore_from_db(self) -> None:
        """재시작 시 DB에서 ETF 포지션 복원."""
        row = self._ds.get_open_etf_position()
        if row:
            self._position = {
                "code": row["code"],
                "entry_price": row["entry_price"],
                "qty": row["qty"],
                "entry_date": row["entry_date"],
                "hold_days": row.get("hold_days", 0),
            }
            logger.info(
                f"ETF 포지션 복원: {row['code']} "
                f"{row['qty']}주 @{row['entry_price']:,}"
            )
