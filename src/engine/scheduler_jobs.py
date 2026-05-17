"""SchedulerJobsMixin — 스케줄 작업 (일간 리포트, 정합성 검사 등)."""
from __future__ import annotations
import asyncio
from datetime import datetime
from loguru import logger
from src.utils.config import config
from src.data_pipeline.db import get_trade_db, get_data_db
from src.models import ExitReason
from src.utils.market_calendar import count_trading_days


class SchedulerJobsMixin:
    """스케줄 작업 관련 메서드."""

    async def _reconcile_positions(self):
        """브로커 잔고 ↔ DB 포지션 정합성 검사 (15:45, live 전용)."""
        if self.mode != "live":
            logger.debug("정합성 검사 스킵 (paper 모드)")
            return

        from src.broker.reconciler import reconcile

        try:
            # 1. DB open 포지션 조회
            db_raw = self._ds.get_positions_by_status("open")
            db_positions = [
                {"code": p["code"], "qty": p["quantity"]}
                for p in db_raw
            ]

            # 2. 브로커 잔고 조회
            account = config.get_env("KIWOOM_ACCOUNT_NO", "")
            if not account:
                logger.warning("정합성 검사 스킵: KIWOOM_ACCOUNT_NO 미설정")
                return

            raw = await self._kiwoom.get_account_info(account)
            broker_holdings = self._parse_broker_holdings(raw)

            # 3. 비교
            result = reconcile(
                db_positions=db_positions,
                broker_holdings=broker_holdings,
            )

            # 4. 로깅 + 알림
            logger.bind(
                event="RECONCILE",
                data={
                    "matched": result.matched,
                    "db_only": [d["code"] for d in result.db_only],
                    "broker_only": [d["code"] for d in result.broker_only],
                    "qty_mismatch": result.qty_mismatch,
                    "is_clean": result.is_clean,
                },
            ).info("정합성 검사: {}", result.summary)

            if not result.is_clean:
                self._telegram.send(f"🔍 잔고 정합성\n{result.summary}")
                if result.db_only:
                    logger.error(
                        "DB에만 존재하는 포지션 감지 — 수동 확인 필요: "
                        f"{[d['code'] for d in result.db_only]}"
                    )

        except Exception as e:
            logger.error(f"정합성 검사 실패: {e}")
            self._telegram.send(f"⚠️ 정합성 검사 오류: {e}")

    def _parse_broker_holdings(self, raw: dict) -> list[dict]:
        """키움 API 계좌잔고 응답에서 보유 종목 목록 추출.

        여러 키 후보를 fallback으로 시도해 응답 포맷 변경에 대응.
        응답 구조가 바뀌면 이 메서드만 수정한다.
        """
        holdings: list[dict] = []

        # 키움 REST API: output1 또는 output 배열에 종목별 데이터
        items = raw.get("output1", raw.get("output", []))
        if isinstance(items, dict):
            items = [items]
        if not isinstance(items, list):
            return holdings

        for item in items:
            # 종목코드 후보 키: pdno(계좌잔고), stk_code, 종목코드(한글)
            code = (
                str(item.get("pdno", ""))
                or str(item.get("stk_code", ""))
                or str(item.get("종목코드", ""))
            ).strip()

            # 보유수량 후보 키: hldg_qty, hold_qty, 보유수량
            qty_raw = (
                item.get("hldg_qty")
                or item.get("hold_qty")
                or item.get("보유수량")
                or 0
            )
            try:
                qty = int(qty_raw)
            except (ValueError, TypeError):
                qty = 0

            if code and qty > 0:
                holdings.append({"code": code, "qty": qty})

        return holdings

    async def _post_market_cleanup(self):
        """장 마감 후 정리 (15:35).

        1. (live) 미체결 주문 전량 취소 + selling 포지션 복원
        2. v2.6 추세 이탈 체크 (MA5 < MA20) — 일봉 확정 후
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

        # 3. v2.6 추세 이탈 체크 (MA5 < MA20 EOD)
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

        # ETF 포지션 상태 리포트
        try:
            if self._etf_params.enabled:
                etf_stats = self._ds.get_etf_stats(limit=20)
                etf_holding = self._etf_handler.is_holding
                etf_inv = self._etf_handler.invested_amount
                lines = [f"📊 ETF IBS ({self._etf_params.etf_code})"]
                if etf_holding:
                    pos = self._etf_handler._position
                    lines.append(
                        f"  보유중: {pos['qty']}주 @{pos['entry_price']:,} "
                        f"({pos['hold_days']}일차)"
                    )
                else:
                    lines.append("  미보유")
                if etf_stats["count"] > 0:
                    lines.append(
                        f"  최근{etf_stats['count']}건: "
                        f"WR {etf_stats['win_rate']:.0%}, "
                        f"총PnL {etf_stats['total_pnl']:+,}원"
                    )
                self._telegram.send("\n".join(lines))
        except Exception as e:
            logger.debug(f"ETF 리포트 생성 실패 (무시): {e}")

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

        # 보유 포지션 hold_days 갱신 (거래일 기준)
        positions = self._ds.get_open_positions()
        today = datetime.now().date()
        for pos in positions:
            try:
                entry = datetime.strptime(pos["entry_date"], "%Y-%m-%d").date()
                hold_days = count_trading_days(entry, today)
                self._ds.update_position(pos["id"], hold_days=hold_days)
            except (ValueError, KeyError):
                pass

        # 전일 일봉 확정분 반영: high_since_entry 재동기화
        try:
            self._sync_high_since_entry()
        except Exception as e:
            logger.warning(f"일일 리셋 시 high_since_entry 보정 실패: {e}")

        # 헬스모니터 알림 중복 방지 세트 초기화
        try:
            self._health.reset_alerts()
        except AttributeError:
            pass

        logger.info("일일 리셋 완료")

    def _run_health_check(self) -> None:
        """1분 주기 헬스체크 스케줄 job."""
        try:
            warnings = self._health.check_health()
            if warnings:
                logger.warning("헬스체크 경고: {}", "; ".join(warnings))
        except AttributeError:
            pass
