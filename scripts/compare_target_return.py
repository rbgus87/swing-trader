"""target_return 비교 백테스트 — 10%, 12%, 15% 수익률 목표 비교.

포트폴리오 레벨 백테스트로 동일 조건에서 target_return만 변경하여 비교합니다.
데이터는 1회만 로드하여 3번 재사용합니다.

Usage:
    python scripts/compare_target_return.py
    python scripts/compare_target_return.py --period 3y
    python scripts/compare_target_return.py --codes 005930 000660 035420
"""
import sys, os, time, argparse, warnings
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
sys.stdout.reconfigure(encoding="utf-8")
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ".")

from src.backtest.engine import BacktestEngine, BacktestResult, COMMISSION_RATE, TAX_RATE, SLIPPAGE_RATE
from src.backtest.report import BacktestReporter
from src.strategy.signals import calculate_indicators

# config.yaml 기본 종목
DEFAULT_CODES = [
    "005930", "000660", "005380", "000270", "068270",
    "035420", "035720", "105560", "055550", "066570",
]

# 비교할 target_return 값들
TARGET_RETURNS = [0.10, 0.12, 0.15]

# 기본 파라미터 (target_return만 변경)
BASE_PARAMS = {
    "volume_multiplier": 1.0,
    "adx_threshold": 20,
    "stop_atr_mult": 2.5,
    "max_hold_days": 15,
    "trailing_activate_pct": 0.07,
    "trailing_atr_mult": 2.5,
    "max_stop_pct": 0.10,
}

CAPITAL = 1_000_000
MAX_POSITIONS = 3


def gen_gc_signals(df_ind, p):
    """골든크로스 신호 생성."""
    va = df_ind["volume"].rolling(20).mean()
    gc = (df_ind["sma5"] > df_ind["sma20"]) & (df_ind["sma5"].shift(1) <= df_ind["sma20"].shift(1))
    entries = gc & (df_ind["rsi"] >= 50) & (df_ind["adx"] >= p.get("adx_threshold", 20)) & (df_ind["volume"] >= va * p.get("volume_multiplier", 1.0))
    dc = (df_ind["sma5"] < df_ind["sma20"]) & (df_ind["sma5"].shift(1) >= df_ind["sma20"].shift(1))
    en = entries.shift(1).fillna(False).astype(bool)
    ex = dc.shift(1).fillna(False).astype(bool)
    return en, ex


def portfolio_backtest(indicator_cache, params, capital, max_positions):
    """포트폴리오 레벨 백테스트 (시장 필터 없음)."""
    p = params
    target_return = p.get("target_return", 0.10)
    stop_atr_mult = p.get("stop_atr_mult", 2.0)
    trailing_atr_mult = p.get("trailing_atr_mult", 2.5)
    trailing_activate_pct = p.get("trailing_activate_pct", 0.05)
    max_hold_days = p.get("max_hold_days", 15)
    max_stop_pct = p.get("max_stop_pct", 0.10)

    all_signals = {}
    for code, df_ind in indicator_cache.items():
        en, ex = gen_gc_signals(df_ind, params)
        for i in range(len(df_ind)):
            date = df_ind.index[i]
            close = int(df_ind["close"].iloc[i])
            high_val = int(df_ind["high"].iloc[i])
            low_val = int(df_ind["low"].iloc[i])
            atr = float(df_ind["atr"].iloc[i]) if not pd.isna(df_ind["atr"].iloc[i]) else close * 0.02

            if date not in all_signals:
                all_signals[date] = []
            if en.iloc[i]:
                all_signals[date].append((code, "entry", close, high_val, low_val, atr))
            if ex.iloc[i]:
                all_signals[date].append((code, "exit", close, high_val, low_val, atr))

    sorted_dates = sorted(all_signals.keys())

    cash = capital
    positions = {}
    trades = []
    equity_dates = []
    equity_vals = []
    day_idx = 0

    for date in sorted_dates:
        signals = all_signals[date]
        day_idx += 1

        codes_to_close = []
        for code, pos in positions.items():
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

            if bar_low <= pos["stop_price"]:
                should_exit = True
                exit_price = pos["stop_price"]
            elif bar_high >= pos["target_price"]:
                should_exit = True
                exit_price = pos["target_price"]
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

            if not should_exit and hold_days >= max_hold_days:
                should_exit = True

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
                "high_since": sig[3],
            }

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
    if len(equity) == 0:
        return BacktestResult(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, params)

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


