"""포트폴리오 레벨 스윙 백테스트 엔진.

시간축 기반 시뮬레이션:
- 매 거래일 순회하며 청산/진입 처리
- 동시 보유 4종목 제한
- Universe Pool 분기별 재계산
- TP1 분할 매도 정확 반영
"""
from dataclasses import dataclass, field
from typing import Optional
import pandas as pd
import numpy as np
from loguru import logger

from src.data_pipeline.db import get_connection
from src.strategy.trend_following_v0 import StrategyParams, calculate_indicators
from src.backtest.swing_backtester import CostModel


@dataclass
class Position:
    ticker: str
    name: str
    entry_date: str
    entry_price: float
    shares: int
    initial_shares: int
    allocated_capital: float
    atr_at_entry: float
    stop_price: float
    tp1_price: float
    highest_since_entry: float
    hold_days: int = 0
    tp1_triggered: bool = False
    strategy: str = 'TF'       # 'TF' or 'MR'


@dataclass
class PortfolioTradeResult:
    ticker: str
    name: str
    entry_date: str
    entry_price: float
    exit_date: str
    exit_price: float
    exit_reason: str
    hold_days: int
    shares: int
    pnl_amount: float
    pnl_pct: float
    is_partial: bool = False


@dataclass
class PortfolioResult:
    period: str
    initial_capital: float
    final_capital: float
    total_return_pct: float
    cagr_pct: float
    max_drawdown_pct: float
    total_trades: int
    winning_trades: int
    win_rate: float
    profit_factor: float
    avg_hold_days: float
    avg_positions: float
    trades: list = field(default_factory=list)
    equity_curve: list = field(default_factory=list)
    exit_reason_dist: dict = field(default_factory=dict)
    monthly_returns: dict = field(default_factory=dict)


MCAP_THRESHOLD = 3_000_000_000_000       # 시총 3조 (fix2 확정)
TRADING_VALUE_THRESHOLD = 5_000_000_000  # 거래대금 50억 (fix2 확정)
EXCLUDED_TYPES = ('SPAC', 'REIT', 'FOREIGN', 'PREFERRED')
UNIVERSE_REFRESH_DAYS = 60
BREADTH_GATE_THRESHOLD = 0.4             # Universe 중 >MA200 비율 40% 이상 시 진입 허용


@dataclass
class RiskParams:
    """Phase 4-1b 포지션 사이징 기반 리스크 파라미터.

    진입 차단이 아닌 베팅 크기 축소로 DD를 완화한다.
    DD 레벨별 3단계 alloc: normal 25% / caution 15% / crisis 10%.
    """
    # 드로다운 기반 포지션 사이징
    dd_normal_threshold: float = -0.15    # DD ≤ -15%: caution 진입
    dd_caution_threshold: float = -0.25   # DD ≤ -25%: crisis 진입
    dd_recovery_threshold: float = -0.10  # DD > -10%: normal 복귀 (caution에서만)
    alloc_normal: float = 0.25
    alloc_caution: float = 0.15
    alloc_crisis: float = 0.10
    # 재진입 쿨다운 (STOP_LOSS 후 동일 종목)
    ticker_sl_cooldown: int = 5
    # 일일 손실 한도 (전일 equity 대비 당일 실현손익)
    daily_loss_limit: float = -0.03

    # ATR 역비례 사이징 (종목 특성 기반, DD 기반 사이징과 배타적)
    atr_sizing_risk_pct: float = 0.02    # 계좌 2% risk
    atr_sizing_max_pct: float = 0.30     # 한 종목 최대 30%

    # 개별 규칙 on/off (실험용)
    enable_sizing: bool = True           # DD 기반 3단계 사이징
    enable_atr_sizing: bool = False      # ATR 역비례 사이징 (enable_sizing과 배타)
    enable_daily_loss: bool = True
    enable_ticker_cooldown: bool = True


