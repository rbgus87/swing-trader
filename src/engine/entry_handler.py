"""EntryHandlerMixin — 진입 처리 로직."""
from __future__ import annotations
import asyncio
from datetime import datetime
from loguru import logger
from src.models import ExitReason, Position, Signal, Tick, TradeRecord
from src.broker.tr_codes import ORDER_BUY, PRICE_LIMIT, PRICE_MARKET
from src.utils.config import config
from src.data_pipeline.db import get_data_db
from src.utils.market_calendar import count_trading_days
from src.utils.tick_size import adjust_price


class EntryHandlerMixin:
    """진입 관련 메서드."""

    async def _check_entry_conditions(self, tick: Tick):
        """v2.6 진입 — 스크리닝에서 확정된 후보만 시가 매수."""
        name = self._poll_stock_names.get(tick.code, tick.code)

        cache = self._v23_entry_cache.get(tick.code)
        if not cache or not cache.get('entry_ready'):
            return

        # 리스크 사전 체크
        signal = Signal(
            code=tick.code, name=name, signal_type="buy",
            price=tick.price, score=cache.get('adx', 0.0),
        )
        risk_result = self._risk_mgr.pre_check(signal)
        if not risk_result.approved:
            reason_key = f"리스크:{risk_result.reason}"
            if self._entry_logged.get(tick.code) != reason_key:
                logger.info(f"진입 차단 ({name}): 리스크 — {risk_result.reason}")
                self._entry_logged[tick.code] = reason_key
            return

        # 이미 보유 중인지 확인
        held = {p['code'] for p in self._get_cached_positions()}
        if tick.code in held:
            return

        # 섹터 분산 제약
        if self._sector_constraint.enabled:
            candidate_industry = cache.get('industry') or self._get_stock_industry(tick.code)
            held_industries: dict[str, int] = {}
            for p in self._get_cached_positions():
                if p.get('status') == 'open':
                    ind = self._get_stock_industry(p['code'])
                    held_industries[ind] = held_industries.get(ind, 0) + 1
            if held_industries.get(candidate_industry, 0) >= self._sector_constraint.max_per_sector:
                reason_key = f"섹터:{candidate_industry}"
                if self._entry_logged.get(tick.code) != reason_key:
                    logger.info(
                        f"진입 차단 ({name}): 동일 업종 최대 보유 "
                        f"({candidate_industry}, max={self._sector_constraint.max_per_sector})"
                    )
                    self._entry_logged[tick.code] = reason_key
                return

        # 포지션 사이징 (v2.5): equity / max_positions (cash로 상한)
        alloc = self._calculate_alloc()
        if alloc < self._min_position_amount:
            reason_key = "자본부족"
            if self._entry_logged.get(tick.code) != reason_key:
                logger.info(
                    f"진입 차단 ({name}): 자본 부족 "
                    f"(alloc={alloc:,} < {self._min_position_amount:,}, "
                    f"mode={self._sizing_mode})"
                )
                self._entry_logged[tick.code] = reason_key
            return

        # Phase B-5: 분할 매수 — 1차 진입 수량 계산
        from src.strategy.scaling import compute_first_entry_qty
        qty = compute_first_entry_qty(alloc, tick.price, self._scaling_params)
        if qty <= 0:
            return

        # 주문 실행
        if self.mode == "live":
            hoga = self._get_hoga_type()
            order_price = tick.price if hoga == PRICE_LIMIT else 0
            result = await self._order_mgr.execute_order(
                tick.code, qty, order_price, ORDER_BUY, hoga
            )
            if result.success:
                await self._record_buy(tick, qty, "TF_v2.6", original_alloc=alloc)
        elif self.mode == "paper":
            from src.engine.paper_fill import simulate_fill_price
            avg_tv = float(cache.get('avg_trading_value_20', 1e10)) if cache else 1e10
            fill_price = simulate_fill_price(
                tick.price,
                float(tick.price * qty),
                avg_tv,
                side="buy",
                params=self._paper_fill_params,
                slippage_params=self._slippage_params,
            )
            from src.models import Tick as _Tick
            fill_tick = _Tick(
                code=tick.code,
                price=fill_price,
                volume=tick.volume,
                timestamp=tick.timestamp,
            )
            await self._record_buy(fill_tick, qty, "TF_v2.6", original_alloc=alloc)
            if fill_price != tick.price:
                logger.bind(
                    event="PAPER_BUY",
                    data={
                        "code": tick.code,
                        "market_price": tick.price,
                        "fill_price": fill_price,
                        "slippage": fill_price - tick.price,
                    },
                ).info(
                    "Paper 매수 체결: {} @{:,} (시장가 {:,}, 슬리피지 {:+,})",
                    tick.code, fill_price, tick.price, fill_price - tick.price,
                )

        # 중복 매수 방지
        self._v23_entry_cache.pop(tick.code, None)
        if tick.code in self._candidates:
            try:
                self._candidates.remove(tick.code)
            except ValueError:
                pass
        self._entry_logged.pop(tick.code, None)
        return

    async def _record_buy(
        self,
        tick: Tick,
        qty: int,
        strategy_name: str = "",
        original_alloc: int = 0,
    ):
        """매수 기록 — v2.6 stop/target 직접 계산."""
        # ATR: 스크리닝 캐시 우선, 없으면 _get_atr 폴백
        cache = self._v23_entry_cache.get(tick.code)
        if cache and cache.get('atr'):
            atr = cache['atr']
        else:
            atr = self._get_atr(tick.code, tick.price)

        # v2.5: SL = entry - ATR×2.0, TP1 = entry + ATR×2.0, TP2 = entry + ATR×4.0
        stop_price = adjust_price(tick.price - atr * self._params.stop_loss_atr, "down")
        target_price = adjust_price(tick.price + atr * self._params.take_profit_atr, "up")
        tp2_price = (
            adjust_price(tick.price + atr * self._params.tp2_atr, "up")
            if self._params.tp2_atr > 0
            else 0
        )
        name = self._get_stock_name(tick.code)

        # Phase B-5: 분할 매수 메타
        from src.strategy.scaling import compute_scale_in_trigger, compute_scale_in_qty
        scaling = self._scaling_params
        if scaling.enabled and atr > 0:
            scale_in_price = compute_scale_in_trigger(tick.price, float(atr), scaling)
            scale_in_target_qty = compute_scale_in_qty(
                original_alloc or (tick.price * qty), tick.price, scaling
            )
        else:
            scale_in_price = 0
            scale_in_target_qty = 0

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
            entry_strategy=strategy_name,
            initial_quantity=qty,
            tp2_price=tp2_price,
            initial_stop_price=stop_price,
            atr_at_entry=float(atr),
            entry_adx=float(cache.get('adx', 25.0)) if cache else 25.0,
            scale_in_triggered=False,
            scale_in_price=scale_in_price,
            scale_in_target_qty=scale_in_target_qty,
            original_alloc=original_alloc or (tick.price * qty),
            tranche_count=1,
        )
        self._ds.insert_position(pos)
        self._invalidate_positions_cache()

        # polling 루프가 보유 종목을 자동으로 포함하므로 별도 구독 불필요

        trade = TradeRecord(
            code=tick.code,
            name=name,
            side="buy",
            price=tick.price,
            quantity=qty,
            amount=tick.price * qty,
            fee=tick.price * qty * self._cost_model.buy_commission,
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
        logger.bind(
            event="BUY",
            data={
                "code": tick.code,
                "name": name,
                "price": tick.price,
                "qty": qty,
                "alloc": original_alloc or (tick.price * qty),
                "strategy": strategy_name,
                "atr": float(atr),
                "composite_score": float(cache.get("composite_score", 0.0)) if cache else 0.0,
            },
        ).log("TRADE", "매수: {} {}주 @{:,}", name, qty, tick.price)
