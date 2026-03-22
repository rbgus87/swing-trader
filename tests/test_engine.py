"""TradingEngine 통합 테스트 — Mock 기반 (async).

모든 외부 의존성(KiwoomAPI, DataStore, TelegramBot 등)을 mock하여
TradingEngine의 조율 로직을 검증한다.

asyncio 기반 engine에 맞추어 모든 테스트가 async로 동작한다.
"""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

from src.broker.tr_codes import ORDER_BUY, ORDER_SELL, PRICE_LIMIT, PRICE_MARKET

import pytest

# loguru TRADE 커스텀 레벨 등록
from loguru import logger as _logger

try:
    _logger.level("TRADE", no=25, color="<yellow>")
except (TypeError, ValueError):
    pass  # 이미 등록된 경우

from src.models import (
    ExitReason,
    OrderResult,
    Position,
    RiskCheckResult,
    Signal,
    Tick,
    TradeRecord,
)


# ── Fixtures ──


@pytest.fixture
def mock_deps():
    """TradingEngine의 모든 의존성을 mock으로 교체."""
    with (
        patch("src.engine.DataStore") as MockDS,
        patch("src.engine.KiwoomAPI") as MockKiwoom,
        patch("src.engine.OrderManager") as MockOrderMgr,
        patch("src.engine.RealtimeDataManager") as MockRealtime,
        patch("src.engine.Screener") as MockScreener,
        patch("src.engine.RiskManager") as MockRiskMgr,
        patch("src.engine.PositionSizer") as MockSizer,
        patch("src.engine.StopManager") as MockStopMgr,
        patch("src.engine.TelegramBot") as MockTelegram,
        patch("src.engine.AsyncIOScheduler") as MockScheduler,
        patch("src.engine.config") as mock_config,
    ):
        # config mock
        mock_config.get_env.return_value = ""
        mock_config.mode = "paper"
        mock_config.is_paper = True
        mock_config.data = {}

        def config_get_side_effect(key, default=None):
            """테스트용 config.get — 기본값 반환."""
            return default

        mock_config.get.side_effect = config_get_side_effect

        # DataStore mock
        ds_instance = MockDS.return_value
        ds_instance.get_open_positions.return_value = []
        ds_instance.insert_position.return_value = 1
        ds_instance.record_trade.return_value = 1
        ds_instance.get_trades_by_date.return_value = []
        ds_instance.save_daily_performance.return_value = None

        # KiwoomAPI mock (async)
        kiwoom_instance = MockKiwoom.return_value
        kiwoom_instance._connected = False
        kiwoom_instance.on_tick_callback = None
        kiwoom_instance.on_chejan_callback = None
        kiwoom_instance.connect = AsyncMock(return_value=None)
        kiwoom_instance.disconnect = AsyncMock(return_value=None)

        # RiskManager mock
        risk_instance = MockRiskMgr.return_value
        risk_instance.is_halted = False
        risk_instance.daily_pnl_pct = 0.0
        risk_instance.current_mdd = 0.0
        risk_instance._daily_loss_limit = -0.03

        # StopManager mock
        stop_instance = MockStopMgr.return_value
        stop_instance.get_initial_stop.return_value = 9300
        stop_instance.update_trailing_stop.return_value = 9300
        stop_instance.is_stopped.return_value = False

        # PositionSizer mock
        sizer_instance = MockSizer.return_value
        sizer_instance.calculate.return_value = 1_000_000

        # OrderManager mock (async execute_order)
        order_instance = MockOrderMgr.return_value
        order_instance.execute_order = AsyncMock(
            return_value=OrderResult(
                success=True, order_no="ORD001", message="OK"
            )
        )

        # RealtimeDataManager mock (async subscribe/subscribe_list)
        realtime_instance = MockRealtime.return_value
        realtime_instance.subscribe = AsyncMock(return_value=None)
        realtime_instance.subscribe_list = AsyncMock(return_value=None)

        # TelegramBot mock (sync)
        telegram_instance = MockTelegram.return_value
        telegram_instance.send.return_value = True
        telegram_instance.send_buy_executed.return_value = True
        telegram_instance.send_sell_executed_profit.return_value = True
        telegram_instance.send_sell_executed_loss.return_value = True
        telegram_instance.send_halt_alert.return_value = True
        telegram_instance.send_system_error.return_value = True
        telegram_instance.send_daily_report.return_value = True

        # Scheduler mock
        scheduler_instance = MockScheduler.return_value

        yield {
            "ds": ds_instance,
            "kiwoom": kiwoom_instance,
            "order_mgr": order_instance,
            "realtime": realtime_instance,
            "screener": MockScreener.return_value,
            "risk_mgr": risk_instance,
            "sizer": sizer_instance,
            "stop_mgr": stop_instance,
            "telegram": telegram_instance,
            "scheduler": scheduler_instance,
            "config": mock_config,
        }


