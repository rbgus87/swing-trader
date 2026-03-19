"""주문 관리.

키움 REST API를 통한 주문 실행, 취소, 체결 상태 관리를 담당한다.
모든 주문은 RiskManager.pre_check() 통과 후에만 실행되어야 한다.
"""

import re

from loguru import logger

from src.broker.kiwoom_api import KiwoomAPI
from src.broker.tr_codes import (
    ORDER_BUY,
    ORDER_BUY_CANCEL,
    ORDER_SELL,
    ORDER_SELL_CANCEL,
    PRICE_MARKET,
)
from src.models import Order, OrderResult

# 미체결 취소 최대 재시도 횟수
_MAX_CANCEL_RETRIES = 3

# 종목코드 형식: 6자리 숫자
_CODE_PATTERN = re.compile(r"^\d{6}$")


class OrderManager:
    """주문 관리 클래스.

    주문 실행, 취소, 미체결 주문 관리 기능을 제공한다.

    Args:
        kiwoom_api: KiwoomAPI 인스턴스.
        account: 계좌번호.
    """

    def __init__(self, kiwoom_api: KiwoomAPI, account: str):
        self._api = kiwoom_api
        self._account = account
        self._pending_orders: dict[str, Order] = {}

    # RISK_CHECK_REQUIRED
    async def execute_order(
        self,
        code: str,
        qty: int,
        price: int,
        order_type: int,
        hoga_type: str = PRICE_MARKET,
    ) -> OrderResult:
        """주문 실행.

        반드시 RiskManager.pre_check() 통과 후 호출해야 한다.

        Args:
            code: 종목코드 (6자리).
            qty: 주문수량.
            price: 주문가격 (시장가이면 0).
            order_type: 주문유형 (1:매수, 2:매도).
            hoga_type: 호가유형 ("00":지정가, "03":시장가).

        Returns:
            주문 실행 결과.
        """
        # 입력 검증
        if not _CODE_PATTERN.match(code):
            logger.error("잘못된 종목코드 형식: {}", code)
            return OrderResult(
                success=False,
                order_no="",
                message=f"잘못된 종목코드 형식: {code} (6자리 숫자 필요)",
            )

        if qty <= 0:
            logger.error("주문수량은 양수여야 합니다: qty={}", qty)
            return OrderResult(
                success=False,
                order_no="",
                message=f"주문수량은 양수여야 합니다: {qty}",
            )

        if price < 0:
            logger.error("주문가격은 0 이상이어야 합니다: price={}", price)
            return OrderResult(
                success=False,
                order_no="",
                message=f"주문가격은 0 이상이어야 합니다: {price}",
            )

        side = "buy" if order_type == ORDER_BUY else "sell"

        # 중복 주문 감지: 동일 종목+방향의 미체결 주문이 있으면 차단
        for pending in self._pending_orders.values():
            if pending.code == code and pending.side == side:
                logger.warning("중복 주문 차단: code={}, side={}", code, side)
                return OrderResult(
                    success=False,
                    order_no="",
                    message=f"중복 주문 차단: {code} ({side}) 미체결 주문 존재",
                )

        logger.info(
            "주문 실행 요청: code={}, side={}, qty={}, price={}, hoga={}",
            code,
            side,
            qty,
            price,
            hoga_type,
        )

        result = await self._api.send_order(
            code, qty, price, order_type, hoga_type, self._account
        )

        # REST API는 dict 반환: {"return_code": 0, "ord_no": "..."}
        return_code = result.get("return_code", -1)
        ord_no = result.get("ord_no", "")

        if return_code == 0:
            order = Order(
                code=code,
                side=side,
                price=price,
                quantity=qty,
                order_type="market" if hoga_type == PRICE_MARKET else "limit",
                hoga_type=hoga_type,
            )
            self._pending_orders[ord_no] = order
            logger.info("주문 접수 성공: {}", ord_no)
            return OrderResult(
                success=True,
                order_no=ord_no,
                message="주문 접수 성공",
            )

        logger.error("주문 접수 실패: code={}, result={}", code, return_code)
        return OrderResult(
            success=False,
            order_no="",
            message=f"주문 접수 실패 (에러코드: {return_code})",
        )

    async def cancel_order(self, order_no: str) -> bool:
        """미체결 주문 취소.

        Args:
            order_no: 취소할 주문번호.

        Returns:
            취소 요청 성공 여부.
        """
        order = self._pending_orders.get(order_no)
        if not order:
            logger.warning("취소할 주문을 찾을 수 없음: {}", order_no)
            return False

        result = await self._api.cancel_order(
            order_no, order.code, order.quantity, self._account
        )

        return_code = result.get("return_code", -1)

        if return_code == 0:
            logger.info("주문 취소 접수 성공: {}", order_no)
            return True

        logger.error("주문 취소 실패: order_no={}, result={}", order_no, return_code)
        return False

    def get_pending_orders(self) -> list[Order]:
        """미체결 주문 목록 조회.

        Returns:
            미체결 Order 리스트.
        """
        return list(self._pending_orders.values())

    async def cancel_all_pending(self) -> dict[str, bool]:
        """미체결 주문 전량 취소.

        장 마감 후 잔존 미체결 주문을 모두 취소한다.
        재시도 로직 포함 (최대 3회).

        Returns:
            {주문번호: 취소성공여부} 딕셔너리.
        """
        if not self._pending_orders:
            logger.info("미체결 주문 없음 — 취소 작업 생략")
            return {}

        results: dict[str, bool] = {}
        order_nos = list(self._pending_orders.keys())

        logger.info(f"미체결 주문 전량 취소 시작: {len(order_nos)}건")

        for order_no in order_nos:
            order = self._pending_orders.get(order_no)
            if not order:
                continue

            success = False
            for attempt in range(1, _MAX_CANCEL_RETRIES + 1):
                try:
                    result = await self._api.cancel_order(
                        order_no, order.code, order.quantity, self._account
                    )
                    return_code = result.get("return_code", -1)
                    if return_code == 0:
                        self._pending_orders.pop(order_no, None)
                        success = True
                        logger.info(
                            f"미체결 취소 성공: {order_no} ({order.code} {order.side} {order.quantity}주)"
                        )
                        break
                    else:
                        logger.warning(
                            f"미체결 취소 실패 (시도 {attempt}/{_MAX_CANCEL_RETRIES}): "
                            f"{order_no}, 에러코드={return_code}"
                        )
                except Exception as e:
                    logger.error(
                        f"미체결 취소 예외 (시도 {attempt}/{_MAX_CANCEL_RETRIES}): "
                        f"{order_no}, {e}"
                    )

            results[order_no] = success
            if not success:
                logger.error(f"미체결 취소 최종 실패: {order_no} ({order.code})")

        cancelled = sum(1 for v in results.values() if v)
        failed = sum(1 for v in results.values() if not v)
        logger.info(f"미체결 전량 취소 완료: 성공 {cancelled}건, 실패 {failed}건")

        return results

    async def on_chejan(self, data: dict) -> None:
        """체결 이벤트 처리.

        체결 완료 시 미체결 주문 목록에서 제거한다.

        Args:
            data: 체결 데이터 딕셔너리.
        """
        order_no = data.get("order_no", "")
        status = data.get("status", "")

        logger.info(
            "체결 이벤트: order_no={}, status={}, code={}",
            order_no,
            status,
            data.get("code", ""),
        )

        # 체결 완료 시 미체결 목록에서 제거
        if status == "체결":
            self._pending_orders.pop(order_no, None)
            logger.info("주문 체결 완료, 미체결 목록에서 제거: {}", order_no)
