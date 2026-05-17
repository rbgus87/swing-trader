"""지수 ETF 평균회귀 알파 스크리닝.

학술적으로 검증된 평균회귀 시그널 5종을 KOSPI 지수 데이터(2014~2026)에
적용해 ETF 비용(0.03%) 환경에서의 PF/WR/CAGR/MDD 를 측정한다.

daily_candles에 ETF 티커(069500 등)가 없으면 index_daily(KOSPI)로 대체.

실행:
    python experiments/experiment_etf_mean_reversion.py
"""
from __future__ import annotations

import math
import os
import sqlite3
import sys
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


# ─── 상수 ────────────────────────────────────────────────────────────────────
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "swing_data.db")
CAPITAL = 5_000_000
COST_ETF = 0.0003    # 왕복 0.03% (ETF: 증권거래세 면제)
COST_STOCK = 0.003   # 왕복 0.30% (개별 종목 비교용)
ALPHA_PF = 1.10
SEP = "=" * 70

# v2.7 성과 기준선 (CLAUDE.md 최종 확정)
V27_CAGR = 7.3 / 100
V27_MDD = -34.3 / 100
V27_PF = 1.27
V27_CAPITAL = 10_000_000
V27_YEARS = 12


# ─── 데이터 로드 ─────────────────────────────────────────────────────────────
def load_index_data(index_code: str = "KOSPI") -> pd.DataFrame:
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql(
        "SELECT date, open, high, low, close, volume FROM index_daily "
        "WHERE index_code=? ORDER BY date",
        conn,
        params=(index_code,),
    )
    conn.close()
    df["date"] = pd.to_datetime(df["date"])
    return df.set_index("date")


def check_etf_data() -> dict:
    """daily_candles 에 ETF 티커 있는지 확인."""
    ETF_MAP = {"069500": "KODEX 200", "229200": "KODEX 코스닥150", "102110": "TIGER 200"}
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    found = {}
    for ticker, name in ETF_MAP.items():
        try:
            c.execute("SELECT COUNT(*) FROM daily_candles WHERE ticker=?", (ticker,))
            cnt = c.fetchone()[0]
            if cnt > 0:
                found[ticker] = (name, cnt)
        except Exception:
            pass
    conn.close()
    return found