def build_universe(date_str: str, conn) -> set:
    """특정 일자 기준 Universe Pool 구성."""
    cursor = conn.execute("""
        SELECT m.ticker
        FROM market_cap_history m
        JOIN stocks s ON m.ticker = s.ticker
        WHERE m.date = ?
          AND m.market_cap >= ?
          AND s.stock_type NOT IN (?, ?, ?, ?)
          AND (s.delisted_date IS NULL OR s.delisted_date > ?)
          AND s.ticker NOT IN (
              SELECT ticker FROM stock_status_events
              WHERE event_type = 'ADMIN_DESIGNATED'
                AND start_date <= ?
                AND (end_date IS NULL OR end_date > ?)
          )
    """, (date_str, MCAP_THRESHOLD, *EXCLUDED_TYPES, date_str, date_str, date_str))

    mcap_tickers = {row['ticker'] for row in cursor.fetchall()}
    if not mcap_tickers:
        return set()

    placeholders = ','.join('?' * len(mcap_tickers))
    cursor = conn.execute(f"""
        SELECT ticker, AVG(close * volume) as avg_tv, COUNT(*) as n
        FROM daily_candles
        WHERE date <= ? AND date >= date(?, '-60 day')
          AND ticker IN ({placeholders})
        GROUP BY ticker
        HAVING n >= 5 AND avg_tv >= ?
    """, (date_str, date_str, *mcap_tickers, TRADING_VALUE_THRESHOLD))

    return {row['ticker'] for row in cursor.fetchall()}


def load_all_candles(tickers: set, params: StrategyParams = None) -> dict:
    """전 종목 일봉 로드 + 지표 계산."""
    if params is None:
        params = StrategyParams()
    result = {}

    with get_connection() as conn:
        for ticker in tickers:
            cursor = conn.execute("""
                SELECT date, open, high, low, close, volume
                FROM daily_candles WHERE ticker = ?
                ORDER BY date
            """, (ticker,))
            rows = cursor.fetchall()

            if len(rows) < params.ma_long + 10:
                continue

            df = pd.DataFrame([dict(r) for r in rows])
            df['date'] = pd.to_datetime(df['date'])
            df = calculate_indicators(df, params)
            df['ma200'] = df['close'].rolling(200).mean()   # 가드레일용
            df = df.reset_index(drop=True)
            result[ticker] = df

    return result


def load_backtest_data(params: StrategyParams = None) -> dict:
    """백테스트에 필요한 데이터(거래일/종목풀/지표) 1회 로드.

    반복 실험에서 재사용 목적. run_portfolio_backtest(preloaded_data=...)로 전달.

    Returns dict keys:
        trading_dates, initial_universe, all_possible,
        ticker_data, ticker_date_idx, ticker_names
    """
    if params is None:
        params = StrategyParams()

    logger.info("Loading trading dates...")
    with get_connection() as conn:
        cursor = conn.execute("""
            SELECT DISTINCT date FROM daily_candles
            WHERE date >= '2014-01-02'
            ORDER BY date
        """)
        trading_dates = [row['date'] for row in cursor.fetchall()]
    logger.info(f"Trading dates: {len(trading_dates)}")

    logger.info("Building initial universe...")
    with get_connection() as conn:
        initial_universe = build_universe(trading_dates[0], conn)
    logger.info(f"Initial universe: {len(initial_universe)} tickers")

    logger.info("Collecting candidate tickers (ever-eligible)...")
    with get_connection() as conn:
        cursor = conn.execute("""
            SELECT DISTINCT m.ticker
            FROM market_cap_history m
            JOIN stocks s ON m.ticker = s.ticker
            WHERE m.market_cap >= ?
              AND s.stock_type NOT IN (?, ?, ?, ?)
        """, (MCAP_THRESHOLD, *EXCLUDED_TYPES))
        all_possible = {row['ticker'] for row in cursor.fetchall()}
    logger.info(f"Candidate tickers: {len(all_possible)}")

    logger.info("Loading candle data + computing indicators...")
    ticker_data = load_all_candles(all_possible, params)
    logger.info(f"Loaded candle data for {len(ticker_data)} tickers")

    ticker_date_idx = {}
    for tk, df in ticker_data.items():
        ticker_date_idx[tk] = {d: i for i, d in enumerate(df['date'])}

    with get_connection() as conn:
        cursor = conn.execute("SELECT ticker, name FROM stocks")
        ticker_names = {row['ticker']: row['name'] for row in cursor.fetchall()}

    return {
        'trading_dates': trading_dates,
        'initial_universe': initial_universe,
        'all_possible': all_possible,
        'ticker_data': ticker_data,
        'ticker_date_idx': ticker_date_idx,
        'ticker_names': ticker_names,
    }


