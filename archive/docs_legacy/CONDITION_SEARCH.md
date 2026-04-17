# CONDITION_SEARCH.md — HTS 조건검색 통합 (독립 모듈)

> 영웅문에 저장된 `swing_pre_cross` 조건식을 장전 스크리닝에 통합.
> 고정 20종목 watchlist → 동적 조건식 기반 watchlist로 전환.

## 설계 원칙

- **기존 ws_client.py는 전혀 건드리지 않음**
- `condition_search.py` 안에 조건검색 전용 미니 WebSocket 클라이언트 포함
- 장전 1회만 연결 → 쿼리 → 즉시 종료 (상시 연결 아님)
- 실패 시 고정 watchlist로 자동 폴백

## 검증된 프로토콜 (day-trader 레포 참조)

- **WebSocket URL**: `wss://api.kiwoom.com:10000/api/dostk/websocket`
- **LOGIN**: `{"trnm": "LOGIN", "token": "<토큰>"}` — Bearer 없이 토큰만
- **LOGIN 응답**: `{"trnm": "LOGIN", "return_code": 0, "return_msg": "..."}`
- **PING 에코**: 서버가 PING 보내면 그대로 에코백 필수
- **ka10171**: `{"trnm": "CNSRLST"}` → 조건식 목록
- **ka10172**: `{"trnm": "CNSRREQ", "seq": "N", "search_type": "0", "stex_tp": "K", "cont_yn": "N", "next_key": ""}` → 실행

---

## 프롬프트

