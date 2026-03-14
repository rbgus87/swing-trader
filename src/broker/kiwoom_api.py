"""키움 OpenAPI+ OCX 래퍼.

PyQt5 QAxWidget 기반으로 키움 OpenAPI+ OCX 컨트롤을 래핑한다.
모든 OCX 호출은 메인 스레드에서만 수행해야 한다.
"""

import threading
from datetime import datetime

import pandas as pd
from loguru import logger

try:
    from PyQt5.QAxContainer import QAxWidget
except ImportError:
    # 테스트 환경에서 PyQt5 없을 때 대비
    QAxWidget = object

from src.broker.rate_limiter import RateLimiter
from src.broker.tr_codes import (
    ERR_CODES,
    FID_CURRENT_PRICE,
    FID_VOLUME,
    SCREEN_ACCOUNT,
    SCREEN_OHLCV,
    SCREEN_ORDER,
    TR_OPT10080,
    TR_OPT10081,
    TR_OPW00018,
)
from src.models import Tick


class KiwoomAPI(QAxWidget):
    """키움 OpenAPI+ OCX 래퍼 클래스.

    PyQt5 QAxWidget을 상속하여 키움 OCX 컨트롤과 통신한다.
    TR 조회, 실시간 시세 수신, 주문 실행 기능을 제공한다.

    주의:
        - 모든 OCX 호출은 메인 스레드에서만 수행
        - threading.Event로 TR 응답 대기 (timeout=5)
        - rate_limiter를 반드시 사용
    """

    def __init__(self):
        super().__init__()
        self.setControl("KHOPENAPI.KHOpenAPICtrl.1")
        self._setup_events()
        self._connected = False
        self._tr_data_buffer: list[dict] = []
        self._tr_event = threading.Event()
        self._tr_rate_limiter = RateLimiter(max_calls=5)
        self._order_rate_limiter = RateLimiter(max_calls=5)
        self._tr_prev_next = "0"

        # 외부 콜백
        self.on_tick_callback: callable = None
        self.on_chejan_callback: callable = None

    def _setup_events(self) -> None:
        """OCX 이벤트 핸들러 연결."""
        self.OnEventConnect.connect(self._on_connect)
        self.OnReceiveTrData.connect(self._on_tr_data)
        self.OnReceiveChejanData.connect(self._on_chejan)
        self.OnReceiveRealData.connect(self._on_realtime)
        self.OnReceiveMsg.connect(self._on_msg)

    def connect(self) -> None:
        """키움 서버 연결 요청.

        CommConnect()를 호출하여 로그인 창을 띄운다.
        연결 결과는 _on_connect 콜백에서 처리된다.
        """
        logger.info("키움 서버 연결 요청")
        self.dynamicCall("CommConnect()")

    @property
    def connected(self) -> bool:
        """서버 연결 상태."""
        return self._connected

    def _on_connect(self, err_code: int) -> None:
        """연결 결과 콜백.

        Args:
            err_code: 연결 결과 코드. 0이면 정상.
        """
        msg = ERR_CODES.get(err_code, f"알 수 없는 오류 ({err_code})")
        if err_code == 0:
            self._connected = True
            logger.info("키움 서버 연결 성공")
        else:
            self._connected = False
            logger.error("키움 서버 연결 실패: {}", msg)

    def _set_input_value(self, key: str, value: str) -> None:
        """TR 입력값 설정."""
        self.dynamicCall("SetInputValue(QString, QString)", key, value)

    def _comm_rq_data(
        self, rq_name: str, tr_code: str, prev_next: int, screen_no: str
    ) -> int:
        """TR 데이터 요청."""
        return self.dynamicCall(
            "CommRqData(QString, QString, int, QString)",
            rq_name,
            tr_code,
            prev_next,
            screen_no,
        )

    def _get_comm_data(
        self, tr_code: str, record_name: str, index: int, field: str
    ) -> str:
        """TR 응답 데이터 조회.

        Returns:
            strip()된 결과 문자열.
        """
        result = self.dynamicCall(
            "GetCommData(QString, QString, int, QString)",
            tr_code,
            record_name,
            index,
            field,
        )
        return result.strip()

    def _get_repeat_cnt(self, tr_code: str, record_name: str) -> int:
        """반복 데이터 건수 조회."""
        return self.dynamicCall(
            "GetRepeatCnt(QString, QString)", tr_code, record_name
        )

    def get_daily_ohlcv(
        self, code: str, start_date: str, adj_price: bool = True
    ) -> pd.DataFrame:
        """주식 일봉 차트 데이터 조회 (OPT10081).

        Args:
            code: 종목코드 (6자리).
            start_date: 조회 시작일 (YYYYMMDD).
            adj_price: 수정주가 사용 여부.

        Returns:
            OHLCV DataFrame (date, open, high, low, close, volume).
        """
        self._tr_rate_limiter.wait()
        self._tr_data_buffer.clear()
        self._tr_event.clear()

        self._set_input_value("종목코드", code)
        self._set_input_value("기준일자", start_date)
        self._set_input_value("수정주가구분", "1" if adj_price else "0")
        self._comm_rq_data("일봉조회", TR_OPT10081, 0, SCREEN_OHLCV)

        if not self._tr_event.wait(timeout=5):
            logger.warning("일봉 조회 타임아웃: {}", code)
            return pd.DataFrame()

        return self._build_ohlcv_dataframe()

    def get_minute_ohlcv(self, code: str, tick_range: int = 60) -> pd.DataFrame:
        """주식 분봉 차트 데이터 조회 (OPT10080).

        Args:
            code: 종목코드 (6자리).
            tick_range: 분봉 간격 (1, 3, 5, 10, 15, 30, 45, 60).

        Returns:
            OHLCV DataFrame (date, open, high, low, close, volume).
        """
        self._tr_rate_limiter.wait()
        self._tr_data_buffer.clear()
        self._tr_event.clear()

        self._set_input_value("종목코드", code)
        self._set_input_value("틱범위", str(tick_range))
        self._set_input_value("수정주가구분", "1")
        self._comm_rq_data("분봉조회", TR_OPT10080, 0, SCREEN_OHLCV)

        if not self._tr_event.wait(timeout=5):
            logger.warning("분봉 조회 타임아웃: {}", code)
            return pd.DataFrame()

        return self._build_ohlcv_dataframe()

    def _build_ohlcv_dataframe(self) -> pd.DataFrame:
        """버퍼의 OHLCV 데이터를 DataFrame으로 변환."""
        if not self._tr_data_buffer:
            return pd.DataFrame()

        df = pd.DataFrame(self._tr_data_buffer)
        for col in ["open", "high", "low", "close", "volume"]:
            if col in df.columns:
                df[col] = df[col].apply(lambda x: abs(int(x)) if x else 0)
        return df

    def _on_tr_data(
        self,
        screen_no: str,
        rq_name: str,
        tr_code: str,
        record_name: str,
        prev_next: str,
    ) -> None:
        """TR 데이터 수신 콜백.

        수신된 데이터를 _tr_data_buffer에 저장하고 _tr_event를 set한다.
        """
        self._tr_prev_next = prev_next
        cnt = self._get_repeat_cnt(tr_code, record_name)

        if tr_code in (TR_OPT10081, TR_OPT10080):
            for i in range(cnt):
                row = {
                    "date": self._get_comm_data(tr_code, record_name, i, "일자"),
                    "open": self._get_comm_data(tr_code, record_name, i, "시가"),
                    "high": self._get_comm_data(tr_code, record_name, i, "고가"),
                    "low": self._get_comm_data(tr_code, record_name, i, "저가"),
                    "close": self._get_comm_data(tr_code, record_name, i, "현재가"),
                    "volume": self._get_comm_data(tr_code, record_name, i, "거래량"),
                }
                self._tr_data_buffer.append(row)
        elif tr_code == TR_OPW00018:
            for i in range(cnt):
                row = {
                    "code": self._get_comm_data(tr_code, record_name, i, "종목번호"),
                    "name": self._get_comm_data(tr_code, record_name, i, "종목명"),
                    "quantity": self._get_comm_data(tr_code, record_name, i, "보유수량"),
                    "buy_price": self._get_comm_data(
                        tr_code, record_name, i, "매입가"
                    ),
                    "current_price": self._get_comm_data(
                        tr_code, record_name, i, "현재가"
                    ),
                    "pnl": self._get_comm_data(tr_code, record_name, i, "평가손익"),
                    "pnl_pct": self._get_comm_data(
                        tr_code, record_name, i, "수익률(%)"
                    ),
                }
                self._tr_data_buffer.append(row)

        logger.debug(
            "TR 데이터 수신: rq_name={}, tr_code={}, cnt={}", rq_name, tr_code, cnt
        )
        self._tr_event.set()

    def _on_chejan(self, gubun: str, item_cnt: int, fid_list: str) -> None:
        """체결/잔고 데이터 콜백.

        Args:
            gubun: "0" 주문체결, "1" 잔고변경, "3" 특이신호.
            item_cnt: 아이템 개수.
            fid_list: FID 목록 (세미콜론 구분).
        """
        data = {
            "gubun": gubun,
            "order_no": self._get_chejan_data(9203),
            "code": self._get_chejan_data(9001),
            "order_type": self._get_chejan_data(905),
            "quantity": self._get_chejan_data(900),
            "price": self._get_chejan_data(901),
            "status": self._get_chejan_data(913),
        }
        logger.info("체결 데이터 수신: {}", data)

        if self.on_chejan_callback:
            self.on_chejan_callback(data)

    def _get_chejan_data(self, fid: int) -> str:
        """체결 데이터 개별 FID 조회."""
        result = self.dynamicCall("GetChejanData(int)", fid)
        return result.strip()

    def _on_realtime(self, code: str, real_type: str, data: str) -> None:
        """실시간 시세 콜백.

        주식체결 실시간 데이터 수신 시 Tick 객체를 생성하여
        on_tick_callback을 호출한다.
        """
        if real_type == "주식체결":
            price_str = self._get_comm_real_data(code, FID_CURRENT_PRICE)
            volume_str = self._get_comm_real_data(code, FID_VOLUME)

            price = abs(int(price_str)) if price_str else 0
            volume = abs(int(volume_str)) if volume_str else 0

            tick = Tick(
                code=code,
                price=price,
                volume=volume,
                timestamp=datetime.now(),
            )

            if self.on_tick_callback:
                self.on_tick_callback(tick)

    def _get_comm_real_data(self, code: str, fid: int) -> str:
        """실시간 데이터 개별 FID 조회."""
        result = self.dynamicCall(
            "GetCommRealData(QString, int)", code, fid
        )
        return result.strip()

    def _on_msg(
        self, screen_no: str, rq_name: str, tr_code: str, msg: str
    ) -> None:
        """메시지 수신 콜백."""
        logger.info(
            "메시지 수신: screen={}, rq={}, tr={}, msg={}",
            screen_no,
            rq_name,
            tr_code,
            msg,
        )

    def get_account_info(self) -> dict:
        """계좌 정보 조회.

        Returns:
            계좌번호, 예수금, 총평가금액 등을 포함하는 딕셔너리.
        """
        accounts = self.dynamicCall("GetLoginInfo(QString)", "ACCNO")
        account_list = accounts.strip().rstrip(";").split(";")

        self._tr_rate_limiter.wait()
        self._tr_data_buffer.clear()
        self._tr_event.clear()

        account = account_list[0] if account_list else ""
        self._set_input_value("계좌번호", account)
        self._set_input_value("비밀번호", "")
        self._set_input_value("비밀번호입력매체구분", "00")
        self._set_input_value("조회구분", "1")
        self._comm_rq_data("계좌평가잔고", TR_OPW00018, 0, SCREEN_ACCOUNT)

        if not self._tr_event.wait(timeout=5):
            logger.warning("계좌 정보 조회 타임아웃")
            return {"account": account, "holdings": []}

        return {
            "account": account,
            "holdings": self._tr_data_buffer.copy(),
        }

    def send_order(
        self,
        rq_name: str,
        screen_no: str,
        account: str,
        order_type: int,
        code: str,
        qty: int,
        price: int,
        hoga_type: str,
        org_order_no: str = "",
    ) -> int:
        """주문 전송.

        Args:
            rq_name: 요청 이름.
            screen_no: 화면번호.
            account: 계좌번호.
            order_type: 주문유형 (1:매수, 2:매도, 3:매수취소, 4:매도취소).
            code: 종목코드.
            qty: 주문수량.
            price: 주문가격 (시장가이면 0).
            hoga_type: 호가유형 ("00":지정가, "03":시장가).
            org_order_no: 원주문번호 (정정/취소 시).

        Returns:
            주문 결과 코드. 0이면 정상.
        """
        self._order_rate_limiter.wait()

        result = self.dynamicCall(
            "SendOrder(QString, QString, QString, int, QString, int, int, QString, QString)",
            [
                rq_name,
                screen_no,
                account,
                order_type,
                code,
                qty,
                price,
                hoga_type,
                org_order_no,
            ],
        )

        logger.info(
            "주문 전송: code={}, type={}, qty={}, price={}, result={}",
            code,
            order_type,
            qty,
            price,
            result,
        )
        return result

    def set_real_reg(
        self, screen_no: str, code_list: str, fid_list: str, opt_type: str
    ) -> None:
        """실시간 시세 등록.

        Args:
            screen_no: 화면번호.
            code_list: 종목코드 목록 (세미콜론 구분).
            fid_list: FID 목록 (세미콜론 구분).
            opt_type: "0" 최초등록, "1" 추가등록.
        """
        self.dynamicCall(
            "SetRealReg(QString, QString, QString, QString)",
            screen_no,
            code_list,
            fid_list,
            opt_type,
        )
        logger.debug("실시간 등록: codes={}, fids={}", code_list, fid_list)

    def set_real_remove(self, screen_no: str, code: str) -> None:
        """실시간 시세 해제.

        Args:
            screen_no: 화면번호.
            code: 종목코드. "ALL"이면 전체 해제.
        """
        self.dynamicCall(
            "SetRealRemove(QString, QString)", screen_no, code
        )
        logger.debug("실시간 해제: screen={}, code={}", screen_no, code)