def precompute_daily_signals(
    trading_dates: list,
    ticker_data: dict,
    ticker_date_idx: dict,
    initial_universe: set,
    params: StrategyParams = None,
) -> dict:
    """SL/Trail/Hold/TP와 무관한 일별 정보를 사전 계산.

    진입 조건(adx_threshold, volume_multiplier, min_trading_value)은 params에 의존.
    이 값들이 바뀌면 precompute를 재실행해야 함.

    Returns:
        {
            'breadth':      {date_str: float},
            'candidates':   {date_str: [dict]},  # score 내림차순
            'universe_at':  {date_str: set},
            'universe_refresh_count': int,
        }
    """
    if params is None:
        params = StrategyParams()

    breadth_by_date = {}
    candidates_by_date = {}
    universe_by_date = {}

    universe = set(initial_universe)
    last_refresh = 0
    refresh_count = 1

    for day_idx, date_str in enumerate(trading_dates):
        ts = pd.Timestamp(date_str)
        if day_idx - last_refresh >= UNIVERSE_REFRESH_DAYS:
            with get_connection() as conn:
                universe = build_universe(date_str, conn)
            last_refresh = day_idx
            refresh_count += 1

        universe_by_date[date_str] = set(universe)

        # breadth
        above = 0
        total_b = 0
        for tk in universe:
            idx_map_b = ticker_date_idx.get(tk, {})
            bi = idx_map_b.get(ts)
            if bi is None or bi < 199:
                continue
            br_row = ticker_data[tk].iloc[bi]
            ma200_v = br_row.get('ma200')
            if pd.isna(ma200_v):
                continue
            total_b += 1
            if br_row['close'] > ma200_v:
                above += 1
        breadth_by_date[date_str] = above / total_b if total_b > 0 else 0.5

        # candidates (held_set 제외는 시뮬에서)
        cands = []
        for ticker in universe:
            idx_map = ticker_date_idx.get(ticker)
            if not idx_map:
                continue
            curr_i = idx_map.get(ts)
            if curr_i is None or curr_i == 0:
                continue
            day = ticker_data[ticker].iloc[curr_i]
            if (pd.isna(day.get('ma5')) or pd.isna(day.get('ma60'))
                    or pd.isna(day.get('adx')) or pd.isna(day.get('atr'))):
                continue
            if day['atr'] <= 0:
                continue
            aligned = day['ma5'] > day['ma20'] > day['ma60']
            trending = day['adx'] >= params.adx_threshold
            liquid = day.get('avg_trading_value_20', 0) >= params.min_trading_value
            if not (aligned and trending and liquid):
                continue
            prev = ticker_data[ticker].iloc[curr_i - 1]
            if pd.isna(prev.get('highest_n')):
                continue
            breakout = day['close'] > prev['highest_n']
            vol_confirm = day['volume'] > day['avg_volume_20'] * params.volume_multiplier
            if breakout and vol_confirm:
                rsi_v = day.get('rsi14')
                rsi_v = float(rsi_v) if (rsi_v is not None and pd.notna(rsi_v)) else 50.0
                atr_ratio = float(day['atr'] / day['close']) if day['close'] > 0 else 0.0
                cands.append({
                    'ticker': ticker,
                    'score': float(day['adx']),
                    'close': float(day['close']),
                    'atr': float(day['atr']),
                    'rsi14': rsi_v,
                    'atr_ratio': atr_ratio,
                })
        cands.sort(key=lambda x: x['score'], reverse=True)
        candidates_by_date[date_str] = cands

    return {
        'breadth': breadth_by_date,
        'candidates': candidates_by_date,
        'universe_at': universe_by_date,
        'universe_refresh_count': refresh_count,
    }


