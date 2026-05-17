"""공유 청산 판단 모듈 (Phase A-4).

백테스터와 실전 엔진이 동일한 로직으로 청산 여부를 판단하도록
순수 함수(pure function) 인터페이스를 제공한다.

호출자 책임:
- ExitContext: 이미 계산된 지표 스냅샷을 전달.
- evaluate_exit 반환값(ExitReason)에 따라 수량/현금 처리는 호출자가 수행.
"""
from __future__ import annotations

from dataclasses import dataclass

from src.models import ExitReason
from src.strategy.dynamic_hold import DynamicHoldParams, compute_dynamic_max_hold
from src.utils.tick_size import adjust_price


@dataclass
class ExitContext:
    """청산 판단에 필요한 현재 상태 스냅샷."""

    entry_price: float
    day_low: float          # 해당 봉의 저가 (실전 틱: current_price)
    day_high: float         # 해당 봉의 고가 (실전 틱: current_price)
    stop_price: float       # 현재 손절가 (백테스트: 진입 SL; 실전: 트레일링 갱신된 값)
    initial_stop_price: float  # 진입 시 SL (SL vs 트레일링 구분용)
    target_price: float     # TP1 발동 가격 (0 = 비활성)
    tp2_price: float        # TP2 발동 가격 (0 = 비활성)
    high_since_entry: float  # 보유 중 최고가 (트레일링 기준점)
    atr_at_entry: float     # 진입 시점 ATR (트레일링 계산용)
    partial_sold: bool      # TP1 발동 여부
    partial_sold_2: bool    # TP2 발동 여부
    hold_days: int
    current_return: float = 0.0  # (current_price / entry_price) - 1
    prev_ma5: float | None = None
    prev_ma20: float | None = None
    curr_ma5: float | None = None
    curr_ma20: float | None = None
    current_adx: float = 0.0   # 현재 ADX (동적 보유기간용)
    entry_adx: float = 0.0     # 진입 시 ADX


@dataclass
class ExitParams:
    """청산 전략 파라미터."""

    max_hold_days: int
    trailing_atr_mult: float
    early_exit_enabled: bool = False
    early_exit_hold_days: int = 10
    early_exit_return_min: float = -0.02
    trend_exit_enabled: bool = True
    dynamic_hold: DynamicHoldParams | None = None


def evaluate_exit(ctx: ExitContext, params: ExitParams) -> ExitReason | None:
    """청산 사유를 반환한다. 청산 불필요 시 None.

    우선순위 (높음→낮음):
    1. SL / 트레일링스탑
    2. TP1 분할매도
    3. TP2 분할매도
    4. 추세이탈 (MA5 < MA20 골든→데드 크로스)
    5a. 조기 시간손절 (dead money)
    5b. 최대 보유일 초과

    순수 함수: DB/API 호출 없음. 모든 입력은 ExitContext에 포함.
    """
    # ── 1. 손절 / 트레일링 ──────────────────────────────────────
    # 트레일링스탑 후보: 최고가 − ATR × 배수
    if ctx.atr_at_entry > 0 and params.trailing_atr_mult > 0:
        trailing_candidate = adjust_price(
            ctx.high_since_entry - ctx.atr_at_entry * params.trailing_atr_mult,
            direction="up",
        )
    else:
        trailing_candidate = 0

    # 실효 손절가 = max(진입 SL 또는 실전 갱신된 stop_price, 트레일링 후보)
    effective_stop = max(ctx.stop_price, trailing_candidate)

    if ctx.day_low <= effective_stop:
        if effective_stop > ctx.initial_stop_price:
            return ExitReason.TRAILING_STOP
        return ExitReason.STOP_LOSS

    # ── 2. TP1 분할 매도 ─────────────────────────────────────────
    if (
        not ctx.partial_sold
        and ctx.target_price > 0
        and ctx.day_high >= ctx.target_price
    ):
        return ExitReason.PARTIAL_TARGET

    # ── 3. TP2 분할 매도 ─────────────────────────────────────────
    if (
        ctx.partial_sold
        and not ctx.partial_sold_2
        and ctx.tp2_price > 0
        and ctx.day_high >= ctx.tp2_price
    ):
        return ExitReason.PARTIAL_TARGET_2

    # ── 4. 추세이탈 (EOD MA 크로스) ──────────────────────────────
    if (
        params.trend_exit_enabled
        and ctx.hold_days > 1
        and ctx.prev_ma5 is not None
        and ctx.prev_ma20 is not None
        and ctx.curr_ma5 is not None
        and ctx.curr_ma20 is not None
        and ctx.prev_ma5 >= ctx.prev_ma20
        and ctx.curr_ma5 < ctx.curr_ma20
    ):
        return ExitReason.TREND_EXIT

    # ── 5a. 조기 시간손절 (dead money) ──────────────────────────
    if (
        params.early_exit_enabled
        and ctx.hold_days >= params.early_exit_hold_days
        and ctx.current_return < params.early_exit_return_min
    ):
        return ExitReason.EARLY_TIME_EXIT

    # ── 5b. 최대 보유일 (동적 보유기간 적용) ─────────────────────
    if params.dynamic_hold and params.dynamic_hold.enabled:
        effective_max = compute_dynamic_max_hold(
            params.dynamic_hold, ctx.current_adx, ctx.entry_adx
        )
    else:
        effective_max = params.max_hold_days
    if ctx.hold_days >= effective_max:
        return ExitReason.MAX_HOLD

    return None