```
CLAUDE.md를 읽어줘.
그리고 docs/CONDITION_SEARCH.md를 읽어줘.

영웅문 HTS의 사용자 조건식 "swing_pre_cross"를 키움 REST API WebSocket으로
실행해서 장전 스크리닝의 watchlist를 동적으로 생성하도록 통합해줘.

**중요: 기존 src/broker/ws_client.py는 절대 수정하지 마.**
조건검색 전용 독립 모듈로 구현한다.

## 1. src/broker/condition_search.py 신규 생성

조건검색 전용 미니 WebSocket 클라이언트.

"""조건검색 전용 미니 WebSocket 클라이언트.

장전 1회만 연결 → LOGIN → ka10171 → ka10172 → 종료.
기존 ws_client.py와 독립 (상시 연결 아님).
day-trader/core/kiwoom_ws.py의 LOGIN 프로토콜 참조.
"""

import asyncio
import json
from loguru import logger

try:
    from websockets.client import connect as ws_connect
except ImportError:
    from websockets import connect as ws_connect


class ConditionSearchClient:
    """조건검색 전용 미니 WebSocket 클라이언트."""

    LOGIN_TIMEOUT = 5.0
    QUERY_TIMEOUT = 15.0

    def __init__(self, ws_url: str, access_token: str):
        self._ws_url = ws_url
        self._access_token = access_token
        self._ws = None

    async def connect(self) -> bool:
        """WebSocket 연결 + LOGIN 패킷 전송."""
        try:
            self._ws = await ws_connect(
                self._ws_url,
                ping_interval=20,
                ping_timeout=10,
            )
            logger.info("[CS] WebSocket 연결 완료")

            # LOGIN (Bearer 없이 토큰만 — day-trader 프로토콜과 동일)
            await self._ws.send(json.dumps({
                "trnm": "LOGIN",
                "token": self._access_token,
            }))
            logger.info("[CS] LOGIN 패킷 전송")

            # LOGIN 응답 대기 (PING 메시지 필터링)
            for _ in range(10):
                raw = await asyncio.wait_for(
                    self._ws.recv(), timeout=self.LOGIN_TIMEOUT
                )
                resp = json.loads(raw)
                trnm = resp.get("trnm", "")

                if trnm == "PING":
                    await self._ws.send(raw)  # 에코백
                    continue

                if trnm == "LOGIN":
                    rc = resp.get("return_code", -1)
                    if isinstance(rc, str):
                        rc = int(rc)
                    if rc != 0:
                        msg = resp.get("return_msg", "")
                        logger.error(f"[CS] LOGIN 실패 (code={rc}): {msg}")
                        return False
                    logger.info("[CS] LOGIN 성공")
                    return True

            logger.error("[CS] LOGIN 응답 타임아웃")
            return False

        except asyncio.TimeoutError:
            logger.error("[CS] LOGIN 응답 타임아웃")
            return False
        except Exception as e:
            logger.error(f"[CS] 연결 실패: {e}")
            return False

    async def fetch_condition_list(self) -> list[dict]:
        """ka10171 — 서버 저장 조건식 목록 조회.

        Returns:
            [{"seq": "0", "name": "swing_pre_cross"}, ...] 형태.
            실패 시 [].
        """
        if not self._ws:
            return []

        try:
            await self._ws.send(json.dumps({"trnm": "CNSRLST"}))
            logger.info("[CS] ka10171 조건식 목록 요청")

            for _ in range(20):
                raw = await asyncio.wait_for(
                    self._ws.recv(), timeout=self.QUERY_TIMEOUT
                )
                resp = json.loads(raw)
                trnm = resp.get("trnm", "")

                if trnm == "PING":
                    await self._ws.send(raw)
                    continue

                if trnm == "CNSRLST":
                    # 첫 통합 시 실제 형식 확인용
                    logger.debug(f"[CS] CNSRLST raw: {resp}")

                    items = resp.get("data", [])
                    result = []
                    for item in items:
                        if isinstance(item, list) and len(item) >= 2:
                            # ["0", "swing_pre_cross"] 형태
                            result.append({
                                "seq": str(item[0]),
                                "name": str(item[1]),
                            })
                        elif isinstance(item, dict):
                            # {"seq": "0", "name": "swing_pre_cross"} 형태
                            seq = item.get("seq") or item.get("0", "")
                            name = item.get("name") or item.get("1", "")
                            if seq or name:
                                result.append({
                                    "seq": str(seq),
                                    "name": str(name),
                                })

                    logger.info(f"[CS] 조건식 목록 수신: {len(result)}개")
                    return result

            logger.warning("[CS] ka10171 응답 타임아웃")
            return []

        except Exception as e:
            logger.error(f"[CS] ka10171 실패: {e}")
            return []

    async def execute_condition(self, seq: str) -> list[str]:
        """ka10172 — 조건식 실행 (일반 모드).

        Args:
            seq: 조건식 일련번호

        Returns:
            매칭 종목 코드 리스트. 실패 시 [].
        """
        if not self._ws:
            return []

        try:
            await self._ws.send(json.dumps({
                "trnm": "CNSRREQ",
                "seq": str(seq),
                "search_type": "0",      # 0: 일반, 1: 실시간
                "stex_tp": "K",          # K: KRX
                "cont_yn": "N",
                "next_key": "",
            }))
            logger.info(f"[CS] ka10172 조건식 실행: seq={seq}")

            all_codes: list[str] = []

            for _ in range(50):
                raw = await asyncio.wait_for(
                    self._ws.recv(), timeout=self.QUERY_TIMEOUT
                )
                resp = json.loads(raw)
                trnm = resp.get("trnm", "")

                if trnm == "PING":
                    await self._ws.send(raw)
                    continue

                if trnm == "CNSRREQ":
                    # 첫 통합 시 실제 형식 확인용
                    logger.debug(f"[CS] CNSRREQ raw: {resp}")

                    items = resp.get("data", [])
                    for item in items:
                        code = None
                        if isinstance(item, dict):
                            # 후보 키 순차 시도 (실제 응답에 맞춰 조정)
                            code = (
                                item.get("jmcode")
                                or item.get("9001")
                                or item.get("code")
                                or item.get("stk_cd")
                            )
                        elif isinstance(item, str):
                            code = item

                        if code:
                            code = str(code).strip()
                            # A005930 → 005930 (접두사 제거)
                            if code.startswith("A") and len(code) == 7:
                                code = code[1:]
                            if code and len(code) >= 6:
                                all_codes.append(code)

                    # 연속 조회 여부
                    if resp.get("cont_yn", "N") != "Y":
                        break
                    logger.warning("[CS] 연속 조회 필요 — 첫 페이지만 사용")
                    break

            logger.info(f"[CS] 조건식 실행 완료: {len(all_codes)}종목")
            return all_codes

        except asyncio.TimeoutError:
            logger.warning("[CS] ka10172 응답 타임아웃")
            return []
        except Exception as e:
            logger.error(f"[CS] ka10172 실패: {e}")
            return []

    async def disconnect(self):
        """WebSocket 연결 종료."""
        if self._ws:
            try:
                await self._ws.close()
                logger.info("[CS] WebSocket 연결 종료")
            except Exception:
                pass
            self._ws = None


async def run_condition_search(
    ws_url: str,
    access_token: str,
    condition_name: str,
) -> list[str]:
    """장전용 조건검색 실행 — 연결 → 쿼리 → 종료 원샷.

    Args:
        ws_url: WebSocket URL
        access_token: 키움 접근 토큰
        condition_name: 영웅문 저장 조건식 이름

    Returns:
        매칭 종목 코드 리스트. 실패 시 [].
    """
    if not ws_url or not access_token:
        logger.error("[CS] ws_url 또는 access_token 없음")
        return []

    client = ConditionSearchClient(ws_url, access_token)

    try:
        if not await client.connect():
            return []

        conditions = await client.fetch_condition_list()
        if not conditions:
            logger.warning("[CS] 조건식 목록 비어있음")
            return []

        target = next(
            (c for c in conditions if c["name"] == condition_name), None
        )
        if not target:
            available = [c["name"] for c in conditions]
            logger.error(
                f"[CS] '{condition_name}' 없음. 등록된 조건식: {available}"
            )
            return []

        logger.info(
            f"[CS] 조건식 매칭: '{condition_name}' (seq={target['seq']})"
        )

        return await client.execute_condition(target["seq"])

    except Exception as e:
        logger.error(f"[CS] 실행 오류: {e}", exc_info=True)
        return []

    finally:
        await client.disconnect()


## 2. config.yaml 설정

kiwoom 섹션에 ws_url 복구 + watchlist_mode 추가:

kiwoom:
  base_url: "https://api.kiwoom.com"
  ws_url: "wss://api.kiwoom.com:10000/api/dostk/websocket"

# 기존 watchlist는 폴백용으로 유지
watchlist:
  - '005930'
  # ... 기존 20종목 그대로

# 신규: watchlist 모드
watchlist_mode: "condition"      # "fixed" 또는 "condition"

condition_search:
  enabled: true
  condition_name: "swing_pre_cross"
  fallback_to_fixed: true
  max_stocks: 30


## 3. src/engine.py _pre_market_screening 수정

기존 watchlist 로드 부분 (config.get("watchlist", []))을 찾아서
아래 분기 블록으로 교체. 토큰 갱신 직후, OHLCV 프리로드 직전에 위치.

    watchlist_mode = config.get("watchlist_mode", "fixed")

    if watchlist_mode == "condition":
        logger.info("watchlist 모드: 조건검색 (HTS 조건식)")
        from src.broker.condition_search import run_condition_search

        cs_config = config.get("condition_search", {})
        condition_name = cs_config.get("condition_name", "swing_pre_cross")
        max_stocks = cs_config.get("max_stocks", 30)
        ws_url = config.get("kiwoom.ws_url", "")

        try:
            codes = await run_condition_search(
                ws_url=ws_url,
                access_token=self._kiwoom.access_token,
                condition_name=condition_name,
            )
        except Exception as e:
            logger.error(f"조건검색 호출 실패: {e}", exc_info=True)
            codes = []

        if codes:
            if len(codes) > max_stocks:
                logger.info(
                    f"조건검색 결과 {len(codes)}개 → 상위 {max_stocks}개 제한"
                )
                codes = codes[:max_stocks]

            watchlist = codes
            self._candidates = set(codes)
            logger.info(
                f"동적 watchlist 설정: {len(watchlist)}종목 {codes[:5]}..."
            )

            try:
                self._telegram.send(
                    f"🎯 조건검색 완료\n"
                    f"조건식: {condition_name}\n"
                    f"매칭: {len(watchlist)}종목"
                )
            except Exception:
                pass
        else:
            if cs_config.get("fallback_to_fixed", True):
                logger.warning("조건검색 실패 → 고정 watchlist 폴백")
                watchlist = config.get("watchlist", [])
                self._candidates = set(watchlist)
                try:
                    self._telegram.send(
                        f"⚠️ 조건검색 실패 → 고정 watchlist 사용 "
                        f"({len(watchlist)}종목)"
                    )
                except Exception:
                    pass
            else:
                logger.error("조건검색 실패 + 폴백 비활성 → 스크리닝 중단")
                return
    else:
        # 기존 동작: 고정 watchlist
        watchlist = config.get("watchlist", [])
        self._candidates = set(watchlist)
        logger.info(f"고정 watchlist: {len(watchlist)}종목")

    # 이하 기존 OHLCV 프리로드 로직 그대로 (watchlist 변수 사용)

주의: self._kiwoom.access_token 접근자가 없으면 rest_client를 통해서 가져와.
(rest_client.access_token 속성이 이미 존재함 — src/broker/rest_client.py 확인)

## 4. 검증

### 4-1. 문법 체크
python -c "from src.broker.condition_search import run_condition_search; print('OK')"

### 4-2. 수동 테스트
python main.py --mode paper
GUI에서 "스크리닝" 버튼 클릭. 로그 순서 확인:
  [CS] WebSocket 연결 완료
  [CS] LOGIN 패킷 전송
  [CS] LOGIN 성공
  [CS] ka10171 조건식 목록 요청
  [CS] 조건식 목록 수신: N개
  [CS] 조건식 매칭: 'swing_pre_cross' (seq=X)
  [CS] ka10172 조건식 실행: seq=X
  [CS] 조건식 실행 완료: N종목
  동적 watchlist 설정: N종목 [...]
  [CS] WebSocket 연결 종료

### 4-3. 폴백 테스트
config.yaml에서 condition_name을 존재하지 않는 이름으로 변경
→ 폴백 동작 확인 → 원래 이름 복구

### 4-4. 커밋
git add -A
git commit -m "feat: HTS 조건검색 통합 — 동적 watchlist

- src/broker/condition_search.py 신규 (독립 미니 WebSocket 클라이언트)
- LOGIN 패킷 + PING 에코 (day-trader 프로토콜 참조)
- ka10171 조건식 목록 + ka10172 조건식 실행
- engine.py: _pre_market_screening 조건검색 모드 분기
- config.yaml: ws_url 복구 + watchlist_mode 토글
- 실패 시 고정 watchlist 자동 폴백

기존 ws_client.py 수정하지 않음 (독립 모듈 설계).
영웅문에 'swing_pre_cross' 사용자조건 저장 필요."
git push


## 5. 트러블슈팅

### 문제: LOGIN 실패
- 토큰 유효성 확인 (만료되지 않았는지)
- ws_url 정확성 확인
- 별도 appkey인지 확인 (day-trader와 충돌 방지)

### 문제: "조건식을 찾을 수 없음"
- 로그의 `등록된 조건식` 리스트 확인
- 영웅문 [0150]에서 사용자조건으로 정확한 이름으로 저장됐는지 확인

### 문제: "조건식 실행 완료: 0종목"
- 로그에서 `[CS] CNSRREQ raw: {...}` 확인
- 실제 응답 키 이름이 jmcode/9001/code/stk_cd 중 아무것도 아닐 수 있음
- 실제 키 이름 찾으면 execute_condition의 code 추출 부분에 추가
```