@pytest.fixture
def engine(mock_deps):
    """Mock 의존성으로 초기화된 TradingEngine (paper 모드)."""
    from src.engine import TradingEngine

    eng = TradingEngine(mode="paper")
    eng._positions_cache = None  # 매 테스트마다 캐시 리셋
    return eng


@pytest.fixture
def engine_live(mock_deps):
    """Mock 의존성으로 초기화된 TradingEngine (live 모드)."""
    from src.engine import TradingEngine

    eng = TradingEngine(mode="live")
    eng._positions_cache = None
    return eng


def _make_tick(code="005930", price=10000, volume=100):
    """테스트용 Tick 생성 헬퍼."""
    return Tick(code=code, price=price, volume=volume, timestamp=datetime.now())


def _make_position_dict(
    id=1,
    code="005930",
    entry_price=10000,
    quantity=10,
    stop_price=9300,
    target_price=10800,
    status="open",
):
    """테스트용 Position dict 생성 헬퍼."""
    return {
        "id": id,
        "code": code,
        "name": "삼성전자",
        "entry_date": "2026-03-15",
        "entry_price": entry_price,
        "quantity": quantity,
        "stop_price": stop_price,
        "target_price": target_price,
        "status": status,
        "high_since_entry": entry_price,
        "updated_at": "",
    }


# ── TradingEngine 생성 테스트 ──


class TestEngineCreation:
    """TradingEngine 생성 테스트."""

    async def test_create_paper_mode(self, engine, mock_deps):
        """paper 모드로 정상 생성."""
        assert engine.mode == "paper"
        assert engine._running is False
        mock_deps["ds"].connect.assert_called_once()
        mock_deps["ds"].create_tables.assert_called_once()

    async def test_create_live_mode(self, engine_live, mock_deps):
        """live 모드로 정상 생성."""
        assert engine_live.mode == "live"

    async def test_default_mode_from_config(self, mock_deps):
        """mode 미지정 시 config.mode 사용."""
        from src.engine import TradingEngine

        mock_deps["config"].mode = "paper"
        eng = TradingEngine()
        assert eng.mode == "paper"


# ── on_price_update 테스트 ──


