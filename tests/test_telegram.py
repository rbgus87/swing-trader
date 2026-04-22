"""텔레그램 봇 알림 모듈 테스트.

unittest.mock을 사용하여 requests.post를 모킹한다.
"""

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from src.notification.telegram_bot import TelegramBot


@pytest.fixture
def bot():
    """테스트용 TelegramBot 인스턴스."""
    return TelegramBot(token="TEST_TOKEN", chat_id="TEST_CHAT_ID")


@pytest.fixture
def mock_post():
    """requests.post 모킹. time.sleep도 같이 무력화해 재시도 지연을 제거한다."""
    with patch("src.notification.telegram_bot.requests.post") as mock, \
            patch("src.notification.telegram_bot.time.sleep"):
        mock.return_value = MagicMock(status_code=200)
        yield mock


# ── send() 테스트 ──


class TestSend:
    """send() 메서드 테스트."""

    def test_send_success(self, bot, mock_post):
        """전송 성공 시 True 반환."""
        result = bot.send("테스트 메시지")

        assert result is True
        mock_post.assert_called_once_with(
            "https://api.telegram.org/botTEST_TOKEN/sendMessage",
            json={
                "chat_id": "TEST_CHAT_ID",
                "text": "테스트 메시지",
                "parse_mode": "HTML",
            },
            timeout=30,
        )

    def test_send_failure_status(self, bot, mock_post):
        """비-200 상태코드 시 False 반환."""
        mock_post.return_value = MagicMock(status_code=400)

        result = bot.send("테스트 메시지")

        assert result is False

    def test_send_exception(self, bot, mock_post):
        """requests 예외 시 False 반환."""
        mock_post.side_effect = ConnectionError("연결 실패")

        result = bot.send("테스트 메시지")

        assert result is False

    def test_send_custom_parse_mode(self, bot, mock_post):
        """커스텀 parse_mode 전달."""
        bot.send("테스트", parse_mode="Markdown")

        call_kwargs = mock_post.call_args[1]
        assert call_kwargs["json"]["parse_mode"] == "Markdown"


# ── send_with_cooldown() 테스트 ──


class TestSendWithCooldown:
    """send_with_cooldown() 메서드 테스트."""

    def test_first_send_succeeds(self, bot, mock_post):
        """첫 전송은 항상 성공."""
        result = bot.send_with_cooldown("key1", "메시지", 60)

        assert result is True
        mock_post.assert_called_once()

    def test_cooldown_blocks_duplicate(self, bot, mock_post):
        """쿨다운 시간 내 중복 전송 차단."""
        bot.send_with_cooldown("key1", "메시지1", 60)
        result = bot.send_with_cooldown("key1", "메시지2", 60)

        assert result is False
        assert mock_post.call_count == 1  # 첫 번째만 전송됨

    def test_cooldown_expires(self, bot, mock_post):
        """쿨다운 만료 후 재전송 허용."""
        bot.send_with_cooldown("key1", "메시지1", 60)

        # 쿨다운 시간을 과거로 조작
        bot._cooldowns["key1"] = datetime.now() - timedelta(seconds=61)

        result = bot.send_with_cooldown("key1", "메시지2", 60)

        assert result is True
        assert mock_post.call_count == 2

    def test_different_keys_independent(self, bot, mock_post):
        """다른 키는 독립적으로 동작."""
        bot.send_with_cooldown("key1", "메시지1", 60)
        result = bot.send_with_cooldown("key2", "메시지2", 60)

        assert result is True
        assert mock_post.call_count == 2


# ── 메시지 템플릿 테스트 ──


