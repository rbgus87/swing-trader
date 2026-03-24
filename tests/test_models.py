"""데이터 모델 생성 테스트."""

from datetime import datetime

from src.models import (
    ExitReason,
    Order,
    OrderResult,
    Position,
    RiskCheckResult,
    Signal,
    Tick,
    TradeRecord,
)


class TestTick:
    def test_tick_creation(self):
        now = datetime.now()
        tick = Tick(code="005930", price=70000, volume=100, timestamp=now)
        assert tick.code == "005930"
        assert tick.price == 70000
        assert tick.volume == 100
        assert tick.timestamp == now

    def test_tick_price_is_int(self):
        tick = Tick(code="005930", price=70000, volume=100, timestamp=datetime.now())
        assert isinstance(tick.price, int)


class TestSignal:
    def test_signal_creation(self):
        sig = Signal(
            code="005930",
            name="삼성전자",
            signal_type="buy",
            price=70000,
            score=0.85,
            indicators={"rsi": 45.0, "macd": 100.0},
        )
        assert sig.code == "005930"
        assert sig.name == "삼성전자"
        assert sig.signal_type == "buy"
        assert sig.price == 70000
        assert sig.score == 0.85
        assert sig.indicators["rsi"] == 45.0

    def test_signal_default_indicators(self):
        sig = Signal(
            code="005930",
            name="삼성전자",
            signal_type="buy",
            price=70000,
            score=0.5,
        )
        assert sig.indicators == {}


class TestPosition:
    def test_position_creation(self):
        pos = Position(
            id=1,
            code="005930",
            name="삼성전자",
            entry_date="2025-01-15",
            entry_price=70000,
            quantity=10,
            stop_price=65000,
            target_price=75000,
        )
        assert pos.id == 1
        assert pos.status == "open"
        assert pos.high_since_entry == 0
        assert pos.hold_days == 0

    def test_position_defaults(self):
        pos = Position(
            id=1,
            code="005930",
            name="삼성전자",
            entry_date="2025-01-15",
            entry_price=70000,
            quantity=10,
            stop_price=65000,
            target_price=75000,
        )
        assert pos.partial_sold is False
        assert pos.updated_at == ""


class TestOrder:
    def test_order_creation(self):
        order = Order(
            code="005930",
            side="buy",
            price=70000,
            quantity=10,
            order_type="limit",
            hoga_type="00",
        )
        assert order.code == "005930"
        assert order.side == "buy"
        assert order.price == 70000
        assert isinstance(order.price, int)


class TestOrderResult:
    def test_order_result_success(self):
        result = OrderResult(success=True, order_no="0001234", message="체결완료")
        assert result.success is True
        assert result.order_no == "0001234"

    def test_order_result_failure(self):
        result = OrderResult(success=False, order_no="", message="잔고부족")
        assert result.success is False


class TestRiskCheckResult:
    def test_approved(self):
        result = RiskCheckResult(approved=True)
        assert result.approved is True
        assert result.reason == ""

    def test_rejected(self):
        result = RiskCheckResult(approved=False, reason="일일 손실 한도 초과")
        assert result.approved is False
        assert result.reason == "일일 손실 한도 초과"


class TestExitReason:
    def test_enum_values(self):
        assert ExitReason.STOP_LOSS.value == "stop_loss"
        assert ExitReason.TRAILING_STOP.value == "trailing_stop"
        assert ExitReason.TARGET_REACHED.value == "target_reached"
        assert ExitReason.MACD_DEAD.value == "macd_dead"
        assert ExitReason.MAX_HOLD.value == "max_hold"

    def test_enum_count(self):
        assert len(ExitReason) == 6


class TestTradeRecord:
    def test_trade_record_creation(self):
        trade = TradeRecord(
            code="005930",
            name="삼성전자",
            side="buy",
            price=70000,
            quantity=10,
            amount=700000,
            fee=105.0,
            tax=0.0,
            pnl=0.0,
            pnl_pct=0.0,
            reason="signal",
            executed_at="2025-01-15 09:30:00",
        )
        assert trade.amount == 700000
        assert isinstance(trade.price, int)
        assert isinstance(trade.fee, float)