def analyze_exit_reasons(trades, target_return):
    """청산 사유별 분석."""
    if not trades:
        return {}

    target_hits = sum(1 for t in trades if t["return"] >= target_return * 0.95)
    stop_hits = sum(1 for t in trades if t["return"] <= -0.05)
    time_exits = sum(1 for t in trades if t["hold_days"] >= 15)
    other = len(trades) - target_hits - stop_hits - time_exits

    return {
        "target_hit": target_hits,
        "stop_loss": stop_hits,
        "time_exit": time_exits,
        "other": max(0, other),
    }


def main():
    parser = argparse.ArgumentParser(description="target_return 비교 백테스트")
    parser.add_argument("--period", type=str, default="5y", help="백테스트 기간 (예: 2y, 3y, 5y)")
    parser.add_argument("--codes", type=str, nargs="+", default=None, help="종목 코드 (기본: 대형주 10종목)")
    parser.add_argument("--capital", type=int, default=CAPITAL, help="초기 자본금")
    parser.add_argument("--targets", type=float, nargs="+", default=TARGET_RETURNS, help="비교할 target_return 값들 (예: 0.10 0.12 0.15)")
    args = parser.parse_args()

    codes = args.codes or DEFAULT_CODES
    targets = args.targets
    capital = args.capital

    # 기간 계산
    end_date = datetime.now()
    if args.period.endswith("y"):
        start_date = end_date - timedelta(days=int(args.period[:-1]) * 365)
    elif args.period.endswith("m"):
        start_date = end_date - timedelta(days=int(args.period[:-1]) * 30)
    else:
        start_date = end_date - timedelta(days=5 * 365)

    start_str = start_date.strftime("%Y%m%d")
    end_str = end_date.strftime("%Y%m%d")

    print(f"{'='*70}")
    print(f"  target_return 비교 백테스트")
    print(f"  기간: {start_str} ~ {end_str} ({args.period})")
    print(f"  종목: {len(codes)}개 | 자본금: {capital:,}원 | 최대 동시보유: {MAX_POSITIONS}")
    print(f"  비교 대상: {', '.join(f'{t*100:.0f}%' for t in targets)}")
    print(f"{'='*70}")

    # 데이터 1회 로드
    print(f"\n데이터 로드 중...")
    t0 = time.time()
    engine = BacktestEngine(initial_capital=capital)
    price_data = engine.load_price_data(codes, start_str, end_str)
    cache = {}
    for c, d in price_data.items():
        try:
            cache[c] = calculate_indicators(d)
        except Exception:
            pass
    print(f"  {len(cache)}종목 로드 완료 ({time.time()-t0:.0f}초)\n")

    # 각 target_return으로 백테스트 실행
    results = {}
    all_trades = {}
    all_equities = {}

    for tr in targets:
        label = f"{tr*100:.0f}%"
        print(f"  {label} 백테스트 실행 중...", end=" ", flush=True)
        t1 = time.time()

        params = {**BASE_PARAMS, "target_return": tr}
        trades, equity = portfolio_backtest(cache, params, capital, MAX_POSITIONS)
        result = calc_metrics(trades, equity, capital, params)

        results[label] = result
        all_trades[label] = trades
        all_equities[label] = equity
        print(f"완료 ({time.time()-t1:.1f}초, {len(trades)}건)")

    # === 비교 테이블 출력 ===
    labels = [f"{t*100:.0f}%" for t in targets]

    print(f"\n{'='*70}")
    print(f"  비교 결과")
    print(f"{'='*70}")

    header = f"{'지표':>20}"
    for label in labels:
        header += f" {label:>12}"
    print(header)
    print(f"{'-'*70}")

    # 총 수익률
    row = f"{'총 수익률':>20}"
    for label in labels:
        row += f" {results[label].total_return:>10.2f}%"
    print(row)

    # 연환산 수익률
    row = f"{'연환산 수익률':>20}"
    for label in labels:
        row += f" {results[label].annual_return:>10.2f}%"
    print(row)

    # MDD
    row = f"{'MDD':>20}"
    for label in labels:
        row += f" {results[label].max_drawdown:>10.2f}%"
    print(row)

    # 샤프
    row = f"{'샤프':>20}"
    for label in labels:
        row += f" {results[label].sharpe_ratio:>10.2f}"
    print(row)

    # 소르티노
    row = f"{'소르티노':>20}"
    for label in labels:
        row += f" {results[label].sortino_ratio:>10.2f}"
    print(row)

    # 승률
    row = f"{'승률':>20}"
    for label in labels:
        row += f" {results[label].win_rate:>10.2f}%"
    print(row)

    # 손익비
    row = f"{'손익비':>20}"
    for label in labels:
        pf = results[label].profit_factor
        val = "inf" if pf == float("inf") else f"{pf:.2f}"
        row += f" {val:>11}"
    print(row)

    # 평균 거래 수익
    row = f"{'평균 거래수익':>20}"
    for label in labels:
        row += f" {results[label].avg_trade_return:>10.2f}%"
    print(row)

    # 거래 수
    row = f"{'거래 수':>20}"
    for label in labels:
        row += f" {results[label].trade_count:>11d}"
    print(row)

    # 평균 보유일
    row = f"{'평균 보유일':>20}"
    for label in labels:
        row += f" {results[label].avg_hold_days:>10.1f}일"
    print(row)

    # 최종 자산
    row = f"{'최종 자산':>20}"
    for label in labels:
        eq = all_equities[label]
        val = eq.iloc[-1] if len(eq) > 0 else capital
        row += f" {val:>10,.0f}"
    print(row)

    # === 청산 사유 분석 ===
    print(f"\n{'-'*70}")
    print(f"  청산 사유 분석")
    print(f"{'-'*70}")

    header = f"{'청산 사유':>20}"
    for label in labels:
        header += f" {label:>12}"
    print(header)
    print(f"{'-'*70}")

    for reason_name, reason_key in [
        ("목표가 도달", "target_hit"),
        ("손절", "stop_loss"),
        ("시간 청산 (15일)", "time_exit"),
        ("기타 (신호 등)", "other"),
    ]:
        row = f"{reason_name:>20}"
        for lbl in labels:
            tr_val = targets[labels.index(lbl)]
            reasons = analyze_exit_reasons(all_trades[lbl], tr_val)
            total = max(len(all_trades[lbl]), 1)
            cnt = reasons.get(reason_key, 0)
            pct = cnt / total * 100
            row += f" {cnt:>4d} ({pct:>4.1f}%)"
        print(row)

    # === 요약 ===
    print(f"\n{'='*70}")
    print(f"  요약")
    print(f"{'='*70}")

    best_return = max(labels, key=lambda l: results[l].total_return)
    best_sharpe = max(labels, key=lambda l: results[l].sharpe_ratio)
    best_winrate = max(labels, key=lambda l: results[l].win_rate)

    print(f"  최고 수익률: {best_return} ({results[best_return].total_return:.2f}%)")
    print(f"  최고 샤프:   {best_sharpe} ({results[best_sharpe].sharpe_ratio:.2f})")
    print(f"  최고 승률:   {best_winrate} ({results[best_winrate].win_rate:.2f}%)")
    print()

    # HTML 리포트 생성
    reporter = BacktestReporter()
    for label in labels:
        filename = f"reports/target_return_{label.replace('%','pct')}.html"
        try:
            reporter.generate_html(
                results[label], filename,
                all_equities[label], all_trades[label],
            )
            print(f"  HTML 리포트: {filename}")
        except Exception as e:
            print(f"  HTML 리포트 생성 실패 ({label}): {e}")

    print()


if __name__ == "__main__":
    main()
