"""필터 완화 백테스트 검증 — RS/ADX/거래량 필터 조합 실험.

8개 변형:
  [0] CURRENT_V27       현재 v2.7 기준선 (ADX≥25, RS≥0.08)
  [1] RS_005            RS ≥ 0.05
  [2] RS_003            RS ≥ 0.03
  [3] RS_000            RS ≥ 0.00 (벤치마크 상회만)
  [4] ADAPTIVE_RS       적응형 RS — RS≥0.08 없으면 RS≥0.03 폴백
  [5] ADX20_RS005       ADX≥20 + RS≥0.05
  [6] ADX20_RS005_NOVOL ADX≥20 + RS≥0.05 + 거래량 필터 OFF
  [7] ADAPTIVE_NOVOL    적응형 RS (0.08/0.03) + 거래량 필터 OFF (ADX≥25)

설계 원칙:
  - precompute_daily_signals 내부에서 RS/ADX 필터를 적용하므로
    임계치가 다른 시나리오별로 precomp을 별도 생성함.
  - NOVOL 변형: precomp의 universe_at 재사용, candidates만 거래량 필터 없이 재빌드.
  - ADAPTIVE 변형: relaxed precomp 후 candidates를 per-date 분기 처리.
  - run_portfolio_backtest 는 candidates를 재필터링하지 않으므로
    exit 파라미터(BASE_PARAMS) 공통 사용.

Usage:
    python experiments/experiment_filter_relax.py
"""
from __future__ import annotations

import sys
import time
from dataclasses import replace
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from loguru import logger
logger.remove()
logger.add(sys.stderr, level="WARNING")

from src.backtest.portfolio_backtester import (
    load_backtest_data,
    precompute_daily_signals,
    run_portfolio_backtest,
)
from src.strategy.dynamic_hold import DynamicHoldParams
from src.strategy.ranking import RankingWeights, compute_composite_score
from src.strategy.scaling import ScalingParams
from src.strategy.sector_constraint import SectorConstraint
from src.strategy.trend_following_v2 import StrategyParams
from src.utils.cost_model import CostModel
from src.utils.slippage_model import SlippageParams

# ─────────────────────────────────────────────────────────────────────────────
# 고정 설정 (v2.7 exit 파라미터)
# ─────────────────────────────────────────────────────────────────────────────

CAPITAL    = 10_000_000
MAX_POS    = 5
MIN_AMOUNT = 300_000
BREADTH    = 0.40

BASE_PARAMS = StrategyParams(
    adx_threshold=25.0,
    relative_strength_threshold=0.08,
    stop_loss_atr=3.0,
    take_profit_atr=1.5,
    trailing_atr=3.0,
    tp1_sell_ratio=0.10,
    tp2_atr=4.0,
    tp2_sell_ratio=0.10,
)

WEIGHTS = RankingWeights(
    rs=0.50, momentum_atr=0.20, adx=0.15, liquidity=0.10, ma_alignment=0.05,
)

COST    = CostModel()
SECTOR  = SectorConstraint(enabled=False)
DYNHOLD = DynamicHoldParams(enabled=False)
SCALING = ScalingParams(enabled=False)
SLIP    = SlippageParams(
    enabled=True, base_slippage=0.0003, impact_coefficient=0.1, max_slippage=0.02,
)

# 변형 메타 (태그 → 설명)
VARIANT_LABELS: dict[str, str] = {
    "CURRENT_V27":       "v2.7 기준선 (ADX≥25, RS≥0.08)",
    "RS_005":            "RS ≥ 0.05",
    "RS_003":            "RS ≥ 0.03",
    "RS_000":            "RS ≥ 0.00 (벤치마크 상회)",
    "ADAPTIVE_RS":       "적응형 RS (0.08→0.03 폴백)",
    "ADX20_RS005":       "ADX≥20 + RS≥0.05",
    "ADX20_RS005_NOVOL": "ADX≥20 + RS≥0.05 + 거래량 OFF",
    "ADAPTIVE_NOVOL":    "적응형 RS + 거래량 OFF (ADX≥25)",
}


