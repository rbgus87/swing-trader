"""텔레그램 알림.

기존 src/notification/telegram_bot.py를 래핑.
일일 시그널 리포트 + 개별 체결 알림.
"""
import os
from loguru import logger


class Notifier:
    def __init__(self):
        self.enabled = bool(os.getenv('TELEGRAM_BOT_TOKEN'))
        self.bot = None
        if self.enabled:
            try:
                from src.notification.telegram_bot import TelegramBot
                token = os.getenv('TELEGRAM_BOT_TOKEN')
                chat_id = os.getenv('TELEGRAM_CHAT_ID')
                self.bot = TelegramBot(token, chat_id)
                logger.info("Telegram notifier initialized")
            except Exception as e:
                logger.warning(f"Telegram init failed: {e}")
                self.enabled = False
        else:
            logger.info("Telegram notifier disabled (no token)")

    def send_daily_report(
        self,
        date_str: str,
        breadth: float,
        gate_open: bool,
        exit_signals: list,
        entry_signals: list,
        cash: float,
        portfolio_value: float,
    ):
        mode = os.getenv('IS_PAPER_TRADING', 'true')
        mode_tag = "[PAPER]" if mode.lower() == 'true' else "[LIVE]"

        lines = [
            f"{mode_tag} 일일 리포트 ({date_str})",
            f"----------------------------------",
            f"시장: breadth {breadth:.0%} | Gate {'OPEN' if gate_open else 'CLOSED'}",
            f"자본: {portfolio_value:,.0f}원 (현금 {cash:,.0f})",
            "",
        ]

        if exit_signals:
            lines.append("청산 신호:")
            for sig in exit_signals:
                lines.append(
                    f"  {sig.ticker} {sig.name} | {sig.reason} @ {sig.price:,.0f}"
                )

        if entry_signals:
            lines.append("")
            lines.append("진입 신호 (내일 시가 매수):")
            for sig in entry_signals:
                lines.append(f"  {sig.ticker} {sig.name} | {sig.shares}주")
                lines.append(
                    f"    손절 {sig.stop_price:,.0f} | TP1 {sig.tp1_price:,.0f}"
                )

        if not exit_signals and not entry_signals:
            lines.append("신호 없음")

        message = "\n".join(lines)
        print(message)

        if self.enabled and self.bot:
            try:
                self.bot.send(message, parse_mode=None)
                logger.info("Telegram report sent")
            except Exception as e:
                logger.warning(f"Telegram send failed: {e}")
