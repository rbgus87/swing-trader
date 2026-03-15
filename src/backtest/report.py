"""백테스트 성과 HTML 리포트 생성."""

import base64
import os
from datetime import datetime
from io import BytesIO

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import matplotlib.dates as mdates  # noqa: E402
import pandas as pd  # noqa: E402
from loguru import logger  # noqa: E402

from src.backtest.engine import BacktestResult  # noqa: E402

# 한글 폰트 설정
plt.rcParams["font.family"] = "Malgun Gothic"
plt.rcParams["axes.unicode_minus"] = False


class BacktestReporter:
    """백테스트 결과를 HTML 및 콘솔 리포트로 출력."""

    def generate_html(
        self,
        result: BacktestResult,
        output_path: str = "reports/backtest_report.html",
        equity: pd.Series | None = None,
        trades: list[dict] | None = None,
    ) -> str:
        """성과 지표 + 차트를 포함한 HTML 리포트 생성.

        Args:
            result: BacktestResult 성과 지표.
            output_path: HTML 파일 저장 경로.
            equity: 자산 곡선 시리즈 (선택).
            trades: 개별 거래 딕셔너리 리스트 (선택).

        Returns:
            생성된 HTML 파일 경로.
        """
        metrics_rows = self._build_metrics_rows(result)
        params_rows = self._build_params_rows(result.params)

        # 차트 섹션 생성
        charts_html = ""
        if equity is not None and len(equity) > 0:
            equity_b64 = self._plot_equity_curve(equity)
            charts_html += f"""
<h2>자산 곡선 (Equity Curve)</h2>
<img src="data:image/png;base64,{equity_b64}" style="max-width:100%; height:auto;">
"""
            drawdown_b64 = self._plot_drawdown(equity)
            charts_html += f"""
<h2>낙폭 (Drawdown)</h2>
<img src="data:image/png;base64,{drawdown_b64}" style="max-width:100%; height:auto;">
"""

        # 거래 내역 테이블
        trade_table_html = ""
        if trades:
            trade_table_html = f"""
<h2>거래 내역</h2>
{self._build_trade_table(trades)}
"""

        html = f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<title>백테스트 리포트</title>
<style>
body {{ font-family: 'Malgun Gothic', sans-serif; margin: 40px; background: #f5f5f5; }}
h1 {{ color: #333; border-bottom: 2px solid #2196F3; padding-bottom: 10px; }}
h2 {{ color: #555; margin-top: 30px; }}
table {{ border-collapse: collapse; width: 100%; max-width: 900px; margin: 10px 0; }}
th, td {{ border: 1px solid #ddd; padding: 10px 14px; text-align: left; }}
th {{ background: #2196F3; color: white; }}
tr:nth-child(even) {{ background: #f9f9f9; }}
.positive {{ color: #4CAF50; font-weight: bold; }}
.negative {{ color: #f44336; font-weight: bold; }}
.footer {{ margin-top: 30px; color: #999; font-size: 12px; }}
img {{ border: 1px solid #ddd; border-radius: 4px; margin: 10px 0; }}
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

{charts_html}

{trade_table_html}

<div class="footer">
realtime-trader 백테스트 엔진
</div>
</body>
</html>"""

        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
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
    def _plot_equity_curve(equity: pd.Series) -> str:
        """자산 곡선 차트를 base64 PNG 문자열로 반환.

        Args:
            equity: 자산 곡선 시리즈.

        Returns:
            base64 인코딩된 PNG 이미지 문자열.
        """
        fig, ax = plt.subplots(figsize=(12, 5))
        ax.plot(equity.index, equity.values, color="#2196F3", linewidth=1.5)
        initial_capital = equity.iloc[0]
        ax.axhline(
            y=initial_capital,
            color="gray",
            linestyle="--",
            linewidth=1,
            label=f"초기 자본: {initial_capital:,.0f}원",
        )
        ax.set_title("자산 곡선 (Equity Curve)", fontsize=14)
        ax.set_ylabel("자산 (원)")
        ax.set_xlabel("날짜")
        ax.legend(loc="upper left")
        ax.grid(True, alpha=0.3)
        ax.yaxis.set_major_formatter(
            plt.FuncFormatter(lambda x, _: f"{x:,.0f}")
        )

        # X축 날짜 포맷 (날짜 인덱스인 경우)
        if hasattr(equity.index, "to_pydatetime"):
            fig.autofmt_xdate()

        fig.tight_layout()

        buf = BytesIO()
        fig.savefig(buf, format="png", dpi=100, bbox_inches="tight")
        plt.close(fig)
        buf.seek(0)
        return base64.b64encode(buf.read()).decode("utf-8")

    @staticmethod
    def _plot_drawdown(equity: pd.Series) -> str:
        """낙폭 차트를 base64 PNG 문자열로 반환.

        Args:
            equity: 자산 곡선 시리즈.

        Returns:
            base64 인코딩된 PNG 이미지 문자열.
        """
        drawdown = (equity - equity.cummax()) / equity.cummax() * 100

        fig, ax = plt.subplots(figsize=(12, 4))
        ax.fill_between(
            drawdown.index, drawdown.values, 0, color="#f44336", alpha=0.4
        )
        ax.plot(drawdown.index, drawdown.values, color="#f44336", linewidth=1)
        ax.set_title("낙폭 (Drawdown)", fontsize=14)
        ax.set_ylabel("낙폭 (%)")
        ax.set_xlabel("날짜")
        ax.grid(True, alpha=0.3)

        if hasattr(equity.index, "to_pydatetime"):
            fig.autofmt_xdate()

        fig.tight_layout()

        buf = BytesIO()
        fig.savefig(buf, format="png", dpi=100, bbox_inches="tight")
        plt.close(fig)
        buf.seek(0)
        return base64.b64encode(buf.read()).decode("utf-8")

    @staticmethod
    def _build_trade_table(trades: list[dict]) -> str:
        """거래 내역을 HTML 테이블로 변환.

        Args:
            trades: 거래 딕셔너리 리스트.

        Returns:
            HTML 테이블 문자열.
        """
        rows = []
        for idx, t in enumerate(trades, 1):
            ret = t.get("return", 0)
            ret_pct = ret * 100
            css_class = "positive" if ret > 0 else ("negative" if ret < 0 else "")
            entry_date = t.get("entry_date", str(t.get("entry_idx", "")))
            exit_date = t.get("exit_date", str(t.get("exit_idx", "")))
            entry_price = t.get("entry_price", 0)
            exit_price = t.get("exit_price", 0)
            shares = t.get("shares", 0)
            hold_days = t.get("hold_days", 0)

            rows.append(
                f'<tr><td>{idx}</td>'
                f"<td>{entry_date}</td>"
                f"<td>{exit_date}</td>"
                f"<td>{entry_price:,}</td>"
                f"<td>{exit_price:,}</td>"
                f"<td>{shares:,}</td>"
                f'<td class="{css_class}">{ret_pct:+.2f}%</td>'
                f"<td>{hold_days}일</td></tr>"
            )

        return (
            '<table>\n'
            "<tr><th>#</th><th>매수일</th><th>매도일</th>"
            "<th>매수가</th><th>매도가</th><th>수량</th>"
            "<th>수익률</th><th>보유일</th></tr>\n"
            + "\n".join(rows)
            + "\n</table>"
        )

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
