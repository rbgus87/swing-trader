"""포지션 사이징 모듈.

하프켈리(Half-Kelly) 기반 포지션 사이징을 제공.
금액은 항상 int(원), 비율은 float(0.0~1.0).
"""

from src.utils.config import config

# 상수 (config.yaml에서 로드 가능)
MAX_POSITION_RATIO = 0.15
MIN_POSITION_RATIO = 0.03
FIXED_RATIO = 0.05


class PositionSizer:
    """하프켈리 기반 포지션 사이징."""

    def __init__(self, max_ratio: float = None, min_ratio: float = None):
        self.max_ratio = max_ratio or config.get(
            "risk.max_position_ratio", MAX_POSITION_RATIO
        )
        self.min_ratio = min_ratio or config.get(
            "risk.min_position_ratio", MIN_POSITION_RATIO
        )

    def calculate(
        self,
        capital: int,
        win_rate: float,
        avg_win: float,
        avg_loss: float,
        method: str = "half_kelly",
    ) -> int:
        """투자 금액 계산.

        Args:
            capital: 총 자본금 (int, 원).
            win_rate: 승률 (0.0~1.0).
            avg_win: 평균 수익률 (양수 float).
            avg_loss: 평균 손실률 (양수 float).
            method: "half_kelly" | "quarter_kelly" | "full_kelly" | "fixed".

        Returns:
            투자 금액 (int, 원).
        """
        if capital <= 0:
            return 0

        # avg_loss == 0이면 최소 비율 적용
        if avg_loss == 0:
            return int(self.min_ratio * capital)

        # 손익비
        b = avg_win / avg_loss

        # Kelly criterion
        kelly = (win_rate * b - (1 - win_rate)) / b

        # 기대값 음수 또는 0이면 투자하지 않음
        if kelly <= 0:
            return 0

        # method에 따라 ratio 결정
        if method == "full_kelly":
            ratio = kelly
        elif method == "half_kelly":
            ratio = kelly * 0.5
        elif method == "quarter_kelly":
            ratio = kelly * 0.25
        elif method == "fixed":
            ratio = FIXED_RATIO
        else:
            ratio = kelly * 0.5  # 기본값은 half_kelly

        # [min_ratio, max_ratio]로 clamp
        ratio = max(self.min_ratio, min(self.max_ratio, ratio))

        return int(capital * ratio)
