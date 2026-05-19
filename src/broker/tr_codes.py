"""키움 API TR 코드 및 상수 정의.

키움 OpenAPI+에서 사용하는 TR 코드, 주문 구분, 호가 구분,
실시간 FID, 화면번호, 오류 코드 상수를 정의한다.
"""

# 데이터 조회 TR
TR_OPT10081 = "OPT10081"  # 주식일봉차트조회요청
TR_OPT10080 = "OPT10080"  # 주식분봉차트조회요청
TR_OPT10001 = "OPT10001"  # 주식기본정보요청
TR_OPT20006 = "OPT20006"  # 업종현재가요청
TR_OPTKWFID = "OPTKWFID"  # 관심종목정보요청

# 계좌 조회 TR
TR_OPW00018 = "OPW00018"  # 계좌평가잔고내역요청
TR_OPW00004 = "OPW00004"  # 계좌잔고요청

# 주문 구분
ORDER_BUY = 1
ORDER_SELL = 2
ORDER_BUY_CANCEL = 3
ORDER_SELL_CANCEL = 4

# 호가 구분
PRICE_LIMIT = "00"   # 지정가
PRICE_MARKET = "03"  # 시장가

# 실시간 FID
FID_CURRENT_PRICE = 10
FID_VOLUME = 15
FID_CHANGE_RATE = 12

# 화면번호
SCREEN_OHLCV = "0101"
SCREEN_ORDER = "0201"
SCREEN_REALTIME = "0301"
SCREEN_ACCOUNT = "0401"

# 연결 오류 코드
ERR_CODES = {
    0: "정상",
    -100: "사용자 정보 교환 실패",
    -101: "서버 접속 실패",
    -102: "버전 처리 실패",
    -200: "시세 제한 초과",
    -201: "조회 과부하",
}

# ── REST API IDs ──
API_AUTH_TOKEN = "au10001"

# 주문
API_STOCK_BUY = "kt10000"     # 주식 매수주문
API_STOCK_SELL = "kt10001"    # 주식 매도주문
API_STOCK_MODIFY = "kt10002"  # 주식 정정주문
API_STOCK_CANCEL = "kt10003"  # 주식 취소주문
API_STOCK_ORDER = API_STOCK_BUY  # deprecated — API_STOCK_BUY 사용 권장

# 시세
API_STOCK_PRICE = "ka10001"
API_STOCK_DAILY = "ka10081"   # 주식일봉차트조회요청 (구: ka10002)
API_STOCK_MINUTE = "ka10080"  # 주식분봉차트조회요청 (구: ka10003)

# 계좌
API_ACCOUNT_BALANCE = "ka10070"    # 업종별계좌평가현황
API_ACCOUNT_HOLDINGS = "kt00018"   # 계좌평가잔고내역 (보유종목/수량/평가금액)
API_ORDER_AVAILABLE = "kt00010"    # 주문인출가능금액

# 종목 목록
API_STOCK_LIST = "ka10100"

# ── REST Endpoints ──
EP_AUTH = "/oauth2/token"
EP_ORDER = "/api/dostk/ordr"
EP_STOCK = "/api/dostk/stkinfo"
EP_CHART = "/api/dostk/chart"
EP_ACCOUNT = "/api/dostk/acnt"

# ── WebSocket 실시간 타입 ──
WS_TYPE_TICK = "0B"
WS_TYPE_ORDERBOOK = "0D"
WS_TYPE_ORDER = "00"