def precompute_pullback_signals(
    trading_dates: list,
    ticker_data: dict,
    ticker_date_idx: dict,
    initial_universe: set,
    params: StrategyParams = None,
    momentum_period: int = 20,
    momentum_top_n: int = 50,
    atr_ratio_min: float = 0.025,
    atr_ratio_max: float = 0.08,
    rsi_min: float = 40.0,
    rsi_max: float = 55.0,
    bearish_lookback: int = 3,
    bearish_count_min: int = 2,
) -> dict:
    """레퍼런스 눌림목 전략 진입 후보 사전 계산.

    필터: MA20>MA60, ADX≥20, 거래대금 50억+, ATR/close 2.5~8%,
          20일 수익률 상위 momentum_top_n.
    진입: close<MA5, RSI rsi_min~rsi_max, 직전 bearish_lookback일 중
          bearish_count_min 이상 음봉, 당일 양봉 + 거래량 > 전일.
    """
    if params is None:
        params = StrategyParams()

    breadth_by_date = {}
    candidates_by_date = {}
    universe_by_date = {}

    universe = set(initial_universe)
    last_refresh = 0
    refresh_count = 1

    for day_idx, date_str in enumerate(trading_dates):
        ts = pd.Timestamp(date_str)
        if day_idx - last_refresh >= UNIVERSE_REFRESH_DAYS:
            with get_connection() as conn:
                universe = build_universe(date_str, conn)
            last_refresh = day_idx
            refresh_count += 1

        universe_by_date[date_str] = set(universe)

        # breadth (공용)
        above = 0
        total_b = 0
        for tk in universe:
            idx_map_b = ticker_date_idx.get(tk, {})
            bi = idx_map_b.get(ts)
            if bi is None or bi < 199:
                continue
            br_row = ticker_data[tk].iloc[bi]
            ma200_v = br_row.get('ma200')
            if pd.isna(ma200_v):
                continue
            total_b += 1
            if br_row['close'] > ma200_v:
                above += 1
        breadth_by_date[date_str] = above / total_b if total_b > 0 else 0.5

        # 1차 필터 + 20일 모멘텀 랭킹
        scored = []
        for ticker in universe:
            idx_map = ticker_date_idx.get(ticker)
            if not idx_map:
                continue
            curr_i = idx_map.get(ts)
            if curr_i is None or curr_i < momentum_period:
                continue
            day = ticker_data[ticker].iloc[curr_i]
            if (pd.isna(day.get('ma5')) or pd.isna(day.get('ma20'))
                    or pd.isna(day.get('ma60')) or pd.isna(day.get('adx'))
                    or pd.isna(day.get('atr')) or pd.isna(day.get('rsi14'))):
                continue
            if day['atr'] <= 0 or day['close'] <= 0:
                continue
            if not (day['ma20'] > day['ma60']):
                continue
            if day['adx'] < params.adx_threshold:
                continue
            if day.get('avg_trading_value_20', 0) < params.min_trading_value:
                continue
            atr_ratio = day['atr'] / day['close']
            if atr_ratio < atr_ratio_min or atr_ratio > atr_ratio_max:
                continue
            prev_n = ticker_data[ticker].iloc[curr_i - momentum_period]['close']
            if prev_n <= 0:
                continue
            mom = (day['close'] / prev_n) - 1
            scored.append((mom, ticker, day, curr_i, atr_ratio))

        scored.sort(key=lambda x: x[0], reverse=True)
        top_pool = scored[:momentum_top_n]

        # 진입 조건 (눌림 + 반등)
        cands = []
        for mom, ticker, day, curr_i, atr_ratio in top_pool:
            if not (day['close'] < day['ma5']):
                continue
            if not (rsi_min <= day['rsi14'] <= rsi_max):
                continue
            if curr_i < bearish_lookback:
                continue
            bearish_n = 0
            for k in range(1, bearish_lookback + 1):
                prev = ticker_data[ticker].iloc[curr_i - k]
                if prev['close'] < prev['open']:
                    bearish_n += 1
            if bearish_n < bearish_count_min:
                continue
            if day['close'] <= day['open']:
                continue
            prev = ticker_data[ticker].iloc[curr_i - 1]
            if day['volume'] <= prev['volume']:
                continue

            cands.append({
                'ticker': ticker,
                'score': float(mom),
                'close': float(day['close']),
                'atr': float(day['atr']),
                'rsi14': float(day['rsi14']),
                'atr_ratio': float(atr_ratio),
            })
        cands.sort(key=lambda x: x['score'], reverse=True)
        candidates_by_date[date_str] = cands

    return {
        'breadth': breadth_by_date,
        'candidates': candidates_by_date,
        'universe_at': universe_by_date,
        'universe_refresh_count': refresh_count,
    }