# ─────────────────────────────────────────────────────────────────────────────
# 헬퍼 함수
# ─────────────────────────────────────────────────────────────────────────────

def _mk_params(adx: float = 25.0, rs: float = 0.08) -> StrategyParams:
    """exit 파라미터는 BASE_PARAMS 유지, entry 임계치만 교체."""
    return replace(BASE_PARAMS, adx_threshold=adx, relative_strength_threshold=rs)


def _compute_precomp(label: str, dates: list, preloaded: dict,
                     adx: float, rs: float) -> dict:
    t0 = time.time()
    print(f"  [{label}] ADX≥{adx:.0f}, RS≥{rs:.2f}...", end="", flush=True)
    p = _mk_params(adx, rs)
    pc = precompute_daily_signals(
        dates,
        preloaded["ticker_data"],
        preloaded["ticker_date_idx"],
        set(preloaded["initial_universe"]),
        params=p,
        kospi_ret_map=preloaded.get("kospi_ret_map"),
        kosdaq_ret_map=preloaded.get("kosdaq_ret_map"),
        ticker_market=preloaded.get("ticker_market"),
        weights=WEIGHTS,
    )
    print(f"  {time.time() - t0:.1f}s")
    return pc


def _rebuild_novol_candidates(
    label: str,
    base_precomp: dict,
    trading_dates: list,
    preloaded: dict,
    adx: float,
    rs: float,
) -> dict:
    """base_precomp 의 universe_at 을 재사용하되, 거래량 필터 없이 candidates 재계산.

    거래량 필터(`avg_volume_5 > avg_volume_20`) 를 제외한 모든 entry 조건 동일.
    """
    t0 = time.time()
    print(f"  [{label}] ADX≥{adx:.0f}, RS≥{rs:.2f}, vol=OFF...", end="", flush=True)

    params = _mk_params(adx, rs)
    ticker_data    = preloaded["ticker_data"]
    ticker_date_idx = preloaded["ticker_date_idx"]
    kospi_ret_map  = preloaded.get("kospi_ret_map", {})
    kosdaq_ret_map = preloaded.get("kosdaq_ret_map", {})
    ticker_market  = preloaded.get("ticker_market", {})
    universe_at    = base_precomp["universe_at"]
    ticker_industry = base_precomp.get("ticker_industry", {})
    market_aware   = kosdaq_ret_map is not None and ticker_market is not None

    candidates_by_date: dict[str, list] = {}

    for date_str in trading_dates:
        ts       = pd.Timestamp(date_str)
        universe = universe_at.get(date_str, set())
        if not universe:
            candidates_by_date[date_str] = []
            continue

        kospi_ret  = kospi_ret_map.get(ts) if kospi_ret_map else None
        kosdaq_ret = kosdaq_ret_map.get(ts) if kosdaq_ret_map else None

        cands: list[dict] = []
        for ticker in universe:
            idx_map = ticker_date_idx.get(ticker)
            if not idx_map:
                continue
            curr_i = idx_map.get(ts)
            if curr_i is None or curr_i < params.ma_long:
                continue

            day = ticker_data[ticker].iloc[curr_i]

            req = ['ma20', 'ma60', 'ma120', 'ma60_slope', 'ma60_dist',
                   'atr', 'adx', 'macd_hist', 'avg_volume_5', 'avg_volume_20',
                   'avg_trading_value_20', 'stock_ret_n']
            if any(pd.isna(day.get(k)) for k in req):
                continue
            if day['atr'] <= 0 or day['close'] <= 0:
                continue

            if not (day['close'] > day['ma20'] > day['ma60'] > day['ma120']):
                continue
            if day['ma60_slope'] <= 0:
                continue
            if not (params.ma60_position_min <= day['ma60_dist'] <= params.ma60_position_max):
                continue
            if day['macd_hist'] <= 0:
                continue
            # ← 거래량 필터 생략 (avg_volume_5 > avg_volume_20 체크 없음)
            if day['adx'] < params.adx_threshold:
                continue
            if day['avg_trading_value_20'] < params.min_trading_value:
                continue
            atr_ratio = day['atr'] / day['close']
            if not (params.atr_price_min <= atr_ratio <= params.atr_price_max):
                continue

            # 상대강도
            if market_aware:
                mkt       = ticker_market.get(ticker, 'KOSPI')
                bench_ret = kosdaq_ret if mkt == 'KOSDAQ' else kospi_ret
            elif kospi_ret_map is not None:
                bench_ret = kospi_ret
            else:
                bench_ret = None

            if kospi_ret_map is not None or market_aware:
                if bench_ret is None or pd.isna(bench_ret):
                    continue
                if (day['stock_ret_n'] - float(bench_ret)) < params.relative_strength_threshold:
                    continue

            rs_val = (
                float(day['stock_ret_n']) - float(bench_ret)
                if bench_ret is not None and not pd.isna(bench_ret) else 0.0
            )
            cands.append({
                'ticker':               ticker,
                'code':                 ticker,
                'score':                float(day['adx']),
                'close':                float(day['close']),
                'atr':                  float(day['atr']),
                'atr_ratio':            float(atr_ratio),
                'ma60_dist':            float(day['ma60_dist']),
                'adx':                  float(day['adx']),
                'rs':                   rs_val,
                'stock_ret_n':          float(day['stock_ret_n']),
                'avg_trading_value_20': float(day['avg_trading_value_20']),
                'industry':             ticker_industry.get(ticker, 'UNKNOWN'),
            })

        compute_composite_score(cands, WEIGHTS)
        candidates_by_date[date_str] = cands

    print(f"  {time.time() - t0:.1f}s")
    result = dict(base_precomp)
    result['candidates'] = candidates_by_date
    return result