class TestSignalAlert:
    """send_signal_alert() 메시지 형식 테스트."""

    def test_signal_alert_format(self, bot, mock_post):
        """매수 신호 메시지 형식 검증."""
        bot.send_signal_alert(
            code="005930",
            name="삼성전자",
            price=74500,
            score=4.0,
            rsi=52.3,
            macd_hist=0.82,
            volume_ratio=1.8,
            ma_diff_pct=2.1,
            target_price=80460,
            stop_price=72200,
        )

        msg = mock_post.call_args[1]["json"]["text"]
        assert "📊 <b>매수 신호</b>" in msg
        assert "삼성전자 (005930)" in msg
        assert "74,500원" in msg
        assert "★★★★☆" in msg
        assert "RSI: 52.3" in msg
        assert "MACD 히스토그램: +0.82" in msg
        assert "1.8배" in msg
        assert "+2.1%" in msg
        assert "80,460원" in msg
        assert "72,200원" in msg
        assert "손익비:" in msg

    def test_signal_alert_cooldown(self, bot, mock_post):
        """동일 종목 매수 신호는 1시간 쿨다운."""
        bot.send_signal_alert(
            "005930", "삼성전자", 74500, 4.0, 52.3, 0.82, 1.8, 2.1, 80460, 72200
        )
        result = bot.send_signal_alert(
            "005930", "삼성전자", 74600, 4.0, 53.0, 0.90, 1.9, 2.2, 80500, 72300
        )

        assert result is False  # 쿨다운 중
        assert mock_post.call_count == 1


class TestBuyExecuted:
    """send_buy_executed() 메시지 형식 테스트."""

    def test_buy_executed_format(self, bot, mock_post):
        """매수 체결 메시지 형식 검증."""
        bot.send_buy_executed(
            code="005930",
            name="삼성전자",
            price=74600,
            qty=13,
            amount=969800,
            capital_pct=9.7,
            stop_price=72200,
            target_price=80568,
        )

        msg = mock_post.call_args[1]["json"]["text"]
        assert "✅ <b>매수 체결</b>" in msg
        assert "삼성전자 (005930)" in msg
        assert "74,600원" in msg
        assert "13주" in msg
        assert "969,800원" in msg
        assert "9.7%" in msg
        assert "72,200원" in msg
        assert "80,568원" in msg

    def test_buy_executed_no_cooldown(self, bot, mock_post):
        """매수 체결에는 쿨다운 없음."""
        bot.send_buy_executed(
            "005930", "삼성전자", 74600, 13, 969800, 9.7, 72200, 80568
        )
        bot.send_buy_executed(
            "005930", "삼성전자", 74600, 13, 969800, 9.7, 72200, 80568
        )

        assert mock_post.call_count == 2


class TestSellExecutedProfit:
    """send_sell_executed_profit() 메시지 형식 테스트."""

    def test_sell_profit_format(self, bot, mock_post):
        """매도 수익 메시지 형식 검증."""
        bot.send_sell_executed_profit(
            code="005930",
            name="삼성전자",
            price=80700,
            hold_days=8,
            pnl=80600,
            pnl_pct=8.2,
            net_pnl=76800,
            net_pnl_pct=7.8,
        )

        msg = mock_post.call_args[1]["json"]["text"]
        assert "💰 <b>매도 체결 (목표 달성)</b>" in msg
        assert "삼성전자 (005930)" in msg
        assert "80,700원" in msg
        assert "8일" in msg
        assert "+80,600원" in msg
        assert "+8.2%" in msg
        assert "+76,800원" in msg
        assert "+7.8%" in msg


class TestSellExecutedLoss:
    """send_sell_executed_loss() 메시지 형식 테스트."""

    def test_sell_loss_format(self, bot, mock_post):
        """매도 손실 메시지 형식 검증."""
        bot.send_sell_executed_loss(
            code="035720",
            name="카카오",
            price=42100,
            hold_days=3,
            pnl=-28500,
            pnl_pct=-3.2,
            reason="손절가 이탈",
        )

        msg = mock_post.call_args[1]["json"]["text"]
        assert "🔴 <b>매도 체결 (손절)</b>" in msg
        assert "카카오 (035720)" in msg
        assert "42,100원" in msg
        assert "3일" in msg
        assert "-28,500원" in msg
        assert "-3.2%" in msg
        assert "손절가 이탈" in msg

    def test_sell_loss_no_cooldown(self, bot, mock_post):
        """손절 알림에는 쿨다운 없음."""
        bot.send_sell_executed_loss(
            "035720", "카카오", 42100, 3, -28500, -3.2, "손절가 이탈"
        )
        bot.send_sell_executed_loss(
            "035720", "카카오", 42100, 3, -28500, -3.2, "손절가 이탈"
        )

        assert mock_post.call_count == 2


