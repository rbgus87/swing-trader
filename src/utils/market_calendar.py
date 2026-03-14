"""장 운영 일정 유틸리티.

KST 기준 장 시간(09:00~15:30) 판정, 거래일 체크(주말/공휴일 제외),
다음 거래일 계산 등을 제공.
"""

from datetime import date, datetime, time, timedelta

import holidays
import pytz

# KST 타임존 상수
KST = pytz.timezone("Asia/Seoul")

# 장 시작/종료 시간
MARKET_OPEN = time(9, 0)
MARKET_CLOSE = time(15, 30)


def now_kst() -> datetime:
    """현재 KST 시각 반환."""
    return datetime.now(KST)


def is_market_open() -> bool:
    """현재 장이 열려 있는지 판정.

    조건: 거래일이고 09:00 <= 현재 시각 < 15:30 (KST).

    Returns:
        장 오픈 여부.
    """
    current = now_kst()
    if not is_trading_day(current.date()):
        return False
    current_time = current.time()
    return MARKET_OPEN <= current_time < MARKET_CLOSE


def is_trading_day(target_date: date | None = None) -> bool:
    """거래일 여부 판정.

    주말 및 한국 공휴일이 아닌 날을 거래일로 판정.

    Args:
        target_date: 판정 대상 날짜. None이면 오늘(KST).

    Returns:
        거래일 여부.
    """
    if target_date is None:
        target_date = now_kst().date()

    # 주말 체크
    if target_date.weekday() >= 5:
        return False

    # 한국 공휴일 체크
    kr_holidays = holidays.KR(years=target_date.year)
    if target_date in kr_holidays:
        return False

    return True


def get_next_trading_day(target_date: date | None = None) -> date:
    """다음 거래일 반환.

    Args:
        target_date: 기준 날짜. None이면 오늘(KST).

    Returns:
        target_date 이후 첫 거래일.
    """
    if target_date is None:
        target_date = now_kst().date()

    next_day = target_date + timedelta(days=1)
    while not is_trading_day(next_day):
        next_day += timedelta(days=1)
    return next_day
