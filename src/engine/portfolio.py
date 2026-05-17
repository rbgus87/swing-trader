"""PortfolioMixin — 포트폴리오/포지션 관리."""
from __future__ import annotations
import sqlite3
from datetime import datetime
from loguru import logger
from src.models import ExitReason, Position, Tick
from src.broker.tr_codes import PRICE_LIMIT, PRICE_MARKET
from src.utils.config import config
from src.data_pipeline.db import get_data_db, get_trade_db
from src.utils.market_calendar import count_trading_days


class PortfolioMixin:
    """포트폴리오/포지션 관련 메서드."""

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
        """v2.6: 단일 전략 모드 — no-op (레거시 호출 호환)."""
        return

    def _get_hoga_type(self) -> str:
        """config 기반 호가 유형 반환."""
        order_type = config.get("trading.order_type", "market")
        return PRICE_LIMIT if order_type == "limit" else PRICE_MARKET

    def _get_stock_market(self, code: str) -> str:
        """종목의 시장 구분 조회 (KOSPI/KOSDAQ). 조회 실패 시 KOSPI 반환."""
        try:
            with get_data_db() as conn:
                row = conn.execute(
                    "SELECT market FROM stocks WHERE ticker = ?", (code,)
                ).fetchone()
            if row and row["market"]:
                return row["market"]
        except Exception:
            pass
        return "KOSPI"

    def _get_avg_trading_value(self, code: str) -> float:
        """종목의 20일 평균 거래대금 조회 (스크리닝 캐시 우선, 없으면 DB 직접 계산)."""
        cache = self._v23_entry_cache.get(code)
        if cache and 'avg_trading_value_20' in cache:
            return float(cache['avg_trading_value_20'])
        try:
            with get_data_db() as conn:
                row = conn.execute(
                    "SELECT AVG(CAST(close AS REAL) * volume) AS avg_tv "
                    "FROM ("
                    "  SELECT close, volume FROM daily_candles "
                    "  WHERE ticker = ? ORDER BY date DESC LIMIT 20"
                    ")",
                    (code,),
                ).fetchone()
            if row and row['avg_tv']:
                return float(row['avg_tv'])
        except Exception:
            pass
        return 1e10  # 기본값 — 슬리피지 최소화 (데이터 없을 때 안전 폴백)

    def _get_available_capital(self) -> int:
        """가용 자본 조회 — 초기자본 - 투자중 금액."""
        positions = self._get_cached_positions()
        invested = sum(p["entry_price"] * p["quantity"] for p in positions)
        return max(0, self._initial_capital - invested)

    def _calculate_alloc(self) -> int:
        """진입 alloc 금액 계산 (v2.5).

        sizing_mode == 'equity': alloc = total_equity / max_positions (균등 + 복리)
        sizing_mode == 'cash':   alloc = cash × (1/max_positions)         (v2.4 호환)
        둘 다 cash로 상한 제한 (없는 돈으로 매수 방지).
        """
        cash = self._get_available_capital()
        max_pos = max(1, self._max_positions)

        if self._sizing_mode == "equity":
            equity = cash
            for p in self._get_cached_positions():
                code = p.get("code", "")
                qty = p.get("quantity", 0)
                price = self._latest_prices.get(code, p.get("entry_price", 0))
                equity += int(price) * int(qty)
            alloc = equity // max_pos
        else:
            alloc = int(cash * (1.0 / max_pos))

        return int(min(alloc, cash))

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
                hold_days = count_trading_days(entry, datetime.now().date())
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
            initial_quantity=d.get("initial_quantity", 0) or d["quantity"],
            tp2_price=d.get("tp2_price", 0),
            partial_sold_2=bool(d.get("partial_sold_2", 0)),
            initial_stop_price=d.get("initial_stop_price", 0) or d["stop_price"],
            atr_at_entry=float(d.get("atr_at_entry", 0.0) or 0.0),
            entry_adx=float(d.get("entry_adx", 0.0) or 0.0),
            scale_in_triggered=bool(d.get("scale_in_triggered", 0)),
            scale_in_price=int(d.get("scale_in_price", 0) or 0),
            scale_in_target_qty=int(d.get("scale_in_target_qty", 0) or 0),
            original_alloc=int(d.get("original_alloc", 0) or 0),
            tranche_count=int(d.get("tranche_count", 1) or 1),
        )

    # ── 장마감 ──

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
            logger.bind(
                event="RISK_UPDATE",
                data={
                    "equity": round(portfolio_value, 2),
                    "current_mdd": round(float(self._risk_mgr.current_mdd), 4),
                    "positions_count": len(positions),
                    "gate_status": gate_status,
                },
            ).info(
                "포트폴리오 스냅샷: equity={:,.0f}, MDD={:.1%}",
                portfolio_value,
                self._risk_mgr.current_mdd,
            )

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
