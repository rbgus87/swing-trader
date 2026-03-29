"""손절가 및 트레일링스탑 관리 모듈.

ATR 기반 초기 손절가 설정과 트레일링스탑 업데이트를 제공.
금액은 항상 int(원).
"""

from src.models import Position


class StopManager:
    """손절가 및 트레일링스탑 관리."""

    def __init__(
        self,
        stop_atr_mult: float = 1.5,
        max_stop_pct: float = 0.07,
        trailing_atr_mult: float = 2.0,
        trailing_activate_pct: float = 0.10,
    ):
        self.stop_atr_mult = stop_atr_mult
        self.max_stop_pct = max_stop_pct
        self.trailing_atr_mult = trailing_atr_mult
        self.trailing_activate_pct = trailing_activate_pct

    def get_initial_stop(self, entry_price: int, atr: float) -> int:
        """초기 손절가 계산.

        둘 중 높은 값(더 타이트한 손절)을 선택:
        - 진입가 - ATR * stop_atr_mult
        - 진입가 * (1 - max_stop_pct)

        Args:
            entry_price: 진입가 (int, 원).
            atr: ATR 값 (float).

        Returns:
            초기 손절가 (int, 원).
        """
        atr_stop = entry_price - atr * self.stop_atr_mult
        pct_stop = entry_price * (1 - self.max_stop_pct)
        return int(max(atr_stop, pct_stop))

    def update_trailing_stop(
        self, position: Position, current_price: int, atr: float
    ) -> int:
        """트레일링스탑 업데이트.

        Rules:
            1. high_since_entry 갱신 (current_price와 비교).
            2. trailing = high_since_entry - ATR * trailing_atr_mult.
            3. trailing은 기존 stop_price보다 낮아지면 안 됨 (후퇴 금지).
            4. 활성화 조건: 미실현 수익 >= trailing_activate_pct 이상이어야 함.
               미충족 시 기존 stop_price 유지.

        Args:
            position: 현재 포지션.
            current_price: 현재가 (int, 원).
            atr: ATR 값 (float).

        Returns:
            새 트레일링스탑 가격 (int, 원).
        """
        # 1. high_since_entry 갱신
        if current_price > position.high_since_entry:
            position.high_since_entry = current_price

        # 4. 활성화 조건 체크: 미실현 수익률
        unrealized_pct = (current_price - position.entry_price) / position.entry_price
        if unrealized_pct < self.trailing_activate_pct:
            return position.stop_price

        # 2. 트레일링스탑 계산
        trailing = int(position.high_since_entry - atr * self.trailing_atr_mult)

        # 3. 후퇴 금지: 기존 stop_price보다 낮으면 기존 유지
        if trailing <= position.stop_price:
            return position.stop_price

        return trailing

    def is_stopped(self, position: Position, current_price: int) -> bool:
        """현재가가 손절가 이하인지 체크.

        Args:
            position: 현재 포지션.
            current_price: 현재가 (int, 원).

        Returns:
            손절 여부.
        """
        return current_price <= position.stop_price
