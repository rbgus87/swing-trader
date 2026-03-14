"""리스크 관리 레이어 테스트.

PositionSizer, StopManager, RiskManager의 경계값 테스트.
"""

from datetime import date, datetime
from unittest.mock import MagicMock, patch

import pytest

from src.models import Position, RiskCheckResult, Signal
from src.risk.position_sizer import PositionSizer
from src.risk.stop_manager import StopManager
from src.risk.risk_manager import RiskManager


# ── PositionSizer 테스트 ──────────────────────────────────────


class TestPositionSizer:
    """PositionSizer 테스트."""

    def setup_method(self):
        self.sizer = PositionSizer(max_ratio=0.15, min_ratio=0.03)

    def test_half_kelly_positive(self):
        """kelly 양수 → 정상 사이징."""
        # win_rate=0.6, avg_win=0.08, avg_loss=0.04 → b=2.0
        # kelly = (0.6*2 - 0.4)/2 = 0.4
        # half_kelly = 0.2 → clamp to [0.03, 0.15] → 0.15
        result = self.sizer.calculate(
            capital=10_000_000, win_rate=0.6, avg_win=0.08, avg_loss=0.04
        )
        assert result == int(10_000_000 * 0.15)

    def test_kelly_negative_returns_zero(self):
        """kelly 음수 (기대값 음수) → 0 반환."""
        # win_rate=0.3, avg_win=0.05, avg_loss=0.10 → b=0.5
        # kelly = (0.3*0.5 - 0.7)/0.5 = (0.15-0.7)/0.5 = -1.1
        result = self.sizer.calculate(
            capital=10_000_000, win_rate=0.3, avg_win=0.05, avg_loss=0.10
        )
        assert result == 0

    def test_kelly_zero_returns_zero(self):
        """kelly == 0 → 0 반환."""
        # win_rate=0.5, avg_win=0.05, avg_loss=0.05 → b=1.0
        # kelly = (0.5*1 - 0.5)/1 = 0.0
        result = self.sizer.calculate(
            capital=10_000_000, win_rate=0.5, avg_win=0.05, avg_loss=0.05
        )
        assert result == 0

    def test_avg_loss_zero_returns_min_ratio(self):
        """avg_loss == 0 → min_ratio * capital 반환."""
        result = self.sizer.calculate(
            capital=10_000_000, win_rate=0.6, avg_win=0.08, avg_loss=0.0
        )
        assert result == int(0.03 * 10_000_000)

    def test_ratio_clamped_to_max(self):
        """ratio가 max_ratio 초과 시 clamp."""
        # win_rate=0.8, avg_win=0.10, avg_loss=0.02 → b=5.0
        # kelly = (0.8*5 - 0.2)/5 = 3.8/5 = 0.76
        # half_kelly = 0.38 → clamp to 0.15
        result = self.sizer.calculate(
            capital=10_000_000, win_rate=0.8, avg_win=0.10, avg_loss=0.02
        )
        assert result == int(10_000_000 * 0.15)

    def test_ratio_clamped_to_min(self):
        """ratio가 min_ratio 미만 시 clamp."""
        # win_rate=0.52, avg_win=0.05, avg_loss=0.05 → b=1.0
        # kelly = (0.52 - 0.48)/1 = 0.04
        # half_kelly = 0.02 → clamp to 0.03
        result = self.sizer.calculate(
            capital=10_000_000, win_rate=0.52, avg_win=0.05, avg_loss=0.05
        )
        assert result == int(10_000_000 * 0.03)

    def test_full_kelly(self):
        """full_kelly method."""
        # win_rate=0.55, avg_win=0.06, avg_loss=0.04 → b=1.5
        # kelly = (0.55*1.5 - 0.45)/1.5 = (0.825-0.45)/1.5 = 0.25
        # full_kelly = 0.25 → clamp to 0.15
        result = self.sizer.calculate(
            capital=10_000_000,
            win_rate=0.55,
            avg_win=0.06,
            avg_loss=0.04,
            method="full_kelly",
        )
        assert result == int(10_000_000 * 0.15)

    def test_quarter_kelly(self):
        """quarter_kelly method."""
        # win_rate=0.55, avg_win=0.06, avg_loss=0.04 → b=1.5
        # kelly = 0.25
        # quarter_kelly = 0.0625 → within [0.03, 0.15]
        result = self.sizer.calculate(
            capital=10_000_000,
            win_rate=0.55,
            avg_win=0.06,
            avg_loss=0.04,
            method="quarter_kelly",
        )
        assert result == int(10_000_000 * 0.0625)

    def test_fixed_method(self):
        """fixed method → FIXED_RATIO 사용."""
        result = self.sizer.calculate(
            capital=10_000_000,
            win_rate=0.6,
            avg_win=0.08,
            avg_loss=0.04,
            method="fixed",
        )
        assert result == int(10_000_000 * 0.05)

    def test_capital_zero(self):
        """capital=0 → 0 반환."""
        result = self.sizer.calculate(
            capital=0, win_rate=0.6, avg_win=0.08, avg_loss=0.04
        )
        assert result == 0

    def test_half_kelly_vs_full_kelly(self):
        """half_kelly는 full_kelly 이하."""
        # kelly가 [min_ratio, max_ratio] 범위 내인 케이스
        # win_rate=0.55, avg_win=0.06, avg_loss=0.04 → kelly=0.25
        # full=0.25 → clamp 0.15, half=0.125 → within range
        full = self.sizer.calculate(
            capital=10_000_000,
            win_rate=0.55,
            avg_win=0.06,
            avg_loss=0.04,
            method="full_kelly",
        )
        half = self.sizer.calculate(
            capital=10_000_000,
            win_rate=0.55,
            avg_win=0.06,
            avg_loss=0.04,
            method="half_kelly",
        )
        assert half <= full