class TestOnPriceUpdate:
    """실시간 시세 수신 콜백 테스트."""

    async def test_ignored_when_not_running(self, engine, mock_deps):
        """_running=False이면 무시."""
        engine._running = False
        tick = _make_tick()
        await engine.on_price_update(tick)
        mock_deps["ds"].get_open_positions.assert_not_called()

    async def test_ignored_when_halted(self, engine, mock_deps):
        """halt 상태에서 무시."""
        engine._running = True
        mock_deps["risk_mgr"].is_halted = True
        tick = _make_tick()
        await engine.on_price_update(tick)
        mock_deps["ds"].get_open_positions.assert_not_called()

    @patch("src.engine.is_market_open", return_value=False)
    async def test_ignored_outside_market_hours(self, mock_market, engine, mock_deps):
        """장 시간 외에는 무시."""
        engine._running = True
        tick = _make_tick()
        await engine.on_price_update(tick)
        mock_deps["ds"].get_open_positions.assert_not_called()

    @patch("src.engine.is_market_open", return_value=True)
    async def test_calls_exit_check_for_positions(self, mock_market, engine, mock_deps):
        """보유 종목에 대해 청산 조건 체크 호출."""
        engine._running = True
        mock_deps["ds"].get_open_positions.return_value = [
            _make_position_dict()
        ]
        tick = _make_tick(code="005930", price=10000)
        await engine.on_price_update(tick)
        mock_deps["ds"].get_open_positions.assert_called()

    @patch("src.engine.is_market_open", return_value=True)
    async def test_stop_loss_triggers_sell(self, mock_market, engine, mock_deps):
        """손절 조건 시 매도 실행."""
        engine._running = True
        pos_dict = _make_position_dict(stop_price=9500)
        mock_deps["ds"].get_open_positions.return_value = [pos_dict]
        mock_deps["stop_mgr"].is_stopped.return_value = True
        mock_deps["stop_mgr"].update_trailing_stop.return_value = 9500

        tick = _make_tick(code="005930", price=9000)
        await engine.on_price_update(tick)

        # 포지션이 closed로 업데이트
        mock_deps["ds"].update_position.assert_called()
        # 매매 기록
        mock_deps["ds"].record_trade.assert_called()

    @patch("src.engine.is_market_open", return_value=True)
    async def test_target_reached_triggers_sell(self, mock_market, engine, mock_deps):
        """목표가 도달 시 매도."""
        engine._running = True
        pos_dict = _make_position_dict(target_price=10800)
        mock_deps["ds"].get_open_positions.return_value = [pos_dict]
        mock_deps["stop_mgr"].is_stopped.return_value = False
        mock_deps["stop_mgr"].update_trailing_stop.return_value = 9300

        tick = _make_tick(code="005930", price=11000)
        await engine.on_price_update(tick)

        mock_deps["ds"].update_position.assert_called()
        mock_deps["ds"].record_trade.assert_called()

    @patch("src.engine.is_market_open", return_value=True)
    @patch("src.strategy.signals.calculate_signal_score", return_value=3.0)
    @patch("src.strategy.signals.calculate_indicators", side_effect=lambda df, **kw: df)
    async def test_entry_check_for_candidate(
        self, mock_calc_ind, mock_score, mock_market, engine, mock_deps
    ):
        """후보 종목에 대해 진입 조건 체크."""
        engine._running = True
        engine._candidates = ["005930"]
        # 전략의 check_realtime_entry를 mock
        engine._strategy.check_realtime_entry = MagicMock(return_value=True)
        mock_deps["risk_mgr"].pre_check.return_value = RiskCheckResult(
            approved=True
        )
        # 키별 config 반환값 분기
        def _config_side_effect(key, default=None):
            if key == "trading.entry_start_time":
                return "00:00"
            if key == "trading.entry_end_time":
                return "23:59"
            return default if default is not None else 10_000_000
        mock_deps["config"].get.side_effect = _config_side_effect
        # OHLCV 캐시 데이터 반환하도록 mock
        mock_deps["ds"].get_cached_ohlcv.return_value = [
            {"date": f"2026-03-{i:02d}", "open": 9900, "high": 10200,
             "low": 9800, "close": 10000, "volume": 100000}
            for i in range(1, 32)
        ]

        tick = _make_tick(code="005930", price=10000)
        await engine.on_price_update(tick)

        mock_deps["risk_mgr"].pre_check.assert_called()
        engine._strategy.check_realtime_entry.assert_called()


# ── _record_buy 테스트 ──


class TestRecordBuy:
    """매수 기록 테스트."""

    async def test_record_buy_creates_position_and_trade(self, engine, mock_deps):
        """매수 시 포지션과 매매기록 모두 생성."""
        mock_deps["config"].get.return_value = 0.08
        tick = _make_tick(code="005930", price=10000)

        await engine._record_buy(tick, qty=10)

        mock_deps["ds"].insert_position.assert_called_once()
        mock_deps["ds"].record_trade.assert_called_once()
        mock_deps["telegram"].send_buy_executed.assert_called_once()

    async def test_record_buy_position_has_stop_and_target(self, engine, mock_deps):
        """매수 기록의 포지션에 손절가/목표가 설정 확인."""
        mock_deps["config"].get.return_value = 0.08
        mock_deps["stop_mgr"].get_initial_stop.return_value = 9700

        tick = _make_tick(code="005930", price=10000)
        await engine._record_buy(tick, qty=5)

        call_args = mock_deps["ds"].insert_position.call_args
        pos = call_args[0][0]
        assert pos.stop_price == 9700
        assert pos.target_price == int(10000 * 1.08)


