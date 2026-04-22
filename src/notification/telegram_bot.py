"""텔레그램 봇 알림 모듈.

requests 직접 호출로 Telegram Bot API를 사용하여 매매 알림을 발송한다.
python-telegram-bot 라이브러리는 사용하지 않는다.
"""

import os
import time
from datetime import datetime

import requests
from loguru import logger


class TelegramBot:
    """텔레그램 봇 알림 — requests 직접 호출 (라이브러리 미사용).

    8종 메시지 템플릿을 제공하며, 쿨다운 기능으로 중복 알림을 방지한다.

    Usage:
        from src.notification.telegram_bot import TelegramBot
        bot = TelegramBot()
        bot.send("테스트 메시지")
    """

    def __init__(self, token: str = None, chat_id: str = None):
        """텔레그램 봇 초기화.

        Args:
            token: 텔레그램 봇 토큰. None이면 TELEGRAM_BOT_TOKEN 환경변수 사용.
            chat_id: 텔레그램 채팅 ID. None이면 TELEGRAM_CHAT_ID 환경변수 사용.
        """
        self._token = token or os.getenv("TELEGRAM_BOT_TOKEN")
        self._chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID")
        self._base = f"https://api.telegram.org/bot{self._token}"
        self._cooldowns: dict[str, datetime] = {}

    def send(
        self,
        message: str,
        parse_mode: str = "HTML",
        retries: int = 2,
        retry_sleep_sec: int = 30,
    ) -> bool:
        """메시지 전송. timeout=30초, 실패 시 지정 간격으로 재시도.

        실패해도 매매는 계속 진행하므로 로그 레벨은 WARNING.

        Args:
            message: 전송할 메시지 텍스트.
            parse_mode: 파싱 모드. 기본값 "HTML".
            retries: 총 시도 횟수 (기본 2 = 최초 1회 + 30초 후 재시도 1회).
            retry_sleep_sec: 재시도 전 대기 시간(초).

        Returns:
            전송 성공 여부.
        """
        for attempt in range(retries):
            try:
                resp = requests.post(
                    f"{self._base}/sendMessage",
                    json={
                        "chat_id": self._chat_id,
                        "text": message,
                        "parse_mode": parse_mode,
                    },
                    timeout=30,
                )
                if resp.status_code == 200:
                    return True
                last_err = f"status={resp.status_code}"
            except Exception as e:
                last_err = str(e)

            is_last = attempt == retries - 1
            if is_last:
                logger.warning(f"텔레그램 전송 최종 실패 (무시): {last_err}")
            else:
                logger.warning(
                    f"텔레그램 전송 실패, {retry_sleep_sec}초 후 재시도: {last_err}"
                )
                time.sleep(retry_sleep_sec)
        return False

    def send_with_cooldown(self, key: str, message: str, cooldown_sec: int) -> bool:
        """쿨다운 적용 메시지 전송.

        동일 key에 대해 cooldown_sec 이내 재전송을 방지한다.

        Args:
            key: 쿨다운 식별 키.
            message: 전송할 메시지 텍스트.
            cooldown_sec: 쿨다운 시간(초).

        Returns:
            전송 성공 여부. 쿨다운 중이면 False.
        """
        now = datetime.now()
        if key in self._cooldowns:
            elapsed = (now - self._cooldowns[key]).total_seconds()
            if elapsed < cooldown_sec:
                return False  # 쿨다운 중
        self._cooldowns[key] = now
        return self.send(message)

    # ── 10종 메시지 템플릿 ──

    def send_startup(self, mode: str, version: str = "0.1.0") -> bool:
        """서비스 시작 알림.

        Args:
            mode: 실행 모드 (paper/simulate/live).
            version: 시스템 버전.

        Returns:
            전송 성공 여부.
        """
        mode_label = {"paper": "모의투자", "live": "실거래"}
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        msg = (
            f"🟢 <b>시스템 시작</b>\n"
            f"모드: {mode_label.get(mode, mode)}\n"
            f"버전: v{version}\n"
            f"시각: {now}"
        )
        return self.send(msg)

    def send_shutdown(self, mode: str, reason: str = "정상 종료") -> bool:
        """서비스 종료 알림.

        Args:
            mode: 실행 모드 (paper/simulate/live).
            reason: 종료 사유.

        Returns:
            전송 성공 여부.
        """
        mode_label = {"paper": "모의투자", "live": "실거래"}
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        msg = (
            f"🔴 <b>시스템 종료</b>\n"
            f"모드: {mode_label.get(mode, mode)}\n"
            f"사유: {reason}\n"
            f"시각: {now}"
        )
        return self.send(msg)

    def send_signal_alert(
        self,
        code: str,
        name: str,
        price: int,
        score: float,
        rsi: float,
        macd_hist: float,
        volume_ratio: float,
        ma_diff_pct: float,
        target_price: int,
        stop_price: int,
    ) -> bool:
        """매수 신호 발생 알림.

        Args:
            code: 종목코드.
            name: 종목명.
            price: 현재가(원).
            score: 신호 강도(1~5).
            rsi: RSI 값.
            macd_hist: MACD 히스토그램 값.
            volume_ratio: 거래량 비율(평균 대비 배수).
            ma_diff_pct: 20일선 대비 괴리율(%).
            target_price: 목표가(원).
            stop_price: 손절가(원).

        Returns:
            전송 성공 여부.
        """
        stars = "★" * int(score) + "☆" * (5 - int(score))
        pnl_ratio = (
            round((target_price - price) / (price - stop_price), 1)
            if price > stop_price
            else 0
        )
        msg = (
            f"📊 <b>매수 신호</b>\n"
            f"종목: {name} ({code})\n"
            f"현재가: {price:,}원\n"
            f"신호 강도: {stars}\n\n"
            f"━━ 지표 ━━\n"
            f"RSI: {rsi:.1f}\n"
            f"MACD 히스토그램: {macd_hist:+.2f}\n"
            f"거래량: 평균 대비 {volume_ratio:.1f}배\n"
            f"20일선 대비: {ma_diff_pct:+.1f}%\n\n"
            f"━━ 진입 계획 ━━\n"
            f"목표가: {target_price:,}원 (+{(target_price / price - 1) * 100:.1f}%)\n"
            f"손절가: {stop_price:,}원 ({(stop_price / price - 1) * 100:.1f}%)\n"
            f"손익비: {pnl_ratio}"
        )
        return self.send_with_cooldown(f"signal_{code}", msg, 3600)  # 동일 종목 1시간

    def send_buy_executed(
        self,
        code: str,
        name: str,
        price: int,
        qty: int,
        amount: int,
        capital_pct: float,
        stop_price: int,
        target_price: int,
    ) -> bool:
        """매수 체결 알림.

        Args:
            code: 종목코드.
            name: 종목명.
            price: 체결가(원).
            qty: 체결 수량(주).
            amount: 투자금(원).
            capital_pct: 자본 대비 비율(%).
            stop_price: 손절가(원).
            target_price: 목표가(원).

        Returns:
            전송 성공 여부.
        """
        msg = (
            f"✅ <b>매수 체결</b>\n"
            f"종목: {name} ({code})\n"
            f"체결가: {price:,}원\n"
            f"수량: {qty}주\n"
            f"투자금: {amount:,}원 (자본의 {capital_pct:.1f}%)\n\n"
            f"손절가: {stop_price:,}원 ({(stop_price / price - 1) * 100:.1f}%)\n"
            f"목표가: {target_price:,}원 (+{(target_price / price - 1) * 100:.1f}%)"
        )
        return self.send(msg)  # 체결 알림에는 쿨다운 없음

    def send_sell_executed_profit(
        self,
        code: str,
        name: str,
        price: int,
        hold_days: int,
        pnl: int,
        pnl_pct: float,
        net_pnl: int,
        net_pnl_pct: float,
    ) -> bool:
        """매도 체결 알림 (수익).

        Args:
            code: 종목코드.
            name: 종목명.
            price: 체결가(원).
            hold_days: 보유 기간(일).
            pnl: 수익금(원).
            pnl_pct: 수익률(%).
            net_pnl: 세후 수익금(원).
            net_pnl_pct: 세후 수익률(%).

        Returns:
            전송 성공 여부.
        """
        msg = (
            f"💰 <b>매도 체결 (목표 달성)</b>\n"
            f"종목: {name} ({code})\n"
            f"체결가: {price:,}원\n"
            f"보유: {hold_days}일\n\n"
            f"수익: {pnl:+,}원 ({pnl_pct:+.1f}%)\n"
            f"세후 수익: {net_pnl:+,}원 ({net_pnl_pct:+.1f}%)"
        )
        return self.send(msg)  # 체결 알림에는 쿨다운 없음

    def send_sell_executed_loss(
        self,
        code: str,
        name: str,
        price: int,
        hold_days: int,
        pnl: int,
        pnl_pct: float,
        reason: str,
    ) -> bool:
        """매도 체결 알림 (손실).

        Args:
            code: 종목코드.
            name: 종목명.
            price: 체결가(원).
            hold_days: 보유 기간(일).
            pnl: 손실금(원, 음수).
            pnl_pct: 손실률(%, 음수).
            reason: 매도 사유.

        Returns:
            전송 성공 여부.
        """
        msg = (
            f"🔴 <b>매도 체결 (손절)</b>\n"
            f"종목: {name} ({code})\n"
            f"체결가: {price:,}원\n"
            f"보유: {hold_days}일\n\n"
            f"손실: {pnl:+,}원 ({pnl_pct:+.1f}%)\n"
            f"사유: {reason}"
        )
        return self.send(msg)  # 손절 알림에는 쿨다운 없음

    def send_daily_warning(
        self, current_pnl_pct: float, limit_pct: float, remaining: float
    ) -> bool:
        """일일 손실 경고 (-2%).

        Args:
            current_pnl_pct: 현재 일일 손익률(%).
            limit_pct: 손실 한도(%).
            remaining: 남은 여유(%p).

        Returns:
            전송 성공 여부.
        """
        msg = (
            f"⚠️ <b>일일 손실 경고</b>\n"
            f"현재 일일 손익: {current_pnl_pct:+.1f}%\n"
            f"한도: {limit_pct:.1f}%\n"
            f"남은 여유: {remaining:.1f}%p\n\n"
            f"보유 포지션 확인 권장"
        )
        today = datetime.now().strftime("%Y-%m-%d")
        return self.send_with_cooldown(
            f"daily_warning_{today}", msg, 86400
        )  # 1회/일

    def send_halt_alert(self, current_pnl_pct: float) -> bool:
        """매매 중단 알림 (-3%).

        Args:
            current_pnl_pct: 현재 일일 손익률(%).

        Returns:
            전송 성공 여부.
        """
        msg = (
            f"🛑 <b>매매 중단</b>\n"
            f"일일 손실 한도(-3%) 초과\n"
            f"현재 손익: {current_pnl_pct:+.1f}%\n\n"
            f"당일 신규 주문 중단됨\n"
            f"내일 09:00 자동 재개"
        )
        return self.send(msg, retries=3)  # 긴급 알림 — 재시도 3회

    def send_daily_report(
        self,
        date: str,
        buy_count: int,
        sell_count: int,
        realized_pnl: int,
        realized_pnl_pct: float,
        position_count: int,
        unrealized_pnl: int,
        initial_capital: int,
        current_capital: int,
        total_return_pct: float,
        current_mdd: float,
    ) -> bool:
        """일간 리포트 (16:00).

        Args:
            date: 리포트 날짜 (YYYY-MM-DD).
            buy_count: 당일 매수 건수.
            sell_count: 당일 매도 건수.
            realized_pnl: 실현 손익(원).
            realized_pnl_pct: 실현 손익률(%).
            position_count: 보유 종목 수.
            unrealized_pnl: 평가 손익(원).
            initial_capital: 기준 자본(원).
            current_capital: 현재 자본(원).
            total_return_pct: 누적 수익률(%).
            current_mdd: 현재 MDD(%).

        Returns:
            전송 성공 여부.
        """
        msg = (
            f"📈 <b>일간 리포트</b> — {date}\n\n"
            f"━━ 당일 매매 ━━\n"
            f"매수: {buy_count}건 | 매도: {sell_count}건\n"
            f"실현 손익: {realized_pnl:+,}원 ({realized_pnl_pct:+.2f}%)\n\n"
            f"━━ 포지션 현황 ━━\n"
            f"보유 종목: {position_count}종목\n"
            f"평가 손익: {unrealized_pnl:+,}원\n\n"
            f"━━ 누적 성과 ━━\n"
            f"기준자본: {initial_capital:,}원\n"
            f"현재자본: {current_capital:,}원\n"
            f"누적 수익률: {total_return_pct:+.2f}%\n"
            f"현재 MDD: {current_mdd:.1f}%"
        )
        return self.send_with_cooldown(
            f"daily_report_{date}", msg, 86400
        )  # 1회/일

    def send_system_error(
        self, error: str, location: str, retry_info: str = ""
    ) -> bool:
        """시스템 오류 알림.

        Args:
            error: 오류 메시지.
            location: 오류 발생 위치.
            retry_info: 재시도 정보 (선택).

        Returns:
            전송 성공 여부.
        """
        msg = (
            f"🚨 <b>시스템 오류</b>\n"
            f"오류: {error}\n"
            f"위치: {location}"
        )
        if retry_info:
            msg += f"\n\n{retry_info}"
        return self.send(msg, retries=3)  # 시스템 오류 — 재시도 3회