# ── StopManager 테스트 ────────────────────────────────────────


class TestStopManager:
    """StopManager 테스트."""

    def setup_method(self):
        self.sm = StopManager(
            stop_atr_mult=1.5,
            max_stop_pct=0.07,
            trailing_atr_mult=2.0,
            trailing_activate_pct=0.03,
        )

    def _make_position(self, **kwargs) -> Position:
        defaults = dict(
            id=1,
            code="005930",
            name="삼성전자",
            entry_date="2026-03-10",
            entry_price=70000,
            quantity=10,
            stop_price=65000,
            target_price=80000,
            status="open",
            high_since_entry=70000,
        )
        defaults.update(kwargs)
        return Position(**defaults)

    # ── get_initial_stop ──

    def test_initial_stop_atr_higher(self):
        """ATR 기반 손절가가 더 높을 때 ATR 기반 선택."""
        # entry=70000, atr=2000 → atr_stop=70000-3000=67000
        # pct_stop=70000*0.93=65100
        # max(67000, 65100) = 67000
        result = self.sm.get_initial_stop(entry_price=70000, atr=2000.0)
        assert result == 67000

    def test_initial_stop_pct_higher(self):
        """최대손절% 기반 손절가가 더 높을 때 최대손절% 선택."""
        # entry=70000, atr=5000 → atr_stop=70000-7500=62500
        # pct_stop=int(70000*0.93)=65099
        # max(62500, 65099) = 65099
        result = self.sm.get_initial_stop(entry_price=70000, atr=5000.0)
        assert result == int(70000 * (1 - 0.07))

    # ── update_trailing_stop ──

    def test_trailing_not_activated(self):
        """활성화 조건 미충족 시 기존 stop 유지."""
        pos = self._make_position(entry_price=70000, stop_price=65000)
        # current_price=71000 → 수익률=(71000-70000)/70000=0.0143 < 0.03
        result = self.sm.update_trailing_stop(pos, current_price=71000, atr=1000.0)
        assert result == 65000

    def test_trailing_no_retreat(self):
        """후퇴 금지: 새 trailing < 기존 stop → 기존 유지."""
        pos = self._make_position(
            entry_price=70000, stop_price=73000, high_since_entry=76000
        )
        # current_price=73500 → 수익률=5% >= 3%
        # high_since_entry stays 76000
        # trailing=76000-2000*2=72000 < 73000 → 기존 유지
        result = self.sm.update_trailing_stop(pos, current_price=73500, atr=2000.0)
        assert result == 73000

    def test_trailing_activated_and_updated(self):
        """활성화 후 정상 트레일링."""
        pos = self._make_position(
            entry_price=70000, stop_price=65000, high_since_entry=70000
        )
        # current_price=75000 → 수익률=7.14% >= 3%
        # high_since_entry 갱신 → 75000
        # trailing=75000-1000*2=73000 > 65000
        result = self.sm.update_trailing_stop(pos, current_price=75000, atr=1000.0)
        assert result == 73000
        assert pos.high_since_entry == 75000

    def test_trailing_high_since_entry_updated(self):
        """high_since_entry가 현재가에 따라 갱신됨."""
        pos = self._make_position(
            entry_price=70000, stop_price=65000, high_since_entry=74000
        )
        # current_price=76000 → high 갱신
        self.sm.update_trailing_stop(pos, current_price=76000, atr=1000.0)
        assert pos.high_since_entry == 76000

    def test_trailing_high_since_entry_not_lowered(self):
        """현재가가 기존 high보다 낮으면 high 유지."""
        pos = self._make_position(
            entry_price=70000, stop_price=65000, high_since_entry=78000
        )
        # current_price=75000 → high stays 78000
        self.sm.update_trailing_stop(pos, current_price=75000, atr=1000.0)
        assert pos.high_since_entry == 78000

    # ── is_stopped ──

    def test_is_stopped_exact_boundary(self):
        """stop 가격 정확히 일치 시 True."""
        pos = self._make_position(stop_price=65000)
        assert self.sm.is_stopped(pos, current_price=65000) is True

    def test_is_stopped_below(self):
        """현재가 < stop → True."""
        pos = self._make_position(stop_price=65000)
        assert self.sm.is_stopped(pos, current_price=64000) is True

    def test_is_stopped_above(self):
        """현재가 > stop → False."""
        pos = self._make_position(stop_price=65000)
        assert self.sm.is_stopped(pos, current_price=66000) is False


