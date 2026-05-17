"""src.engine — TradingEngine 핵심 조율자.

이 패키지는 src.trading_engine의 리팩터링 결과다.
TradingEngine은 6개 Mixin을 상속하여 책임별로 분리한다.
"""
"""TradingEngine — 전체 모듈 조율자.

장전 스크리닝 -> 장중 실시간 파이프라인 -> 체결 이벤트 -> 장마감 리포트.
asyncio 기반으로 동작한다.
"""

import asyncio
import inspect
import logging
import sqlite3
import time
from datetime import datetime
from typing import Literal

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from loguru import logger


class _InterceptHandler(logging.Handler):
    """표준 logging → loguru 브릿지.

    APScheduler 등 표준 logging 사용 라이브러리의 로그를 loguru로 전달.
    """

    def emit(self, record):
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        frame, depth = logging.currentframe(), 2
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1
        logger.opt(depth=depth, exception=record.exc_info).log(
            level, record.getMessage()
        )

from src.broker.kiwoom_api import KiwoomAPI
from src.broker.order_manager import OrderManager
from src.broker.tr_codes import ORDER_BUY, ORDER_SELL, PRICE_LIMIT, PRICE_MARKET
from src.data_pipeline.db import get_data_db, get_trade_db
from src.datastore import DataStore
from src.models import ExitReason, Position, Signal, Tick, TradeRecord
from src.notification.telegram_bot import TelegramBot
from src.risk.position_sizer import PositionSizer
from src.risk.risk_manager import RiskManager
from src.risk.stop_manager import StopManager
from src.strategy.exit_evaluator import ExitContext, ExitParams, evaluate_exit
from src.strategy.trend_following_v2 import (
    StrategyParams,
    calculate_indicators as calc_v23_indicators,
)
from src.utils.config import config
from src.utils.cost_model import CostModel
from src.utils.market_calendar import count_trading_days, is_market_open
from src.utils.tick_size import adjust_price


# v2.6 Universe 파라미터 (config에서 로드, 인스턴스 변수로 관리)
V23_EXCLUDED_TYPES = ('SPAC', 'REIT', 'FOREIGN', 'PREFERRED')
V23_BREADTH_GATE = 0.40

from src.engine.health_monitor import HealthMonitor
from src.engine.screener import ScreenerMixin
from src.engine.entry_handler import EntryHandlerMixin
from src.engine.exit_handler import ExitHandlerMixin
from src.engine.portfolio import PortfolioMixin
from src.engine.scheduler_jobs import SchedulerJobsMixin
from src.engine.polling import PollingMixin