# ── _execute_sell 테스트 ──


class TestExecuteSell:
    """매도 실행 테스트."""

    async def test_sell_closes_position_and_records_trade(self, engine, mock_deps):
        """매도 시 포지션 종료 + 매매기록 생성."""
        pos = Position(
            id=1,
            code="005930",
            name="삼성전자",
            entry_date="2026-03-10",
            entry_price=10000,
            quantity=10,
            stop_price=9300,
            target_price=10800,
            high_since_entry=10000,
        )

        await engine._execute_sell(pos, price=10800, reason=ExitReason.TARGET_REACHED)

        mock_deps["ds"].update_position.assert_called_once_with(
            1, status="closed"
        )
        mock_deps["ds"].record_trade.assert_called_once()
        # 수익이므로 send_sell_executed_profit 호출
        mock_deps["telegram"].send_sell_executed_profit.assert_called_once()

    async def test_sell_loss_sends_loss_alert(self, engine, mock_deps):
        """손실 매도 시 손실 알림 발송."""
        pos = Position(
            id=2,
            code="005930",
            name="삼성전자",
            entry_date="2026-03-10",
            entry_price=10000,
            quantity=10,
            stop_price=9300,
            target_price=10800,
            high_since_entry=10000,
        )

        await engine._execute_sell(pos, price=9000, reason=ExitReason.STOP_LOSS)

        mock_deps["ds"].update_position.assert_called_once_with(
            2, status="closed"
        )
        mock_deps["telegram"].send_sell_executed_loss.assert_called_once()

    async def test_sell_trade_record_has_correct_pnl(self, engine, mock_deps):
        """매매기록의 손익 계산 검증."""
        pos = Position(
            id=3,
            code="005930",
            name="삼성전자",
            entry_date="2026-03-10",
            entry_price=10000,
            quantity=10,
            stop_price=9300,
            target_price=10800,
            high_since_entry=10000,
        )

        await engine._execute_sell(pos, price=11000, reason=ExitReason.TARGET_REACHED)

        call_args = mock_deps["ds"].record_trade.call_args
        trade = call_args[0][0]
        assert trade.side == "sell"
        assert trade.pnl == (11000 - 10000) * 10  # 10,000
        assert trade.pnl_pct == pytest.approx(0.1)
        assert trade.reason == "target_reached"


# ── Paper 모드 vs Live 모드 ──


class TestPaperVsLive:
    """Paper/Live 모드 차이 검증."""

    async def test_paper_mode_no_ocx_order_on_buy(self, engine, mock_deps):
        """paper 모드에서 주문 미호출 (매수)."""
        mock_deps["config"].get.return_value = 0.08
        tick = _make_tick(code="005930", price=10000)

        await engine._record_buy(tick, qty=10)

        # OrderManager.execute_order가 호출되지 않음
        mock_deps["order_mgr"].execute_order.assert_not_called()

    async def test_paper_mode_no_ocx_order_on_sell(self, engine, mock_deps):
        """paper 모드에서 주문 미호출 (매도)."""
        pos = Position(
            id=1,
            code="005930",
            name="삼성전자",
            entry_date="2026-03-10",
            entry_price=10000,
            quantity=10,
            stop_price=9300,
            target_price=10800,
            high_since_entry=10000,
        )

        await engine._execute_sell(pos, price=10800, reason=ExitReason.TARGET_REACHED)

        mock_deps["order_mgr"].execute_order.assert_not_called()

    async def test_live_mode_calls_order_on_sell(self, engine_live, mock_deps):
        """live 모드에서 주문 호출 (매도)."""
        pos = Position(
            id=1,
            code="005930",
            name="삼성전자",
            entry_date="2026-03-10",
            entry_price=10000,
            quantity=10,
            stop_price=9300,
            target_price=10800,
            high_since_entry=10000,
        )

        await engine_live._execute_sell(
            pos, price=10800, reason=ExitReason.TARGET_REACHED
        )

        mock_deps["order_mgr"].execute_order.assert_called_once()

    async def test_live_sell_failure_aborts(self, engine_live, mock_deps):
        """live 모드에서 주문 실패 시 포지션 open으로 복원."""
        mock_deps["order_mgr"].execute_order.return_value = OrderResult(
            success=False, order_no="", message="주문 실패"
        )

        pos = Position(
            id=1,
            code="005930",
            name="삼성전자",
            entry_date="2026-03-10",
            entry_price=10000,
            quantity=10,
            stop_price=9300,
            target_price=10800,
            high_since_entry=10000,
        )

        await engine_live._execute_sell(
            pos, price=10800, reason=ExitReason.TARGET_REACHED
        )

        # 주문 실패: selling → open 복원 (update_position 2회 호출)
        calls = mock_deps["ds"].update_position.call_args_list
        assert len(calls) == 2
        assert calls[0] == ((1,), {"status": "selling"})
        assert calls[1] == ((1,), {"status": "open"})
        # 주문 실패 시 매매기록 미생성
        mock_deps["ds"].record_trade.assert_not_called()


