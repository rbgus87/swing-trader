"""백테스트 성과 HTML 리포트 생성."""

import os
from datetime import datetime

from loguru import logger

from src.backtest.engine import BacktestResult


class BacktestReporter:
    """백테스트 결과를 HTML 및 콘솔 리포트로 출력."""

    def generate_html(
        self,
        result: BacktestResult,
        output_path: str = "reports/backtest_report.html",
    ) -> str:
        """성과 지표 + 파라미터를 간단한 HTML 테이블 리포트로 생성.

        Args:
            result: BacktestResult 성과 지표.
            output_path: HTML 파일 저장 경로.

        Returns:
            생성된 HTML 파일 경로.
        """
        metrics_rows = self._build_metrics_rows(result)
        params_rows = self._build_params_rows(result.params)

        html = f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<title>백테스트 리포트</title>
<style>
body {{ font-family: 'Malgun Gothic', sans-serif; margin: 40px; background: #f5f5f5; }}
h1 {{ color: #333; border-bottom: 2px solid #2196F3; padding-bottom: 10px; }}
h2 {{ color: #555; margin-top: 30px; }}
table {{ border-collapse: collapse; width: 100%; max-width: 600px; margin: 10px 0; }}
th, td {{ border: 1px solid #ddd; padding: 10px 14px; text-align: left; }}
th {{ background: #2196F3; color: white; }}
tr:nth-child(even) {{ background: #f9f9f9; }}
.positive {{ color: #4CAF50; font-weight: bold; }}
.negative {{ color: #f44336; font-weight: bold; }}
.footer {{ margin-top: 30px; color: #999; font-size: 12px; }}
</style>
</head>
<body>
<h1>백테스트 성과 리포트</h1>
<p>생성 시각: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}</p>

<h2>성과 지표</h2>
<table>
<tr><th>지표</th><th>값</th></tr>
{metrics_rows}
</table>

<h2>사용 파라미터</h2>
<table>
<tr><th>파라미터</th><th>값</th></tr>
{params_rows}
</table>

<div class="footer">
realtime-trader 백테스트 엔진
</div>
</body>
</html>"""

        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(html)

        logger.info(f"HTML 리포트 생성: {output_path}")
        return output_path

    def print_summary(self, result: BacktestResult) -> None:
        """콘솔 요약 출력.

        Args:
            result: BacktestResult 성과 지표.
        """
        print("\n" + "=" * 50)
        print("       백테스트 성과 요약")
        print("=" * 50)
        print(f"  총 수익률:        {result.total_return:>10.2f}%")
        print(f"  연환산 수익률:    {result.annual_return:>10.2f}%")
        print(f"  최대 낙폭(MDD):   {result.max_drawdown:>10.2f}%")
        print(f"  샤프 비율:        {result.sharpe_ratio:>10.2f}")
        print(f"  소르티노 비율:    {result.sortino_ratio:>10.2f}")
        print(f"  승률:             {result.win_rate:>10.2f}%")
        print(f"  손익비:           {result.profit_factor:>10.2f}")
        print(f"  평균 거래 수익:   {result.avg_trade_return:>10.2f}%")
        print(f"  총 거래 수:       {result.trade_count:>10d}건")
        print(f"  평균 보유 기간:   {result.avg_hold_days:>10.1f}일")
        print("=" * 50)

        if result.params:
            print("\n  사용 파라미터:")
            for k, v in result.params.items():
                print(f"    {k}: {v}")
            print()

    @staticmethod
    def _build_metrics_rows(result: BacktestResult) -> str:
        """성과 지표를 HTML 테이블 행으로 변환."""
        metrics = [
            ("총 수익률", f"{result.total_return:.2f}%", result.total_return),
            ("연환산 수익률", f"{result.annual_return:.2f}%", result.annual_return),
            ("최대 낙폭 (MDD)", f"{result.max_drawdown:.2f}%", result.max_drawdown),
            ("샤프 비율", f"{result.sharpe_ratio:.2f}", result.sharpe_ratio),
            ("소르티노 비율", f"{result.sortino_ratio:.2f}", result.sortino_ratio),
            ("승률", f"{result.win_rate:.2f}%", result.win_rate),
            ("손익비", f"{result.profit_factor:.2f}", result.profit_factor),
            ("평균 거래 수익", f"{result.avg_trade_return:.2f}%", result.avg_trade_return),
            ("총 거래 수", f"{result.trade_count}건", result.trade_count),
            ("평균 보유 기간", f"{result.avg_hold_days:.1f}일", 0),
        ]
        rows = []
        for name, display, value in metrics:
            css_class = ""
            if isinstance(value, (int, float)) and value != 0:
                css_class = ' class="positive"' if value > 0 else ' class="negative"'
            rows.append(f'<tr><td>{name}</td><td{css_class}>{display}</td></tr>')
        return "\n".join(rows)

    @staticmethod
    def _build_params_rows(params: dict | None) -> str:
        """파라미터를 HTML 테이블 행으로 변환."""
        if not params:
            return "<tr><td colspan='2'>기본 파라미터 사용</td></tr>"
        rows = []
        for k, v in params.items():
            rows.append(f"<tr><td>{k}</td><td>{v}</td></tr>")
        return "\n".join(rows)
