"""리스크 관리 모듈.

사전 리스크 체크와 일일 한도 모니터링을 제공.
실거래 자금 보호의 핵심 모듈.
"""

from datetime import date, datetime

from src.datastore import DataStore
from src.models import RiskCheckResult, Signal
from src.utils.market_calendar import is_market_open


class RiskManager:
    """사전 리스크 체크 + 일일 한도 모니터링."""

    def __init__(self, datastore: DataStore, config: dict):
        self._ds = datastore
        self._max_positions = config.get("trading", {}).get("max_positions", 5)
        self._daily_loss_limit = config.get("risk", {}).get("daily_loss_limit", -0.03)
        self._daily_loss_warning = config.get("risk", {}).get(
            "daily_loss_warning", -0.02
        )
        self._max_mdd = config.get("risk", {}).get("max_mdd", -0.20)
        self._reentry_cooldown = config.get("trading", {}).get(
            "reentry_cooldown_days", 3
        )

        self.daily_pnl_pct: float = 0.0
        self.current_mdd: float = 0.0
        self._peak_capital: float = 0.0  # MDD 계산용 자본 최고점
        self._halted: bool = False

    def pre_check(self, signal: Signal) -> RiskCheckResult:
        """주문 전 모든 리스크 조건 체크.

        체크 항목 (순서대로):
            1. halt 상태 체크.
            2. 일일 손실 한도 체크.
            3. 최대 동시 보유 종목 수.
            4. 동일 종목 재진입 쿨다운.
            5. 장 시간 체크.
            6. MDD 체크.

        Args:
            signal: 매매 신호.

        Returns:
            RiskCheckResult(approved=True/False, reason="...").
        """
        # 1. halt 상태
        if self._halted:
            return RiskCheckResult(approved=False, reason="매매 중단 상태")

        # 2. 일일 손실 한도
        if self.daily_pnl_pct <= self._daily_loss_limit:
            return RiskCheckResult(approved=False, reason="일일 손실 한도 초과")

        # 3. 최대 동시 보유 종목 수
        open_count = self._ds.count_open_positions()
        if open_count >= self._max_positions:
            return RiskCheckResult(approved=False, reason="최대 보유 종목 수 초과")

        # 4. 동일 종목 재진입 쿨다운
        last_trade = self._ds.get_last_trade(signal.code)
        if last_trade and last_trade.get("executed_at"):
            last_date_str = last_trade["executed_at"][:10]  # "YYYY-MM-DD"
            try:
                last_date = datetime.strptime(last_date_str, "%Y-%m-%d").date()
                days_since = (date.today() - last_date).days
                if days_since < self._reentry_cooldown:
                    return RiskCheckResult(approved=False, reason="재진입 쿨다운 중")
            except ValueError:
                # 날짜 파싱 실패 — 안전하게 쿨다운 적용 (매수 차단)
                return RiskCheckResult(
                    approved=False, reason="재진입 쿨다운 체크 실패 (날짜 파싱 오류)"
                )

        # 5. 장 시간 체크
        if not is_market_open():
            return RiskCheckResult(approved=False, reason="장 시간 외")

        # 6. MDD 체크
        if self.current_mdd <= self._max_mdd:
            return RiskCheckResult(approved=False, reason="최대 낙폭 초과")

        return RiskCheckResult(approved=True)

    def halt(self) -> None:
        """매매 중단."""
        self._halted = True

    def resume(self) -> None:
        """매매 재개."""
        self._halted = False

    @property
    def is_halted(self) -> bool:
        """매매 중단 상태 조회."""
        return self._halted

    def update_daily_pnl(self, pnl_pct: float) -> None:
        """일일 손익률 업데이트."""
        self.daily_pnl_pct = pnl_pct

    def update_mdd(self, current_capital: float) -> None:
        """MDD 업데이트 — 현재 자본 기준으로 최대 낙폭 계산.

        Args:
            current_capital: 현재 총 자본 (원).
        """
        if current_capital > self._peak_capital:
            self._peak_capital = current_capital
        if self._peak_capital > 0:
            drawdown = (current_capital - self._peak_capital) / self._peak_capital
            if drawdown < self.current_mdd:
                self.current_mdd = drawdown

    def set_initial_capital(self, capital: float) -> None:
        """초기 자본 설정 — MDD 계산 기준점."""
        if self._peak_capital <= 0:
            self._peak_capital = capital

    def reset_daily(self) -> None:
        """일일 상태 초기화 (장 시작 시)."""
        self.daily_pnl_pct = 0.0
        self._halted = False