# ── halt / stop 테스트 ──


class TestHaltAndStop:
    """매매 중단/시스템 중지 테스트."""

    async def test_halt_calls_risk_halt_and_telegram(self, engine, mock_deps):
        """halt() 호출 시 RiskManager.halt + 텔레그램 알림."""
        engine.halt()
        mock_deps["risk_mgr"].halt.assert_called_once()
        mock_deps["telegram"].send_halt_alert.assert_called_once()

    async def test_stop_sets_running_false(self, engine, mock_deps):
        """stop() 호출 시 _running=False."""
        engine._running = True
        await engine.stop()
        assert engine._running is False
        mock_deps["scheduler"].shutdown.assert_called_once()
        mock_deps["ds"].close.assert_called()

    async def test_start_sets_running_true(self, engine, mock_deps):
        """start() 호출 시 _running=True, 스케줄러 시작."""
        mock_deps["config"].get.side_effect = lambda key, default=None: {
            "schedule.screening_time": "08:30",
            "schedule.daily_report_time": "16:00",
            "schedule.reconnect_time": "08:45",
        }.get(key, default)

        await engine.start()
        assert engine._running is True
        mock_deps["scheduler"].start.assert_called_once()
        mock_deps["kiwoom"].connect.assert_called()


# ── 장전 스크리닝 테스트 ──


class TestPreMarketScreening:
    """장전 스크리닝 테스트."""

    async def test_screening_success(self, engine, mock_deps):
        """스크리닝 성공 시 후보 등록 + 구독."""
        mock_deps["screener"].run_daily_screening.return_value = [
            "005930",
            "000660",
        ]

        await engine._pre_market_screening()

        assert engine._candidates == ["005930", "000660"]
        mock_deps["realtime"].subscribe_list.assert_called_once_with(
            ["005930", "000660"]
        )
        mock_deps["telegram"].send.assert_called()

    async def test_screening_failure_sends_error(self, engine, mock_deps):
        """스크리닝 실패 시 텔레그램 에러 알림."""
        mock_deps["screener"].run_daily_screening.side_effect = RuntimeError(
            "pykrx error"
        )

        await engine._pre_market_screening()

        mock_deps["telegram"].send_system_error.assert_called()


# ── 재연결 테스트 ──


class TestEnsureConnection:
    """키움 API 연결 확인/재연결 테스트."""

    async def test_reconnect_when_disconnected(self, engine, mock_deps):
        """연결 끊김 시 재연결 시도."""
        mock_deps["kiwoom"]._connected = False
        await engine._ensure_connection()
        mock_deps["kiwoom"].connect.assert_called()
        assert engine._reconnect_count == 1

    async def test_max_reconnect_exceeded(self, engine, mock_deps):
        """최대 재연결 횟수 초과 시 에러."""
        mock_deps["kiwoom"]._connected = False
        engine._reconnect_count = 5
        await engine._ensure_connection()
        mock_deps["telegram"].send_system_error.assert_called()

    async def test_no_reconnect_when_connected(self, engine, mock_deps):
        """연결 정상이면 재연결 안 함."""
        mock_deps["kiwoom"]._connected = True
        initial_count = engine._reconnect_count
        await engine._ensure_connection()
        assert engine._reconnect_count == initial_count


