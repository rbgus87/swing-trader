"""매매 비용 모델 — 시장별 거래세 지원.

2026년 현행 기준:
  KOSPI: 거래세 0.05% + 농특세 0.15% = 0.20%
  KOSDAQ: 거래세 0.20%
  수수료: 0.015% (편도)
  슬리피지: 0.05% 고정 또는 동적 (slippage_model 참조)
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CostModel:
    """매매 비용 모델."""

    buy_commission: float = 0.00015     # 매수 수수료 0.015%
    sell_commission: float = 0.00015    # 매도 수수료 0.015%
    sell_tax_kospi: float = 0.0020      # KOSPI 거래세+농특세 0.20%
    sell_tax_kosdaq: float = 0.0020     # KOSDAQ 거래세 0.20%
    slippage: float = 0.0005            # 슬리피지 0.05%

    def sell_tax(self, market: str = "KOSPI") -> float:
        """시장별 거래세 반환."""
        if market.upper() in ("KOSDAQ", "KONEX"):
            return self.sell_tax_kosdaq
        return self.sell_tax_kospi

    def total_cost_pct(self, market: str = "KOSPI") -> float:
        """왕복 총 비용 비율 (고정 슬리피지)."""
        return (
            self.buy_commission
            + self.sell_commission
            + self.sell_tax(market)
            + self.slippage
        )

    def total_cost_pct_dynamic(
        self,
        market: str = "KOSPI",
        order_value: float = 0,
        avg_trading_value: float = 0,
        slippage_params=None,  # SlippageParams | None
    ) -> float:
        """왕복 총 비용 비율 (동적 슬리피지).

        slippage_params가 None이거나 disabled면 고정 slippage 사용.
        """
        from src.utils.slippage_model import compute_slippage
        slip = compute_slippage(order_value, avg_trading_value, slippage_params)
        return (
            self.buy_commission
            + self.sell_commission
            + self.sell_tax(market)
            + slip
        )

    @classmethod
    def from_config(cls, config_dict: dict) -> "CostModel":
        """config.data 딕셔너리에서 CostModel 생성.

        trend_following 섹션을 우선 참조하고, 없으면 backtest 섹션 폴백.
        """
        tf = config_dict.get("trend_following", {})
        bt = config_dict.get("backtest", {})
        return cls(
            buy_commission=float(
                tf.get("buy_commission", bt.get("commission", 0.00015))
            ),
            sell_commission=float(
                tf.get("sell_commission", bt.get("commission", 0.00015))
            ),
            sell_tax_kospi=float(
                tf.get("sell_tax_kospi", bt.get("tax_kospi", 0.0020))
            ),
            sell_tax_kosdaq=float(
                tf.get("sell_tax_kosdaq", bt.get("tax_kosdaq", 0.0020))
            ),
            slippage=float(
                tf.get("slippage", bt.get("slippage", 0.0005))
            ),
        )