class TradingEngine(
    ScreenerMixin,
    EntryHandlerMixin,
    ExitHandlerMixin,
    PortfolioMixin,
    SchedulerJobsMixin,
    PollingMixin,
):
    """매매 시스템 핵심 조율자."""

    def __init__(self, mode: Literal["paper", "live"] | None = None):
        # 표준 logging → loguru 브릿지 (APScheduler 에러 포착)
        logging.basicConfig(
            handlers=[_InterceptHandler()], level=logging.WARNING, force=True
        )

        self.mode = mode or config.mode
        self._running = False

        # 모듈 초기화
        self._ds = DataStore()
        self._ds.connect()
        self._ds.create_tables()

        # 키움 API (REST polling) — paper/live 모두 실전 서버 사용
        # paper 모드는 주문만 시뮬레이션하고, 시세는 REST polling으로 수신
        base_url = config.get("broker.base_url", "https://api.kiwoom.com")
        ws_url = config.get(
            "broker.ws_url",
            "wss://api.kiwoom.com:10000/api/dostk/websocket",
        )
        appkey = config.get_env("KIWOOM_APPKEY", "")
        secretkey = config.get_env("KIWOOM_SECRETKEY", "")

        self._kiwoom = KiwoomAPI(base_url, ws_url, appkey, secretkey)
        account = config.get_env("KIWOOM_ACCOUNT", "")
        self._order_mgr = OrderManager(self._kiwoom, account, is_paper=(self.mode == "paper"))
        if self.mode == "paper":
            logger.warning("PAPER 모드 — 실전 주문 비활성화")

        # REST polling 상태
        self._polling_task: asyncio.Task | None = None
        self._polling_interval: int = config.get(
            "schedule.polling_interval", 30
        )
        self._poll_stock_names: dict[str, str] = {}  # 폴링에서 수집한 종목명

        # v2.5 전략 파라미터 — config의 TP2/sizing 값 반영
        self._params = StrategyParams(
            tp1_sell_ratio=float(config.get("trend_following.tp1_sell_ratio", 0.30)),
            tp2_atr=float(config.get("trend_following.tp2_atr", 0.0)),
            tp2_sell_ratio=float(config.get("trend_following.tp2_sell_ratio", 0.0)),
        )
        self._sizing_mode = str(
            config.get("trend_following.sizing_mode", "equity")
        ).lower()

        self._max_positions = int(config.get("trading.max_positions", 6))
        self._min_position_amount = int(config.get("risk.min_position_amount", 300_000))
        self._mcap_threshold = int(config.get("universe_pool.min_market_cap", 3_000_000_000_000))
        self._cost_model = CostModel.from_config(config.data)

        self._risk_mgr = RiskManager(self._ds, config.data)
        self._sizer = PositionSizer()
        # StopManager는 v2.6 규칙으로 초기화:
        #   SL = entry - ATR×2.0, Trail = highest - ATR×4.0
        #   trailing_activate_pct=0 → 즉시 활성 (후퇴 금지 룰로 초기 SL 유지)
        self._stop_mgr = StopManager(
            stop_atr_mult=self._params.stop_loss_atr,
            max_stop_pct=config.get("risk.max_stop_pct", 0.10),
            trailing_atr_mult=self._params.trailing_atr,
            trailing_activate_pct=0.0,
        )
        self._telegram = TelegramBot()

        # 단일 전략 모드 (v2.6)
        self._strategy_type = "TF_v2.6"
        self._is_adaptive = False
        self._strategies = []
        self._strategy = None
        logger.info("전략 로드: TrendFollowing v2.6")

        # breadth 가드레일 캐시 (장전에 갱신)
        self._breadth_ok: bool = True
        self._breadth_value: float = 0.0

        # Phase B-3: 섹터 분산 제약
        from src.strategy.sector_constraint import SectorConstraint
        self._sector_constraint = SectorConstraint.from_config(
            config.data.get("trend_following", {})
        )
        self._industry_cache: dict[str, str] = {}

        # 17:00 일일 데이터 갱신 중복 실행 방지
        self._data_update_running: bool = False

        # v2.6 진입 후보 캐시 (스크리닝에서 사전 계산)
        self._v23_entry_cache: dict = {}

        # 시장 국면 판단기 (breadth로 대체되지만 레거시 호환 유지)
        from src.strategy.market_regime import MarketRegime
        self._market_regime = MarketRegime()

        # Phase B-4: 동적 보유기간
        from src.strategy.dynamic_hold import DynamicHoldParams
        self._dynamic_hold_params = DynamicHoldParams.from_config(
            config.data.get("trend_following", {})
        )

        # Phase B-5: 분할 매수
        from src.strategy.scaling import ScalingParams
        self._scaling_params = ScalingParams.from_config(
            config.data.get("trend_following", {})
        )

        # Phase A-4: 공유 청산 파라미터
        self._exit_params = ExitParams(
            max_hold_days=self._params.max_hold_days,
            trailing_atr_mult=self._params.trailing_atr,
            early_exit_enabled=bool(config.get("risk.early_exit_enabled", False)),
            early_exit_hold_days=int(config.get("risk.early_exit_hold_days", 10)),
            early_exit_return_min=float(config.get("risk.early_exit_return_min", -0.02)),
            trend_exit_enabled=bool(config.get("risk.trend_exit_enabled", True)),
            dynamic_hold=self._dynamic_hold_params,
        )

        # 상태
        self._candidates: list[str] = []  # 당일 매수 후보
        self._premarket_queue: list[dict] = []  # 동시호가 매수 큐
        self._initial_capital = config.get(
            "trading.initial_capital",
            config.get("backtest.initial_capital", 1_000_000),
        )
        self._positions_cache: list[dict] | None = None  # 포지션 메모리 캐시
        self._sell_retry_counts: dict[int, int] = {}  # 매도 재시도 카운터
        self._latest_prices: dict[str, int] = {}  # 종목별 최신 가격 캐시
        self._atr_cache: dict[str, float] = {}  # 종목별 ATR 캐시
        self._last_entry_check: dict[str, float] = {}  # 종목별 마지막 진입 체크 시각 (throttle)
        self._minute_ohlcv_cache: dict[str, "pd.DataFrame"] = {}  # 종목별 60분봉 캐시
        # partial_sold는 DB positions.partial_sold 컬럼으로 관리
        self._daily_trades_cache: list[dict] | None = None  # 당일 매매 기록 캐시
        self._entry_logged: dict[str, str] = {}  # 종목별 마지막 로그 사유 (반복 방지)

        # MDD 초기 자본 설정 + DB에서 마지막 세션 복원
        self._risk_mgr.set_initial_capital(float(self._initial_capital))
        self._restore_mdd_from_db()

        # 스케줄러
        self._scheduler = AsyncIOScheduler()

        # Paper 체결 시뮬레이터
        from src.engine.paper_fill import PaperFillParams
        from src.utils.slippage_model import SlippageParams
        self._paper_fill_params = PaperFillParams.from_config(config.data)
        self._slippage_params = SlippageParams.from_config(
            config.data.get("trend_following", {})
        )

        # 헬스모니터
        from src.utils.config import _get_app_dir
        self._health = HealthMonitor(
            heartbeat_file=_get_app_dir() / "logs" / "heartbeat.json",
            stale_threshold=int(config.get("monitoring.heartbeat_stale_seconds", 120)),
            poll_fail_threshold=int(config.get("monitoring.poll_fail_threshold", 5)),
            telegram=self._telegram,
        )

        # ETF IBS 평균회귀 전략 (유휴 현금 활용)
        from src.strategy.etf_mean_reversion import ETFStrategyParams
        from src.engine.etf_handler import ETFHandler
        self._etf_params = ETFStrategyParams.from_config(config.data)
        self._etf_handler = ETFHandler(
            ds=self._ds,
            order_mgr=self._order_mgr,
            telegram=self._telegram,
            params=self._etf_params,
            mode=self.mode,
        )
        self._etf_handler.restore_from_db()

        # 체결 콜백 등록 (REST polling 모드에서도 체결 이벤트는 별도 처리)
        self._kiwoom.on_chejan_callback = self.on_chejan

    def _make_safe_job(self, func, job_name: str):
        """스케줄 job 안전 래퍼 팩토리.

        AsyncIOScheduler가 직접 호출할 수 있는 async 함수를 반환.
        async/sync 함수 모두 지원.
        """
        async def wrapper():
            try:
                logger.info(f"스케줄 실행: {job_name}")
                if inspect.iscoroutinefunction(func):
                    await func()
                else:
                    await asyncio.to_thread(func)
                logger.info(f"스케줄 완료: {job_name}")
            except Exception as e:
                logger.error(f"스케줄 job 실패 [{job_name}]: {e}", exc_info=True)
                try:
                    self._telegram.send(f"⚠️ 스케줄 실패: {job_name}\n{str(e)[:200]}")
                except Exception:
                    pass
        return wrapper

    async def start(self):
        """메인루프 시작."""
        logger.info(f"TradingEngine 시작 (mode={self.mode})")
        self._running = True
        self._health.status.engine_started = True

        # 스케줄러 등록
        screening_time = config.get("schedule.screening_time", "08:30")
        report_time = config.get("schedule.daily_report_time", "16:00")
        polling_start_time = config.get("schedule.polling_start_time", "09:25")
        polling_stop_time = config.get("schedule.polling_stop_time", "15:35")

        _grace = 3600  # misfire_grace_time: PC 일시 중단 후 1시간 이내면 실행

        h, m = screening_time.split(":")
        self._scheduler.add_job(
            self._make_safe_job(self._pre_market_screening, "장전스크리닝"),
            "cron", hour=int(h), minute=int(m),
            misfire_grace_time=_grace,
        )

        premarket_queue_time = config.get("schedule.premarket_queue_time", "08:35")
        h, m = premarket_queue_time.split(":")
        self._scheduler.add_job(
            self._make_safe_job(self._queue_premarket_orders, "동시호가큐"),
            "cron", hour=int(h), minute=int(m),
            misfire_grace_time=_grace,
        )

        premarket_submit_time = config.get("schedule.premarket_submit_time", "08:50")
        h, m = premarket_submit_time.split(":")
        self._scheduler.add_job(
            self._make_safe_job(self._submit_premarket_orders, "동시호가제출"),
            "cron", hour=int(h), minute=int(m),
            misfire_grace_time=_grace,
        )

        h, m = report_time.split(":")
        self._scheduler.add_job(
            self._make_safe_job(self._daily_report, "일간리포트"),
            "cron", hour=int(h), minute=int(m),
            misfire_grace_time=_grace,
        )

        # 17:00 일일 데이터 갱신 (일봉/시총/지수 수집 + 시그널 생성)
        # 모든 원천(FDR/KRX/Yahoo)이 종가/마감 데이터를 반영한 뒤 실행.
        data_update_time = config.get("schedule.data_update_time", "17:00")
        h, m = data_update_time.split(":")
        self._scheduler.add_job(
            self._make_safe_job(self._daily_data_update, "일일데이터갱신"),
            "cron", hour=int(h), minute=int(m),
            misfire_grace_time=_grace,
        )

        h, m = polling_start_time.split(":")
        self._scheduler.add_job(
            self._make_safe_job(self._start_polling, "폴링시작"),
            "cron", hour=int(h), minute=int(m),
            misfire_grace_time=_grace,
        )

        h, m = polling_stop_time.split(":")
        self._scheduler.add_job(
            self._make_safe_job(self._stop_polling, "폴링중지"),
            "cron", hour=int(h), minute=int(m),
            misfire_grace_time=_grace,
        )

        # 미체결 주문 정리 (15:35 — 장 마감 5분 후)
        self._scheduler.add_job(
            self._make_safe_job(self._post_market_cleanup, "장후정리"),
            "cron", hour=15, minute=35,
            misfire_grace_time=_grace,
        )

        # 잔고 정합성 검사 (15:45 — post_market_cleanup 후, daily_report 전)
        reconcile_time = config.get("schedule.reconcile_time", "15:45")
        h, m = reconcile_time.split(":")
        self._scheduler.add_job(
            self._make_safe_job(self._reconcile_positions, "정합성검사"),
            "cron", hour=int(h), minute=int(m),
            misfire_grace_time=_grace,
        )

        # 저녁 조건검색 스케줄 (15:40 — 장마감 10분 후)
        # 다음 거래일 watchlist를 DB에 저장
        self._scheduler.add_job(
            self._make_safe_job(self._evening_watchlist_screening, "저녁스크리닝"),
            "cron", hour=15, minute=40,
            misfire_grace_time=_grace,
        )

        # 일일 리셋 (09:00)
        self._scheduler.add_job(
            self._make_safe_job(self._daily_reset, "일일리셋"),
            "cron", hour=9, minute=0,
            misfire_grace_time=_grace,
        )

        # 분기 watchlist 자동 갱신 (3/6/9/12월 1일 08:00)
        if config.get("watchlist_refresh.enabled", False):
            self._scheduler.add_job(
                self._make_safe_job(self._quarterly_watchlist_refresh, "WL갱신"),
                "cron", month="3,6,9,12", day=1, hour=8, minute=0,
                misfire_grace_time=_grace,
            )

        # 60분봉 갱신 — 비활성화 (golden_cross에서 60분봉 미사용, 5107608 커밋)
        # 재활성화하려면 아래 주석 해제
        # self._scheduler.add_job(
        #     self._refresh_minute_ohlcv, "cron",
        #     hour="10-15", minute=1,  # 정각 1분 후 (캔들 확정 대기)
        # )

        # 헬스체크 1분 주기 (config.monitoring.health_check_enabled)
        if config.get("monitoring.health_check_enabled", True):
            self._scheduler.add_job(
                self._make_safe_job(self._run_health_check, "헬스체크"),
                "interval", minutes=1,
                misfire_grace_time=60,
            )

        self._scheduler.start()

        # watchlist → 후보 등록 (polling 시작 전에 준비)
        # v2.6: 고정 watchlist 경로 폐기 → 스크리닝으로 Universe 동적 선정
        self._candidates = []
        logger.info("v2.6 모드 — 장전 스크리닝으로 후보 동적 선정")

        # 재시작 시 보유 포지션 high_since_entry 복구 (일봉 기반)
        try:
            self._sync_high_since_entry()
        except Exception as e:
            logger.warning(f"기동 시 high_since_entry 보정 실패: {e}")

        # 엔진 기동 즉시 v2.6 스크리닝 1회 실행 (스케줄 시각(08:30) 대기 없이 바로 후보 확보)
        try:
            await self._pre_market_screening()
        except Exception as e:
            logger.warning(f"기동 시 v2.6 스크리닝 실패 (cron에서 재시도됨): {e}")

        # 장 시간대이면 즉시 REST polling 시작 (프로그램이 장중에 시작된 경우)
        from src.utils.market_calendar import is_trading_day, now_kst
        kst_now = now_kst()
        ps_h, ps_m = polling_start_time.split(":")
        pe_h, pe_m = polling_stop_time.split(":")
        from datetime import time as dt_time
        polling_start_t = dt_time(int(ps_h), int(ps_m))
        polling_stop_t = dt_time(int(pe_h), int(pe_m))
        if is_trading_day(kst_now.date()) and polling_start_t <= kst_now.time() < polling_stop_t:
            await self._start_polling()

        # 서비스 시작 알림
        self._telegram.send_startup(self.mode)

    async def stop(self):
        """시스템 중지."""
        try:
            self._telegram.send_shutdown(self.mode)
        except Exception as e:
            logger.warning(f"종료 알림 전송 실패 (무시): {e}")
        self._running = False
        self._health.status.engine_started = False
        await self._stop_polling()
        self._scheduler.shutdown(wait=False)
        await self._kiwoom.disconnect()
        self._ds.close()
        logger.info("TradingEngine 중지")

    def halt(self):
        """매매 중단 (일일 한도 초과 등)."""
        self._risk_mgr.halt()
        self._telegram.send_halt_alert(self._risk_mgr.daily_pnl_pct)
        logger.warning("매매 중단 - 일일 한도 초과")

    # ── 장전 ──

    async def on_price_update(self, tick: Tick):
        """실시간 시세 수신 콜백."""
        if not self._running or self._risk_mgr.is_halted:
            return
        if not is_market_open():
            return

        # 틱 수신 기록 (헬스모니터)
        self._health.record_tick()

        # 첫 틱 수신 로깅 (종목별 1회)
        if tick.code not in self._latest_prices:
            name = self._poll_stock_names.get(tick.code, "")
            label = f"{tick.code} {name}" if name else tick.code
            logger.info(f"첫 틱 수신: {label} = {tick.price:,}원")

        # 최신 가격 캐시 갱신
        self._latest_prices[tick.code] = tick.price

        # 1. 보유 종목 손절/트레일링/목표가 체크
        await self._check_exit_conditions(tick)

        # 2. 일일 손익 업데이트 및 한도 체크
        self._update_daily_pnl(tick)

        # 3. 후보 종목 진입 조건 체크
        # v2.6 가드레일: breadth < 0.40이면 매수 전체 차단
        if not self._breadth_ok:
            if not getattr(self, "_regime_block_logged", False):
                logger.info(
                    f"진입 게이트: breadth {self._breadth_value:.0%} "
                    f"< {V23_BREADTH_GATE:.0%} — 매수 전체 차단"
                )
                self._regime_block_logged = True
            return
        self._regime_block_logged = False
        # 시간대 진입 제한: 장 초반 변동성/장 마감 갭 리스크 회피
        now_hm = datetime.now().strftime("%H:%M")
        entry_start = config.get("trading.entry_start_time", "09:30")
        entry_end = config.get("trading.entry_end_time", "14:30")
        if not (entry_start <= now_hm <= entry_end):
            if not getattr(self, "_time_block_logged", False):
                logger.info(f"진입 게이트: 시간대 밖 ({now_hm}, 허용={entry_start}~{entry_end})")
                self._time_block_logged = True
            return
        self._time_block_logged = False
        # 쓰로틀링: 같은 종목은 30초 간격으로만 진입 판단 (지표 계산 부하 방지)
        if tick.code in self._candidates:
            now = time.monotonic()
            last_check = self._last_entry_check.get(tick.code, 0)
            if now - last_check < 30:
                return
            held_codes = {p["code"] for p in self._get_cached_positions()}
            if tick.code not in held_codes:
                self._last_entry_check[tick.code] = now
                await self._check_entry_conditions(tick)

    async def on_chejan(self, data: dict):
        """체결 이벤트 수신 — selling 포지션의 최종 종료 처리."""
        logger.info(f"체결 이벤트: {data}")

        # selling 상태 포지션 중 체결된 종목 찾아서 closed 처리
        try:
            code = data.get("item", "") or data.get("code", "")
            if not code:
                return

            positions = self._ds.get_open_positions()
            # selling 상태인 포지션도 별도 조회
            selling_positions = self._ds.get_positions_by_code_and_status(
                code, "selling"
            )

            for pos_dict in selling_positions:
                self._ds.update_position(pos_dict["id"], status="closed")
                logger.info(f"체결 확인 → 포지션 종료: {code} (id={pos_dict['id']})")

            if selling_positions:
                self._invalidate_positions_cache()
                self._sell_retry_counts = {
                    k: v for k, v in self._sell_retry_counts.items()
                    if k not in {p["id"] for p in selling_positions}
                }
        except Exception as e:
            logger.error(f"체결 이벤트 처리 실패: {e}")
