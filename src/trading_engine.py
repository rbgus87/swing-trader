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
from src.strategy.trend_following_v2 import (
    StrategyParams,
    calculate_indicators as calc_v23_indicators,
)
from src.utils.config import config
from src.utils.market_calendar import is_market_open


# v2.3 Universe 파라미터
V23_MCAP_THRESHOLD = 3_000_000_000_000
V23_EXCLUDED_TYPES = ('SPAC', 'REIT', 'FOREIGN', 'PREFERRED')
V23_BREADTH_GATE = 0.40
V23_MAX_POSITIONS = 4
V23_MIN_POSITION_AMOUNT = 300_000
V23_POSITION_RATIO = 0.25


class TradingEngine:
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

        # v2.3 전략 파라미터 (단일 전략)
        self._params = StrategyParams()

        self._risk_mgr = RiskManager(self._ds, config.data)
        self._sizer = PositionSizer()
        # StopManager는 v2.3 규칙으로 초기화:
        #   SL = entry - ATR×2.0, Trail = highest - ATR×4.0
        #   trailing_activate_pct=0 → 즉시 활성 (후퇴 금지 룰로 초기 SL 유지)
        self._stop_mgr = StopManager(
            stop_atr_mult=self._params.stop_loss_atr,
            max_stop_pct=config.get("risk.max_stop_pct", 0.10),
            trailing_atr_mult=self._params.trailing_atr,
            trailing_activate_pct=0.0,
        )
        self._telegram = TelegramBot()

        # 단일 전략 모드 (v2.3)
        self._strategy_type = "TF_v2.3"
        self._is_adaptive = False
        self._strategies = []
        self._strategy = None
        logger.info("전략 로드: TrendFollowing v2.3")

        # breadth 가드레일 캐시 (장전에 갱신)
        self._breadth_ok: bool = True
        self._breadth_value: float = 0.0

        # 17:00 일일 데이터 갱신 중복 실행 방지
        self._data_update_running: bool = False

        # v2.3 진입 후보 캐시 (스크리닝에서 사전 계산)
        self._v23_entry_cache: dict = {}

        # 시장 국면 판단기 (breadth로 대체되지만 레거시 호환 유지)
        from src.strategy.market_regime import MarketRegime
        self._market_regime = MarketRegime()

        # 상태
        self._candidates: list[str] = []  # 당일 매수 후보
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

        self._scheduler.start()

        # watchlist → 후보 등록 (polling 시작 전에 준비)
        # v2.3: 고정 watchlist 경로 폐기 → 스크리닝으로 Universe 동적 선정
        self._candidates = []
        logger.info("v2.3 모드 — 장전 스크리닝으로 후보 동적 선정")

        # 재시작 시 보유 포지션 high_since_entry 복구 (일봉 기반)
        try:
            self._sync_high_since_entry()
        except Exception as e:
            logger.warning(f"기동 시 high_since_entry 보정 실패: {e}")

        # 엔진 기동 즉시 v2.3 스크리닝 1회 실행 (스케줄 시각(08:30) 대기 없이 바로 후보 확보)
        try:
            await self._pre_market_screening()
        except Exception as e:
            logger.warning(f"기동 시 v2.3 스크리닝 실패 (cron에서 재시도됨): {e}")

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

    async def _pre_market_screening(self, _retry: int = 0):
        """장전 스크리닝 — v2.3 상태 기반 추세추종 후보 생성 (08:30)."""
        max_retries = 3

        # 토큰 갱신
        try:
            await self._ensure_connection()
        except Exception as e:
            logger.warning(f"스크리닝 전 토큰 갱신 실패: {e}")

        try:
            await asyncio.to_thread(self._v23_screen_universe)
        except Exception as e:
            logger.error(f"v2.3 스크리닝 실패 (시도 {_retry + 1}/{max_retries + 1}): {e}")
            if _retry < max_retries:
                delay = 60 * (_retry + 1)
                logger.info(f"스크리닝 재시도 예약: {delay}초 후")
                await asyncio.sleep(delay)
                await self._pre_market_screening(_retry=_retry + 1)
            else:
                self._telegram.send_system_error(
                    str(e), "engine._v23_screen_universe",
                    f"최대 재시도({max_retries}회) 초과",
                )
        return

    def _v23_screen_universe(self):
        """v2.3 진입 조건으로 Universe를 스캔 → self._candidates + self._v23_entry_cache 갱신."""
        import pandas as pd
        from datetime import timedelta

        today = datetime.now().strftime("%Y-%m-%d")
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        params = self._params

        # 1. KOSPI / KOSDAQ 20일 수익률 (v2.4 시장별 상대강도용)
        def _fetch_index_ret(index_code: str) -> float | None:
            try:
                with get_data_db() as conn:
                    idx_rows = conn.execute(
                        "SELECT date, close FROM index_daily "
                        "WHERE index_code = ? AND date <= ? "
                        "ORDER BY date DESC LIMIT ?",
                        (index_code, today, params.relative_strength_period + 5),
                    ).fetchall()
                if len(idx_rows) >= params.relative_strength_period + 1:
                    return (
                        idx_rows[0]['close']
                        / idx_rows[params.relative_strength_period]['close']
                    ) - 1.0
            except Exception as e:
                logger.warning(f"{index_code} index_daily 조회 실패: {e}")
            return None

        kospi_ret_n = _fetch_index_ret('KOSPI')
        kosdaq_ret_n = _fetch_index_ret('KOSDAQ')
        if kospi_ret_n is not None:
            logger.info(f"KOSPI 20d return: {kospi_ret_n:+.2%}")
        if kosdaq_ret_n is not None:
            logger.info(f"KOSDAQ 20d return: {kosdaq_ret_n:+.2%}")

        # 2. 가드레일: breadth (MA200 위 종목 비율)
        breadth = self._compute_breadth(today)
        self._breadth_value = breadth
        self._breadth_ok = breadth >= V23_BREADTH_GATE
        logger.info(
            f"breadth={breadth:.1%} "
            f"gate={'OPEN' if self._breadth_ok else 'CLOSED'} "
            f"(>= {V23_BREADTH_GATE:.0%})"
        )

        # 3. Universe 구축 (어제 기준 — 스크리닝 시점에는 당일 시총 미확정)
        with get_data_db() as conn:
            universe_row = conn.execute(
                """
                SELECT DISTINCT m.ticker, s.name, s.market
                FROM market_cap_history m
                JOIN stocks s ON m.ticker = s.ticker
                WHERE m.date = (
                    SELECT MAX(date) FROM market_cap_history WHERE date < ?
                )
                  AND m.market_cap >= ?
                  AND s.stock_type NOT IN (?, ?, ?, ?)
                  AND (s.delisted_date IS NULL OR s.delisted_date > ?)
                """,
                (today, V23_MCAP_THRESHOLD, *V23_EXCLUDED_TYPES, today),
            ).fetchall()
        universe = [(r['ticker'], r['name'], r['market']) for r in universe_row]
        logger.info(f"v2.3 Universe: {len(universe)}종목")

        # 4. 각 종목 일봉 로드 + 조건 체크
        candidates = {}
        with get_data_db() as conn:
            for ticker, name, market in universe:
                rows = conn.execute(
                    "SELECT date, open, high, low, close, volume "
                    "FROM daily_candles WHERE ticker = ? AND date <= ? "
                    "ORDER BY date DESC LIMIT 150",
                    (ticker, today),
                ).fetchall()
                if len(rows) < params.ma_long + 5:
                    continue

                df = pd.DataFrame([dict(r) for r in reversed(rows)])
                df['date'] = pd.to_datetime(df['date'])
                df = calc_v23_indicators(df, params)
                if df.empty:
                    continue
                t = df.iloc[-1]

                req = ['ma20', 'ma60', 'ma120', 'ma60_slope', 'ma60_dist',
                       'atr', 'adx', 'macd_hist', 'avg_volume_5',
                       'avg_volume_20', 'avg_trading_value_20', 'stock_ret_n']
                if any(pd.isna(t.get(k)) for k in req):
                    continue
                if t['atr'] <= 0 or t['close'] <= 0:
                    continue
                if not (t['close'] > t['ma20'] > t['ma60'] > t['ma120']):
                    continue
                if t['ma60_slope'] <= 0:
                    continue
                if not (params.ma60_position_min <= t['ma60_dist']
                        <= params.ma60_position_max):
                    continue
                if t['macd_hist'] <= 0:
                    continue
                if t['avg_volume_5'] <= t['avg_volume_20']:
                    continue
                if t['adx'] < params.adx_threshold:
                    continue
                if t['avg_trading_value_20'] < params.min_trading_value:
                    continue
                atr_ratio = t['atr'] / t['close']
                if not (params.atr_price_min <= atr_ratio
                        <= params.atr_price_max):
                    continue
                # 상대강도 (v2.4 시장별 분기): KOSDAQ→KOSDAQ, 그 외→KOSPI
                bench_ret = kosdaq_ret_n if market == 'KOSDAQ' else kospi_ret_n
                if bench_ret is not None:
                    rs = t['stock_ret_n'] - bench_ret
                    if rs < params.relative_strength_threshold:
                        continue

                candidates[ticker] = {
                    'atr': float(t['atr']),
                    'adx': float(t['adx']),
                    'ma60_dist': float(t['ma60_dist']),
                    'macd_hist': float(t['macd_hist']),
                    'close': float(t['close']),
                    'entry_ready': True,
                    'name': name,
                }
                self._poll_stock_names[ticker] = name or ticker

        # 5. 상태 갱신
        self._candidates = list(candidates.keys())
        self._v23_entry_cache = candidates
        self._atr_cache.clear()
        for tkr, c in candidates.items():
            self._atr_cache[tkr] = c['atr']

        logger.info(
            f"v2.3 후보 확정: {len(candidates)}종목 "
            f"(breadth={breadth:.0%})"
        )

        # 텔레그램 알림
        try:
            sample_lines = []
            for tkr in list(candidates.keys())[:5]:
                c = candidates[tkr]
                sample_lines.append(
                    f"  {c.get('name', tkr)} ({tkr}) ADX={c['adx']:.1f}"
                )
            more = (
                f"\n  ... 외 {len(candidates) - 5}종목"
                if len(candidates) > 5 else ""
            )
            sample_text = "\n".join(sample_lines) if sample_lines else "  (없음)"
            gate_mark = "🟢 OPEN" if self._breadth_ok else "🔴 CLOSED"
            self._telegram.send(
                f"📋 v2.3 후보 {len(candidates)}종목\n"
                f"시장: {gate_mark} (breadth {breadth:.0%})\n\n"
                f"{sample_text}{more}"
            )
        except Exception:
            pass

    def _compute_breadth(self, today: str) -> float:
        """Universe에서 MA200 위 종목 비율(breadth) 계산."""
        import pandas as pd
        try:
            with get_data_db() as conn:
                tickers = [
                    r['ticker'] for r in conn.execute(
                        """
                        SELECT DISTINCT m.ticker
                        FROM market_cap_history m
                        JOIN stocks s ON m.ticker = s.ticker
                        WHERE m.date = (
                            SELECT MAX(date) FROM market_cap_history WHERE date < ?
                        )
                          AND m.market_cap >= ?
                          AND s.stock_type NOT IN (?, ?, ?, ?)
                          AND (s.delisted_date IS NULL OR s.delisted_date > ?)
                        """,
                        (today, V23_MCAP_THRESHOLD, *V23_EXCLUDED_TYPES, today),
                    ).fetchall()
                ]
                above = 0
                total = 0
                for t in tickers:
                    rows = conn.execute(
                        "SELECT close FROM daily_candles WHERE ticker = ? "
                        "AND date <= ? ORDER BY date DESC LIMIT 200",
                        (t, today),
                    ).fetchall()
                    if len(rows) < 200:
                        continue
                    closes = [r['close'] for r in rows]
                    ma200 = sum(closes) / 200.0
                    last = closes[0]
                    total += 1
                    if last > ma200:
                        above += 1
                return above / total if total > 0 else 0.0
        except Exception as e:
            logger.warning(f"breadth 계산 실패: {e}")
            return 0.0

    # ── 장중 실시간 ──

    async def on_price_update(self, tick: Tick):
        """실시간 시세 수신 콜백."""
        if not self._running or self._risk_mgr.is_halted:
            return
        if not is_market_open():
            return

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
        # v2.3 가드레일: breadth < 0.40이면 매수 전체 차단
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

            # OHLCV 기반 종합 청산 판단
            exit_reason = self._evaluate_exit(pos, tick.price)
            if exit_reason:
                await self._execute_sell(pos, tick.price, exit_reason)

    def _evaluate_exit(self, pos: Position, current_price: int) -> ExitReason | None:
        """v2.3 통합 청산 판단 (틱마다 호출).

        실시간: SL / TP1 분할(30%) / Trail
        EOD: 추세이탈(MA5<MA20) / Hold 20일
        """
        # 1. 손절 (SL ATR×2.0)
        if self._stop_mgr.is_stopped(pos, current_price):
            return ExitReason.STOP_LOSS

        # 2. TP1 분할 매도 (ATR×2.0, 30% 매도)
        if (
            not getattr(pos, "partial_sold", False)
            and pos.target_price > 0
            and current_price >= pos.target_price
        ):
            return ExitReason.PARTIAL_TARGET

        # 3. 트레일링 — StopManager가 high_since_entry 갱신 + stop_price 상향
        #    _check_exit_conditions에서 update_trailing_stop 호출 후 is_stopped 체크로
        #    자연히 처리됨. 여기서 별도 처리 불필요.

        # 4. 시간 청산 (Hold 20일)
        if pos.hold_days >= self._params.max_hold_days:
            return ExitReason.MAX_HOLD

        # 5. 추세 이탈 (MA5 < MA20)은 EOD _post_market_cleanup에서 체크
        return None

    def _check_strategy_exit(self, pos: Position, current_price: int) -> ExitReason | None:
        """v2.3: 전략별 분기 제거 — _evaluate_exit에 통합. 호환성 위해 no-op."""
        return None

    def _check_strategy_exit_legacy_unused(self, pos: Position, current_price: int) -> ExitReason | None:
        """레거시 전략별 청산 — v2.3 전환 후 미사용."""
        strategy = pos.entry_strategy

        # golden_cross: 데드크로스 + RSI 과열
        if strategy == "golden_cross":
            try:
                from src.strategy.signals import calculate_indicators
                import pandas as pd
                from datetime import timedelta
                end = datetime.now().strftime("%Y-%m-%d")
                start = (datetime.now() - timedelta(days=40)).strftime("%Y-%m-%d")
                ohlcv = self._ds.get_cached_ohlcv(pos.code, start, end)
                if ohlcv and len(ohlcv) >= 2:
                    df = pd.DataFrame(ohlcv)
                    df = calculate_indicators(df)
                    if len(df) >= 2:
                        prev = df.iloc[-2]
                        curr = df.iloc[-1]
                        if curr["sma5"] < curr["sma20"] and prev["sma5"] >= prev["sma20"]:
                            return ExitReason.MACD_DEAD
                        if curr.get("rsi", 50) > 70:
                            return ExitReason.TARGET_REACHED
            except Exception:
                pass

        # disparity_reversion: 이격도 기반 청산
        if strategy == "disparity_reversion":
            try:
                from src.strategy.signals import calculate_indicators
                import pandas as pd
                from datetime import timedelta
                end = datetime.now().strftime("%Y-%m-%d")
                start = (datetime.now() - timedelta(days=40)).strftime("%Y-%m-%d")
                ohlcv = self._ds.get_cached_ohlcv(pos.code, start, end)
                if ohlcv and len(ohlcv) >= 20:
                    df = pd.DataFrame(ohlcv)
                    df = calculate_indicators(df)
                    if not df.empty:
                        latest = df.iloc[-1]
                        sma20 = latest.get("sma20", 0)
                        if sma20 > 0:
                            disparity = latest["close"] / sma20 * 100
                            if disparity >= config.get("strategy.disparity_exit", 100):
                                return ExitReason.DISPARITY_EXIT
                            if disparity <= config.get("strategy.disparity_stop", 88):
                                return ExitReason.STOP_LOSS
            except Exception:
                pass
            # 이격도 전략 전용 최대 보유일 (기본 7일)
            disparity_max_hold = config.get("strategy.disparity_max_hold", 7)
            if pos.hold_days >= disparity_max_hold:
                return ExitReason.MAX_HOLD
            return None  # disparity 전략은 MACD 폴백 불필요

        # institutional_flow: 비활성 전략, 재활성화 시 주석 해제
        # if strategy == "institutional_flow":
        #     try:
        #         from src.strategy.signals import get_institutional_net_buying
        #         _inst_net, foreign_net = get_institutional_net_buying(pos.code, days=2)
        #         if foreign_net < 0:
        #             return ExitReason.FLOW_EXIT
        #     except Exception:
        #         pass

        # 공통 MACD 데드크로스 (수익 +2% 이상)
        from src.strategy.signals import calculate_indicators
        import pandas as pd

        pnl_pct = (current_price - pos.entry_price) / pos.entry_price
        if pnl_pct >= 0.02:
            try:
                from datetime import timedelta
                end = datetime.now().strftime("%Y-%m-%d")
                start = (datetime.now() - timedelta(days=40)).strftime("%Y-%m-%d")
                ohlcv = self._ds.get_cached_ohlcv(pos.code, start, end)
                if ohlcv and len(ohlcv) >= 30:
                    df = pd.DataFrame(ohlcv)
                    df = calculate_indicators(df)
                    if len(df) >= 2:
                        prev_hist = df.iloc[-2].get("macd_hist", 0)
                        curr_hist = df.iloc[-1].get("macd_hist", 0)
                        if prev_hist > 0 and curr_hist < 0:
                            return ExitReason.MACD_DEAD
            except Exception:
                pass

        return None

    async def _check_entry_conditions(self, tick: Tick):
        """v2.3 진입 — 스크리닝에서 확정된 후보만 시가 매수."""
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

        # 포지션 사이징: 가용자본 × 25%
        capital = self._get_available_capital()
        alloc = int(capital * V23_POSITION_RATIO)
        if alloc < V23_MIN_POSITION_AMOUNT:
            reason_key = "자본부족"
            if self._entry_logged.get(tick.code) != reason_key:
                logger.info(
                    f"진입 차단 ({name}): 자본 부족 "
                    f"(alloc={alloc:,} < {V23_MIN_POSITION_AMOUNT:,})"
                )
                self._entry_logged[tick.code] = reason_key
            return

        qty = alloc // max(1, tick.price)
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
                await self._record_buy(tick, qty, "TF_v2.3")
        elif self.mode == "paper":
            await self._record_buy(tick, qty, "TF_v2.3")

        # 중복 매수 방지
        self._v23_entry_cache.pop(tick.code, None)
        if tick.code in self._candidates:
            try:
                self._candidates.remove(tick.code)
            except ValueError:
                pass
        self._entry_logged.pop(tick.code, None)
        return

    async def _check_entry_conditions_legacy_unused(self, tick: Tick):
        """레거시 멀티전략 진입 로직 — v2.3 전환 후 미사용."""
        from src.strategy.signals import (
            calculate_indicators,
            calculate_signal_score,
            get_institutional_net_buying,
        )
        import pandas as pd

        score = 0.0
        matched_strategy = None
        name = self._poll_stock_names.get(tick.code, tick.code)
        try:
            from datetime import timedelta

            # 일봉 데이터 준비 (SMA120 계산에 최소 170거래일 필요 → 250일)
            end = datetime.now().strftime("%Y-%m-%d")
            start = (datetime.now() - timedelta(days=250)).strftime("%Y-%m-%d")
            ohlcv = self._ds.get_cached_ohlcv(tick.code, start, end)
            if not ohlcv or len(ohlcv) < 30:
                logger.info(f"진입체크 탈락 ({name}): OHLCV 캐시 부족 ({len(ohlcv) if ohlcv else 0}행, 최소 30 필요)")
                return

            df_daily_raw = pd.DataFrame(ohlcv)  # raw OHLCV 보관 (dropna 전, 250행)
            df_daily = calculate_indicators(df_daily_raw.copy())
            if df_daily.empty:
                logger.info(f"진입체크 탈락 ({name}): 지표 계산 실패")
                return

            # 기관/외국인 수급 데이터 (graceful: 실패 시 0)
            inst_net, foreign_net = get_institutional_net_buying(tick.code)

            # 신호 강도 점수 (OBV + 수급 포함)
            score = calculate_signal_score(
                df_daily,
                institutional_net=inst_net,
                foreign_net=foreign_net,
            )

            df_60m = self._minute_ohlcv_cache.get(tick.code)

            # 주요 지표 로깅 (종목당 첫 진입체크 시 1회)
            if tick.code not in self._last_entry_check or self._last_entry_check[tick.code] == 0:
                latest = df_daily.iloc[-1]
                logger.info(
                    f"진입체크 지표 ({name}): "
                    f"score={score:.1f}, "
                    f"RSI={latest.get('rsi', 0):.1f}, "
                    f"ADX={latest.get('adx', 0):.1f}, "
                    f"SMA5/20={latest.get('sma5', 0):,.0f}/{latest.get('sma20', 0):,.0f}, "
                    f"vol_ratio={latest.get('volume', 0) / max(1, latest.get('volume_sma20', 1)):.2f}x, "
                    f"60m={'있음' if df_60m is not None else '없음'}"
                )

            # 멀티전략 순회: 카테고리별 게이트 분기
            # NOTE: score는 스크리닝(장전 후보 선정)에서만 사용.
            # 실전 진입 판단은 check_realtime_entry()만으로 결정 (백테스트 동일).
            reject_reasons = []
            for strategy in self._strategies:
                is_mr = strategy.category == "mean_reversion"

                # v3: 주봉 SMA20 필터 비활성화 — 백테스트에 없는 조건 (백테스트 ↔ 실전 일치 원칙)
                # if not is_mr and not self._check_weekly_trend(df_daily):
                #     reject_reasons.append(f"{strategy.name}: 주봉 추세 미확인")
                #     continue

                # 전략별 실시간 진입 판단
                # golden_cross는 오늘 현재가를 가상 일봉으로 반영 (장중 크로스 감지)
                if strategy.name == "golden_cross":
                    if strategy.check_realtime_entry(
                        df_daily, df_60m,
                        current_price=tick.price,
                        today_volume=getattr(tick, "volume", None),
                        df_daily_raw=df_daily_raw,
                    ):
                        matched_strategy = strategy
                        break
                    else:
                        detail = getattr(strategy, '_last_reject', '')
                        reject_reasons.append(f"{strategy.name}: {detail}")
                else:
                    if strategy.check_realtime_entry(df_daily, df_60m):
                        matched_strategy = strategy
                        break
                    else:
                        detail = getattr(strategy, '_last_reject', '')
                        reject_reasons.append(f"{strategy.name}: {detail}")

            if not matched_strategy:
                reason_key = f"불발:{','.join(reject_reasons)}"
                if self._entry_logged.get(tick.code) != reason_key:
                    logger.info(
                        f"진입체크 불발 ({name}): score={score:.1f}, "
                        f"사유=[{', '.join(reject_reasons)}]"
                    )
                    self._entry_logged[tick.code] = reason_key
                return
            logger.info(
                f"진입 신호: {tick.code} by {matched_strategy.name} "
                f"(score={score:.1f})"
            )
            self._entry_logged.pop(tick.code, None)
        except Exception as e:
            # loguru는 exc_info 대신 opt(exception=True)로 stack trace 출력
            logger.opt(exception=True).warning(f"진입 조건 체크 실패 ({tick.code}): {e}")
            return

        # 4. 리스크 사전 체크 (추세 유지 시 재진입 쿨다운 단축)
        trend_intact = False
        try:
            latest = df_daily.iloc[-1]
            adx_thr = config.get("strategy.adx_threshold", 20)
            if (
                latest.get("sma5", 0) > latest.get("sma20", 0)
                and latest.get("adx", 0) > adx_thr
            ):
                trend_intact = True
        except Exception:
            pass

        name = self._get_stock_name(tick.code)
        signal = Signal(
            code=tick.code,
            name=name,
            signal_type="buy",
            price=tick.price,
            score=score,
        )
        risk_result = self._risk_mgr.pre_check(signal, trend_intact=trend_intact)
        if not risk_result.approved:
            reason_key = f"리스크:{risk_result.reason}"
            if self._entry_logged.get(tick.code) != reason_key:
                logger.info(f"진입 차단 ({name}): 리스크 체크 거부 — {risk_result.reason}")
                self._entry_logged[tick.code] = reason_key
            return

        # 5. 포지션 사이징
        capital = self._get_available_capital()
        win_rate = config.get("risk.default_win_rate", 0.5)
        avg_win = config.get("risk.default_avg_win", 0.08)
        avg_loss = config.get("risk.default_avg_loss", 0.04)
        invest_amount = self._sizer.calculate(
            capital=capital, win_rate=win_rate, avg_win=avg_win, avg_loss=avg_loss
        )

        # 국면별 포지션 스케일링
        regime = self._market_regime.regime_type
        scale_map = config.get("strategy.regime_position_scale", {})
        scale = scale_map.get(regime, 1.0) if isinstance(scale_map, dict) else 1.0
        invest_amount = int(invest_amount * scale)

        if invest_amount <= 0:
            logger.info(f"진입 차단 ({name}): 가용자본 부족 (capital={capital:,}, invest=0)")
            return

        qty = invest_amount // tick.price
        if qty <= 0:
            logger.info(f"진입 차단 ({name}): 최소수량 미달 (invest={invest_amount:,}, price={tick.price:,})")
            return

        # 6. 주문 실행  # RISK_CHECK_REQUIRED
        if self.mode == "live":
            hoga = self._get_hoga_type()
            order_price = tick.price if hoga == PRICE_LIMIT else 0

            result = await self._order_mgr.execute_order(
                tick.code, qty, order_price, ORDER_BUY, hoga
            )
            if result.success:
                await self._record_buy(tick, qty, matched_strategy.name)
        elif self.mode == "paper":
            await self._record_buy(tick, qty, matched_strategy.name)

    async def _execute_sell(self, position: Position, price: int, reason: ExitReason):
        """매도 실행. 부분 매도(PARTIAL_TARGET) 시 절반만 매도하고 포지션 유지."""
        # 부분 매도 수량 결정 (v2.3: tp1_sell_ratio=0.3)
        is_partial = reason == ExitReason.PARTIAL_TARGET
        if is_partial:
            sell_ratio = self._params.tp1_sell_ratio
            sell_qty = max(1, int(position.quantity * sell_ratio))
            # 잔여 수량이 0이 되면 전량 매도로 전환
            if sell_qty >= position.quantity:
                is_partial = False
                sell_qty = position.quantity
        else:
            sell_qty = position.quantity

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
            # 부분 매도: 수량 감소, 포지션 유지
            remaining_qty = position.quantity - sell_qty
            self._ds.update_position(
                position.id, quantity=remaining_qty, status="open", partial_sold=1
            )
            logger.info(f"부분 매도 완료: {position.code} {sell_qty}주 매도, {remaining_qty}주 잔여 (트레일링 계속)")
        else:
            if self.mode == "paper":
                self._ds.update_position(position.id, status="closed")
        self._invalidate_positions_cache()
        self._sell_retry_counts.pop(position.id, None)

        # 손익 계산 (매도 수량 기준)
        pnl = (price - position.entry_price) * sell_qty
        pnl_pct = (price - position.entry_price) / position.entry_price
        fee = price * sell_qty * 0.00015
        tax = price * sell_qty * 0.0015  # 거래세 0.15% (2025년)

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

        logger.log(
            "TRADE",
            f"{sell_label} {position.code} @ {price:,} ({reason.value}), PnL: {pnl:+,}",
        )

    @staticmethod
    def _check_weekly_trend(df_daily: "pd.DataFrame") -> bool:
        """주봉 SMA20 필터 — 일봉 데이터를 주봉으로 리샘플링하여 추세 확인.

        Args:
            df_daily: 지표 계산 완료된 일봉 DataFrame (최소 100행 권장).

        Returns:
            주간 종가 > 주봉 SMA20이면 True.
        """
        import pandas as pd
        try:
            if len(df_daily) < 60:
                return True  # 데이터 부족 시 필터 통과

            # 날짜 인덱스가 없으면 리샘플링 불가 → 필터 통과
            if not hasattr(df_daily.index, 'to_period'):
                return True

            weekly = df_daily.resample("W").agg({
                "open": "first",
                "high": "max",
                "low": "min",
                "close": "last",
                "volume": "sum",
            }).dropna()

            if len(weekly) < 20:
                return True  # 주봉 데이터 부족 시 필터 통과

            weekly_sma20 = weekly["close"].rolling(20).mean().iloc[-1]
            weekly_close = weekly["close"].iloc[-1]
            return weekly_close > weekly_sma20
        except Exception:
            return True  # 오류 시 필터 통과 (보수적)

    def _get_atr(self, code: str, fallback_price: int = 0) -> float:
        """종목의 ATR 조회 — OHLCV 캐시 우선, 없으면 가격 기반 추정.

        Args:
            code: 종목코드.
            fallback_price: OHLCV 없을 때 기준 가격.

        Returns:
            ATR 값 (float).
        """
        if code in self._atr_cache:
            return self._atr_cache[code]

        # OHLCV 캐시에서 최근 20일 데이터로 ATR 계산
        try:
            from datetime import timedelta
            end = datetime.now().strftime("%Y-%m-%d")
            start = (datetime.now() - timedelta(days=40)).strftime("%Y-%m-%d")
            ohlcv = self._ds.get_cached_ohlcv(code, start, end)

            if len(ohlcv) >= 14:
                trs = []
                for i in range(1, len(ohlcv)):
                    high = ohlcv[i]["high"]
                    low = ohlcv[i]["low"]
                    prev_close = ohlcv[i - 1]["close"]
                    tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
                    trs.append(tr)
                atr = sum(trs[-14:]) / 14
                self._atr_cache[code] = atr
                return atr
        except Exception:
            pass

        # 폴백: 가격의 2%
        atr = fallback_price * 0.02 if fallback_price > 0 else 0.0
        return atr

    def _get_stock_name(self, code: str) -> str:
        """종목명 조회 — swing.db의 stocks 테이블 + 메모리 캐시.

        swing_trade.db에는 stocks가 없으므로 데이터 DB(swing_data.db) 경유.
        """
        if code in self._poll_stock_names:
            name = self._poll_stock_names[code]
            if name and name != code:
                return name
        try:
            with get_data_db() as conn:
                row = conn.execute(
                    "SELECT name FROM stocks WHERE ticker = ?", (code,)
                ).fetchone()
            if row and row['name']:
                self._poll_stock_names[code] = row['name']
                return row['name']
        except Exception as e:
            logger.debug(f"stocks 조회 실패 ({code}): {e}")
        return code

    async def _record_buy(self, tick: Tick, qty: int, strategy_name: str = ""):
        """매수 기록 — v2.3 stop/target 직접 계산."""
        # ATR: 스크리닝 캐시 우선, 없으면 _get_atr 폴백
        cache = self._v23_entry_cache.get(tick.code)
        if cache and cache.get('atr'):
            atr = cache['atr']
        else:
            atr = self._get_atr(tick.code, tick.price)

        # v2.3: SL = entry - ATR×2.0, TP1 = entry + ATR×2.0
        stop_price = int(tick.price - atr * self._params.stop_loss_atr)
        target_price = int(tick.price + atr * self._params.take_profit_atr)
        name = self._get_stock_name(tick.code)

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
            fee=tick.price * qty * 0.00015,
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
        logger.log("TRADE", f"매수 {tick.code} @ {tick.price:,} × {qty}주")

    def _update_daily_pnl(self, tick: Tick):
        """일일 손익 업데이트 — 보유 포지션 기반 미실현 손익."""
        positions = self._get_cached_positions()
        if not positions:
            return

        unrealized_pnl = 0
        for pos_dict in positions:
            code = pos_dict["code"]
            entry_price = pos_dict["entry_price"]
            qty = pos_dict["quantity"]
            # 최신 가격 캐시에서 조회 — 없으면 매입가 사용 (변동 0)
            current = self._latest_prices.get(code, entry_price)
            unrealized_pnl += (current - entry_price) * qty

        # 당일 실현 손익 (메모리 캐시 — 매매 발생 시만 갱신)
        if self._daily_trades_cache is None:
            today = datetime.now().strftime("%Y-%m-%d")
            self._daily_trades_cache = self._ds.get_trades_by_date(today)
        realized_pnl = sum(
            t.get("pnl", 0) for t in self._daily_trades_cache if t["side"] == "sell"
        )

        total_pnl = realized_pnl + unrealized_pnl
        pnl_pct = total_pnl / self._initial_capital if self._initial_capital > 0 else 0.0

        self._risk_mgr.update_daily_pnl(pnl_pct)

        # MDD 업데이트
        current_capital = self._initial_capital + total_pnl
        self._risk_mgr.update_mdd(float(current_capital))

        # 일일 한도 체크
        if pnl_pct <= self._risk_mgr._daily_loss_limit and not self._risk_mgr.is_halted:
            self.halt()

    def _switch_strategy_by_regime(self):
        """v2.3: 단일 전략 모드 — no-op (레거시 호출 호환)."""
        return

    def _switch_strategy_by_regime_legacy_unused(self):
        """레거시 국면별 전략 전환 — v2.3 전환 후 미사용."""
        regime = self._market_regime.regime_type
        regime_map = getattr(self, '_strategy_config', {}).get("regime_strategy", {})
        new_names = regime_map.get(regime)

        if not new_names:
            return

        # str → list 하위 호환
        if isinstance(new_names, str):
            new_names = [new_names]

        current_names = sorted(s.name for s in self._strategies)
        target_names = sorted(new_names)

        if current_names == target_names:
            logger.info(f"전략 유지: {current_names} (국면: {regime})")
            return

        self._strategies = [
            get_strategy(n, self._strategy_config) for n in new_names
        ]
        self._strategy = self._strategies[0]  # 하위 호환
        names_str = ", ".join(new_names)
        logger.info(
            f"전략 전환: {current_names} → [{names_str}] (국면: {regime})"
        )
        self._telegram.send(
            f"전략 전환: [{names_str}] "
            f"(국면: {regime}, ADX {self._market_regime.kospi_adx:.1f})"
        )

    def _get_hoga_type(self) -> str:
        """config 기반 호가 유형 반환."""
        order_type = config.get("trading.order_type", "market")
        return PRICE_LIMIT if order_type == "limit" else PRICE_MARKET

    def _get_available_capital(self) -> int:
        """가용 자본 조회 — 초기자본 - 투자중 금액."""
        positions = self._get_cached_positions()
        invested = sum(p["entry_price"] * p["quantity"] for p in positions)
        return max(0, self._initial_capital - invested)

    def _get_cached_positions(self) -> list[dict]:
        """포지션 메모리 캐시 반환. 없으면 DB 조회."""
        if self._positions_cache is None:
            self._positions_cache = self._ds.get_open_positions()
        return self._positions_cache

    def _invalidate_positions_cache(self):
        """포지션 캐시 무효화 (매수/매도 후)."""
        self._positions_cache = None

    def _dict_to_position(self, d: dict) -> Position:
        """dict -> Position 변환."""
        # hold_days 계산: entry_date 기준 경과일
        hold_days = d.get("hold_days", 0)
        if hold_days == 0 and d.get("entry_date"):
            try:
                entry = datetime.strptime(d["entry_date"], "%Y-%m-%d").date()
                hold_days = (datetime.now().date() - entry).days
            except ValueError:
                hold_days = 0

        return Position(
            id=d["id"],
            code=d["code"],
            name=d.get("name", ""),
            entry_date=d["entry_date"],
            entry_price=d["entry_price"],
            quantity=d["quantity"],
            stop_price=d["stop_price"],
            target_price=d.get("target_price", 0),
            status=d.get("status", "open"),
            high_since_entry=d.get("high_since_entry", d["entry_price"]),
            hold_days=hold_days,
            partial_sold=bool(d.get("partial_sold", 0)),
            entry_strategy=d.get("entry_strategy", ""),
            updated_at=d.get("updated_at", ""),
        )

    # ── 장마감 ──

    async def _quarterly_watchlist_refresh(self):
        """분기 watchlist 자동 갱신."""
        try:
            from data.provider import get_provider
            provider = get_provider()

            wl_config = config.data.get("watchlist_refresh", {})
            new_list = await asyncio.to_thread(
                provider.generate_watchlist,
                top_n=wl_config.get("top_n", 20),
                min_market_cap=wl_config.get("min_market_cap", 5_000_000_000_000),
                min_daily_amount=wl_config.get("min_daily_amount", 10_000_000_000),
                min_atr_pct=wl_config.get("min_atr_pct", 0.02),
                max_atr_pct=wl_config.get("max_atr_pct", 0.05),
            )

            if not new_list or len(new_list) < 10:
                logger.warning("watchlist 갱신 실패: 조건 충족 종목 부족")
                return

            new_codes = [item["code"] for item in new_list]
            old_codes = config.get("watchlist", [])
            if isinstance(old_codes, list):
                added = set(new_codes) - set(old_codes)
                removed = set(old_codes) - set(new_codes)
            else:
                added, removed = set(new_codes), set()

            if not added and not removed:
                logger.info("watchlist 변경 없음")
                return

            self._update_watchlist_config(new_codes)
            config.reload()

            msg = f"분기 watchlist 갱신: {len(new_codes)}종목 (추가 {len(added)}, 제거 {len(removed)})"
            logger.info(msg)
            await self._telegram.send(msg)
        except Exception as e:
            logger.error(f"watchlist 갱신 실패: {e}")

    def _update_watchlist_config(self, codes: list[str]):
        """config.yaml watchlist 업데이트."""
        try:
            from ruamel.yaml import YAML
            yaml = YAML()
            yaml.preserve_quotes = True
            with open("config.yaml", "r", encoding="utf-8") as f:
                data = yaml.load(f)
            data["watchlist"] = codes
            with open("config.yaml", "w", encoding="utf-8") as f:
                yaml.dump(data, f)
            logger.info(f"config.yaml watchlist 업데이트: {len(codes)}종목")
        except Exception as e:
            logger.error(f"config.yaml 업데이트 실패: {e}")

    async def _evening_watchlist_screening(self):
        """장마감 후 HTS 조건검색 — v2.3 모드에서는 불필요하여 스킵.

        v2.3은 장전 _v23_screen_universe()로 Universe 기반 후보를 동적 생성하므로
        전날 저녁 조건검색 결과를 DB에 적재할 필요 없음.
        """
        logger.info(
            "v2.3 모드 — 저녁 HTS 조건검색 스킵 (장전 스크리닝으로 대체)"
        )
        return

    async def _evening_watchlist_screening_legacy_unused(self):
        """레거시 저녁 조건검색 — v2.3 전환 후 미사용."""
        logger.info("저녁 조건검색 시작 (다음 거래일 watchlist 생성)")

        watchlist_mode = config.get("watchlist_mode", "fixed")
        if watchlist_mode != "condition":
            logger.info("watchlist_mode != 'condition' → 저녁 스크리닝 건너뜀")
            return

        cs_config = config.get("condition_search", {})
        condition_name = cs_config.get("condition_name", "swing_pre_cross")
        max_stocks = cs_config.get("max_stocks", 30)
        ws_url = config.get("broker.ws_url", "")

        if not ws_url:
            logger.error("broker.ws_url 미설정 → 저녁 스크리닝 중단")
            return

        # 토큰 확인 (만료 대비)
        try:
            await self._ensure_connection()
        except Exception as e:
            logger.warning(f"저녁 스크리닝 토큰 갱신 실패: {e}")

        # 조건검색 실행
        try:
            from src.broker.condition_search import run_condition_search
            stocks = await run_condition_search(
                ws_url=ws_url,
                access_token=self._kiwoom._rest.access_token,
                condition_name=condition_name,
            )
        except Exception as e:
            logger.error(f"저녁 조건검색 호출 실패: {e}", exc_info=True)
            stocks = []

        if not stocks:
            logger.warning("저녁 조건검색 결과 없음 → 저장 스킵 (다음날 폴백 예정)")
            try:
                self._telegram.send(
                    "⚠️ 저녁 스크리닝 결과 없음\n"
                    "내일 아침 고정 watchlist 사용 예정"
                )
            except Exception:
                pass
            return

        # 상한 적용
        if len(stocks) > max_stocks:
            logger.info(
                f"저녁 조건검색 {len(stocks)}개 → 상위 {max_stocks}개 제한"
            )
            stocks = stocks[:max_stocks]

        # 다음 영업일 계산 (주말 건너뛰기)
        from datetime import timedelta
        today = datetime.now()
        next_day = today + timedelta(days=1)
        while next_day.weekday() >= 5:  # 5=토, 6=일
            next_day += timedelta(days=1)
        next_date_str = next_day.strftime("%Y-%m-%d")

        # DB 저장
        try:
            self._ds.save_daily_watchlist(
                date=next_date_str,
                stocks=stocks,
                source="condition_search",
            )
            logger.info(
                f"저녁 조건검색 완료: {len(stocks)}종목 → {next_date_str} watchlist 저장"
            )

            # 텔레그램 알림 (종목명 포함)
            sample_lines = []
            for s in stocks[:5]:
                name = s.get("name") or "?"
                code = s.get("code", "")
                sample_lines.append(f"  {name} ({code})")
            more = f"\n  ... 외 {len(stocks) - 5}종목" if len(stocks) > 5 else ""
            sample_text = "\n".join(sample_lines)

            try:
                self._telegram.send(
                    f"🌙 저녁 스크리닝 완료\n"
                    f"대상일: {next_date_str}\n"
                    f"매칭: {len(stocks)}종목\n\n"
                    f"{sample_text}{more}"
                )
            except Exception:
                pass
        except Exception as e:
            logger.error(f"watchlist DB 저장 실패: {e}", exc_info=True)

    async def _post_market_cleanup(self):
        """장 마감 후 정리 (15:35).

        1. (live) 미체결 주문 전량 취소 + selling 포지션 복원
        2. v2.3 추세 이탈 체크 (MA5 < MA20) — 일봉 확정 후
        """
        logger.info("장마감 정리 시작")

        cancel_results = {}
        selling_positions = []
        restored_count = 0

        if self.mode == "live":
            # 1. 미체결 주문 전량 취소
            cancel_results = await self._order_mgr.cancel_all_pending()

            # 2. "selling" 상태 포지션 복원 (체결 안 된 매도 주문)
            try:
                selling_positions = self._ds.get_positions_by_status("selling")
                for pos_dict in selling_positions:
                    self._ds.update_position(pos_dict["id"], status="open")
                    restored_count += 1
                    logger.warning(
                        f"미체결 매도 포지션 복원: {pos_dict['code']} "
                        f"(id={pos_dict['id']})"
                    )
            except Exception as e:
                logger.error(f"selling 포지션 복원 실패: {e}")

            if selling_positions:
                self._invalidate_positions_cache()
                self._sell_retry_counts.clear()

        # 3. v2.3 추세 이탈 체크 (MA5 < MA20 EOD)
        try:
            await self._v23_check_trend_exit()
        except Exception as e:
            logger.error(f"추세 이탈 체크 실패: {e}")

        # 텔레그램 알림
        cancelled = sum(1 for v in cancel_results.values() if v)
        failed = sum(1 for v in cancel_results.values() if not v)
        if cancel_results or restored_count > 0:
            msg_parts = []
            if cancel_results:
                msg_parts.append(f"미체결 취소 {cancelled}건")
                if failed > 0:
                    msg_parts.append(f"(실패 {failed}건)")
            if restored_count > 0:
                msg_parts.append(f"매도 미체결 복원 {restored_count}건")
            self._telegram.send(f"🔄 장마감 정리: {', '.join(msg_parts)}")

        logger.info(
            f"장마감 정리 완료: 취소 {cancelled}건, 복원 {restored_count}건"
        )

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
            logger.info("v2.3 추세 이탈 청산 대상 없음")
            return

        logger.info(f"v2.3 추세 이탈 청산: {len(to_close)}건")
        for pos, last_close in to_close:
            await self._execute_sell(pos, last_close, ExitReason.TREND_EXIT)

    def _daily_report(self):
        """일간 리포트 (16:00)."""
        today = datetime.now().strftime("%Y-%m-%d")
        trades = self._ds.get_trades_by_date(today)
        positions = self._ds.get_open_positions()

        buy_count = sum(1 for t in trades if t["side"] == "buy")
        sell_count = sum(1 for t in trades if t["side"] == "sell")
        realized_pnl = sum(t.get("pnl", 0) for t in trades if t["side"] == "sell")

        # 미실현 손익 (보유 포지션 × 최신가). 폴링 중 _latest_prices에 마감 근접 가격이
        # 남아있으므로 장마감(15:35) 이후 리포트(16:00) 시점에 사실상 종가 기준이 된다.
        # 가격 캐시에 없는 종목은 daily_candles의 최신 종가로 폴백, 그래도 없으면 매입가.
        unrealized_pnl = 0
        for pos in positions:
            code = pos["code"]
            entry_price = pos["entry_price"]
            qty = pos["quantity"]
            current = self._latest_prices.get(code)
            if current is None:
                current = self._get_latest_close(code) or entry_price
            unrealized_pnl += (current - entry_price) * qty

        pnl_pct = realized_pnl / self._initial_capital * 100 if self._initial_capital > 0 else 0.0
        current_capital = self._initial_capital + int(realized_pnl) + int(unrealized_pnl)

        self._telegram.send_daily_report(
            date=today,
            buy_count=buy_count,
            sell_count=sell_count,
            realized_pnl=int(realized_pnl),
            realized_pnl_pct=pnl_pct,
            position_count=len(positions),
            unrealized_pnl=unrealized_pnl,
            initial_capital=self._initial_capital,
            current_capital=current_capital,
            total_return_pct=pnl_pct,
            current_mdd=self._risk_mgr.current_mdd,
        )

        # 일일 성과 DB 저장
        self._ds.save_daily_performance(
            date=today,
            realized_pnl=realized_pnl,
            unrealized_pnl=float(unrealized_pnl),
            total_capital=float(current_capital),
            daily_return=pnl_pct,
            mdd_current=self._risk_mgr.current_mdd,
            trade_count=buy_count + sell_count,
        )

        logger.info(
            f"일간 리포트 발송: 매수{buy_count}/매도{sell_count}/PnL{int(realized_pnl):+,}"
        )

        try:
            self._update_swing_db_snapshot()
        except Exception as e:
            logger.warning(f"swing.db snapshot 갱신 실패: {e}")

    def _restore_mdd_from_db(self):
        """마지막 daily_portfolio_snapshot 행에서 peak_capital + MDD 복원.

        엔진 재시작 시 RiskManager.peak_capital/current_mdd가 0으로 리셋되어
        MDD 추적이 끊기는 것을 방지. 컬럼이 없거나 행이 없으면 묵살하고 초기값
        유지 (set_initial_capital 결과).
        """
        try:
            with get_trade_db() as conn:
                row = conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name='daily_portfolio_snapshot'"
                ).fetchone()
                if not row:
                    return

                # 컬럼 확인 — mdd/peak_capital 없으면 복원 불가
                cols = {
                    r[1] for r in conn.execute(
                        "PRAGMA table_info(daily_portfolio_snapshot)"
                    ).fetchall()
                }
                if 'mdd' not in cols or 'peak_capital' not in cols:
                    return

                row = conn.execute(
                    "SELECT portfolio_value, mdd, peak_capital "
                    "FROM daily_portfolio_snapshot "
                    "ORDER BY date DESC LIMIT 1"
                ).fetchone()
                if not row:
                    return

                last_value = float(row['portfolio_value'] or 0)
                last_mdd = float(row['mdd'] or 0)
                last_peak = float(row['peak_capital'] or 0)

                # peak_capital 우선 사용. 없으면 mdd 역산.
                if last_peak > 0:
                    peak = last_peak
                elif last_mdd < 0 and last_value > 0:
                    peak = last_value / (1 + last_mdd)
                else:
                    peak = max(last_value, float(self._initial_capital))

                self._risk_mgr._peak_capital = float(peak)
                self._risk_mgr.current_mdd = float(last_mdd)
                logger.info(
                    f"MDD 복원: peak={peak:,.0f}, MDD={last_mdd:.2%}"
                )
        except Exception as e:
            logger.warning(f"MDD 복원 실패 (초기값 유지): {e}")

    def _update_swing_db_snapshot(self):
        """swing_trade.db daily_portfolio_snapshot 갱신 — GUI 표시용."""
        today = datetime.now().strftime("%Y-%m-%d")
        positions = self._ds.get_open_positions()

        cash = self._get_available_capital()
        portfolio_value = float(cash)
        for pos in positions:
            code = pos.get("code", "")
            entry_price = pos.get("entry_price", 0)
            qty = pos.get("quantity", 0)
            price = self._latest_prices.get(code)
            if price is None:
                price = self._get_latest_close(code) or entry_price
            portfolio_value += float(price) * qty

        gate_status = "OPEN" if self._breadth_ok else "CLOSED"

        with get_trade_db() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS daily_portfolio_snapshot (
                    date DATE PRIMARY KEY,
                    cash REAL NOT NULL,
                    portfolio_value REAL NOT NULL,
                    positions_count INTEGER NOT NULL,
                    breadth REAL,
                    gate_status TEXT,
                    mdd REAL DEFAULT 0,
                    peak_capital REAL DEFAULT 0,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            # 기존 DB 호환: mdd/peak_capital 컬럼 추가 (없으면)
            for col, defn in (
                ("mdd", "REAL DEFAULT 0"),
                ("peak_capital", "REAL DEFAULT 0"),
            ):
                try:
                    conn.execute(
                        f"ALTER TABLE daily_portfolio_snapshot ADD COLUMN {col} {defn}"
                    )
                except sqlite3.OperationalError:
                    pass

            # MDD 갱신 (현재 portfolio_value 기준)
            self._risk_mgr.update_mdd(float(portfolio_value))

            conn.execute(
                """
                INSERT OR REPLACE INTO daily_portfolio_snapshot
                (date, cash, portfolio_value, positions_count, breadth,
                 gate_status, mdd, peak_capital)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    today,
                    float(cash),
                    portfolio_value,
                    len(positions),
                    float(self._breadth_value),
                    gate_status,
                    float(self._risk_mgr.current_mdd),
                    float(self._risk_mgr._peak_capital),
                ),
            )

    async def _daily_data_update(self):
        """17:00 자동 실행 — 일봉/시총/지수 수집 (데이터 레이어만).

        시그널 생성은 별도 단계 없이 익일 08:30 _pre_market_screening에서
        v2.4 스크리닝이 담당. GUI의 DailyRunWorker는 4단계 (수집만) — 수동
        실행 시 즉시 데이터 갱신용.

        원천 타이밍:
          - FDR(Naver): 15:40~16:00 종가 반영
          - KRX OpenAPI(시총): 16:00~16:30
          - Yahoo(지수): 16:00~17:00
        17:00이면 세 소스 모두 안전하게 반영 완료된 시점.

        각 step은 실패해도 다음 step 계속 — 부분 성공 허용 (견고성 우선).
        """
        from src.utils.market_calendar import is_trading_day

        today = datetime.now()
        if not is_trading_day(today.date()):
            logger.info("비거래일 — 일일 데이터 갱신 스킵")
            return

        if self._data_update_running:
            logger.info("일일 데이터 갱신 이미 실행 중 — 스킵")
            return
        self._data_update_running = True

        logger.info("일일 데이터 갱신 시작 (17:00 스케줄)")
        steps = [
            ("1/4 신규 상장 감지", self._run_detect_new_listings),
            ("2/4 일봉 증분",      self._run_collect_candles),
            ("3/4 시총 증분",      self._run_collect_market_cap),
            ("4/4 지수 갱신",      self._run_collect_index),
        ]

        failed: list[str] = []
        try:
            for label, func in steps:
                try:
                    logger.info(f"📦 {label} 시작")
                    await asyncio.to_thread(func)
                    logger.info(f"✅ {label} 완료")
                except Exception as e:
                    logger.opt(exception=True).warning(f"⚠ {label} 실패: {e}")
                    failed.append(label)
        finally:
            self._data_update_running = False

        if failed:
            msg = f"⚠ 일일 데이터 갱신 완료 (실패 {len(failed)}/4): {', '.join(failed)}"
            logger.warning(msg)
        else:
            msg = "📦 일일 데이터 갱신 완료 (4/4)"
            logger.info(msg)

        try:
            self._telegram.send(msg)
        except Exception as e:
            logger.warning(f"일일 데이터 갱신 알림 실패 (무시): {e}")

    # ── _daily_data_update 단계별 실행기 ──

    def _run_detect_new_listings(self):
        from src.data_pipeline import detect_new_listings as m
        m.main()

    def _run_collect_candles(self):
        from src.data_pipeline import collect_daily_candles as m
        m.main(force_resume=False, incremental=True)

    def _run_collect_market_cap(self):
        from src.data_pipeline import collect_market_cap as m
        m.main()

    def _run_collect_index(self):
        import sys
        from src.data_pipeline import collect_index_daily as m
        orig_argv = sys.argv
        sys.argv = [sys.argv[0], "--update-only"]
        try:
            m.main()
        finally:
            sys.argv = orig_argv

    def _daily_reset(self):
        """일일 리셋 (09:00)."""
        self._risk_mgr.reset_daily()
        self._invalidate_positions_cache()
        self._sell_retry_counts.clear()
        self._atr_cache.clear()  # ATR 캐시 리프레시
        self._last_entry_check.clear()  # 진입 체크 쓰로틀 초기화
        self._minute_ohlcv_cache.clear()  # 60분봉 캐시 초기화
        # partial_sold 초기화 불필요 (DB 기반)
        self._daily_trades_cache = None  # 당일 trades 캐시 초기화
        self._entry_logged.clear()  # 진입 로그 반복 방지 캐시 초기화

        # OHLCV 캐시 정리 (400일 이상 된 데이터 삭제)
        try:
            self._ds.cleanup_ohlcv_cache(400)
        except Exception as e:
            logger.warning(f"OHLCV 캐시 정리 실패 (무시): {e}")

        # 보유 포지션 hold_days 갱신
        positions = self._ds.get_open_positions()
        today = datetime.now().date()
        for pos in positions:
            try:
                entry = datetime.strptime(pos["entry_date"], "%Y-%m-%d").date()
                hold_days = (today - entry).days
                self._ds.update_position(pos["id"], hold_days=hold_days)
            except (ValueError, KeyError):
                pass

        # 전일 일봉 확정분 반영: high_since_entry 재동기화
        try:
            self._sync_high_since_entry()
        except Exception as e:
            logger.warning(f"일일 리셋 시 high_since_entry 보정 실패: {e}")

        logger.info("일일 리셋 완료")

    def _get_latest_close(self, code: str) -> int | None:
        """daily_candles 최신 종가 조회 (일일 리포트 미실현 손익 폴백용)."""
        try:
            with get_data_db() as conn:
                row = conn.execute(
                    "SELECT close FROM daily_candles "
                    "WHERE ticker = ? ORDER BY date DESC LIMIT 1",
                    (code,),
                ).fetchone()
            if row and row["close"]:
                return int(row["close"])
        except Exception as e:
            logger.debug(f"최신 종가 조회 실패 ({code}): {e}")
        return None

    def _sync_high_since_entry(self):
        """보유 포지션의 high_since_entry를 daily_candles 기준으로 보정.

        폴링 누락·프로세스 재시작으로 장중 고가를 놓쳤을 때,
        진입일 이후 일봉 고가의 max로 DB를 동기화한다.
        엔진 기동 시 및 매 거래일 09:00 일일 리셋에서 호출.
        """
        positions = self._ds.get_open_positions()
        if not positions:
            return

        for pos_dict in positions:
            code = pos_dict["code"]
            entry_date = pos_dict["entry_date"]
            current_high = int(
                pos_dict.get("high_since_entry") or pos_dict["entry_price"]
            )
            try:
                with get_data_db() as conn:
                    row = conn.execute(
                        "SELECT MAX(high) AS max_high FROM daily_candles "
                        "WHERE ticker = ? AND date >= ?",
                        (code, entry_date),
                    ).fetchone()
                if not row or not row["max_high"]:
                    continue
                max_high = int(row["max_high"])
                if max_high > current_high:
                    self._ds.update_position(
                        pos_dict["id"], high_since_entry=max_high
                    )
                    logger.info(
                        f"high_since_entry 보정: {code} "
                        f"{current_high:,} → {max_high:,}"
                    )
            except Exception as e:
                logger.warning(f"high_since_entry 보정 실패 ({code}): {e}")

        self._invalidate_positions_cache()

    async def _refresh_minute_ohlcv(self):
        """60분봉 캐시 갱신 (장중 매시 정각+1분).

        watchlist/후보 종목의 60분봉을 키움 REST API로 조회하여
        메모리 캐시에 저장. 진입 판단의 2층(타이밍) 데이터로 사용.
        """
        import pandas as pd
        from data.column_mapper import OHLCV_MAP, map_columns

        codes = self._candidates or config.get("watchlist", [])
        if not codes:
            return

        tick_range = config.get("strategy.timeframe_entry", 60)
        success = 0

        for code in codes:
            for attempt in range(2):  # 최대 1회 재시도
                try:
                    raw = await self._kiwoom.get_minute_ohlcv(
                        code, tick_range=tick_range, count=30
                    )
                    if raw and isinstance(raw, list) and len(raw) > 0:
                        df = pd.DataFrame(raw)
                        if not df.empty:
                            df = map_columns(df, OHLCV_MAP)
                            self._minute_ohlcv_cache[code] = df
                            success += 1
                    else:
                        logger.debug(f"60분봉 빈 응답 ({code}): raw={type(raw).__name__}, len={len(raw) if isinstance(raw, list) else 'N/A'}")
                    break  # 성공 시 재시도 루프 탈출
                except Exception as e:
                    if "429" in str(e) and attempt == 0:
                        await asyncio.sleep(2)  # 429 시 2초 대기 후 재시도
                        continue
                    logger.warning(f"60분봉 조회 실패 ({code}): {e}")
                    break
            # API rate limit 준수: 종목 간 1초 대기
            await asyncio.sleep(1.0)

        logger.info(f"60분봉 갱신 완료: {success}/{len(codes)}종목")

    async def _ensure_connection(self):
        """REST API 연결 확인 및 재연결."""
        if not self._kiwoom._connected:
            try:
                await self._kiwoom.connect(use_websocket=False)
                logger.info("REST API 연결 성공")
                self._telegram.send("REST API 연결 확인 완료")
            except Exception as e:
                logger.error(f"REST API 연결 실패: {e}")
                self._telegram.send_system_error(str(e), "ensure_connection")
        else:
            logger.info("REST API 이미 연결됨")

    async def _start_polling(self):
        """REST polling 시작 (09:25 스케줄)."""
        from src.utils.market_calendar import is_trading_day, now_kst
        if not is_trading_day(now_kst().date()):
            logger.info("비거래일 — REST polling 생략")
            return

        if self._polling_task and not self._polling_task.done():
            logger.debug("polling 이미 실행 중")
            return

        # REST 인증 확인
        if not self._kiwoom._connected:
            try:
                await self._kiwoom.connect(use_websocket=False)
            except Exception as e:
                logger.error(f"REST 인증 실패: {e}")
                self._telegram.send_system_error(str(e), "polling_start")
                return

        self._polling_task = asyncio.create_task(self._polling_loop())
        logger.info(
            f"REST polling 시작 (간격: {self._polling_interval}초)"
        )

    async def _stop_polling(self):
        """REST polling 중지 (15:35 스케줄)."""
        if self._polling_task and not self._polling_task.done():
            self._polling_task.cancel()
            try:
                await self._polling_task
            except asyncio.CancelledError:
                pass
            self._polling_task = None
            logger.info("REST polling 중지")
        else:
            logger.debug("polling 이미 중지 상태")

    async def _polling_loop(self):
        """REST polling 메인 루프 — 보유 종목 우선, 만석 시 후보 제외."""
        logger.info("polling 루프 진입")
        try:
            while self._running:
                cycle_start = asyncio.get_event_loop().time()

                # polling 대상 결정: 보유 종목 + (여유 있을 때만) 후보 종목
                open_positions = self._get_cached_positions()
                held_codes = {p["code"] for p in open_positions} if open_positions else set()
                max_pos = config.get("trading.max_positions", 8)
                positions_full = len(held_codes) >= max_pos

                poll_codes: set[str] = set(held_codes)
                if not positions_full and self._candidates:
                    poll_codes.update(self._candidates)

                if not poll_codes:
                    logger.debug("polling 대상 없음 — 다음 주기 대기")
                    await asyncio.sleep(self._polling_interval)
                    continue

                # 종목별 현재가 REST 조회 (rate limit: 5 TR/sec → 0.2초 간격)
                success_count = 0
                fail_count = 0
                for code in poll_codes:
                    if not self._running:
                        break
                    try:
                        data = await self._kiwoom.get_current_price(code)
                        # 키움 REST 응답: cur_prc (부호 포함 문자열, 예: "+4685", "-61200")
                        raw_price = data.get("cur_prc") or data.get("cur_pr") or data.get("stk_pr") or "0"
                        price = abs(int(str(raw_price).replace(",", "").replace("+", "")))
                        raw_vol = data.get("trde_qty") or data.get("tr_vol") or data.get("acc_vol") or "0"
                        volume = abs(int(str(raw_vol).replace(",", "")))
                        if price > 0:
                            tick = Tick(
                                code=code,
                                price=price,
                                volume=volume,
                                timestamp=datetime.now(),
                            )
                            # 종목명 캐시 (첫 틱 로그용)
                            stk_nm = data.get("stk_nm", "")
                            if stk_nm:
                                self._poll_stock_names[code] = stk_nm
                            await self.on_price_update(tick)
                            success_count += 1
                        else:
                            fail_count += 1
                            logger.debug(f"가격 0원 ({code}): data={data}")
                    except Exception as e:
                        fail_count += 1
                        logger.warning(f"현재가 조회 실패 ({code}): {e}")
                    # rate limit 준수: 5 TR/sec → 0.2초 간격
                    await asyncio.sleep(0.2)

                # 실패 시에만 로깅
                if fail_count > 0:
                    mode_label = f"보유{len(held_codes)}종목" if positions_full else f"보유{len(held_codes)}+후보{len(poll_codes)-len(held_codes)}"
                    logger.info(
                        f"polling 주기: {success_count}/{len(poll_codes)}종목 "
                        f"가격 수신 ({mode_label}, 실패: {fail_count})"
                    )

                # 주기 맞춤 대기 (polling_interval - 소요시간)
                elapsed = asyncio.get_event_loop().time() - cycle_start
                wait = max(0, self._polling_interval - elapsed)
                if wait > 0:
                    await asyncio.sleep(wait)
        except asyncio.CancelledError:
            logger.info("polling 루프 취소됨")
            raise
        except Exception as e:
            logger.error(f"polling 루프 오류: {e}")
            self._telegram.send_system_error(str(e), "polling_loop")

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
