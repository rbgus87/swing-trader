"""ScreenerMixin — 스크리닝 로직."""
from __future__ import annotations
import asyncio
from datetime import datetime
from loguru import logger
from src.utils.config import config
from src.data_pipeline.db import get_data_db
from src.strategy.trend_following_v2 import (
    StrategyParams,
    calculate_indicators as calc_v23_indicators,
)


V23_EXCLUDED_TYPES = ('SPAC', 'REIT', 'FOREIGN', 'PREFERRED')
V23_BREADTH_GATE = 0.40


class ScreenerMixin:
    """스크리닝 관련 메서드."""

    async def _pre_market_screening(self, _retry: int = 0):
        """장전 스크리닝 — v2.6 상태 기반 추세추종 후보 생성 + ETF IBS 판단 (08:30)."""
        max_retries = 3

        # 토큰 갱신
        try:
            await self._ensure_connection()
        except Exception as e:
            logger.warning(f"스크리닝 전 토큰 갱신 실패: {e}")

        try:
            await asyncio.to_thread(self._v23_screen_universe)
        except Exception as e:
            logger.error(f"v2.6 스크리닝 실패 (시도 {_retry + 1}/{max_retries + 1}): {e}")
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

        # ETF IBS 평균회귀 — 주식 스크리닝 완료 후 유휴 현금 기준으로 ETF 진입 판단
        if self._etf_params.enabled:
            try:
                yesterday_ohlcv = self._get_yesterday_kospi_ohlcv()
                if yesterday_ohlcv:
                    idle_cash = self._get_idle_cash_for_etf()
                    self._etf_handler.evaluate_at_screening(yesterday_ohlcv, idle_cash)
            except Exception as e:
                logger.warning(f"ETF IBS 스크리닝 실패 (무시): {e}")

    async def _queue_premarket_orders(self) -> None:
        """동시호가 큐 구성 (08:35) — ETF 전용.

        주식 동시호가는 비활성화. 주식은 09:30 장중 진입 경로만 사용.
        ETF 큐잉은 _pre_market_screening의 evaluate_at_screening에서 완료됨.
        """
        # [A-3 주식 동시호가 비활성 — 복원 필요 시 if False: 제거]
        if False:  # noqa: SIM210
            held = {p['code'] for p in self._get_cached_positions() if p.get('status') == 'open'}
            open_slots = self._max_positions - len(held)
            if open_slots <= 0:
                return
            candidates: list[dict] = []
            for code in self._candidates:
                if code in held:
                    continue
                cache = self._v23_entry_cache.get(code)
                if cache and cache.get('entry_ready'):
                    candidates.append({
                        'code': code,
                        'name': self._poll_stock_names.get(code, code),
                        'score': cache.get('composite_score', cache.get('adx', 0.0)),
                        'atr': cache.get('atr', 0.0),
                        'cache_data': cache,
                    })
            candidates.sort(key=lambda x: x['score'], reverse=True)
            for c in candidates:
                c.setdefault('industry', self._v23_entry_cache.get(c['code'], {}).get('industry', 'UNKNOWN'))
            held_with_industry = [
                {'code': p['code'], 'industry': self._get_stock_industry(p['code'])}
                for p in self._get_cached_positions() if p.get('status') == 'open'
            ]
            from src.strategy.sector_constraint import filter_by_sector
            candidates = filter_by_sector(candidates, held_with_industry, self._sector_constraint)
            self._premarket_queue = candidates[:open_slots]
            if self._premarket_queue:
                names = [f"{c['name']}({c['code']})" for c in self._premarket_queue]
                logger.bind(
                    event="PREMARKET_QUEUE",
                    data={"queued": [c["code"] for c in self._premarket_queue]},
                ).info("동시호가 큐: {}종목 — {}", len(self._premarket_queue), ", ".join(names))
                self._telegram.send(
                    f"📋 동시호가 큐 ({len(self._premarket_queue)}종목): {', '.join(names)}"
                )

        # 주식 큐 비워두기 (장중 진입만 사용)
        self._premarket_queue = []

        # ETF 큐 상태 로깅
        if self._etf_params.enabled and self._etf_handler.has_queued_order:
            action = self._etf_handler._queued_action
            logger.bind(
                event="PREMARKET_QUEUE",
                data={"queued": [self._etf_params.etf_code], "type": "ETF"},
            ).info("동시호가 큐: ETF {}", action)
            self._telegram.send(
                f"📋 동시호가 큐: ETF {action} ({self._etf_params.etf_code})"
            )
        else:
            logger.info("동시호가 큐: 주문 없음 (주식=장중 진입, ETF=조건 미충족)")

    async def _submit_premarket_orders(self) -> None:
        """동시호가 주문 제출 (08:50) — ETF 전용.

        주식 동시호가는 비활성화. ETF만 처리.
        ETF 09:00 매도 체결 → 매도대금 09:30 주식 매수에 재사용 가능.
        """
        # [A-3 주식 동시호가 비활성 — 복원 필요 시 if False: 제거]
        if False:  # noqa: SIM210
            if not self._premarket_queue:
                return
            valid_items: list[dict] = []
            for item in list(self._premarket_queue):
                code = item['code']
                name = item['name']
                signal = Signal(
                    code=code, name=name, signal_type="buy",
                    price=0, score=item['score'],
                )
                risk_result = self._risk_mgr.pre_check(signal)
                if not risk_result.approved:
                    continue
                prev_close = item['cache_data'].get('close', 0)
                if prev_close <= 0:
                    continue
                alloc = self._calculate_alloc()
                qty = int(alloc) // max(1, int(prev_close))
                if qty <= 0 or alloc < self._min_position_amount:
                    continue
                item['qty'] = qty
                item['prev_close'] = int(prev_close)
                valid_items.append(item)
            for item in valid_items:
                code = item['code']
                name = item['name']
                qty = item['qty']
                if self.mode == "paper":
                    prev_close = item['prev_close']
                    from src.engine.paper_fill import simulate_fill_price
                    avg_tv = float(
                        item.get('cache_data', {}).get('avg_trading_value_20', 1e10) or 1e10
                    )
                    fill_price = simulate_fill_price(
                        prev_close, float(prev_close * qty), avg_tv, side="buy",
                        params=self._paper_fill_params,
                        slippage_params=self._slippage_params,
                    )
                    mock_tick = Tick(
                        code=code, price=fill_price, volume=0, timestamp=datetime.now()
                    )
                    await self._record_buy(mock_tick, qty, "TF_v2.6_pre")
                    self._v23_entry_cache.pop(code, None)
                    if code in self._candidates:
                        try:
                            self._candidates.remove(code)
                        except ValueError:
                            pass
            self._premarket_queue.clear()

        # ETF 동시호가 제출
        if self._etf_params.enabled and self._etf_handler.has_queued_order:
            try:
                await self._etf_handler.submit_queued_order()
            except Exception as e:
                logger.error(f"ETF 동시호가 제출 실패: {e}")
                self._telegram.send(f"⚠️ ETF 동시호가 제출 오류: {e}")
        else:
            logger.info("동시호가 제출: ETF 주문 없음 — 스킵")

    def _v23_screen_universe(self):
        """v2.6 진입 조건으로 Universe를 스캔 → self._candidates + self._v23_entry_cache 갱신."""
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

        # 2. 가드레일: breadth + KOSPI MA200 이중 게이트
        breadth = self._compute_breadth(today)
        self._breadth_value = breadth
        breadth_ok = breadth >= V23_BREADTH_GATE
        logger.info(
            f"breadth={breadth:.1%} {'OK' if breadth_ok else 'FAIL'} "
            f"(>= {V23_BREADTH_GATE:.0%})"
        )
        regime_ok = True
        regime_gate_enabled = config.get("trend_following.regime_gate_enabled", True)
        if regime_gate_enabled:
            regime_ok = self._market_regime.check()
            logger.info(
                f"KOSPI MA200: {'OK' if regime_ok else 'FAIL'} "
                f"(close={self._market_regime.kospi_close:,}, "
                f"ma200={self._market_regime.kospi_sma200:,.0f})"
            )
        self._breadth_ok = breadth_ok and regime_ok
        logger.info(f"gate={'OPEN' if self._breadth_ok else 'CLOSED'}")

        # 3. Universe 구축 (어제 기준 — 스크리닝 시점에는 당일 시총 미확정)
        with get_data_db() as conn:
            universe_row = conn.execute(
                """
                SELECT DISTINCT m.ticker, s.name, s.market, s.industry
                FROM market_cap_history m
                JOIN stocks s ON m.ticker = s.ticker
                WHERE m.date = (
                    SELECT MAX(date) FROM market_cap_history WHERE date < ?
                )
                  AND m.market_cap >= ?
                  AND s.stock_type NOT IN (?, ?, ?, ?)
                  AND (s.delisted_date IS NULL OR s.delisted_date > ?)
                """,
                (today, self._mcap_threshold, *V23_EXCLUDED_TYPES, today),
            ).fetchall()
        universe = [
            (r['ticker'], r['name'], r['market'], r['industry'])
            for r in universe_row
        ]
        logger.info(f"v2.6 Universe: {len(universe)}종목")

        # 4. 각 종목 일봉 로드 + 조건 체크
        candidates = {}
        with get_data_db() as conn:
            for ticker, name, market, industry in universe:
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

                # rs: bench_ret이 None이면 0 (RS 필터를 통과한 경우엔 항상 유효)
                rs_val = (
                    float(t['stock_ret_n']) - float(bench_ret)
                    if bench_ret is not None
                    else 0.0
                )
                candidates[ticker] = {
                    'code': ticker,
                    'atr': float(t['atr']),
                    'adx': float(t['adx']),
                    'ma60_dist': float(t['ma60_dist']),
                    'macd_hist': float(t['macd_hist']),
                    'close': float(t['close']),
                    'entry_ready': True,
                    'name': name,
                    'industry': industry or 'UNKNOWN',
                    # 복합 랭킹용 추가 데이터
                    'rs': rs_val,
                    'stock_ret_n': float(t['stock_ret_n']),
                    'atr_ratio': atr_ratio,
                    'avg_trading_value_20': float(t['avg_trading_value_20']),
                }
                self._poll_stock_names[ticker] = name or ticker

        # 5. 복합 스코어 계산 + 상태 갱신
        from src.strategy.ranking import RankingWeights, compute_composite_score
        _weights = RankingWeights.from_config(config.data.get("trend_following", {}))
        scored = compute_composite_score(list(candidates.values()), _weights)

        self._candidates = [c['code'] for c in scored]
        self._v23_entry_cache = {c['code']: c for c in scored}
        self._atr_cache.clear()
        for c in scored:
            self._atr_cache[c['code']] = c['atr']

        logger.bind(
            event="SCREENING",
            data={
                "date": today,
                "breadth": round(breadth, 4),
                "regime_ok": bool(regime_ok),
                "gate_open": bool(self._breadth_ok),
                "candidates_count": len(candidates),
                "top_candidates": [c["code"] for c in scored[:5]],
            },
        ).info("v2.6 후보 확정: {}종목 (breadth={:.0%})", len(candidates), breadth)

        # 텔레그램 알림
        try:
            sample_lines = []
            for tkr in self._candidates[:5]:
                c = self._v23_entry_cache[tkr]
                sample_lines.append(
                    f"  {c.get('name', tkr)} ({tkr}) "
                    f"Score={c.get('composite_score', 0):.2f} "
                    f"ADX={c['adx']:.1f} RS={c.get('rs', 0):.2%}"
                )
            n_cands = len(self._v23_entry_cache)
            more = (
                f"\n  ... 외 {n_cands - 5}종목"
                if n_cands > 5 else ""
            )
            sample_text = "\n".join(sample_lines) if sample_lines else "  (없음)"
            gate_mark = "🟢 OPEN" if self._breadth_ok else "🔴 CLOSED"
            regime_label = ""
            if config.get("trend_following.regime_gate_enabled", True):
                ma200_tag = "OK" if self._market_regime.is_bullish else "FAIL"
                regime_label = f" / MA200 {ma200_tag}"
            self._telegram.send(
                f"📋 v2.6 후보 {n_cands}종목\n"
                f"시장: {gate_mark} (breadth {breadth:.0%}{regime_label})\n\n"
                f"{sample_text}{more}"
            )
        except Exception:
            pass

        # 스크리닝 완료 기록 (헬스모니터)
        try:
            self._health.record_screening()
        except AttributeError:
            pass

    def _get_yesterday_kospi_ohlcv(self) -> dict | None:
        """index_daily에서 KOSPI 가장 최근 OHLCV 조회."""
        try:
            with get_data_db() as conn:
                row = conn.execute(
                    "SELECT open, high, low, close FROM index_daily "
                    "WHERE index_code = 'KOSPI' ORDER BY date DESC LIMIT 1"
                ).fetchone()
            if row:
                return dict(row)
        except Exception as e:
            logger.warning(f"KOSPI OHLCV 조회 실패: {e}")
        return None

    def _get_idle_cash_for_etf(self) -> int:
        """v2.7 주식 투자금 + ETF 투자금을 제외한 유휴 현금."""
        available = self._get_available_capital()  # initial_capital - 주식투자금
        etf_invested = self._etf_handler.invested_amount
        return max(0, available - etf_invested)

    def _get_stock_industry(self, code: str) -> str:
        """종목의 업종 조회 (캐시 활용)."""
        if code in self._industry_cache:
            return self._industry_cache[code]
        try:
            with get_data_db() as conn:
                row = conn.execute(
                    "SELECT industry FROM stocks WHERE ticker = ?", (code,)
                ).fetchone()
            industry = row['industry'] if row and row['industry'] else "UNKNOWN"
        except Exception:
            industry = "UNKNOWN"
        self._industry_cache[code] = industry
        return industry

    def _get_cached_adx(self, code: str) -> float:
        """종목의 최신 ADX 값 (장전 스크리닝 캐시 우선, 없으면 25.0 반환)."""
        cache = self._v23_entry_cache.get(code)
        if cache and 'adx' in cache:
            return float(cache['adx'])
        return 25.0  # neutral: 동적 보유기간 미발동 (base 구간)

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
                        (today, self._mcap_threshold, *V23_EXCLUDED_TYPES, today),
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
        """장마감 후 HTS 조건검색 — v2.6 모드에서는 불필요하여 스킵.

        v2.6은 장전 _v23_screen_universe()로 Universe 기반 후보를 동적 생성하므로
        전날 저녁 조건검색 결과를 DB에 적재할 필요 없음.
        """
        logger.info(
            "v2.6 모드 — 저녁 HTS 조건검색 스킵 (장전 스크리닝으로 대체)"
        )
        return
