"""포트폴리오 백테스트 — 100만원 자본금, 다종목 순차 매매.

기존 종목별 독립 백테스트와 달리, 하나의 자본금으로
전 종목의 신호를 시간순 정렬하여 순차 매매합니다.
"""
import sys, os, time, warnings
import numpy as np, pandas as pd

warnings.filterwarnings("ignore")
sys.stdout.reconfigure(encoding="utf-8")
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ".")

from src.backtest.engine import BacktestEngine, BacktestResult, COMMISSION_RATE, TAX_RATE, SLIPPAGE_RATE
from src.backtest.report import BacktestReporter
from src.strategy.signals import calculate_indicators

CODES = [
    "005930", "000660", "005380", "000270", "068270",
    "035420", "035720", "105560", "055550", "066570",
]
START = "20200101"
END = "20250314"
CAPITAL = 1_000_000  # 100만원
MAX_POSITIONS = 3     # 동시 보유 최대 3종목 (100만원이므로)

# 골든크로스 최적 파라미터
PARAMS = {
    "volume_multiplier": 1.0,
    "adx_threshold": 20,
    "stop_atr_mult": 2.5,
    "target_return": 0.10,
    "max_hold_days": 15,
    "trailing_activate_pct": 0.07,
    "trailing_atr_mult": 2.5,
    "max_stop_pct": 0.10,
}


def gen_gc_signals(df_ind, p):
    """골든크로스 신호 생성."""
    va = df_ind["volume"].rolling(20).mean()
    gc = (df_ind["sma5"] > df_ind["sma20"]) & (df_ind["sma5"].shift(1) <= df_ind["sma20"].shift(1))
    entries = gc & (df_ind["rsi"] >= 50) & (df_ind["adx"] >= p.get("adx_threshold", 20)) & (df_ind["volume"] >= va * p.get("volume_multiplier", 1.0))
    dc = (df_ind["sma5"] < df_ind["sma20"]) & (df_ind["sma5"].shift(1) >= df_ind["sma20"].shift(1))
    en = entries.shift(1).fillna(False).astype(bool)
    ex = dc.shift(1).fillna(False).astype(bool)
    return en, ex