def _make_adaptive_precomp(
    relaxed_precomp: dict,
    rs_strict: float = 0.08,
) -> dict:
    """당일 RS≥rs_strict 후보가 있으면 사용, 없으면 relaxed_precomp 후보 전체 사용."""
    adaptive_candidates: dict[str, list] = {}
    for date_str, cands in relaxed_precomp['candidates'].items():
        strict = [c for c in cands if c.get('rs', 0.0) >= rs_strict]
        adaptive_candidates[date_str] = strict if strict else list(cands)

    result = dict(relaxed_precomp)
    result['candidates'] = adaptive_candidates
    return result


def _run_bt(preloaded: dict, precomp: dict, params: StrategyParams):
    return run_portfolio_backtest(
        initial_capital=CAPITAL,
        max_positions=MAX_POS,
        params=params,
        cost=COST,
        min_position_amount=MIN_AMOUNT,
        preloaded_data=preloaded,
        precomputed=precomp,
        sizing_mode="equity",
        breadth_gate_threshold=BREADTH,
        regime_gate_enabled=True,
        sector_constraint=SECTOR,
        dynamic_hold=DYNHOLD,
        scaling=SCALING,
        slippage_params=SLIP,
    )


def _fmt_pf(pf: float) -> str:
    return "inf" if pf == float("inf") else f"{pf:.2f}"


# ─────────────────────────────────────────────────────────────────────────────
# 보고서
# ─────────────────────────────────────────────────────────────────────────────