class TestDailyWarning:
    """send_daily_warning() 메시지 형식 테스트."""

    def test_daily_warning_format(self, bot, mock_post):
        """일일 손실 경고 메시지 형식 검증."""
        bot.send_daily_warning(
            current_pnl_pct=-2.1,
            limit_pct=-3.0,
            remaining=0.9,
        )

        msg = mock_post.call_args[1]["json"]["text"]
        assert "⚠️ <b>일일 손실 경고</b>" in msg
        assert "-2.1%" in msg
        assert "-3.0%" in msg
        assert "0.9%p" in msg
        assert "보유 포지션 확인 권장" in msg

    def test_daily_warning_cooldown(self, bot, mock_post):
        """일일 손실 경고는 1회/일 쿨다운."""
        bot.send_daily_warning(-2.1, -3.0, 0.9)
        result = bot.send_daily_warning(-2.5, -3.0, 0.5)

        assert result is False
        assert mock_post.call_count == 1


class TestHaltAlert:
    """send_halt_alert() 메시지 형식 테스트."""

    def test_halt_alert_format(self, bot, mock_post):
        """매매 중단 메시지 형식 검증."""
        bot.send_halt_alert(current_pnl_pct=-3.2)

        msg = mock_post.call_args[1]["json"]["text"]
        assert "🛑 <b>매매 중단</b>" in msg
        assert "-3.0%" in msg or "-3%" in msg
        assert "-3.2%" in msg
        assert "당일 신규 주문 중단됨" in msg
        assert "내일 09:00 자동 재개" in msg

    def test_halt_alert_no_cooldown(self, bot, mock_post):
        """매매 중단 알림에는 쿨다운 없음."""
        bot.send_halt_alert(-3.2)
        bot.send_halt_alert(-3.5)

        assert mock_post.call_count == 2


class TestDailyReport:
    """send_daily_report() 메시지 형식 테스트."""

    def test_daily_report_format(self, bot, mock_post):
        """일간 리포트 메시지 형식 검증."""
        bot.send_daily_report(
            date="2024-11-15",
            buy_count=2,
            sell_count=1,
            realized_pnl=45200,
            realized_pnl_pct=0.45,
            position_count=3,
            unrealized_pnl=128000,
            initial_capital=10000000,
            current_capital=10523400,
            total_return_pct=5.23,
            current_mdd=-3.8,
        )

        msg = mock_post.call_args[1]["json"]["text"]
        assert "📈 <b>일간 리포트</b> — 2024-11-15" in msg
        assert "매수: 2건" in msg
        assert "매도: 1건" in msg
        assert "+45,200원" in msg
        assert "+0.45%" in msg
        assert "3종목" in msg
        assert "+128,000원" in msg
        assert "10,000,000원" in msg
        assert "10,523,400원" in msg
        assert "+5.23%" in msg
        assert "-3.8%" in msg

    def test_daily_report_cooldown(self, bot, mock_post):
        """일간 리포트는 동일 날짜 1회/일 쿨다운."""
        bot.send_daily_report(
            "2024-11-15", 2, 1, 45200, 0.45, 3, 128000,
            10000000, 10523400, 5.23, -3.8,
        )
        result = bot.send_daily_report(
            "2024-11-15", 2, 1, 45200, 0.45, 3, 128000,
            10000000, 10523400, 5.23, -3.8,
        )

        assert result is False
        assert mock_post.call_count == 1