def run_portfolio_backtest(
    initial_capital: float = 5_000_000,
    max_positions: int = 4,
    params: StrategyParams = None,
    cost: CostModel = None,
    min_position_amount: float = 300_000,
    preloaded_data: dict = None,
    precomputed: dict = None,
    risk: "RiskParams | None" = None,
) -> PortfolioResult:
    """포트폴리오 레벨 백테스트 실행.

    preloaded_data: load_backtest_data()의 반환값. 제공 시 데이터 로딩 스킵.
    risk: RiskParams 객체. None이면 리스크 규칙 미적용 (v2 baseline).
    """
    if params is None:
        params = StrategyParams()
    if cost is None:
        cost = CostModel()

    if preloaded_data is None:
        preloaded_data = load_backtest_data(params)

    trading_dates = preloaded_data['trading_dates']
    universe = set(preloaded_data['initial_universe'])
    ticker_data = preloaded_data['ticker_data']
    ticker_date_idx = preloaded_data['ticker_date_idx']
    ticker_names = preloaded_data['ticker_names']

    cash = initial_capital
    positions = []
    trades = []
    equity_curve = []
    total_cost_pct = cost.total_cost_pct()

    last_universe_refresh = 0
    universe_refresh_count = 1
    max_concurrent = 0
    concurrent_sum = 0
    gate_open_days = 0
    gate_closed_days = 0
    universe_size_sum = 0

    if precomputed is not None:
        universe_refresh_count = precomputed.get('universe_refresh_count', 1)

    # ── 리스크 상태 (포지션 사이징 방식) ──
    peak_equity = initial_capital
    prev_eod_equity = initial_capital
    dd_state = 'normal'             # 'normal' | 'caution' | 'crisis'
    ticker_cooldown = {}            # {ticker: cooldown_end_day_idx}

    # ── 리스크 발동 통계 ──
    dd_normal_days = 0
    dd_caution_days = 0
    dd_crisis_days = 0
    daily_loss_trigger_count = 0
    ticker_cooldown_block_count = 0

    for day_idx, date_str in enumerate(trading_dates):
        ts = pd.Timestamp(date_str)
        if precomputed is not None:
            universe = precomputed['universe_at'].get(date_str, universe)
        else:
            if day_idx - last_universe_refresh >= UNIVERSE_REFRESH_DAYS:
                with get_connection() as conn:
                    universe = build_universe(date_str, conn)
                last_universe_refresh = day_idx
                universe_refresh_count += 1

        universe_size_sum += len(universe)

        # 당일 실현 PnL (일일 손실 한도용)
        today_realized_pnl = 0.0

        # 현재 일자의 보유 포지션 및 후보군 가격 lookup
        def get_day_row(ticker):
            idx_map = ticker_date_idx.get(ticker)
            if not idx_map:
                return None, None
            i = idx_map.get(ts)
            if i is None:
                return None, None
            return ticker_data[ticker].iloc[i], i

        # ── 1. 보유 종목 청산 체크 ──
        closed_positions = []
        for pos in positions[:]:
            day, curr_i = get_day_row(pos.ticker)
            if day is None:
                continue

            pos.hold_days += 1
            pos.highest_since_entry = max(pos.highest_since_entry, day['high'])

            trailing_stop = pos.highest_since_entry - pos.atr_at_entry * params.trailing_atr

            exit_price = None
            exit_reason = None

            # 1. 손절
            if day['low'] <= pos.stop_price:
                exit_price = pos.stop_price
                exit_reason = 'STOP_LOSS'

            # 2. TP1 (take_profit_atr > 0 이고 아직 미발동이면)
            elif (params.take_profit_atr > 0
                  and not pos.tp1_triggered
                  and day['high'] >= pos.tp1_price):
                partial_shares = int(pos.shares * params.tp1_sell_ratio)
                if partial_shares > 0:
                    pnl_pct = (pos.tp1_price / pos.entry_price - 1) - total_cost_pct
                    pnl_amount = (partial_shares * pos.entry_price
                                  * (pos.tp1_price / pos.entry_price - 1)
                                  - partial_shares * pos.entry_price * total_cost_pct)
                    trades.append(PortfolioTradeResult(
                        ticker=pos.ticker,
                        name=ticker_names.get(pos.ticker, pos.ticker),
                        entry_date=pos.entry_date,
                        entry_price=pos.entry_price,
                        exit_date=date_str,
                        exit_price=pos.tp1_price,
                        exit_reason='TAKE_PROFIT_1',
                        hold_days=pos.hold_days,
                        shares=partial_shares,
                        pnl_amount=pnl_amount,
                        pnl_pct=pnl_pct,
                        is_partial=True,
                    ))
                    cash += partial_shares * pos.tp1_price
                    pos.shares -= partial_shares
                    pos.tp1_triggered = True
                    today_realized_pnl += pnl_amount
                continue

            # 3. 트레일링
            elif day['low'] <= trailing_stop:
                exit_price = trailing_stop
                exit_reason = 'TRAILING'

            # 4. 추세 이탈
            elif pos.hold_days > 1 and curr_i is not None and curr_i > 0:
                prev = ticker_data[pos.ticker].iloc[curr_i - 1]
                if (pd.notna(prev.get('ma5')) and pd.notna(prev.get('ma20')) and
                    pd.notna(day.get('ma5')) and pd.notna(day.get('ma20'))):
                    if prev['ma5'] >= prev['ma20'] and day['ma5'] < day['ma20']:
                        exit_price = day['close']
                        exit_reason = 'TREND_EXIT'

            # 5. 시간 청산
            if exit_price is None and pos.hold_days >= params.max_hold_days:
                exit_price = day['close']
                exit_reason = 'TIME_EXIT'

            if exit_price is not None:
                pnl_pct = (exit_price / pos.entry_price - 1) - total_cost_pct
                pnl_amount = (pos.shares * pos.entry_price * (exit_price / pos.entry_price - 1)
                              - pos.shares * pos.entry_price * total_cost_pct)
                trades.append(PortfolioTradeResult(
                    ticker=pos.ticker,
                    name=ticker_names.get(pos.ticker, pos.ticker),
                    entry_date=pos.entry_date,
                    entry_price=pos.entry_price,
                    exit_date=date_str,
                    exit_price=exit_price,
                    exit_reason=exit_reason,
                    hold_days=pos.hold_days,
                    shares=pos.shares,
                    pnl_amount=pnl_amount,
                    pnl_pct=pnl_pct,
                    is_partial=pos.tp1_triggered,
                ))
                cash += pos.shares * exit_price
                today_realized_pnl += pnl_amount
                closed_positions.append(pos)

                # 재진입 쿨다운
                if risk is not None and risk.enable_ticker_cooldown and exit_reason == 'STOP_LOSS':
                    ticker_cooldown[pos.ticker] = day_idx + risk.ticker_sl_cooldown

        for pos in closed_positions:
            positions.remove(pos)

        # ── 시장 국면 breadth 계산 (MA200 위 비율) ──
        if precomputed is not None:
            breadth = precomputed['breadth'].get(date_str, 0.5)
        else:
            above = 0
            total_b = 0
            for tk in universe:
                idx_map_b = ticker_date_idx.get(tk, {})
                bi = idx_map_b.get(ts)
                if bi is None or bi < 199:
                    continue
                br_row = ticker_data[tk].iloc[bi]
                ma200_v = br_row.get('ma200')
                if pd.isna(ma200_v):
                    continue
                total_b += 1
                if br_row['close'] > ma200_v:
                    above += 1
            breadth = above / total_b if total_b > 0 else 0.5
        gate_open = breadth >= BREADTH_GATE_THRESHOLD
        if gate_open:
            gate_open_days += 1
        else:
            gate_closed_days += 1

        # ── 2. 신규 진입 (gate + 일일한도 반영) ──
        block_entry_by_risk = False
        if risk is not None and risk.enable_daily_loss and prev_eod_equity > 0:
            daily_loss_pct = today_realized_pnl / prev_eod_equity
            if daily_loss_pct <= risk.daily_loss_limit:
                block_entry_by_risk = True
                daily_loss_trigger_count += 1

        open_slots = max_positions - len(positions) if gate_open else 0
        if block_entry_by_risk:
            open_slots = 0

        if open_slots > 0:
            held_set = {p.ticker for p in positions}

            if precomputed is not None:
                candidates = [c for c in precomputed['candidates'].get(date_str, [])
                              if c['ticker'] not in held_set]
            else:
                candidates = []
                for ticker in universe:
                    if ticker in held_set:
                        continue
                    day, curr_i = get_day_row(ticker)
                    if day is None or curr_i is None or curr_i == 0:
                        continue

                    if pd.isna(day.get('ma5')) or pd.isna(day.get('ma60')) or pd.isna(day.get('adx')) or pd.isna(day.get('atr')):
                        continue
                    if day['atr'] <= 0:
                        continue

                    aligned = day['ma5'] > day['ma20'] > day['ma60']
                    trending = day['adx'] >= params.adx_threshold
                    liquid = day.get('avg_trading_value_20', 0) >= params.min_trading_value

                    if not (aligned and trending and liquid):
                        continue

                    prev = ticker_data[ticker].iloc[curr_i - 1]
                    if pd.isna(prev.get('highest_n')):
                        continue

                    breakout = day['close'] > prev['highest_n']
                    vol_confirm = day['volume'] > day['avg_volume_20'] * params.volume_multiplier

                    if breakout and vol_confirm:
                        candidates.append({
                            'ticker': ticker,
                            'score': day['adx'],
                            'close': day['close'],
                            'atr': day['atr'],
                        })

                candidates.sort(key=lambda x: x['score'], reverse=True)

            filtered_cands = []
            for c in candidates:
                if risk is not None and risk.enable_ticker_cooldown:
                    cd_end = ticker_cooldown.get(c['ticker'])
                    if cd_end is not None and day_idx < cd_end:
                        ticker_cooldown_block_count += 1
                        continue
                filtered_cands.append(c)

            for cand in filtered_cands[:open_slots]:
                next_day_idx = day_idx + 1
                if next_day_idx >= len(trading_dates):
                    break
                next_date = trading_dates[next_day_idx]
                next_ts = pd.Timestamp(next_date)
                idx_map = ticker_date_idx.get(cand['ticker'], {})
                ni = idx_map.get(next_ts)
                if ni is None:
                    continue
                next_day_row = ticker_data[cand['ticker']].iloc[ni]
                entry_price = next_day_row['open']
                if entry_price <= 0:
                    continue

                current_open_slots = max_positions - len(positions)
                if current_open_slots <= 0:
                    break

                if risk is not None and risk.enable_atr_sizing:
                    # ATR 역비례 사이징 — 주당 손실 × 주수 = 계좌 리스크 일정
                    risk_amount = initial_capital * risk.atr_sizing_risk_pct
                    dollar_risk = cand['atr'] * params.stop_loss_atr
                    if dollar_risk <= 0:
                        continue
                    target_shares = int(risk_amount / dollar_risk)
                    max_shares_by_cap = int(cash * risk.atr_sizing_max_pct / entry_price)
                    shares = min(target_shares, max_shares_by_cap)
                    if shares <= 0:
                        continue
                    actual_cost = shares * entry_price
                    if actual_cost < min_position_amount or actual_cost > cash:
                        continue
                else:
                    if risk is not None and risk.enable_sizing:
                        if dd_state == 'crisis':
                            max_alloc_pct = risk.alloc_crisis
                        elif dd_state == 'caution':
                            max_alloc_pct = risk.alloc_caution
                        else:
                            max_alloc_pct = risk.alloc_normal
                    else:
                        max_alloc_pct = 1.0 / max_positions
                    alloc = min(cash * max_alloc_pct, cash)
                    if alloc < min_position_amount:
                        continue
                    shares = int(alloc / entry_price)
                    if shares <= 0:
                        continue
                    actual_cost = shares * entry_price

                cash -= actual_cost

                positions.append(Position(
                    ticker=cand['ticker'],
                    name=ticker_names.get(cand['ticker'], cand['ticker']),
                    entry_date=next_date,
                    entry_price=entry_price,
                    shares=shares,
                    initial_shares=shares,
                    allocated_capital=actual_cost,
                    atr_at_entry=cand['atr'],
                    stop_price=entry_price - cand['atr'] * params.stop_loss_atr,
                    tp1_price=entry_price + cand['atr'] * params.take_profit_atr,
                    highest_since_entry=entry_price,
                ))

        # ── 3. equity 기록 ──
        portfolio_value = cash
        for pos in positions:
            day, _ = get_day_row(pos.ticker)
            if day is not None:
                portfolio_value += pos.shares * day['close']
            else:
                portfolio_value += pos.shares * pos.entry_price

        equity_curve.append((date_str, portfolio_value))

        # ── 드로다운 상태 전이 (EOD) ──
        if risk is not None and risk.enable_sizing:
            peak_equity = max(peak_equity, portfolio_value)
            drawdown = (portfolio_value - peak_equity) / peak_equity if peak_equity > 0 else 0.0

            if dd_state == 'normal':
                if drawdown <= risk.dd_caution_threshold:
                    dd_state = 'crisis'
                elif drawdown <= risk.dd_normal_threshold:
                    dd_state = 'caution'
            elif dd_state == 'caution':
                if drawdown <= risk.dd_caution_threshold:
                    dd_state = 'crisis'
                elif drawdown > risk.dd_recovery_threshold:
                    dd_state = 'normal'
            elif dd_state == 'crisis':
                if drawdown > risk.dd_caution_threshold:
                    dd_state = 'caution'

            if dd_state == 'normal':
                dd_normal_days += 1
            elif dd_state == 'caution':
                dd_caution_days += 1
            else:
                dd_crisis_days += 1

        prev_eod_equity = portfolio_value

        max_concurrent = max(max_concurrent, len(positions))
        concurrent_sum += len(positions)

        if day_idx % 500 == 0 and day_idx > 0:
            logger.info(
                f"Day {day_idx}/{len(trading_dates)}: "
                f"equity={portfolio_value:,.0f}, "
                f"positions={len(positions)}, "
                f"trades={len(trades)}, "
                f"breadth={breadth:.0%}, "
                f"gate={'OPEN' if gate_open else 'CLOSED'}"
            )

    # 미청산 포지션 강제 청산
    last_date = trading_dates[-1]
    last_ts = pd.Timestamp(last_date)
    for pos in positions:
        idx_map = ticker_date_idx.get(pos.ticker, {})
        li = idx_map.get(last_ts)
        if li is not None:
            exit_price = ticker_data[pos.ticker].iloc[li]['close']
            pnl_pct = (exit_price / pos.entry_price - 1) - total_cost_pct
            pnl_amount = (pos.shares * pos.entry_price * (exit_price / pos.entry_price - 1)
                          - pos.shares * pos.entry_price * total_cost_pct)
            trades.append(PortfolioTradeResult(
                ticker=pos.ticker,
                name=ticker_names.get(pos.ticker, pos.ticker),
                entry_date=pos.entry_date,
                entry_price=pos.entry_price,
                exit_date=last_date,
                exit_price=exit_price,
                exit_reason='FINAL_CLOSE',
                hold_days=pos.hold_days,
                shares=pos.shares,
                pnl_amount=pnl_amount,
                pnl_pct=pnl_pct,
            ))

    logger.info(f"Universe refreshed {universe_refresh_count} times, max concurrent={max_concurrent}")
    logger.info(f"Gate OPEN days: {gate_open_days}, CLOSED days: {gate_closed_days}")
    avg_concurrent = concurrent_sum / len(trading_dates) if trading_dates else 0

    result = _compute_portfolio_summary(
        initial_capital, equity_curve, trades, trading_dates,
        avg_concurrent, max_concurrent, universe_refresh_count,
    )
    result.gate_open_days = gate_open_days
    result.gate_closed_days = gate_closed_days
    n_days = len(trading_dates) if trading_dates else 1
    result.avg_universe_size = universe_size_sum / n_days
    # 리스크 발동 통계 (포지션 사이징 방식)
    result.dd_normal_days = dd_normal_days
    result.dd_caution_days = dd_caution_days
    result.dd_crisis_days = dd_crisis_days
    result.daily_loss_trigger_count = daily_loss_trigger_count
    result.ticker_cooldown_block_count = ticker_cooldown_block_count
    return result


