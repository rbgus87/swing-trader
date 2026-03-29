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
    MACD_DEAD = "macd_dead"
    FLOW_EXIT = "flow_exit"
    MAX_HOLD = "max_hold"


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