def portfolio_backtest(indicator_cache, params, capital, max_positions, use_market_filter=False, kospi_data=None):
    """포트폴리오 레벨 백테스트.

    하나의 자본금으로 전 종목을 순차 매매.
    """
    p = params
    target_return = p.get("target_return", 0.10)
    stop_atr_mult = p.get("stop_atr_mult", 2.0)
    trailing_atr_mult = p.get("trailing_atr_mult", 2.5)
    trailing_activate_pct = p.get("trailing_activate_pct", 0.05)
    max_hold_days = p.get("max_hold_days", 15)
    max_stop_pct = p.get("max_stop_pct", 0.10)

    # 전 종목의 신호를 날짜별로 수집
    all_signals = {}  # {date: [(code, 'entry'|'exit', close, high, low, atr), ...]}

    for code, df_ind in indicator_cache.items():
        en, ex = gen_gc_signals(df_ind, params)
        for i in range(len(df_ind)):
            date = df_ind.index[i]
            close = int(df_ind["close"].iloc[i])
            high = int(df_ind["high"].iloc[i])
            low = int(df_ind["low"].iloc[i])
            atr = float(df_ind["atr"].iloc[i]) if not pd.isna(df_ind["atr"].iloc[i]) else close * 0.02

            if date not in all_signals:
                all_signals[date] = []

            if en.iloc[i]:
                all_signals[date].append((code, "entry", close, high, low, atr))
            if ex.iloc[i]:
                all_signals[date].append((code, "exit", close, high, low, atr))

    # 날짜순 정렬
    sorted_dates = sorted(all_signals.keys())

    # 시장 필터 준비 (KOSPI 200일선)
    market_ok = {}
    if use_market_filter and kospi_data is not None:
        kospi_sma200 = kospi_data["close"].rolling(200).mean()
        for date in sorted_dates:
            if date in kospi_data.index:
                idx = kospi_data.index.get_loc(date)
                if idx < 200:
                    market_ok[date] = True
                else:
                    market_ok[date] = kospi_data["close"].iloc[idx] > kospi_sma200.iloc[idx]
            else:
                market_ok[date] = True
    else:
        for date in sorted_dates:
            market_ok[date] = True

    # 시뮬레이션
    cash = capital
    positions = {}  # {code: {entry_price, shares, entry_date, entry_idx, stop_price, target_price, high_since}}
    trades = []
    equity_dates = []
    equity_vals = []
    day_idx = 0

    for date in sorted_dates:
        signals = all_signals[date]
        day_idx += 1

        # 1. 기존 포지션 청산 체크 (모든 보유 종목)
        codes_to_close = []
        for code, pos in positions.items():
            # 해당 종목의 당일 데이터 가져오기
            if code not in indicator_cache:
                continue
            df = indicator_cache[code]
            if date not in df.index:
                continue
            idx = df.index.get_loc(date)
            price = int(df["close"].iloc[idx])
            bar_high = int(df["high"].iloc[idx])
            bar_low = int(df["low"].iloc[idx])
            cur_atr = float(df["atr"].iloc[idx]) if not pd.isna(df["atr"].iloc[idx]) else price * 0.02

            pos["high_since"] = max(pos["high_since"], bar_high)
            hold_days = day_idx - pos["entry_idx"]

            should_exit = False
            exit_price = price

            # 손절
            if bar_low <= pos["stop_price"]:
                should_exit = True
                exit_price = pos["stop_price"]
            # 목표가
            elif bar_high >= pos["target_price"]:
                should_exit = True
                exit_price = pos["target_price"]
            # 트레일링
            else:
                unrealized = (price - pos["entry_price"]) / pos["entry_price"]
                if unrealized >= trailing_activate_pct:
                    trailing = int(pos["high_since"] - cur_atr * trailing_atr_mult)
                    trailing = max(trailing, pos["stop_price"])
                    if trailing > pos["stop_price"]:
                        pos["stop_price"] = trailing
                    if bar_low <= pos["stop_price"]:
                        should_exit = True
                        exit_price = pos["stop_price"]
            # 최대보유
            if not should_exit and hold_days >= max_hold_days:
                should_exit = True

            # 신호 매도
            for sig in signals:
                if sig[0] == code and sig[1] == "exit" and not should_exit:
                    should_exit = True

            if should_exit:
                proceeds = pos["shares"] * exit_price * (1 - COMMISSION_RATE - SLIPPAGE_RATE - TAX_RATE)
                cash += proceeds
                pnl_pct = (exit_price - pos["entry_price"]) / pos["entry_price"]
                trades.append({
                    "code": code,
                    "entry_date": str(pos["entry_date"]),
                    "exit_date": str(date),
                    "entry_price": pos["entry_price"],
                    "exit_price": exit_price,
                    "shares": pos["shares"],
                    "return": pnl_pct,
                    "hold_days": hold_days,
                })
                codes_to_close.append(code)

        for code in codes_to_close:
            del positions[code]

        # 2. 새 진입 (자본 여유 + 최대 포지션 미만 + 시장 필터 통과)
        if market_ok.get(date, True):
            for sig in signals:
                code, sig_type = sig[0], sig[1]
                if sig_type != "entry":
                    continue
                if code in positions:
                    continue
                if len(positions) >= max_positions:
                    break

                price = sig[2]
                atr = sig[5]
                position_size = cash // max_positions if max_positions > 0 else cash
                cost_per_share = price * (1 + COMMISSION_RATE + SLIPPAGE_RATE)
                shares = int(position_size // cost_per_share)

                if shares <= 0:
                    continue

                atr_stop = int(price - atr * stop_atr_mult)
                pct_stop = int(price * (1 - max_stop_pct))
                stop_price = max(atr_stop, pct_stop)
                target_price = int(price * (1 + target_return))

                cost = shares * cost_per_share
                cash -= cost
                positions[code] = {
                    "entry_price": price,
                    "shares": shares,
                    "entry_date": date,
                    "entry_idx": day_idx,
                    "stop_price": stop_price,
                    "target_price": target_price,
                    "high_since": sig[3],  # bar_high
                }

        # 에퀴티 계산
        portfolio_value = cash
        for code, pos in positions.items():
            if code in indicator_cache:
                df = indicator_cache[code]
                if date in df.index:
                    idx = df.index.get_loc(date)
                    portfolio_value += pos["shares"] * int(df["close"].iloc[idx])
        equity_dates.append(date)
        equity_vals.append(portfolio_value)

    equity = pd.Series(equity_vals, index=equity_dates)
    return trades, equity


def calc_metrics(trades, equity, capital, params):
    """성과 지표 계산."""
    total_return = (equity.iloc[-1] - capital) / capital * 100

    peak = equity.cummax()
    dd = (equity - peak) / peak * 100
    max_dd = dd.min()

    daily_ret = equity.pct_change().dropna()
    sharpe = (daily_ret.mean() / daily_ret.std()) * np.sqrt(252) if len(daily_ret) > 0 and daily_ret.std() > 0 else 0
    neg_ret = daily_ret[daily_ret < 0]
    sortino = (daily_ret.mean() / neg_ret.std()) * np.sqrt(252) if len(neg_ret) > 0 and neg_ret.std() > 0 else 0

    tc = len(trades)
    if tc > 0:
        rets = [t["return"] for t in trades]
        win = sum(1 for r in rets if r > 0)
        win_rate = win / tc * 100
        gp = sum(r for r in rets if r > 0)
        gl = abs(sum(r for r in rets if r < 0))
        pf = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0)
        avg_ret = np.mean(rets) * 100
        avg_hold = np.mean([t["hold_days"] for t in trades])
    else:
        win_rate = pf = avg_ret = avg_hold = 0

    years = len(equity) / 252 if len(equity) > 0 else 1
    annual = ((1 + total_return / 100) ** (1 / years) - 1) * 100 if total_return > -100 else -100

    return BacktestResult(
        total_return=round(total_return, 2), annual_return=round(annual, 2),
        max_drawdown=round(max_dd, 2), sharpe_ratio=round(sharpe, 2),
        sortino_ratio=round(sortino, 2), win_rate=round(win_rate, 2),
        profit_factor=round(pf, 2) if pf != float("inf") else float("inf"),
        avg_trade_return=round(avg_ret, 2), trade_count=tc,
        avg_hold_days=round(avg_hold, 1), params=params,
    )