class TestSystemError:
    """send_system_error() 메시지 형식 테스트."""

    def test_system_error_format(self, bot, mock_post):
        """시스템 오류 메시지 형식 검증."""
        bot.send_system_error(
            error="ConnectionError",
            location="kiwoom_api.py:142",
            retry_info="자동 재연결 시도 중... (1/5)",
        )

        msg = mock_post.call_args[1]["json"]["text"]
        assert "🚨 <b>시스템 오류</b>" in msg
        assert "ConnectionError" in msg
        assert "kiwoom_api.py:142" in msg
        assert "자동 재연결 시도 중... (1/5)" in msg

    def test_system_error_without_retry(self, bot, mock_post):
        """재시도 정보 없는 시스템 오류."""
        bot.send_system_error(
            error="FileNotFoundError",
            location="config.py:10",
        )

        msg = mock_post.call_args[1]["json"]["text"]
        assert "🚨 <b>시스템 오류</b>" in msg
        assert "FileNotFoundError" in msg
        assert "config.py:10" in msg

    def test_system_error_no_cooldown(self, bot, mock_post):
        """시스템 오류에는 쿨다운 없음."""
        bot.send_system_error("Error1", "loc1")
        bot.send_system_error("Error2", "loc2")

        assert mock_post.call_count == 2


class TestStartup:
    """send_startup() 메시지 형식 테스트."""

    def test_startup_format_paper(self, bot, mock_post):
        """모의투자 모드 시작 메시지 형식 검증."""
        bot.send_startup(mode="paper")

        msg = mock_post.call_args[1]["json"]["text"]
        assert "🟢 <b>시스템 시작</b>" in msg
        assert "모의투자" in msg
        assert "v0.1.0" in msg

    def test_startup_format_live(self, bot, mock_post):
        """실거래 모드 시작 메시지 형식 검증."""
        bot.send_startup(mode="live", version="1.0.0")

        msg = mock_post.call_args[1]["json"]["text"]
        assert "🟢 <b>시스템 시작</b>" in msg
        assert "실거래" in msg
        assert "v1.0.0" in msg

    def test_startup_no_cooldown(self, bot, mock_post):
        """시작 알림에는 쿨다운 없음."""
        bot.send_startup(mode="paper")
        bot.send_startup(mode="paper")

        assert mock_post.call_count == 2


class TestShutdown:
    """send_shutdown() 메시지 형식 테스트."""

    def test_shutdown_format(self, bot, mock_post):
        """종료 메시지 형식 검증."""
        bot.send_shutdown(mode="paper")

        msg = mock_post.call_args[1]["json"]["text"]
        assert "🔴 <b>시스템 종료</b>" in msg
        assert "모의투자" in msg
        assert "정상 종료" in msg

    def test_shutdown_custom_reason(self, bot, mock_post):
        """사유 지정 종료 메시지."""
        bot.send_shutdown(mode="live", reason="사용자 중단")

        msg = mock_post.call_args[1]["json"]["text"]
        assert "실거래" in msg
        assert "사용자 중단" in msg

    def test_shutdown_no_cooldown(self, bot, mock_post):
        """종료 알림에는 쿨다운 없음."""
        bot.send_shutdown(mode="paper")
        bot.send_shutdown(mode="paper")

        assert mock_post.call_count == 2


class TestEnvironmentVariables:
    """환경변수에서 토큰/채팅ID 로드 테스트."""

    def test_from_env(self, mock_post):
        """환경변수에서 설정 로드."""
        with patch.dict(
            "os.environ",
            {"TELEGRAM_BOT_TOKEN": "ENV_TOKEN", "TELEGRAM_CHAT_ID": "ENV_CHAT"},
        ):
            bot = TelegramBot()
            bot.send("테스트")

            url = mock_post.call_args[0][0]
            assert "ENV_TOKEN" in url

            chat_id = mock_post.call_args[1]["json"]["chat_id"]
            assert chat_id == "ENV_CHAT"
