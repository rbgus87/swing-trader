# KIWOOM_SPEC.md — 키움 OpenAPI+ 연동 명세

## 1. 환경 요구사항

- Windows 10/11 64bit
- 키움 OpenAPI+ 설치 (32bit OCX) → Python도 32bit 필요
  - **중요**: 키움 OCX는 32bit 전용 → Python 3.10 32bit 사용
  - 또는 64bit 환경에서 별도 32bit 프로세스로 분리 후 IPC 통신
- PyQt5: `pip install PyQt5`
- 키움증권 계좌 + OpenAPI 사용 신청 완료

## 2. 초기화 패턴

```python
# src/broker/kiwoom_api.py
import sys
from PyQt5.QtWidgets import QApplication
from PyQt5.QAxContainer import QAxWidget

class KiwoomAPI(QAxWidget):
    def __init__(self):
        super().__init__()
        self.setControl("KHOPENAPI.KHOpenAPICtrl.1")
        self._setup_events()
        self._connected = False

    def _setup_events(self):
        self.OnEventConnect.connect(self._on_connect)
        self.OnReceiveTrData.connect(self._on_tr_data)
        self.OnReceiveChejanData.connect(self._on_chejan)
        self.OnReceiveRealData.connect(self._on_realtime)
        self.OnReceiveMsg.connect(self._on_msg)

    def connect(self):
        """로그인 요청 — 별도 로그인 창 팝업"""
        self.dynamicCall("CommConnect()")

    def _on_connect(self, err_code: int):
        if err_code == 0:
            self._connected = True
            logger.info("키움 API 연결 성공")
        else:
            logger.error(f"키움 API 연결 실패: {err_code}")

# 메인 실행
if __name__ == "__main__":
    app = QApplication(sys.argv)
    api = KiwoomAPI()
    api.connect()
    app.exec_()    # PyQt 이벤트루프 — 블로킹
```

## 3. 주요 TR 코드

### 데이터 조회

| TR 코드 | 이름 | 용도 |
|--------|------|------|
| OPT10081 | 주식일봉차트조회요청 | 일봉 OHLCV (최대 600봉) |
| OPT10080 | 주식분봉차트조회요청 | 분봉 (틱범위: 60 = 60분봉) |
| OPT10001 | 주식기본정보요청 | 현재가, 시총, 등락률 |
| OPT20006 | 업종현재가요청 | KOSPI/KOSDAQ 지수 |
| OPTKWFID | 관심종목정보요청 | 다종목 현재가 일괄 조회 |

### 주문

| TR 코드 | 이름 | 용도 |
|--------|------|------|
| SendOrder | 주식 주문 | 매수/매도/취소 |

### 계좌

| TR 코드 | 이름 | 용도 |
|--------|------|------|
| OPW00018 | 계좌평가잔고내역요청 | 보유 종목, 손익 |
| OPW00004 | 계좌잔고요청 | 예수금, 가능금액 |

## 4. TR 데이터 조회 패턴

```python
def get_daily_ohlcv(self, code: str, start_date: str, adj_price: bool = True) -> pd.DataFrame:
    """
    OPT10081 — 주식일봉차트조회요청
    start_date: 'YYYYMMDD' 형식
    adj_price: True = 수정주가
    """
    self.SetInputValue("종목코드", code)
    self.SetInputValue("기준일자", start_date)
    self.SetInputValue("수정주가구분", "1" if adj_price else "0")

    self._tr_event.clear()
    self.CommRqData("주식일봉차트조회", "OPT10081", 0, self.SCREEN_OHLCV)
    self._tr_event.wait(timeout=5)  # 최대 5초 대기

    return self._parse_ohlcv_data()

def _on_tr_data(self, screen_no, rq_name, tr_code, record_name, prev_next):
    """TR 데이터 수신 콜백 — 메인 스레드에서만 실행됨"""
    if rq_name == "주식일봉차트조회":
        count = self.GetRepeatCnt(tr_code, record_name)
        rows = []
        for i in range(count):
            row = {
                'date':   self.GetCommData(tr_code, record_name, i, "일자").strip(),
                'open':   int(self.GetCommData(tr_code, record_name, i, "시가").strip()),
                'high':   int(self.GetCommData(tr_code, record_name, i, "고가").strip()),
                'low':    int(self.GetCommData(tr_code, record_name, i, "저가").strip()),
                'close':  int(self.GetCommData(tr_code, record_name, i, "현재가").strip()),
                'volume': int(self.GetCommData(tr_code, record_name, i, "거래량").strip()),
            }
            rows.append(row)
        self._tr_data_buffer = rows
        self._tr_event.set()
```

## 5. 실시간 시세 등록