def main():
    engine = BacktestEngine(initial_capital=CAPITAL)
    reporter = BacktestReporter()

    # 데이터 로드 (1회)
    print(f"데이터 로드: {len(CODES)}종목, {START}~{END}")
    t0 = time.time()
    price_data = engine.load_price_data(CODES, START, END)
    cache = {}
    for c, d in price_data.items():
        try:
            cache[c] = calculate_indicators(d)
        except:
            pass
    print(f"  {len(cache)}종목 로드 ({time.time()-t0:.0f}초)")

    # KOSPI 지수 로드 (시장 필터용)
    print("KOSPI 지수 로드...")
    from pykrx import stock
    from data.column_mapper import map_columns, OHLCV_MAP
    try:
        kospi_raw = stock.get_index_ohlcv_by_date(START, END, "1001")  # KOSPI
        kospi_data = map_columns(kospi_raw, OHLCV_MAP)
    except:
        kospi_data = None
        print("  KOSPI 데이터 로드 실패 - 시장 필터 미사용")

    # === Test A: 포트폴리오 백테스트 (시장 필터 없음) ===
    print(f"\n{'='*60}")
    print(f"  Test A: 포트폴리오 백테스트 (자본금 {CAPITAL:,}원, 최대 {MAX_POSITIONS}종목)")
    print(f"{'='*60}")
    trades_a, equity_a = portfolio_backtest(cache, PARAMS, CAPITAL, MAX_POSITIONS)
    result_a = calc_metrics(trades_a, equity_a, CAPITAL, PARAMS)
    reporter.print_summary(result_a)
    reporter.generate_html(result_a, "reports/portfolio_no_filter.html", equity_a, trades_a)

    # === Test B: 포트폴리오 + 시장 필터 ===
    print(f"\n{'='*60}")
    print(f"  Test B: 포트폴리오 + KOSPI 200일선 시장 필터")
    print(f"{'='*60}")
    trades_b, equity_b = portfolio_backtest(cache, PARAMS, CAPITAL, MAX_POSITIONS, use_market_filter=True, kospi_data=kospi_data)
    result_b = calc_metrics(trades_b, equity_b, CAPITAL, PARAMS)
    reporter.print_summary(result_b)
    reporter.generate_html(result_b, "reports/portfolio_with_filter.html", equity_b, trades_b)

    # === 비교 ===
    print(f"\n{'='*60}")
    print(f"  비교 (자본금 {CAPITAL:,}원, {len(CODES)}종목, 5년)")
    print(f"{'='*60}")
    print(f"{'':>25} {'A:필터없음':>12} {'B:시장필터':>12} {'기준':>10}")
    print(f"{'-'*60}")
    print(f"{'총 수익률':>25} {result_a.total_return:>10.2f}% {result_b.total_return:>10.2f}% {'> 0%':>10}")
    print(f"{'연환산 수익률':>25} {result_a.annual_return:>10.2f}% {result_b.annual_return:>10.2f}% {'> 15%':>10}")
    print(f"{'MDD':>25} {result_a.max_drawdown:>10.2f}% {result_b.max_drawdown:>10.2f}% {'> -20%':>10}")
    print(f"{'샤프':>25} {result_a.sharpe_ratio:>10.2f} {result_b.sharpe_ratio:>10.2f} {'> 0.8':>10}")
    print(f"{'승률':>25} {result_a.win_rate:>10.2f}% {result_b.win_rate:>10.2f}% {'> 43%':>10}")
    pf_a = f"{result_a.profit_factor:.2f}" if result_a.profit_factor != float("inf") else "inf"
    pf_b = f"{result_b.profit_factor:.2f}" if result_b.profit_factor != float("inf") else "inf"
    print(f"{'손익비':>25} {pf_a:>10} {pf_b:>10} {'> 1.5':>10}")
    print(f"{'거래 수':>25} {result_a.trade_count:>10d} {result_b.trade_count:>10d} {'> 50':>10}")
    print(f"{'평균 보유일':>25} {result_a.avg_hold_days:>10.1f} {result_b.avg_hold_days:>10.1f} {'3~15':>10}")
    print(f"{'최종 자산':>25} {equity_a.iloc[-1]:>10,.0f} {equity_b.iloc[-1]:>10,.0f}")
    print()


if __name__ == "__main__":
    main()