# ─── 지표 계산 ───────────────────────────────────────────────────────────────
def _wilder_rsi(series: pd.Series, period: int) -> pd.Series:
    """Wilder's RSI (com = period - 1)."""
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(com=period - 1, adjust=False).mean()
    avg_loss = loss.ewm(com=period - 1, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _consec_down(close_arr: np.ndarray, open_arr: np.ndarray) -> np.ndarray:
    """연속 음봉(close < open) 일 수 카운트 (O(n))."""
    n = len(close_arr)
    result = np.zeros(n, dtype=np.int32)
    for i in range(n):
        result[i] = (result[i - 1] + 1 if i > 0 else 1) if close_arr[i] < open_arr[i] else 0
    return result


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    # RSI(2)
    df["rsi2"] = _wilder_rsi(df["close"], period=2)

    # IBS (Internal Bar Strength)
    rng = df["high"] - df["low"]
    df["ibs"] = (df["close"] - df["low"]) / rng.where(rng > 0, np.nan)

    # MA200
    df["ma200"] = df["close"].rolling(200).mean()

    # 볼린저 밴드 (20일, 2σ)
    sma20 = df["close"].rolling(20).mean()
    std20 = df["close"].rolling(20).std(ddof=1)
    df["bb_lower"] = sma20 - 2 * std20
    df["bb_sma20"] = sma20

    # 연속 음봉 카운트
    df["consec_down"] = _consec_down(df["close"].values, df["open"].values)

    return df


# ─── 백테스터 ────────────────────────────────────────────────────────────────
@dataclass
class TradeRecord:
    entry_date: pd.Timestamp
    exit_date: pd.Timestamp
    entry_price: float
    exit_price: float
    hold_days: int
    net_ret: float


def run_backtest(
    df: pd.DataFrame,
    entry_fn: Callable,  # (row, prev_row, i, df) -> bool
    exit_fn: Callable,   # (row, prev_row, entry_price, hold_days) -> bool
    cost_pct: float,
    capital: float,
    use_ma200: bool = False,
) -> Tuple[List[TradeRecord], List[float], int]:
    """단일 포지션 미니 백테스터.

    - 진입: 시그널 다음날 시가 (익일 시가)
    - 청산: 해당 일 종가, 최소 1거래일 보유 후 평가
    - 복리 운용 (매 거래마다 전체 equity 투입)

    Returns: (trades, equity_curve, invested_days)
    """
    trades: List[TradeRecord] = []
    equity = capital
    equity_curve: List[float] = [capital]

    in_position = False
    pending_entry = False
    entry_price = 0.0
    entry_date: Optional[pd.Timestamp] = None
    entry_idx = 0
    invested_days = 0
    n = len(df)

    for i in range(n):
        row = df.iloc[i]
        prev_row = df.iloc[i - 1] if i > 0 else row

        # 익일 시가 진입 처리
        if pending_entry:
            ep = float(row["open"])
            pending_entry = False
            if ep > 0:
                entry_price = ep
                entry_date = df.index[i]
                entry_idx = i
                in_position = True

        if in_position:
            invested_days += 1
            hold_days = i - entry_idx
            # 최소 1거래일 보유 후 청산 평가
            if hold_days >= 1 and exit_fn(row, prev_row, entry_price, hold_days):
                exit_price = float(row["close"])
                gross_ret = (exit_price - entry_price) / entry_price
                net_ret = gross_ret - cost_pct
                equity *= 1 + net_ret
                trades.append(
                    TradeRecord(
                        entry_date=entry_date,
                        exit_date=df.index[i],
                        entry_price=entry_price,
                        exit_price=exit_price,
                        hold_days=hold_days,
                        net_ret=net_ret,
                    )
                )
                in_position = False
        elif not pending_entry:
            # MA200 필터
            ma200_ok = True
            if use_ma200:
                ma200 = row["ma200"]
                ma200_ok = (not pd.isna(ma200)) and row["close"] >= ma200

            if ma200_ok and entry_fn(row, prev_row, i, df):
                pending_entry = True

        equity_curve.append(equity)

    # 미청산 포지션: 마지막 날 종가 강제 청산
    if in_position:
        last = df.iloc[-1]
        exit_price = float(last["close"])
        gross_ret = (exit_price - entry_price) / entry_price
        net_ret = gross_ret - cost_pct
        equity *= 1 + net_ret
        trades.append(
            TradeRecord(
                entry_date=entry_date,
                exit_date=df.index[-1],
                entry_price=entry_price,
                exit_price=exit_price,
                hold_days=n - 1 - entry_idx,
                net_ret=net_ret,
            )
        )

    return trades, equity_curve, invested_days


def compute_metrics(
    trades: List[TradeRecord],
    equity_curve: List[float],
    capital: float,
    total_days: int,
    invested_days: int,
    start_date: pd.Timestamp,
    end_date: pd.Timestamp,
) -> dict:
    if not trades:
        return dict(
            trades=0, wr=0.0, pf=0.0, cagr=0.0, mdd=0.0,
            avg_hold=0.0, utilization=0.0, final_equity=float(capital),
        )

    wins = [t for t in trades if t.net_ret > 0]
    losses = [t for t in trades if t.net_ret <= 0]
    gross_profit = sum(t.net_ret for t in wins) if wins else 0.0
    gross_loss = abs(sum(t.net_ret for t in losses)) if losses else 0.0

    pf = gross_profit / gross_loss if gross_loss > 0 else (math.inf if gross_profit > 0 else 0.0)
    wr = len(wins) / len(trades)
    avg_hold = float(np.mean([t.hold_days for t in trades]))

    years = (end_date - start_date).days / 365.25
    final_equity = equity_curve[-1]
    cagr = (final_equity / capital) ** (1 / years) - 1 if years > 0 else 0.0

    peak = float(capital)
    mdd = 0.0
    for eq in equity_curve:
        if eq > peak:
            peak = eq
        dd = (eq - peak) / peak
        if dd < mdd:
            mdd = dd

    utilization = invested_days / total_days if total_days > 0 else 0.0

    return dict(
        trades=len(trades),
        wr=wr,
        pf=pf,
        cagr=cagr,
        mdd=mdd,
        avg_hold=avg_hold,
        utilization=utilization,
        final_equity=final_equity,
    )


# ─── 시그널 정의 ─────────────────────────────────────────────────────────────

# RSI 계열 공통 청산: 종가 > 전일 고가
def _rsi_exit(row, prev_row, entry_price, hold_days):
    ph = prev_row["high"]
    return (not pd.isna(ph)) and row["close"] > ph

# [1] RSI(2) < 10
def _rsi2_10_entry(row, prev_row, i, df):
    v = row["rsi2"]
    return (not pd.isna(v)) and v < 10

# [2] RSI(2) < 5
def _rsi2_5_entry(row, prev_row, i, df):
    v = row["rsi2"]
    return (not pd.isna(v)) and v < 5

# [3] IBS < 0.1  →  청산: IBS > 0.8
def _ibs_entry(row, prev_row, i, df):
    v = row["ibs"]
    return (not pd.isna(v)) and v < 0.1

def _ibs_exit(row, prev_row, entry_price, hold_days):
    v = row["ibs"]
    return (not pd.isna(v)) and v > 0.8

# [4] 3일 연속 음봉  →  청산: 첫 양봉(종가 > 시가)
def _consec3_entry(row, prev_row, i, df):
    return int(row["consec_down"]) >= 3

def _consec_exit(row, prev_row, entry_price, hold_days):
    return row["close"] > row["open"]

# [5] 5일 연속 음봉
def _consec5_entry(row, prev_row, i, df):
    return int(row["consec_down"]) >= 5

# [6] 볼린저 밴드 하단 이탈  →  청산: SMA20 복귀
def _bb_entry(row, prev_row, i, df):
    lb = row["bb_lower"]
    return (not pd.isna(lb)) and row["close"] < lb

def _bb_exit(row, prev_row, entry_price, hold_days):
    sma = row["bb_sma20"]
    return (not pd.isna(sma)) and row["close"] > sma


# ─── 실험 변형 ───────────────────────────────────────────────────────────────
VARIANTS: list[tuple] = [
    # (label,                    entry_fn,         exit_fn,      cost,       use_ma200)
    ("[1] RSI2<10",              _rsi2_10_entry,   _rsi_exit,    COST_ETF,   False),
    ("[2] RSI2<5",               _rsi2_5_entry,    _rsi_exit,    COST_ETF,   False),
    ("[3] IBS<0.1",              _ibs_entry,       _ibs_exit,    COST_ETF,   False),
    ("[4] CONSEC_3DOWN",         _consec3_entry,   _consec_exit, COST_ETF,   False),
    ("[5] CONSEC_5DOWN",         _consec5_entry,   _consec_exit, COST_ETF,   False),
    ("[6] BB_LOWER",             _bb_entry,        _bb_exit,     COST_ETF,   False),
    ("[7] RSI2<10+MA200",        _rsi2_10_entry,   _rsi_exit,    COST_ETF,   True),
    ("[8] RSI2<5+MA200",         _rsi2_5_entry,    _rsi_exit,    COST_ETF,   True),
    ("[9] RSI2<10 주식비용",     _rsi2_10_entry,   _rsi_exit,    COST_STOCK, False),
]


# ─── 보고서 출력 ─────────────────────────────────────────────────────────────
def _pf_str(pf: float) -> str:
    if math.isinf(pf):
        return "  ∞  "
    return f"{pf:.2f}"


def print_report(results: dict, df: pd.DataFrame, etf_found: dict) -> None:
    start = df.index[0].strftime("%Y-%m-%d")
    end = df.index[-1].strftime("%Y-%m-%d")
    total_days = len(df)

    print()
    print(SEP)
    print("  지수 ETF 평균회귀 알파 스크리닝 (KOSPI, 2014~2026)")
    print(SEP)
    print()
    print("■ 데이터 확인")
    print(f"  KOSPI 지수 일봉: {total_days:,}건 ({start}~{end})")
    if etf_found:
        for ticker, (name, cnt) in etf_found.items():
            print(f"  ETF {ticker} {name}: {cnt:,}건 ✅ (daily_candles)")
    else:
        print("  ETF 데이터: ❌ (daily_candles 없음 → KOSPI 지수로 대체)")
    print()

    # ── 시그널별 결과표 ──────────────────────────────────────────────────────
    print("■ 시그널별 결과 (초기자본 5M)")
    hdr = (
        f"  {'시그널':<24} {'건수':>5} {'WR':>6} {'PF':>5}"
        f" {'CAGR':>7} {'MDD':>7} {'활용':>5} {'평균보유':>6}"
    )
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))

    alpha_variants: list[tuple] = []

    for label, *_ in VARIANTS:
        m = results[label]
        if m["trades"] == 0:
            line = f"  {label:<24} {'0':>5} {'N/A':>6} {'N/A':>5} {'N/A':>7} {'N/A':>7} {'N/A':>5} {'N/A':>6}"
        else:
            pf_s = _pf_str(m["pf"])
            is_stock_cost = "주식비용" in label
            mark = ""
            if not is_stock_cost and m["pf"] >= ALPHA_PF and not math.isinf(m["pf"]):
                mark = "  ← ✅"
                alpha_variants.append((label, m))
            line = (
                f"  {label:<24}"
                f" {m['trades']:>5}"
                f" {m['wr']*100:>5.1f}%"
                f" {pf_s:>5}"
                f" {m['cagr']*100:>+6.1f}%"
                f" {m['mdd']*100:>6.1f}%"
                f" {m['utilization']*100:>4.1f}%"
                f" {m['avg_hold']:>5.1f}일"
                f"{mark}"
            )
        print(line)

    print()

    # ── 비용 영향 ────────────────────────────────────────────────────────────
    r1 = results.get("[1] RSI2<10", {})
    r9 = results.get("[9] RSI2<10 주식비용", {})
    if r1.get("trades", 0) > 0 and r9.get("trades", 0) > 0:
        print("■ 비용 영향 (RSI2<10 기준, 거래 수 동일)")
        print(f"  ETF 비용(0.03%):  PF {_pf_str(r1['pf'])}, CAGR {r1['cagr']*100:+.1f}%")
        print(f"  주식비용(0.30%):  PF {_pf_str(r9['pf'])}, CAGR {r9['cagr']*100:+.1f}%")
        if r1["pf"] > 0 and not math.isinf(r1["pf"]):
            pf_drop = (r1["pf"] - r9["pf"]) / r1["pf"] * 100
            cagr_drop = (r1["cagr"] - r9["cagr"]) * 100
            print(f"  → 비용 0.27%p 차이: PF {pf_drop:.0f}% 하락, CAGR {cagr_drop:+.1f}%p")
        print()

    # ── MA200 필터 효과 ───────────────────────────────────────────────────────
    pairs = [
        ("[1] RSI2<10",   "[7] RSI2<10+MA200"),
        ("[2] RSI2<5",    "[8] RSI2<5+MA200"),
    ]
    print("■ MA200 필터 효과")
    for base_key, ma_key in pairs:
        rb = results.get(base_key, {})
        rm = results.get(ma_key, {})
        if rb.get("trades", 0) > 0 and rm.get("trades", 0) > 0:
            mdd_diff = (rm["mdd"] - rb["mdd"]) * 100
            pf_diff = rm["pf"] - rb["pf"] if not (math.isinf(rm["pf"]) or math.isinf(rb["pf"])) else float("nan")
            trades_diff = rm["trades"] - rb["trades"]
            improve = "개선" if mdd_diff > 0 else ("악화" if mdd_diff < 0 else "동일")
            print(
                f"  {base_key:<18}: PF {_pf_str(rb['pf'])}, MDD {rb['mdd']*100:.1f}%"
                f"  →  +MA200: PF {_pf_str(rm['pf'])}, MDD {rm['mdd']*100:.1f}%"
                f"  (MDD {mdd_diff:+.1f}%p {improve}, 거래 {trades_diff:+d}건)"
            )
    print()

    # ── 판정 ─────────────────────────────────────────────────────────────────
    print("■ 판정")
    if alpha_variants:
        labels_str = ", ".join(l for l, _ in alpha_variants)
        print(f"  PF ≥ 1.10 전략: {labels_str}")
        best_label, best_m = max(alpha_variants, key=lambda x: x[1]["pf"])
        print(f"  최우수: {best_label} — PF {_pf_str(best_m['pf'])}, CAGR {best_m['cagr']*100:+.1f}%")
    else:
        etf_only = {
            k: v for k, v in results.items()
            if "주식비용" not in k and v.get("trades", 0) > 0
        }
        if etf_only:
            best_k = max(etf_only, key=lambda k: etf_only[k]["pf"])
            bm = etf_only[best_k]
            print(f"  PF ≥ 1.10 전략: 없음 (최고 PF: {best_k} → {_pf_str(bm['pf'])})")
        else:
            print("  PF ≥ 1.10 전략: 없음")
    print()

    # ── v2.7 결합 시뮬 ───────────────────────────────────────────────────────
    if alpha_variants:
        best_label, best_m = max(alpha_variants, key=lambda x: x[1]["pf"])
        etf_cagr = best_m["cagr"]
        etf_mdd = best_m["mdd"]
        etf_util = best_m["utilization"]

        # 7M v2.7 + 3M ETF 분리 운용
        v27_alloc = 7_000_000
        etf_alloc = 3_000_000

        v27_final = v27_alloc * (1 + V27_CAGR) ** V27_YEARS
        etf_final = etf_alloc * (1 + etf_cagr) ** V27_YEARS
        combined_final = v27_final + etf_final
        combined_cagr = (combined_final / (v27_alloc + etf_alloc)) ** (1 / V27_YEARS) - 1

        # v2.7 단독 (10M)
        v27_alone_final = V27_CAPITAL * (1 + V27_CAGR) ** V27_YEARS

        print("■ v2.7 결합 시뮬 (최우수 전략 채택, 자본 7M v2.7 + 3M ETF)")
        print(
            f"  v2.7 단독   (10M): CAGR {V27_CAGR*100:.1f}%, MDD {V27_MDD*100:.1f}%,"
            f" 최종 {v27_alone_final/1e6:.2f}M"
        )
        print(
            f"  ETF 단독    ( 5M): CAGR {etf_cagr*100:+.1f}%, MDD {etf_mdd*100:.1f}%,"
            f" 활용 {etf_util*100:.0f}%"
        )
        print(
            f"  결합(7M+3M) (10M): CAGR ~{combined_cagr*100:.1f}%,"
            f" 최종 ~{combined_final/1e6:.2f}M (독립 가정)"
        )
        diff_cagr = (combined_cagr - V27_CAGR) * 100
        print(f"  v2.7 대비 CAGR {'증가' if diff_cagr > 0 else '감소'}: {diff_cagr:+.1f}%p")
        print(f"  (MDD: 두 전략 독립 시 max({V27_MDD*100:.1f}%, {etf_mdd*100:.1f}%) 미만 예상)")
    print()
    print(SEP)


# ─── 진입점 ──────────────────────────────────────────────────────────────────
def main() -> None:
    print("데이터 로드 중...")
    df = load_index_data("KOSPI")
    etf_found = check_etf_data()
    print(f"KOSPI 지수: {len(df)}건 ({df.index[0].date()}~{df.index[-1].date()})")

    print("지표 계산 중...")
    df = add_indicators(df)

    total_days = len(df)
    start_date, end_date = df.index[0], df.index[-1]

    results: dict[str, dict] = {}
    for label, entry_fn, exit_fn, cost, use_ma200 in VARIANTS:
        print(f"  {label} ...")
        trades, equity_curve, invested_days = run_backtest(
            df, entry_fn, exit_fn, cost, CAPITAL, use_ma200
        )
        results[label] = compute_metrics(
            trades, equity_curve, CAPITAL, total_days, invested_days, start_date, end_date
        )

    print_report(results, df, etf_found)


if __name__ == "__main__":
    main()
