"""데이터 모델 정의.

매매 시스템 전반에서 사용하는 데이터 클래스 및 열거형.
금액은 항상 int(원), 비율은 float(0.0~1.0) 컨벤션을 따름.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


@dataclass
class Tick:
    """실시간 체결 틱 데이터."""

    code: str
    price: int
    volume: int
    timestamp: datetime


@dataclass
class Signal:
    """매매 신호."""

    code: str
    name: str
    signal_type: str  # "buy" | "sell"
    price: int
    score: float
    indicators: dict = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class Position:
    """보유 포지션."""

    id: int
    code: str
    name: str
    entry_date: str
    entry_price: int
    quantity: int
    stop_price: int
    target_price: int
    status: str = "open"  # "open" | "closed" | "selling"
    high_since_entry: int = 0
    hold_days: int = 0
    partial_sold: bool = False
    entry_strategy: str = ""  # 진입 전략명 (adaptive 국면 전환 시 올바른 exit 적용)
    updated_at: str = ""
    # v2.5 — 2단계 익절 + 초기 수량 보존
    initial_quantity: int = 0    # 진입 시점 수량 (TP2 사이징 기준)
    tp2_price: int = 0           # entry_price + atr × tp2_atr (TP2 발동 가격)
    partial_sold_2: bool = False # TP2 발동 여부
    # 청산 사유 정확 분류용: 진입 시점 SL 가격 (트레일링으로 상향되면 trailing_stop)
    initial_stop_price: int = 0
    # Phase A-4: 트레일링스탑 일관성용 진입 ATR
    atr_at_entry: float = 0.0
    # Phase B-4: 동적 보유기간용 진입 ADX
    entry_adx: float = 0.0
    # Phase B-5: 분할 매수(Scaling-in)
    scale_in_triggered: bool = False   # 2차 진입 완료 여부
    scale_in_price: int = 0            # 2차 진입 트리거 가격
    scale_in_target_qty: int = 0       # 2차 진입 목표 수량
    original_alloc: int = 0            # 최초 배분 금액 (2차 계산용)
    tranche_count: int = 1             # 현재 트랜치 수


@dataclass
class Order:
    """주문 요청."""

    code: str
    side: str  # "buy" | "sell"
    price: int
    quantity: int
    order_type: str  # "limit" | "market"
    hoga_type: str  # 호가 유형 코드


@dataclass
class OrderResult:
    """주문 실행 결과."""

    success: bool
    order_no: str
    message: str


@dataclass
class RiskCheckResult:
    """리스크 점검 결과."""

    approved: bool
    reason: str = ""


class ExitReason(Enum):
    """청산 사유."""

    STOP_LOSS = "stop_loss"
    TRAILING_STOP = "trailing_stop"
    TARGET_REACHED = "target_reached"
    PARTIAL_TARGET = "partial_target"
    PARTIAL_TARGET_2 = "partial_target_2"  # v2.5: TP2 분할(30%)
    MACD_DEAD = "macd_dead"
    FLOW_EXIT = "flow_exit"
    DISPARITY_EXIT = "disparity_exit"
    MAX_HOLD = "max_hold"
    TREND_EXIT = "trend_exit"        # v2.3: MA5 < MA20 EOD 전환
    EARLY_TIME_EXIT = "early_time_exit"  # Phase A-4: dead money 조기 청산


@dataclass
class TradeRecord:
    """매매 기록."""

    code: str
    name: str
    side: str
    price: int
    quantity: int
    amount: int
    fee: float
    tax: float
    pnl: float
    pnl_pct: float
    reason: str
    executed_at: str