# ── 일일 리셋 테스트 ──


class TestDailyReport:
    """일간 리포트 테스트."""

    async def test_daily_report_sends_telegram(self, engine, mock_deps):
        """일간 리포트 시 텔레그램 전송."""
        mock_deps["ds"].get_trades_by_date.return_value = [
            {"side": "buy", "pnl": 0},
            {"side": "sell", "pnl": 5000},
        ]
        mock_deps["ds"].get_open_positions.return_value = [
            {"code": "005930"},
        ]
        mock_deps["config"].get.return_value = 1_000_000

        engine._daily_report()

        mock_deps["telegram"].send_daily_report.assert_called_once()
        call_kwargs = mock_deps["telegram"].send_daily_report.call_args
        # buy_count=1, sell_count=1, realized_pnl=5000
        assert call_kwargs[1]["buy_count"] == 1
        assert call_kwargs[1]["sell_count"] == 1
        assert call_kwargs[1]["realized_pnl"] == 5000

    async def test_daily_report_no_trades(self, engine, mock_deps):
        """거래 없는 날 리포트."""
        mock_deps["ds"].get_trades_by_date.return_value = []
        mock_deps["ds"].get_open_positions.return_value = []
        mock_deps["config"].get.return_value = 1_000_000

        engine._daily_report()

        mock_deps["telegram"].send_daily_report.assert_called_once()
        call_kwargs = mock_deps["telegram"].send_daily_report.call_args
        assert call_kwargs[1]["buy_count"] == 0
        assert call_kwargs[1]["sell_count"] == 0
        assert call_kwargs[1]["realized_pnl"] == 0


class TestDailyReset:
    """일일 리셋 테스트."""

    async def test_daily_reset_calls_risk_reset(self, engine, mock_deps):
        """일일 리셋 시 RiskManager.reset_daily 호출."""
        engine._daily_reset()
        mock_deps["risk_mgr"].reset_daily.assert_called_once()


# ── 체결 이벤트 테스트 ──


class TestOnChejan:
    """체결 이벤트 수신 테스트."""

    async def test_on_chejan_logs_data(self, engine):
        """체결 이벤트 수신 시 예외 없이 실행."""
        data = {"order_no": "ORD001", "code": "005930", "status": "체결"}
        await engine.on_chejan(data)


# ── dict_to_position 변환 테스트 ──


class TestDictToPosition:
    """dict -> Position 변환 테스트."""

    async def test_conversion(self, engine):
        """dict에서 Position 올바르게 변환."""
        d = _make_position_dict(
            id=5,
            code="000660",
            entry_price=120000,
            quantity=3,
            stop_price=111600,
            target_price=129600,
        )
        pos = engine._dict_to_position(d)
        assert pos.id == 5
        assert pos.code == "000660"
        assert pos.entry_price == 120000
        assert pos.quantity == 3
        assert pos.stop_price == 111600
        assert pos.target_price == 129600
        assert pos.status == "open"


# ── trailing stop 업데이트 테스트 ──


class TestTrailingStopUpdate:
    """트레일링스탑 업데이트 로직 테스트."""

    @patch("src.engine.is_market_open", return_value=True)
    async def test_trailing_stop_updated_in_db(self, mock_market, engine, mock_deps):
        """트레일링스탑 변경 시 DB 업데이트."""
        engine._running = True
        pos_dict = _make_position_dict(stop_price=9300)
        mock_deps["ds"].get_open_positions.return_value = [pos_dict]
        mock_deps["stop_mgr"].update_trailing_stop.return_value = 9500
        mock_deps["stop_mgr"].is_stopped.return_value = False

        tick = _make_tick(code="005930", price=10200)
        await engine.on_price_update(tick)

        mock_deps["ds"].update_position.assert_called_with(1, stop_price=9500)

    @patch("src.engine.is_market_open", return_value=True)
    async def test_trailing_stop_unchanged_no_db_call(
        self, mock_market, engine, mock_deps
    ):
        """트레일링스탑 변경 없으면 DB 업데이트 안 함."""
        engine._running = True
        pos_dict = _make_position_dict(stop_price=9300)
        mock_deps["ds"].get_open_positions.return_value = [pos_dict]
        mock_deps["stop_mgr"].update_trailing_stop.return_value = 9300
        mock_deps["stop_mgr"].is_stopped.return_value = False

        tick = _make_tick(code="005930", price=10000)
        await engine.on_price_update(tick)

        mock_deps["ds"].update_position.assert_not_called()


