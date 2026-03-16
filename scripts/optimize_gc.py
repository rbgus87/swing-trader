"""골든크로스 전략 파라미터 최적화 스크립트."""
import sys, os, itertools, time, warnings
import numpy as np, pandas as pd

warnings.filterwarnings("ignore")
sys.stdout.reconfigure(encoding="utf-8")
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ".")

from src.backtest.engine import BacktestEngine
from src.strategy.signals import calculate_indicators

CODES = ["005930","000660","005380","000270","068270","035420","035720","105560","055550","066570"]
engine = BacktestEngine(initial_capital=10_000_000)

print("데이터 로드...")
t0 = time.time()
price_data = engine.load_price_data(CODES, "20200101", "20250314")
cache = {}
for c, d in price_data.items():
    try:
        cache[c] = calculate_indicators(d)
    except:
        pass
print(f"{len(cache)}종목 로드 ({time.time()-t0:.0f}초)")

def gc_signals(df_ind, p):
    va = df_ind["volume"].rolling(20).mean()
    gc = (df_ind["sma5"] > df_ind["sma20"]) & (df_ind["sma5"].shift(1) <= df_ind["sma20"].shift(1))
    e = gc & (df_ind["rsi"] >= 50) & (df_ind["adx"] >= p.get("adx_threshold", 20)) & (df_ind["volume"] >= va * p.get("volume_multiplier", 1.0))
    dc = (df_ind["sma5"] < df_ind["sma20"]) & (df_ind["sma5"].shift(1) >= df_ind["sma20"].shift(1))
    en = e.shift(1).fillna(False).astype(bool)
    ex = dc.shift(1).fillna(False).astype(bool)
    en.index = df_ind.index
    ex.index = df_ind.index
    return en, ex

grid = {
    "volume_multiplier": [0.8, 1.0, 1.2, 1.5],
    "adx_threshold": [10, 15, 20, 25],
    "stop_atr_mult": [2.0, 2.5, 3.0],
    "target_return": [0.08, 0.10, 0.12, 0.15],
    "max_hold_days": [10, 15, 20],
    "trailing_activate_pct": [0.03, 0.05, 0.07],
}
combos = [dict(zip(grid.keys(), c)) for c in itertools.product(*grid.values())]
print(f"{len(combos)} 조합 탐색 시작")

t1 = time.time()
results = []
for i, p in enumerate(combos):
    sr = []
    for c, di in cache.items():
        try:
            en, ex = gc_signals(di, p)
            tr, eq = engine._simulate_portfolio(di["close"], di["high"], di["low"], di["atr"], en, ex, p)
            r = engine._calculate_metrics(tr, eq, p)
            sr.append(r)
        except:
            pass
    if sr:
        pf = [r.profit_factor for r in sr if r.profit_factor != float("inf")]
        results.append({**p,
            "ret": round(np.mean([r.total_return for r in sr]), 2),
            "mdd": round(np.min([r.max_drawdown for r in sr]), 2),
            "sha": round(np.mean([r.sharpe_ratio for r in sr]), 3),
            "win": round(np.mean([r.win_rate for r in sr]), 2),
            "pf": round(np.mean(pf), 2) if pf else 0,
            "cnt": sum(r.trade_count for r in sr),
            "hld": round(np.mean([r.avg_hold_days for r in sr]), 1),
        })
    if (i + 1) % 100 == 0:
        e = time.time() - t1
        eta = e / (i + 1) * (len(combos) - i - 1)
        print(f"  {i+1}/{len(combos)} ({e:.0f}s, ETA {eta:.0f}s)")

df = pd.DataFrame(results).sort_values("sha", ascending=False)
print(f"\n완료: {len(combos)}조합, {time.time()-t1:.0f}초")

cols = ["volume_multiplier", "adx_threshold", "stop_atr_mult", "target_return", "max_hold_days", "trailing_activate_pct", "ret", "mdd", "sha", "win", "pf", "cnt", "hld"]
print(f"\n=== 상위 15개 (sharpe 기준) ===")
print(df[cols].head(15).to_string())

good = df[(df["cnt"] >= 30) & (df["mdd"] >= -20) & (df["win"] >= 43)]
print(f"\n=== 기준충족 (거래>=30, MDD>=-20%, 승률>=43%) ===")
if not good.empty:
    print(good[cols].head(10).to_string())
    best = good.iloc[0]
    print(f'\n최적: sharpe={best["sha"]:.3f}, return={best["ret"]:.2f}%, mdd={best["mdd"]:.2f}%, trades={int(best["cnt"])}')
else:
    print("없음")
    r2 = df[(df["cnt"] >= 30) & (df["mdd"] >= -30)]
    print(f"\n(완화) MDD>=-30%, 거래>=30:")
    if not r2.empty:
        print(r2[cols].head(5).to_string())