def print_report(
    results: list[tuple[str, object | None]],
    elapsed: float,
    log_fn,
) -> None:
    SEP  = "═" * 82
    SEP2 = "─" * 82

    log_fn(SEP)
    log_fn(f"  필터 완화 백테스트 (10M/5종목, 2014~2026)")
    log_fn(SEP)
    log_fn("")

    rows: list[dict] = []
    for tag, r in results:
        if r is None:
            rows.append({'tag': tag, 'error': True})
            continue
        util = r.avg_positions / MAX_POS * 100
        # cagr_pct / max_drawdown_pct 모두 소수 (0.073 = 7.3%)
        mdd_abs = abs(r.max_drawdown_pct)
        cm      = (r.cagr_pct / mdd_abs) if mdd_abs > 0 else 0.0
        rows.append({
            'tag':    tag,
            'label':  VARIANT_LABELS.get(tag, tag),
            'trades': r.total_trades,
            'wr':     r.win_rate * 100,
            'pf':     r.profit_factor,
            'cagr':   r.cagr_pct * 100,
            'mdd':    r.max_drawdown_pct * 100,
            'cm':     cm,
            'util':   util,
            'error':  False,
        })

    # ── 전체 비교 ──────────────────────────────────────────────────────────────
    log_fn("■ 전체 비교")
    hdr = (f"  {'변형':<22}  {'건':>4}  {'WR':>6}  {'PF':>5}  "
           f"{'CAGR':>7}  {'MDD':>7}  {'C/M':>5}  {'활용':>5}")
    log_fn(hdr)
    log_fn(SEP2)
    for row in rows:
        if row.get('error'):
            log_fn(f"  [{row['tag']:<20}]  오류")
            continue
        log_fn(
            f"  [{row['tag']:<20}]  "
            f"{row['trades']:>4}  {row['wr']:>5.1f}%  {_fmt_pf(row['pf']):>5}  "
            f"{row['cagr']:>+6.1f}%  {row['mdd']:>+6.1f}%  "
            f"{row['cm']:>5.2f}  {row['util']:>4.0f}%"
        )
    log_fn("")
    log_fn("  활용: avg_positions / max_positions × 100%")
    log_fn("")

    valid_rows = [r for r in rows if not r.get('error')]
    base = next((r for r in valid_rows if r['tag'] == 'CURRENT_V27'), None)
    if not base or len(valid_rows) < 2:
        return

    # ── [0] 대비 변화 ─────────────────────────────────────────────────────────
    log_fn("■ [CURRENT_V27] 대비 변화")
    dhdr = (f"  {'변형':<22}  {'Δ건':>5}  {'ΔWR':>7}  {'ΔPF':>6}  "
            f"{'ΔCAGR':>7}  {'ΔMDD':>7}  {'ΔC/M':>6}  {'Δ활용':>6}")
    log_fn(dhdr)
    log_fn(SEP2)
    for row in valid_rows:
        if row['tag'] == 'CURRENT_V27':
            continue
        dt  = row['trades'] - base['trades']
        dw  = row['wr']     - base['wr']
        dc  = row['cagr']   - base['cagr']
        dm  = row['mdd']    - base['mdd']
        dcm = row['cm']     - base['cm']
        du  = row['util']   - base['util']
        dpf = row['pf'] - base['pf'] if row['pf'] != float('inf') else float('inf')
        dpf_str = "  +inf" if dpf == float('inf') else f"{dpf:>+6.2f}"
        log_fn(
            f"  [{row['tag']:<20}]  "
            f"{dt:>+5}  {dw:>+6.1f}%p  {dpf_str}  "
            f"{dc:>+6.1f}%  {dm:>+6.1f}%  {dcm:>+6.2f}  {du:>+5.0f}%p"
        )
    log_fn("")

    # ── 핵심 질문 답변 ─────────────────────────────────────────────────────────
    log_fn("■ 핵심 질문 답변")

    # Q1: RS 완화 시 WR/PF 하락 규모
    rs_tags = ['RS_005', 'RS_003', 'RS_000']
    rs_rows = [r for r in valid_rows if r['tag'] in rs_tags]
    if rs_rows:
        changes = [(r['tag'], r['wr'] - base['wr'], r['pf'] - base['pf']) for r in rs_rows]
        log_fn(f"  Q1 RS 완화 WR 변화: " +
               ", ".join(f"{t} {dw:+.1f}%p (PF {dp:+.2f})" for t, dw, dp in changes))

    # Q2: 적응형 vs 고정 RS_003
    adap = next((r for r in valid_rows if r['tag'] == 'ADAPTIVE_RS'), None)
    r003 = next((r for r in valid_rows if r['tag'] == 'RS_003'), None)
    if adap and r003:
        winner = '적응형 우수' if adap['cm'] > r003['cm'] else ('동률' if adap['cm'] == r003['cm'] else 'RS_003 우수')
        log_fn(f"  Q2 적응형 vs RS_003: CAGR/MDD {adap['cm']:.2f} vs {r003['cm']:.2f} → {winner}")
        log_fn(f"     거래 수: ADAPTIVE {adap['trades']} vs RS_003 {r003['trades']} "
               f"({adap['trades'] - r003['trades']:+d}건)")

    # Q3: 거래량 필터 제거 품질
    adx20    = next((r for r in valid_rows if r['tag'] == 'ADX20_RS005'), None)
    novol    = next((r for r in valid_rows if r['tag'] == 'ADX20_RS005_NOVOL'), None)
    adp_novol = next((r for r in valid_rows if r['tag'] == 'ADAPTIVE_NOVOL'), None)
    if adx20 and novol:
        log_fn(f"  Q3 거래량 제거: WR {adx20['wr']:.1f}% → {novol['wr']:.1f}% ({novol['wr']-adx20['wr']:+.1f}%p)")
        log_fn(f"     PF {_fmt_pf(adx20['pf'])} → {_fmt_pf(novol['pf'])}, "
               f"CAGR {adx20['cagr']:+.1f}% → {novol['cagr']:+.1f}%")

    # Q4: 자본활용 개선 → CAGR 개선 상관
    top_util = max(valid_rows, key=lambda r: r['util'])
    top_cagr = max(valid_rows, key=lambda r: r['cagr'])
    log_fn(f"  Q4 자본활용 최고: [{top_util['tag']}] {top_util['util']:.0f}%  "
           f"CAGR 최고: [{top_cagr['tag']}] {top_cagr['cagr']:+.1f}%")
    if top_util['tag'] == top_cagr['tag']:
        log_fn(f"     → 자본활용 ↑ = CAGR ↑ 일치")
    else:
        log_fn(f"     → 불일치 — 활용률이 높아도 CAGR이 낮을 수 있음 (진입 품질 희석)")
    log_fn("")

    # ── 권장 조합 ──────────────────────────────────────────────────────────────
    log_fn("■ 권장 조합 (CAGR/MDD 최우선, 자본활용 ≥ 기준선)")
    base_util = base['util']
    viable = [r for r in valid_rows if r['util'] >= base_util - 2]  # 기준선 -2%p 허용
    if viable:
        best_cm   = max(viable, key=lambda r: r['cm'])
        best_cagr = max(viable, key=lambda r: r['cagr'])
        log_fn(f"  CAGR/MDD 최고: [{best_cm['tag']}]  "
               f"C/M {best_cm['cm']:.2f}, CAGR {best_cm['cagr']:+.1f}%, 활용 {best_cm['util']:.0f}%")
        if best_cm['tag'] != best_cagr['tag']:
            log_fn(f"  CAGR 절대값 최고: [{best_cagr['tag']}]  "
                   f"C/M {best_cagr['cm']:.2f}, CAGR {best_cagr['cagr']:+.1f}%, 활용 {best_cagr['util']:.0f}%")
    log_fn("")
    log_fn(f"  소요 시간: {elapsed:.1f}s")
    log_fn(SEP)