```python
# 실시간 FID 코드
FID_CURRENT_PRICE = 10    # 현재가
FID_VOLUME        = 15    # 거래량
FID_CHANGE_RATE   = 12    # 등락률

def subscribe_realtime(self, codes: list[str]):
    """종목 실시간 시세 등록"""
    code_str = ";".join(codes)
    fid_str  = f"{FID_CURRENT_PRICE};{FID_VOLUME};{FID_CHANGE_RATE}"
    # 0: 신규 등록, 1: 기존 유지하고 추가
    self.SetRealReg(self.SCREEN_REALTIME, code_str, fid_str, "0")

def _on_realtime(self, code, real_type, data):
    if real_type == "주식체결":
        price  = abs(int(self.GetCommRealData(code, FID_CURRENT_PRICE)))
        volume = int(self.GetCommRealData(code, FID_VOLUME))
        tick   = Tick(code=code, price=price, volume=volume)
        self.on_tick_callback(tick)  # TradingEngine으로 전달
```

## 6. 주문 실행

```python
# 주문 구분
ORDER_BUY        = 1   # 신규 매수
ORDER_SELL       = 2   # 신규 매도
ORDER_BUY_CANCEL = 3   # 매수 취소
ORDER_SELL_CANCEL= 4   # 매도 취소

# 호가 구분
PRICE_LIMIT  = "00"   # 지정가
PRICE_MARKET = "03"   # 시장가

def send_order(
    self,
    rq_name:    str,
    screen_no:  str,
    account:    str,
    order_type: int,
    code:       str,
    qty:        int,
    price:      int,
    hoga_type:  str,
    org_order_no: str = ""
) -> int:
    """
    반환값: 0 = 성공, 그 외 = 오류코드
    실제 체결 확인은 OnReceiveChejanData 콜백에서
    """
    return self.dynamicCall(
        "SendOrder(QString, QString, QString, int, QString, int, int, QString, QString)",
        [rq_name, screen_no, account, order_type, code, qty, price, hoga_type, org_order_no]
    )
```

## 7. 요청 제한 (Rate Limiting)

```python
# src/broker/rate_limiter.py
import time
from collections import deque

class RateLimiter:
    """키움 API: TR 조회 5건/초, 주문 5건/초"""
    def __init__(self, max_calls: int = 5, period: float = 1.0):
        self.max_calls = max_calls
        self.period    = period
        self._calls    = deque()

    def wait(self):
        now = time.monotonic()
        # 1초 이전 호출 제거
        while self._calls and now - self._calls[0] > self.period:
            self._calls.popleft()
        if len(self._calls) >= self.max_calls:
            sleep_time = self.period - (now - self._calls[0])
            if sleep_time > 0:
                time.sleep(sleep_time)
        self._calls.append(time.monotonic())
```

## 8. pykrx 컬럼 매핑

```python
# data/column_mapper.py
OHLCV_MAP = {
    "시가": "open",
    "고가": "high",
    "저가": "low",
    "종가": "close",
    "거래량": "volume",
    "거래대금": "amount",
    "등락률": "change_rate",
}

FUNDAMENTAL_MAP = {
    "BPS":  "bps",
    "PER":  "per",
    "PBR":  "pbr",
    "EPS":  "eps",
    "DIV":  "div_yield",
    "DPS":  "dps",
}

def map_columns(df: pd.DataFrame, col_map: dict) -> pd.DataFrame:
    return df.rename(columns=col_map)

# ROE 수동 계산 (pykrx에서 직접 미제공)
def calculate_roe(df: pd.DataFrame) -> pd.DataFrame:
    """BPS > 0인 종목만 계산"""
    mask = df['bps'] > 0
    df.loc[mask, 'roe'] = df.loc[mask, 'eps'] / df.loc[mask, 'bps'] * 100
    df.loc[~mask, 'roe'] = float('nan')
    return df
```

## 9. 장 시간 체크

```python
# src/utils/market_calendar.py
from datetime import datetime, time
import pytz

KST = pytz.timezone('Asia/Seoul')
MARKET_OPEN  = time(9, 0)
MARKET_CLOSE = time(15, 30)

def is_market_open() -> bool:
    now = datetime.now(KST).time()
    return MARKET_OPEN <= now <= MARKET_CLOSE

def is_trading_day(date: datetime = None) -> bool:
    """공휴일 체크 — holidays 라이브러리 사용"""
    import holidays
    kr_holidays = holidays.KR()
    d = (date or datetime.now(KST)).date()
    return d.weekday() < 5 and d not in kr_holidays
```

## 10. 연결 오류 코드

| 코드 | 의미 |
|------|------|
| 0 | 정상 |
| -100 | 사용자 정보 교환 실패 |
| -101 | 서버 접속 실패 |
| -102 | 버전 처리 실패 |
| -200 | 시세 제한 초과 |
| -201 | 조회 과부하 |
