# BACKTEST_SPEC.md — 백테스트 명세

## 1. vectorbt 기반 백테스트 엔진

### 실행 방법
```bash
# 기본 백테스트
python -m src.backtest.engine --strategy macd_rsi --period 2y

# 파라미터 최적화
python -m src.backtest.optimizer --strategy macd_rsi --grid

# Walk-Forward 검증
python -m src.backtest.engine --strategy macd_rsi --walk-forward
```

---

## 2. 비용 모델 (필수 반영)

```python
# src/backtest/engine.py
COMMISSION_RATE = 0.00015   # 수수료 0.015% (편도)
TAX_RATE        = 0.0015    # 거래세 0.15% (매도 시만, 2025년 기준)
SLIPPAGE_RATE   = 0.001     # 슬리피지 0.1%

# vectorbt에서 비용 적용
portfolio = vbt.Portfolio.from_signals(
    close        = price_data,
    entries      = entry_signals,
    exits        = exit_signals,
    init_cash    = INITIAL_CAPITAL,
    fees         = COMMISSION_RATE + SLIPPAGE_RATE,   # 진입 시
    slippage     = SLIPPAGE_RATE,
    # 매도세는 별도 처리 필요 (vectorbt fees는 양방향 동일 적용)
)
```

**주의**: vectorbt `fees`는 매수/매도 동일하게 적용됨. 거래세(매도만)는 순수익 계산 후 후처리로 반영:
```python
# 세후 순수익 계산
gross_return = portfolio.total_return()
estimated_tax_drag = TAX_RATE * portfolio.trades.count() * 0.5  # 근사치
net_return = gross_return - estimated_tax_drag
```

---

## 3. 백테스트 엔진 코드 구조

```python
# src/backtest/engine.py
import vectorbt as vbt
import pandas as pd
from pykrx import stock

class BacktestEngine:

    def run(
        self,
        codes:       list[str],
        start_date:  str,        # 'YYYY-MM-DD'
        end_date:    str,
        params:      dict
    ) -> BacktestResult:

        # 1. 데이터 로드
        price_data = self._load_price_data(codes, start_date, end_date)

        # 2. 지표 계산
        indicators = {code: calculate_indicators(df)
                     for code, df in price_data.items()}

        # 3. 신호 생성
        entries, exits = self._generate_signals(indicators, params)

        # 4. 포트폴리오 시뮬레이션
        portfolio = vbt.Portfolio.from_signals(
            close    = price_data,
            entries  = entries,
            exits    = exits,
            init_cash= params.get('initial_capital', 10_000_000),
            fees     = COMMISSION_RATE + SLIPPAGE_RATE,
            freq     = 'D',
        )

        # 5. 성과 지표 추출
        return self._extract_metrics(portfolio)

    def _extract_metrics(self, portfolio) -> dict:
        stats = portfolio.stats()
        return {
            'total_return':   round(portfolio.total_return() * 100, 2),
            'annual_return':  round(portfolio.annualized_return() * 100, 2),
            'max_drawdown':   round(portfolio.max_drawdown() * 100, 2),
            'sharpe_ratio':   round(portfolio.sharpe_ratio(), 3),
            'sortino_ratio':  round(portfolio.sortino_ratio(), 3),
            'win_rate':       round(portfolio.trades.win_rate() * 100, 2),
            'profit_factor':  round(portfolio.trades.profit_factor(), 3),
            'avg_trade_return': round(portfolio.trades.returns.mean() * 100, 2),
            'trade_count':    portfolio.trades.count(),
            'avg_hold_days':  round(portfolio.trades.duration.mean().days, 1),
        }
```

---

## 4. 파라미터 최적화

```python
# src/backtest/optimizer.py
import vectorbt as vbt
import numpy as np

class ParameterOptimizer:

    def run_grid_search(
        self,
        codes:      list[str],
        start_date: str,
        end_date:   str
    ) -> pd.DataFrame:

        # 파라미터 그리드 정의
        macd_fast   = vbt.Param([8, 10, 12], name='macd_fast')
        macd_slow   = vbt.Param([22, 24, 26], name='macd_slow')
        rsi_period  = vbt.Param([12, 14], name='rsi_period')
        rsi_min     = vbt.Param([35, 40, 45], name='rsi_min')
        target_ret  = vbt.Param([0.06, 0.08, 0.10], name='target_ret')
        stop_atr    = vbt.Param([1.0, 1.5, 2.0], name='stop_atr')

        # 신호 생성 (벡터화)
        price_data = self._load_price_data(codes, start_date, end_date)

        results = []
        for params in self._param_combinations():
            result = BacktestEngine().run(codes, start_date, end_date, params)
            results.append({**params, **result})

        df_results = pd.DataFrame(results)

        # 필터링 기준 적용
        filtered = df_results[
            (df_results['sharpe_ratio']  >= 1.0) &
            (df_results['max_drawdown']  >= -15.0) &
            (df_results['win_rate']      >= 45.0) &
            (df_results['profit_factor'] >= 1.8)
        ]

        return filtered.sort_values('sharpe_ratio', ascending=False)
```

---

## 5. Walk-Forward 검증

과최적화 방지를 위한 Walk-Forward 분석.

```
전체 데이터: 2020-01-01 ~ 2024-12-31 (5년)
│
├── 1구간: Train 2020~2022 / Test 2023 Q1
├── 2구간: Train 2020~2023 Q1 / Test 2023 Q2
├── 3구간: Train 2020~2023 Q2 / Test 2023 Q3
└── ...
```

```python
def walk_forward(
    codes:        list[str],
    train_months: int = 24,    # 학습 기간
    test_months:  int = 3,     # 검증 기간
    step_months:  int = 3      # 슬라이딩 스텝
) -> list[BacktestResult]:

    results = []
    # 각 구간별로 최적 파라미터 찾고 다음 기간 OOS 성과 측정
    for train_start, train_end, test_start, test_end in generate_windows(...):
        best_params = optimizer.run_grid_search(codes, train_start, train_end)
        oos_result  = engine.run(codes, test_start, test_end, best_params)
        results.append(oos_result)

    return results   # OOS 결과 평균이 실거래 기댓값
```

---

## 6. 성과 기준 (실거래 전환 기준)

백테스트 또는 Paper Trading에서 아래 기준 모두 충족 시 실거래 전환 고려.

| 지표 | 최소 기준 | 권장 기준 |
|------|---------|---------|
| 연환산 수익률 | ≥ 15% | ≥ 25% |
| 최대 낙폭 (MDD) | ≥ -20% | ≥ -15% |
| 샤프지수 | ≥ 0.8 | ≥ 1.2 |
| 승률 | ≥ 43% | ≥ 50% |
| 손익비 | ≥ 1.5 | ≥ 2.0 |
| 총 매매 횟수 | ≥ 50회 | ≥ 100회 |
| OOS 성과 저하 | ≤ 30% 감소 | ≤ 20% 감소 |

---

## 7. 백테스트 주의사항 (Look-ahead bias 방지)

```python
# ❌ 잘못된 예: 당일 종가로 당일 신호 생성 후 당일 체결 (불가능)
entry = df['close'] > df['sma20']           # 당일 종가 확인
price = df['close']                         # 당일 종가로 체결 → BIAS!

# ✅ 올바른 예: 전일 신호 확인 → 다음날 시가 체결
entry = (df['close'] > df['sma20']).shift(1)  # 전일 신호
price = df['open']                             # 익일 시가 체결
```

vectorbt에서 `signal_bar='close'`, `order_bar='next_open'` 설정 권장.