# ─────────────────────────────────────────────────────────────────────────────
# 메인
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    out_path = ROOT / "experiments" / "results_filter_relax.txt"
    output_lines: list[str] = []

    def log(msg: str = "") -> None:
        print(msg)
        output_lines.append(msg)

    t_total = time.time()

    # ── 1. 데이터 로드 ────────────────────────────────────────────────────────
    print("\n[1/3] 데이터 로드...")
    t0 = time.time()
    preloaded = load_backtest_data(BASE_PARAMS)
    all_dates: list[str] = preloaded['trading_dates']
    print(f"  완료: {time.time() - t0:.1f}s  ({len(all_dates)}거래일)")

    # ── 2. Precompute ──────────────────────────────────────────────────────────
    print("\n[2/3] Precompute 생성...")

    # 표준 5개 (DB 기반 universe refresh 포함)
    pc_v27         = _compute_precomp("v27",         all_dates, preloaded, 25.0, 0.08)
    pc_rs05        = _compute_precomp("rs05",         all_dates, preloaded, 25.0, 0.05)
    pc_rs03        = _compute_precomp("rs03",         all_dates, preloaded, 25.0, 0.03)
    pc_rs00        = _compute_precomp("rs00",         all_dates, preloaded, 25.0, 0.00)
    pc_adx20_rs05  = _compute_precomp("adx20_rs05",   all_dates, preloaded, 20.0, 0.05)

    # NOVOL 2개 (universe_at 재사용, candidates만 재빌드)
    pc_adx20_rs05_novol = _rebuild_novol_candidates(
        "adx20_rs05_NOVOL", pc_adx20_rs05, all_dates, preloaded, 20.0, 0.05,
    )
    pc_adp_novol_base = _rebuild_novol_candidates(
        "adp_NOVOL",        pc_rs03,         all_dates, preloaded, 25.0, 0.03,
    )

    # ADAPTIVE 2개 (precomp post-process — O(dates × candidates))
    pc_adaptive       = _make_adaptive_precomp(pc_rs03,          rs_strict=0.08)  # [4]
    pc_adaptive_novol = _make_adaptive_precomp(pc_adp_novol_base, rs_strict=0.08)  # [7]

    # ── 3. 백테스트 ─────────────────────────────────────────────────────────────
    print("\n[3/3] 백테스트 실행 (8개 변형)...")

    runs: list[tuple[str, StrategyParams, dict]] = [
        ("CURRENT_V27",       _mk_params(25.0, 0.08), pc_v27),
        ("RS_005",            _mk_params(25.0, 0.05), pc_rs05),
        ("RS_003",            _mk_params(25.0, 0.03), pc_rs03),
        ("RS_000",            _mk_params(25.0, 0.00), pc_rs00),
        ("ADAPTIVE_RS",       _mk_params(25.0, 0.03), pc_adaptive),
        ("ADX20_RS005",       _mk_params(20.0, 0.05), pc_adx20_rs05),
        ("ADX20_RS005_NOVOL", _mk_params(20.0, 0.05), pc_adx20_rs05_novol),
        ("ADAPTIVE_NOVOL",    _mk_params(25.0, 0.03), pc_adaptive_novol),
    ]

    results: list[tuple[str, object | None]] = []
    for i, (tag, params, precomp) in enumerate(runs, 1):
        t0 = time.time()
        print(f"  [{i}/8] {tag}...", end="", flush=True)
        try:
            r = _run_bt(preloaded, precomp, params)
            results.append((tag, r))
            print(f"  {time.time() - t0:.1f}s  "
                  f"PF={_fmt_pf(r.profit_factor)}  "
                  f"CAGR={r.cagr_pct*100:+.1f}%  "
                  f"WR={r.win_rate*100:.1f}%  "
                  f"건={r.total_trades}")
        except Exception as exc:
            print(f"  오류: {exc}")
            results.append((tag, None))

    elapsed = time.time() - t_total
    print()
    print_report(results, elapsed, log)

    out_path.write_text("\n".join(output_lines), encoding="utf-8")
    print(f"\n결과 저장: {out_path}")


if __name__ == "__main__":
    main()