def _compute_portfolio_summary(
    initial_capital: float,
    equity_curve: list,
    trades: list,
    trading_dates: list,
    avg_concurrent: float,
    max_concurrent: int,
    universe_refresh_count: int,
) -> PortfolioResult:
    final_equity = equity_curve[-1][1] if equity_curve else initial_capital
    total_return = (final_equity / initial_capital) - 1

    years = len(trading_dates) / 245
    cagr = (final_equity / initial_capital) ** (1 / years) - 1 if years > 0 and final_equity > 0 else 0

    peak = equity_curve[0][1] if equity_curve else initial_capital
    max_dd = 0
    for _, eq in equity_curve:
        peak = max(peak, eq)
        dd = (peak - eq) / peak if peak > 0 else 0
        max_dd = max(max_dd, dd)

    winners = [t for t in trades if t.pnl_amount > 0]
    losers = [t for t in trades if t.pnl_amount <= 0]

    gross_profit = sum(t.pnl_amount for t in winners) if winners else 0
    gross_loss = abs(sum(t.pnl_amount for t in losers)) if losers else 0
    pf = gross_profit / gross_loss if gross_loss > 0 else float('inf')

    exit_dist = {}
    for t in trades:
        exit_dist[t.exit_reason] = exit_dist.get(t.exit_reason, 0) + 1

    monthly = {}
    for i in range(len(equity_curve)):
        date_str = equity_curve[i][0]
        month_key = date_str[:7]
        monthly[month_key] = equity_curve[i][1]

    result = PortfolioResult(
        period=f"{trading_dates[0]} ~ {trading_dates[-1]}",
        initial_capital=initial_capital,
        final_capital=final_equity,
        total_return_pct=total_return,
        cagr_pct=cagr,
        max_drawdown_pct=max_dd,
        total_trades=len(trades),
        winning_trades=len(winners),
        win_rate=len(winners) / len(trades) if trades else 0,
        profit_factor=pf,
        avg_hold_days=float(np.mean([t.hold_days for t in trades])) if trades else 0,
        avg_positions=avg_concurrent,
        trades=trades,
        equity_curve=equity_curve,
        exit_reason_dist=exit_dist,
        monthly_returns=monthly,
    )
    # 메타 필드 추가
    result.max_concurrent = max_concurrent
    result.universe_refresh_count = universe_refresh_count
    return result