# ── 장마감 미체결 정리 테스트 ──


class TestPostMarketCleanup:
    """장마감 미체결 주문 정리 테스트."""

    async def test_cleanup_skipped_in_paper_mode(self, engine, mock_deps):
        """paper 모드에서는 정리 스킵."""
        await engine._post_market_cleanup()
        mock_deps["order_mgr"].cancel_all_pending.assert_not_called()

    async def test_cleanup_cancels_pending_orders(self, engine_live, mock_deps):
        """live 모드에서 미체결 전량 취소."""
        mock_deps["order_mgr"].cancel_all_pending = AsyncMock(return_value={})
        mock_deps["ds"]._lock = MagicMock()
        mock_deps["ds"]._lock.__enter__ = MagicMock()
        mock_deps["ds"]._lock.__exit__ = MagicMock()
        mock_deps["ds"].conn = MagicMock()
        mock_deps["ds"].conn.execute.return_value.fetchall.return_value = []

        await engine_live._post_market_cleanup()

        mock_deps["order_mgr"].cancel_all_pending.assert_awaited_once()

    async def test_cleanup_restores_selling_positions(self, engine_live, mock_deps):
        """selling 상태 포지션을 open으로 복원."""
        mock_deps["order_mgr"].cancel_all_pending = AsyncMock(return_value={})

        selling_pos = {"id": 1, "code": "005930", "status": "selling"}
        mock_deps["ds"]._lock = MagicMock()
        mock_deps["ds"]._lock.__enter__ = MagicMock()
        mock_deps["ds"]._lock.__exit__ = MagicMock()
        mock_deps["ds"].conn = MagicMock()
        mock_deps["ds"].conn.execute.return_value.fetchall.return_value = [selling_pos]

        await engine_live._post_market_cleanup()

        mock_deps["ds"].update_position.assert_any_call(1, status="open")


# ── 호가 유형 설정 테스트 ──


class TestHogaType:
    """config 기반 호가 유형 테스트."""

    async def test_default_market_order(self, engine, mock_deps):
        """기본값은 시장가."""
        from src.broker.tr_codes import PRICE_MARKET
        assert engine._get_hoga_type() == PRICE_MARKET

    async def test_limit_order_from_config(self, engine, mock_deps):
        """config에서 limit 설정 시 지정가."""
        from src.broker.tr_codes import PRICE_LIMIT
        mock_deps["config"].get.side_effect = lambda key, default=None: {
            "trading.order_type": "limit",
        }.get(key, default)

        assert engine._get_hoga_type() == PRICE_LIMIT

    async def test_live_sell_uses_configured_hoga(self, engine_live, mock_deps):
        """live 매도 시 config 호가 유형 사용."""
        from src.broker.tr_codes import PRICE_LIMIT

        mock_deps["config"].get.side_effect = lambda key, default=None: {
            "trading.order_type": "limit",
        }.get(key, default)

        pos = Position(
            id=1,
            code="005930",
            name="삼성전자",
            entry_date="2026-03-10",
            entry_price=10000,
            quantity=10,
            stop_price=9300,
            target_price=10800,
            high_since_entry=10000,
        )

        await engine_live._execute_sell(
            pos, price=10800, reason=ExitReason.TARGET_REACHED
        )

        call_args = mock_deps["order_mgr"].execute_order.call_args
        assert call_args[0][3] == ORDER_SELL
        assert call_args[0][4] == PRICE_LIMIT
        # 지정가이므로 price가 0이 아님
        assert call_args[0][2] == 10800
