"""파라미터 최적화 -- 그리드/랜덤 서치 + Walk-Forward.

속도 최적화:
- 랜덤 서치: 전체 조합의 50% 샘플링 (최소 24개 하한선)
- 가격 데이터 캐싱: BacktestEngine의 _price_cache 활용
"""

import itertools
import random
from datetime import datetime, timedelta

import pandas as pd
from loguru import logger

from src.backtest.engine import BacktestEngine, BacktestResult

PARAM_GRID = {
    "macd_fast": [8, 10, 12],
    "macd_slow": [22, 24, 26],
    "macd_signal": [7, 9],
    "rsi_period": [12, 14],
    "rsi_min": [30, 35, 40],
    "rsi_max": [65, 70, 75],
    "volume_multiplier": [1.0, 1.2, 1.5],
    "stop_atr_mult": [1.5, 2.0, 2.5],
    "target_return": [0.08, 0.10, 0.12],
    "max_hold_days": [10, 15, 20],
}


class ParameterOptimizer:
    """그리드/랜덤 서치 및 Walk-Forward 검증 기반 파라미터 최적화."""

    def __init__(self, engine: BacktestEngine | None = None):
        self.engine = engine or BacktestEngine()

    def run_grid_search(
        self,
        codes: list[str],
        start_date: str,
        end_date: str,
        param_grid: dict | None = None,
        strategy_name: str = "momentum_pullback",
        use_portfolio: bool = True,
        max_positions: int = 3,
        sample_ratio: float = 0.5,
        min_samples: int = 24,
    ) -> pd.DataFrame:
        """그리드/랜덤 서치 실행.

        전체 조합이 min_samples 이하면 전수 검사,
        아니면 sample_ratio 비율로 랜덤 샘플링 (최소 min_samples).

        Args:
            codes: 종목 코드 리스트.
            start_date: 시작일 (YYYYMMDD).
            end_date: 종료일 (YYYYMMDD).
            param_grid: 파라미터 그리드 (기본: PARAM_GRID).
            strategy_name: 전략 이름.
            use_portfolio: True면 run_portfolio() 사용.
            max_positions: 포트폴리오 최대 보유 종목 수.
            sample_ratio: 랜덤 샘플링 비율 (0.0~1.0, 기본 0.5).
            min_samples: 최소 샘플 수 (기본 24).

        Returns:
            결과 DataFrame, sharpe 내림차순 정렬.
        """
        grid = param_grid or PARAM_GRID
        all_combos = self._generate_param_combinations(grid)
        total = len(all_combos)

        # 랜덤 샘플링 적용
        if total <= min_samples:
            combos = all_combos
            search_mode = "전수"
        else:
            n_samples = max(min_samples, int(total * sample_ratio))
            n_samples = min(n_samples, total)
            combos = random.sample(all_combos, n_samples)
            search_mode = f"랜덤 {n_samples}/{total}"

        logger.info(
            f"그리드 서치 시작: {search_mode} ({len(combos)}개 조합, "
            f"전략: {strategy_name}, "
            f"{'포트폴리오' if use_portfolio else '독립'} 모드)"
        )

        # 포트폴리오 모드: 데이터/지표를 한 번만 계산 (프리컴퓨팅)
        context = None
        if use_portfolio:
            context = self.engine.prepare_portfolio_context(
                codes, start_date, end_date, strategy_name, use_market_filter=True,
            )
            if context is None:
                logger.warning("프리컴퓨팅 실패, 데이터 없음")
                return pd.DataFrame()

        results = []
        for i, params in enumerate(combos):
            try:
                if use_portfolio:
                    result = self.engine.run_portfolio(
                        codes, start_date, end_date, params, strategy_name,
                        max_positions=max_positions, use_market_filter=True,
                        _context=context,
                    )
                else:
                    result = self.engine.run(
                        codes, start_date, end_date, params, strategy_name,
                    )

                row = {**params}
                row["total_return"] = result.total_return
                row["annual_return"] = result.annual_return
                row["max_drawdown"] = result.max_drawdown
                row["sharpe_ratio"] = result.sharpe_ratio
                row["sortino_ratio"] = result.sortino_ratio
                row["win_rate"] = result.win_rate
                row["profit_factor"] = result.profit_factor
                row["avg_trade_return"] = result.avg_trade_return
                row["trade_count"] = result.trade_count
                row["avg_hold_days"] = result.avg_hold_days
                results.append(row)

                if (i + 1) % 10 == 0:
                    logger.info(f"진행: {i + 1}/{len(combos)}")
            except Exception as e:
                logger.warning(f"조합 {i + 1} 실패: {e}")

        logger.info(f"그리드 서치 완료: {len(results)}/{len(combos)}개 유효")

        if not results:
            logger.warning("유효한 결과 없음")
            return pd.DataFrame()

        df = pd.DataFrame(results)

        # 필터 적용
        filtered = df[
            (df["sharpe_ratio"] >= 1.0)
            & (df["max_drawdown"] >= -15.0)
            & (df["win_rate"] >= 45.0)
            & (df["profit_factor"] >= 1.8)
        ]

        if filtered.empty:
            logger.warning(
                "필터 통과 조합 없음, 전체 결과 sharpe 정렬로 반환"
            )
            return df.sort_values("sharpe_ratio", ascending=False).reset_index(
                drop=True
            )

        return filtered.sort_values(
            "sharpe_ratio", ascending=False
        ).reset_index(drop=True)

    def walk_forward(
        self,
        codes: list[str],
        start_date: str,
        end_date: str,
        train_months: int = 24,
        test_months: int = 6,
        step_months: int = 12,
        strategy_name: str = "momentum_pullback",
        param_grid: dict | None = None,
        use_portfolio: bool = True,
        max_positions: int = 3,
    ) -> list[BacktestResult]:
        """Walk-Forward 검증.

        각 구간: train 기간에서 최적 파라미터 -> test 기간 OOS 성과 측정.
        """
        windows = self._generate_walk_forward_windows(
            start_date, end_date, train_months, test_months, step_months
        )

        if not windows:
            logger.warning("Walk-Forward 윈도우 생성 실패")
            return []

        grid = param_grid or {
            "stop_atr_mult": [2.0, 2.5],
            "target_return": [0.06, 0.08],
            "max_hold_days": [10, 15],
            "max_stop_pct": [0.07, 0.10],
        }

        def _run(codes, start, end, params):
            if use_portfolio:
                return self.engine.run_portfolio(
                    codes, start, end, params, strategy_name,
                    max_positions=max_positions, use_market_filter=True,
                )
            return self.engine.run(codes, start, end, params, strategy_name)

        logger.info(f"Walk-Forward: {len(windows)}개 구간")
        oos_results = []

        for i, (train_start, train_end, test_start, test_end) in enumerate(
            windows
        ):
            logger.info(
                f"구간 {i + 1}: "
                f"Train {train_start}~{train_end}, "
                f"Test {test_start}~{test_end}"
            )

            try:
                train_results = self.run_grid_search(
                    codes, train_start, train_end, grid,
                    strategy_name=strategy_name,
                    use_portfolio=use_portfolio,
                    max_positions=max_positions,
                )

                if train_results.empty:
                    logger.warning(f"구간 {i + 1}: 훈련 결과 없음, 기본 파라미터 사용")
                    best_params = {}
                else:
                    best_row = train_results.iloc[0]
                    best_params = {
                        k: best_row[k]
                        for k in grid.keys()
                        if k in best_row.index
                    }

                oos_result = _run(codes, test_start, test_end, best_params)
                oos_result.params = best_params
                oos_results.append(oos_result)

                logger.info(
                    f"구간 {i + 1} OOS: "
                    f"수익률 {oos_result.total_return:.2f}%, "
                    f"Sharpe {oos_result.sharpe_ratio:.2f}, "
                    f"거래 {oos_result.trade_count}건"
                )
            except Exception as e:
                logger.error(f"구간 {i + 1} 실패: {e}")

        return oos_results

    def _generate_param_combinations(self, param_grid: dict) -> list[dict]:
        """파라미터 조합 생성."""
        keys = list(param_grid.keys())
        values = list(param_grid.values())
        combinations = []
        for combo in itertools.product(*values):
            combinations.append(dict(zip(keys, combo)))
        return combinations

    @staticmethod
    def _generate_walk_forward_windows(
        start_date: str,
        end_date: str,
        train_months: int,
        test_months: int,
        step_months: int,
    ) -> list[tuple[str, str, str, str]]:
        """Walk-Forward 윈도우 생성."""
        start = datetime.strptime(start_date, "%Y%m%d")
        end = datetime.strptime(end_date, "%Y%m%d")

        windows = []
        current = start

        while True:
            train_start = current
            train_end = train_start + timedelta(days=train_months * 30)
            test_start = train_end + timedelta(days=1)
            test_end = test_start + timedelta(days=test_months * 30)

            if test_end > end:
                break

            windows.append((
                train_start.strftime("%Y%m%d"),
                train_end.strftime("%Y%m%d"),
                test_start.strftime("%Y%m%d"),
                test_end.strftime("%Y%m%d"),
            ))

            current += timedelta(days=step_months * 30)

        return windows