# ── RiskManager 테스트 ────────────────────────────────────────


class TestRiskManager:
    """RiskManager 테스트."""

    def _make_signal(self, code="005930") -> Signal:
        return Signal(
            code=code,
            name="삼성전자",
            signal_type="buy",
            price=70000,
            score=0.8,
        )

    def _make_risk_manager(self, tmp_db, sample_config) -> RiskManager:
        return RiskManager(datastore=tmp_db, config=sample_config)

    @patch("src.risk.risk_manager.is_market_open", return_value=True)
    def test_pre_check_all_pass(self, mock_market, tmp_db, sample_config):
        """모든 조건 통과 시 approved=True."""
        rm = self._make_risk_manager(tmp_db, sample_config)
        result = rm.pre_check(self._make_signal())
        assert result.approved is True

    def test_pre_check_halted(self, tmp_db, sample_config):
        """halt 상태 → rejected."""
        rm = self._make_risk_manager(tmp_db, sample_config)
        rm.halt()
        result = rm.pre_check(self._make_signal())
        assert result.approved is False
        assert "매매 중단" in result.reason

    @patch("src.risk.risk_manager.is_market_open", return_value=True)
    def test_pre_check_daily_loss_exceeded(self, mock_market, tmp_db, sample_config):
        """일일 한도 초과 → rejected."""
        rm = self._make_risk_manager(tmp_db, sample_config)
        rm.update_daily_pnl(-0.03)  # exactly at limit
        result = rm.pre_check(self._make_signal())
        assert result.approved is False
        assert "일일 손실 한도" in result.reason

    @patch("src.risk.risk_manager.is_market_open", return_value=True)
    def test_pre_check_daily_loss_within_limit(self, mock_market, tmp_db, sample_config):
        """일일 손실이 한도 미만이면 통과."""
        rm = self._make_risk_manager(tmp_db, sample_config)
        rm.update_daily_pnl(-0.02)  # above limit
        result = rm.pre_check(self._make_signal())
        assert result.approved is True

    @patch("src.risk.risk_manager.is_market_open", return_value=True)
    def test_pre_check_max_positions(self, mock_market, tmp_db, sample_config):
        """최대 종목 수 초과 → rejected."""
        rm = self._make_risk_manager(tmp_db, sample_config)
        # 5개 포지션 삽입
        for i in range(5):
            pos = Position(
                id=0,
                code=f"00{i:04d}",
                name=f"종목{i}",
                entry_date="2026-03-10",
                entry_price=10000,
                quantity=10,
                stop_price=9000,
                target_price=12000,
                status="open",
            )
            tmp_db.insert_position(pos)
        result = rm.pre_check(self._make_signal())
        assert result.approved is False
        assert "최대 보유 종목 수" in result.reason

    @patch("src.risk.risk_manager.is_market_open", return_value=True)
    @patch("src.risk.risk_manager.date")
    def test_pre_check_reentry_cooldown(
        self, mock_date, mock_market, tmp_db, sample_config
    ):
        """쿨다운 중 → rejected."""
        mock_date.today.return_value = date(2026, 3, 15)
        mock_date.side_effect = lambda *a, **kw: date(*a, **kw)

        rm = self._make_risk_manager(tmp_db, sample_config)
        # 어제 매매 기록 삽입
        from src.models import TradeRecord

        trade = TradeRecord(
            code="005930",
            name="삼성전자",
            side="sell",
            price=70000,
            quantity=10,
            amount=700000,
            fee=105.0,
            tax=1400.0,
            pnl=-5000.0,
            pnl_pct=-0.007,
            reason="stop_loss",
            executed_at="2026-03-14 10:00:00",
        )
        tmp_db.record_trade(trade)
        result = rm.pre_check(self._make_signal())
        assert result.approved is False
        assert "쿨다운" in result.reason

    @patch("src.risk.risk_manager.is_market_open", return_value=False)
    def test_pre_check_market_closed(self, mock_market, tmp_db, sample_config):
        """장 시간 외 → rejected."""
        rm = self._make_risk_manager(tmp_db, sample_config)
        result = rm.pre_check(self._make_signal())
        assert result.approved is False
        assert "장 시간 외" in result.reason

    @patch("src.risk.risk_manager.is_market_open", return_value=True)
    def test_pre_check_mdd_exceeded(self, mock_market, tmp_db, sample_config):
        """MDD 초과 → rejected."""
        rm = self._make_risk_manager(tmp_db, sample_config)
        rm.update_mdd(-0.20)  # exactly at limit
        result = rm.pre_check(self._make_signal())
        assert result.approved is False
        assert "최대 낙폭" in result.reason

    def test_halt_resume_toggle(self, tmp_db, sample_config):
        """halt/resume 토글."""
        rm = self._make_risk_manager(tmp_db, sample_config)
        assert rm.is_halted is False
        rm.halt()
        assert rm.is_halted is True
        rm.resume()
        assert rm.is_halted is False

    def test_reset_daily(self, tmp_db, sample_config):
        """reset_daily 동작."""
        rm = self._make_risk_manager(tmp_db, sample_config)
        rm.update_daily_pnl(-0.05)
        rm.halt()
        rm.reset_daily()
        assert rm.daily_pnl_pct == 0.0
        assert rm.is_halted is False

    @patch("src.risk.risk_manager.is_market_open", return_value=True)
    def test_pre_check_mdd_within_limit(self, mock_market, tmp_db, sample_config):
        """MDD가 한도 미만이면 통과."""
        rm = self._make_risk_manager(tmp_db, sample_config)
        rm.update_mdd(-0.10)
        result = rm.pre_check(self._make_signal())
        assert result.approved is True

    def test_update_daily_pnl(self, tmp_db, sample_config):
        """update_daily_pnl 값 저장 확인."""
        rm = self._make_risk_manager(tmp_db, sample_config)
        rm.update_daily_pnl(-0.015)
        assert rm.daily_pnl_pct == -0.015

    def test_update_mdd(self, tmp_db, sample_config):
        """update_mdd 값 저장 확인."""
        rm = self._make_risk_manager(tmp_db, sample_config)
        rm.update_mdd(-0.12)
        assert rm.current_mdd == -0.12
