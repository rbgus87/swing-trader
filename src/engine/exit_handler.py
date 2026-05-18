"""ExitHandlerMixin — 청산 처리 로직."""
from __future__ import annotations
import asyncio
from datetime import datetime
from loguru import logger
from src.models import ExitReason, Position, Tick, TradeRecord
from src.broker.tr_codes import ORDER_SELL, PRICE_LIMIT, PRICE_MARKET
from src.utils.config import config
from src.strategy.exit_evaluator import ExitContext, ExitParams, evaluate_exit
from src.utils.tick_size import adjust_price
from src.data_pipeline.db import get_data_db


class ExitHandlerMixin:
    """청산 관련 메서드."""

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
            # StopManager.update_trailing_stop은 pos.high_since_entry를 in-memory로만
            # 갱신하므로, 여기서 DB와 캐시에 persist해야 재시작 후에도 최고가가 유지된다.
            atr = self._get_atr(tick.code, pos.entry_price)
            old_high = pos.high_since_entry
            new_stop = self._stop_mgr.update_trailing_stop(pos, tick.price, atr)
            if pos.high_since_entry > old_high:
                self._ds.update_position(
                    pos.id, high_since_entry=pos.high_since_entry
                )
                pos_dict["high_since_entry"] = pos.high_since_entry
            if new_stop != pos.stop_price:
                self._ds.update_position(pos.id, stop_price=new_stop)
                pos_dict["stop_price"] = new_stop
                pos.stop_price = new_stop

            # Phase B-5: 2차 진입 체크 (청산 전에 먼저 확인)
            if (
                self._scaling_params.enabled
                and not pos.scale_in_triggered
                and pos.scale_in_price > 0
                and tick.price >= pos.scale_in_price
                and pos.tranche_count < self._scaling_params.max_tranches
            ):
                await self._apply_scale_in(pos, tick.price)

            # OHLCV 기반 종합 청산 판단
            exit_reason = self._evaluate_exit(pos, tick.price)
            if exit_reason:
                await self._execute_sell(pos, tick.price, exit_reason)

    def _evaluate_exit(self, pos: Position, current_price: int) -> ExitReason | None:
        """공유 evaluate_exit를 통해 청산 사유 판단 (틱마다 호출).

        추세이탈(MA5<MA20)은 EOD _v23_check_trend_exit에서 별도 처리.
        """
        ctx = ExitContext(
            entry_price=pos.entry_price,
            day_low=current_price,
            day_high=current_price,
            stop_price=pos.stop_price,
            initial_stop_price=pos.initial_stop_price or pos.stop_price,
            target_price=pos.target_price,
            tp2_price=pos.tp2_price,
            high_since_entry=pos.high_since_entry,
            atr_at_entry=pos.atr_at_entry,
            partial_sold=pos.partial_sold,
            partial_sold_2=pos.partial_sold_2,
            hold_days=pos.hold_days,
            current_return=(current_price / pos.entry_price - 1) if pos.entry_price else 0.0,
            # MA 값은 None → 추세이탈 체크 비활성 (EOD에서 별도 처리)
            prev_ma5=None,
            prev_ma20=None,
            curr_ma5=None,
            curr_ma20=None,
            current_adx=self._get_cached_adx(pos.code),
            entry_adx=pos.entry_adx,
        )
        return evaluate_exit(ctx, self._exit_params)

    async def _apply_scale_in(self, pos: Position, current_price: int) -> None:
        """Phase B-5: 2차 진입 반영 — 수량 추가 + 평균단가/SL 재계산."""
        from src.strategy.scaling import compute_scale_in_qty, compute_adjusted_stop

        scale_qty = compute_scale_in_qty(pos.original_alloc, current_price, self._scaling_params)
        if scale_qty <= 0:
            return

        # 현금 충분성 확인
        cost = scale_qty * current_price
        available = self._get_available_capital()
        if cost > available:
            logger.info(
                f"2차 매수 스킵 ({pos.code}): 현금 부족 "
                f"(필요 {cost:,} > 가용 {available:,})"
            )
            return

        # live 모드는 실제 주문
        if self.mode == "live":
            hoga = self._get_hoga_type()
            order_price = current_price if hoga == PRICE_LIMIT else 0
            result = await self._order_mgr.execute_order(
                pos.code, scale_qty, order_price, ORDER_BUY, hoga
            )
            if not result.success:
                logger.warning(f"2차 매수 주문 실패 ({pos.code}): {result.message}")
                return

        old_qty = pos.quantity
        new_qty = old_qty + scale_qty
        avg_price = (pos.entry_price * old_qty + current_price * scale_qty) // new_qty

        atr = pos.atr_at_entry or self._get_atr(pos.code, avg_price)
        if self._scaling_params.adjust_stop_on_scale:
            new_stop = compute_adjusted_stop(
                pos.entry_price, old_qty,
                current_price, scale_qty,
                float(atr), self._params.stop_loss_atr,
            )
            new_stop = adjust_price(new_stop, "down")
        else:
            new_stop = pos.stop_price

        self._ds.update_position(
            pos.id,
            quantity=new_qty,
            entry_price=avg_price,
            stop_price=new_stop,
            scale_in_triggered=1,
            tranche_count=pos.tranche_count + 1,
        )
        self._invalidate_positions_cache()

        logger.info(
            f"2차 매수: {pos.code} +{scale_qty}주 @{current_price:,} "
            f"(총 {new_qty}주, 평균 {avg_price:,}, SL {new_stop:,})"
        )
        self._telegram.send(
            f"📈 추가매수: {pos.code} +{scale_qty}주 @{current_price:,}\n"
            f"총 {new_qty}주, 평균가 {avg_price:,}, SL {new_stop:,}"
        )

    def _check_strategy_exit(self, pos: Position, current_price: int) -> ExitReason | None:
        """v2.7: 전략별 분기 제거 — _evaluate_exit에 통합. 호환성 위해 no-op."""
        return None

    async def _execute_sell(self, position: Position, price: int, reason: ExitReason):
        """매도 실행. 부분 매도(TP1/TP2) 시 일부만 매도하고 포지션 유지."""
        # 부분 매도 수량 결정
        # v2.5: TP1 = quantity × tp1_sell_ratio, TP2 = initial_quantity × tp2_sell_ratio
        is_partial = reason in (ExitReason.PARTIAL_TARGET, ExitReason.PARTIAL_TARGET_2)
        if is_partial:
            if reason == ExitReason.PARTIAL_TARGET:
                sell_ratio = self._params.tp1_sell_ratio
                base_qty = position.quantity
            else:  # PARTIAL_TARGET_2
                sell_ratio = self._params.tp2_sell_ratio
                # 초기 수량 기준 — TP1 후 잔여 quantity가 작아도 TP2 비율은 initial 기반
                base_qty = position.initial_quantity if position.initial_quantity > 0 else position.quantity
            sell_qty = max(1, int(base_qty * sell_ratio))
            # 잔여 수량이 0 이하가 되면 전량 매도로 전환
            if sell_qty >= position.quantity:
                is_partial = False
                sell_qty = position.quantity
        else:
            sell_qty = position.quantity

        # Paper 모드: 슬리피지 반영 체결가 계산
        if self.mode == "paper":
            from src.engine.paper_fill import simulate_fill_price
            avg_tv = self._get_avg_trading_value(position.code)
            price = simulate_fill_price(
                price,
                float(price * sell_qty),
                avg_tv,
                side="sell",
                params=self._paper_fill_params,
                slippage_params=self._slippage_params,
            )

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
            # 부분 매도: 수량 감소, 포지션 유지. TP1/TP2 플래그 분기.
            remaining_qty = position.quantity - sell_qty
            update_kwargs = {
                "quantity": remaining_qty,
                "status": "open",
            }
            if reason == ExitReason.PARTIAL_TARGET:
                update_kwargs["partial_sold"] = 1
                tag = "TP1"
            else:  # PARTIAL_TARGET_2
                update_kwargs["partial_sold_2"] = 1
                tag = "TP2"
            self._ds.update_position(position.id, **update_kwargs)
            logger.info(
                f"{tag} 부분 매도: {position.code} {sell_qty}주 매도, "
                f"{remaining_qty}주 잔여 (트레일링 계속)"
            )
        else:
            if self.mode == "paper":
                self._ds.update_position(position.id, status="closed")
        self._invalidate_positions_cache()
        self._sell_retry_counts.pop(position.id, None)

        # 손익 계산 (매도 수량 기준)
        pnl = (price - position.entry_price) * sell_qty
        pnl_pct = (price - position.entry_price) / position.entry_price
        _market = self._get_stock_market(position.code)
        fee = price * sell_qty * self._cost_model.sell_commission
        tax = price * sell_qty * self._cost_model.sell_tax(_market)

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

        logger.bind(
            event="SELL",
            data={
                "code": position.code,
                "price": price,
                "qty": sell_qty,
                "reason": reason.value,
                "pnl": float(pnl),
                "pnl_pct": round(pnl_pct, 4),
                "hold_days": position.hold_days,
                "entry_price": position.entry_price,
            },
        ).log("TRADE", "{} {} @{:,} ({}), PnL: {:+,}", sell_label, position.code, price, reason.value, pnl)

    async def _v23_check_trend_exit(self):
        """MA5 < MA20 교차 시 전량 청산 (EOD 일봉 확정 후)."""
        import pandas as pd
        from datetime import timedelta

        positions = self._ds.get_open_positions()
        if not positions:
            return

        today = datetime.now().strftime("%Y-%m-%d")
        to_close: list[tuple[Position, int]] = []

        for pos_dict in positions:
            if pos_dict.get("status") != "open":
                continue
            code = pos_dict["code"]
            try:
                with get_data_db() as conn:
                    rows = conn.execute(
                        "SELECT date, close FROM daily_candles "
                        "WHERE ticker = ? AND date <= ? "
                        "ORDER BY date DESC LIMIT 25",
                        (code, today),
                    ).fetchall()
                if len(rows) < 21:
                    continue
                closes = [r['close'] for r in reversed(rows)]
                ma5 = sum(closes[-5:]) / 5
                ma20 = sum(closes[-20:]) / 20
                prev_ma5 = sum(closes[-6:-1]) / 5
                prev_ma20 = sum(closes[-21:-1]) / 20
                if prev_ma5 >= prev_ma20 and ma5 < ma20:
                    pos = self._dict_to_position(pos_dict)
                    last_close = int(closes[-1])
                    to_close.append((pos, last_close))
            except Exception as e:
                logger.warning(f"추세 체크 실패 ({code}): {e}")

        if not to_close:
            logger.info("v2.7 추세 이탈 청산 대상 없음")
            return

        logger.info(f"v2.7 추세 이탈 청산: {len(to_close)}건")
        for pos, last_close in to_close:
            await self._execute_sell(pos, last_close, ExitReason.TREND_EXIT)
