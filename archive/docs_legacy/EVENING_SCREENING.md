# EVENING_SCREENING.md — 저녁 조건검색 (종목명 포함)

> 장마감 후 15:40에 조건검색 실행 → 다음날 watchlist를 DB에 저장.
> 종목명까지 함께 저장하여 텔레그램 알림 가독성 개선.

## 운용 흐름

```
[15:40] 저녁 조건검색
   ↓ {code, name} 30개 → DB daily_watchlist (date=내일)
   ↓ 텔레그램: "🌙 저녁 스크리닝 완료" + 종목명 샘플

[다음날 08:30] 장전 스크리닝
   ↓ DB에서 watchlist 로드
   ↓ 종목명 캐시(_poll_stock_names)에 미리 채움
   ↓ 텔레그램: "📋 watchlist 로드 완료" + 종목명 샘플
```

---

## 프롬프트

```
CLAUDE.md를 읽어줘.
그리고 docs/EVENING_SCREENING.md를 읽어줘.

조건검색 실행 시점을 장 시작 전(08:30)에서 장 마감 후(15:40)로 변경하고,
종목명까지 함께 저장하여 텔레그램 알림 가독성을 개선해줘.

이유: swing_pre_cross 조건식은 0봉전(오늘) 데이터를 사용하므로
장 시작 전에는 0건 반환. 장 마감 후가 데이터 확정되는 유일한 시점.
또한 조건검색 응답에 종목명이 포함되어 있는데 현재는 코드만 사용 중.

## 1. src/broker/condition_search.py — 종목명도 반환

execute_condition 메서드 수정:

기존: list[str] 반환 (코드만)
변경: list[dict] 반환 ({"code": "005930", "name": "삼성전자"})

코드 변경 부분:

    async def execute_condition(self, seq: str) -> list[dict]:
        """ka10172 — 조건식 실행 (일반 모드).

        Returns:
            [{"code": "005930", "name": "삼성전자"}, ...] 형태.
            실패 시 [].
        """
        if not self._ws:
            return []

        try:
            await self._ws.send(json.dumps({
                "trnm": "CNSRREQ",
                "seq": str(seq),
                "search_type": "0",
                "stex_tp": "K",
                "cont_yn": "N",
                "next_key": "",
            }))
            logger.info(f"[CS] ka10172 조건식 실행: seq={seq}")

            all_stocks: list[dict] = []

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
                    logger.debug(f"[CS] CNSRREQ raw: {resp}")

                    items = resp.get("data", [])
                    for item in items:
                        if not isinstance(item, dict):
                            continue
                        
                        # 종목 코드 추출 (9001 키가 정답)
                        code = (
                            item.get("9001")
                            or item.get("jmcode")
                            or item.get("code")
                            or item.get("stk_cd")
                        )
                        # 종목명 추출 (302 키가 정답)
                        name = (
                            item.get("302")
                            or item.get("hts_kor_isnm")
                            or item.get("name")
                            or ""
                        )

                        if code:
                            code = str(code).strip()
                            # A005930 → 005930 (접두사 제거)
                            if code.startswith("A") and len(code) == 7:
                                code = code[1:]
                            if code and len(code) >= 6:
                                all_stocks.append({
                                    "code": code,
                                    "name": str(name).strip(),
                                })

                    if resp.get("cont_yn", "N") != "Y":
                        break
                    logger.warning("[CS] 연속 조회 필요 — 첫 페이지만 사용")
                    break

            logger.info(f"[CS] 조건식 실행 완료: {len(all_stocks)}종목")
            return all_stocks

        except asyncio.TimeoutError:
            logger.warning("[CS] ka10172 응답 타임아웃")
            return []
        except Exception as e:
            logger.error(f"[CS] ka10172 실패: {e}")
            return []


run_condition_search 함수 시그니처도 list[dict] 반환으로 변경:

async def run_condition_search(
    ws_url: str,
    access_token: str,
    condition_name: str,
) -> list[dict]:
    """장전용 조건검색 실행 — 연결 → 쿼리 → 종료 원샷.

    Returns:
        [{"code": "005930", "name": "삼성전자"}, ...] 형태.
        실패 시 [].
    """
    # ... 기존 로직 그대로, return 타입만 변경

## 2. src/datastore.py — daily_watchlist 테이블 추가

create_tables 메서드에 추가:

CREATE TABLE IF NOT EXISTS daily_watchlist (
    date TEXT PRIMARY KEY,
    stocks TEXT NOT NULL,                -- JSON: [{"code": "005930", "name": "삼성전자"}, ...]
    source TEXT DEFAULT 'condition_search',
    stock_count INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_daily_watchlist_date
ON daily_watchlist(date);

저장/로드 메서드 추가:

def save_daily_watchlist(
    self, date: str, stocks: list[dict],
    source: str = "condition_search",
) -> None:
    """날짜별 watchlist 저장 (종목 코드+이름).

    Args:
        date: 대상 날짜 (YYYY-MM-DD)
        stocks: [{"code": "005930", "name": "삼성전자"}, ...]
        source: 소스 태그
    """
    import json
    self._ensure_connection()
    try:
        self.conn.execute(
            "INSERT OR REPLACE INTO daily_watchlist "
            "(date, stocks, source, stock_count) VALUES (?, ?, ?, ?)",
            (date, json.dumps(stocks, ensure_ascii=False), source, len(stocks)),
        )
        self.conn.commit()
        logger.info(
            f"daily_watchlist 저장: {date} ({len(stocks)}종목, source={source})"
        )
    except Exception as e:
        logger.error(f"daily_watchlist 저장 실패 ({date}): {e}")
        raise


def load_daily_watchlist(self, date: str) -> list[dict] | None:
    """특정 날짜 watchlist 로드.

    Returns:
        [{"code": "005930", "name": "삼성전자"}, ...] 또는 None.
    """
    import json
    self._ensure_connection()
    try:
        cursor = self.conn.execute(
            "SELECT stocks FROM daily_watchlist WHERE date = ?",
            (date,),
        )
        row = cursor.fetchone()
        if row and row[0]:
            stocks = json.loads(row[0])
            logger.info(f"daily_watchlist 로드: {date} ({len(stocks)}종목)")
            return stocks
        return None
    except Exception as e:
        logger.error(f"daily_watchlist 로드 실패 ({date}): {e}")
        return None

## 3. src/engine.py — _evening_watchlist_screening 메서드 추가

_post_market_cleanup 메서드 근처에 새 메서드 추가:

async def _evening_watchlist_screening(self):
    """장마감 후 조건검색 → 다음 거래일 watchlist를 DB에 저장.

    실행 시점: 15:40 (장마감 10분 후)
    조건검색 실패 시 저장 스킵 → 다음날 고정 watchlist 폴백.
    """
    logger.info("저녁 조건검색 시작 (다음 거래일 watchlist 생성)")

    watchlist_mode = config.get("watchlist_mode", "fixed")
    if watchlist_mode != "condition":
        logger.info("watchlist_mode != 'condition' → 저녁 스크리닝 건너뜀")
        return

    cs_config = config.get("condition_search", {})
    condition_name = cs_config.get("condition_name", "swing_pre_cross")
    max_stocks = cs_config.get("max_stocks", 30)
    ws_url = config.get("kiwoom.ws_url", "")

    if not ws_url:
        logger.error("kiwoom.ws_url 미설정 → 저녁 스크리닝 중단")
        return

    # 토큰 확인
    try:
        await asyncio.to_thread(self._ensure_connection)
    except Exception as e:
        logger.warning(f"저녁 스크리닝 토큰 갱신 실패: {e}")

    # 조건검색 실행
    try:
        from src.broker.condition_search import run_condition_search
        stocks = await run_condition_search(
            ws_url=ws_url,
            access_token=self._kiwoom.access_token,
            condition_name=condition_name,
        )
    except Exception as e:
        logger.error(f"저녁 조건검색 호출 실패: {e}", exc_info=True)
        stocks = []

    if not stocks:
        logger.warning("저녁 조건검색 결과 없음 → 저장 스킵")
        try:
            self._telegram.send(
                "⚠️ 저녁 스크리닝 결과 없음\n"
                "내일 아침 고정 watchlist 사용 예정"
            )
        except Exception:
            pass
        return

    # 상한 적용
    if len(stocks) > max_stocks:
        logger.info(
            f"저녁 조건검색 {len(stocks)}개 → 상위 {max_stocks}개 제한"
        )
        stocks = stocks[:max_stocks]

    # 다음 영업일 계산
    from datetime import datetime, timedelta
    today = datetime.now()
    next_day = today + timedelta(days=1)
    while next_day.weekday() >= 5:  # 5=토, 6=일
        next_day += timedelta(days=1)
    next_date_str = next_day.strftime("%Y-%m-%d")

    # DB 저장
    try:
        self._ds.save_daily_watchlist(
            date=next_date_str,
            stocks=stocks,
            source="condition_search",
        )
        logger.info(
            f"저녁 조건검색 완료: {len(stocks)}종목 → {next_date_str} 저장"
        )

        # 텔레그램 알림 (종목명 포함)
        sample_lines = []
        for s in stocks[:5]:
            name = s.get("name") or "?"
            code = s.get("code", "")
            sample_lines.append(f"• {name} ({code})")
        
        more = f"\n... 외 {len(stocks) - 5}종목" if len(stocks) > 5 else ""
        sample_text = "\n".join(sample_lines)

        try:
            self._telegram.send(
                f"🌙 저녁 스크리닝 완료\n"
                f"대상일: {next_date_str}\n"
                f"매칭: {len(stocks)}종목\n\n"
                f"{sample_text}{more}"
            )
        except Exception:
            pass

    except Exception as e:
        logger.error(f"watchlist DB 저장 실패: {e}", exc_info=True)

## 4. src/engine.py — _pre_market_screening 수정

기존 _pre_market_screening의 watchlist 로드 부분을 수정.
조건검색 직접 호출 코드를 제거하고, DB 우선 로드로 변경:

watchlist_mode = config.get("watchlist_mode", "fixed")

if watchlist_mode == "condition":
    # 1. 오늘 날짜로 DB에서 로드 (어제 저녁 저장된 것)
    from datetime import datetime
    today_str = datetime.now().strftime("%Y-%m-%d")
    saved_stocks = self._ds.load_daily_watchlist(today_str)

    if saved_stocks:
        logger.info(
            f"watchlist 모드: DB 로드 (어제 저녁 저장) — {len(saved_stocks)}종목"
        )
        watchlist = [s["code"] for s in saved_stocks]
        self._candidates = set(watchlist)
        
        # 종목명 캐시 미리 채움 (polling 시점부터 종목명 사용 가능)
        for s in saved_stocks:
            code = s.get("code", "")
            name = s.get("name", "")
            if code and name:
                self._poll_stock_names[code] = name

        # 텔레그램 알림 (종목명 포함)
        sample_lines = []
        for s in saved_stocks[:5]:
            name = s.get("name") or "?"
            code = s.get("code", "")
            sample_lines.append(f"• {name} ({code})")
        more = f"\n... 외 {len(saved_stocks) - 5}종목" if len(saved_stocks) > 5 else ""
        sample_text = "\n".join(sample_lines)

        try:
            self._telegram.send(
                f"📋 watchlist 로드 완료\n"
                f"소스: 어제 저녁 조건검색\n"
                f"종목: {len(watchlist)}개\n\n"
                f"{sample_text}{more}"
            )
        except Exception:
            pass
    else:
        # 2. DB에 없으면 폴백
        logger.warning(
            f"오늘 날짜({today_str}) watchlist DB에 없음 (첫날 또는 실패)"
        )
        cs_config = config.get("condition_search", {})
        if cs_config.get("fallback_to_fixed", True):
            watchlist = config.get("watchlist", [])
            self._candidates = set(watchlist)
            logger.info(f"고정 watchlist 폴백: {len(watchlist)}종목")
            try:
                self._telegram.send(
                    f"⚠️ DB watchlist 없음 → 고정 {len(watchlist)}종목 사용"
                )
            except Exception:
                pass
        else:
            logger.error("폴백 비활성 → 스크리닝 중단")
            return
else:
    # 기존 동작: 고정 watchlist
    watchlist = config.get("watchlist", [])
    self._candidates = set(watchlist)
    logger.info(f"고정 watchlist: {len(watchlist)}종목")

# 이하 기존 OHLCV 프리로드 로직 그대로 (watchlist 변수 사용)

**중요**: 기존 _pre_market_screening에 있던 조건검색 직접 호출 코드
(run_condition_search 호출 + telegram 알림 블록)는 **완전히 제거**해야 함.
아침 스크리닝은 더 이상 조건검색 실행 안 함, DB 로드만.

## 5. src/engine.py — 스케줄 등록

start() 메서드에서 _post_market_cleanup 등록 직후 추가:

# 저녁 조건검색 스케줄 (15:40 — 장마감 10분 후)
self._scheduler.add_job(
    self._make_safe_job(self._evening_watchlist_screening, "저녁스크리닝"),
    "cron",
    hour=15, minute=40,
    misfire_grace_time=3600,
)

## 6. config.yaml — 스케줄 시간 추가 (참고용)

schedule 섹션에:

schedule:
  screening_time: "08:30"
  polling_start_time: "09:25"
  polling_stop_time: "15:35"
  evening_screening_time: "15:40"   # 신규 (참고용, 코드는 하드코딩)
  daily_report_time: "16:00"

## 7. 검증

### 7-1. 문법 체크
python -c "from src.broker.condition_search import run_condition_search; print('OK')"
python -c "from src.datastore import DataStore; ds = DataStore(); ds.create_tables(); print('OK')"

### 7-2. 저장/로드 단위 테스트
python -c "
from src.datastore import DataStore
ds = DataStore()
test_stocks = [
    {'code': '005930', 'name': '삼성전자'},
    {'code': '000660', 'name': 'SK하이닉스'},
]
ds.save_daily_watchlist('2099-01-01', test_stocks)
loaded = ds.load_daily_watchlist('2099-01-01')
print('Loaded:', loaded)
assert loaded == test_stocks
ds.conn.execute(\"DELETE FROM daily_watchlist WHERE date='2099-01-01'\")
ds.conn.commit()
print('OK')
"

### 7-3. 저녁 스크리닝 수동 트리거 (오늘 즉시 테스트)
python -c "
import asyncio
from src.engine import TradingEngine
async def test():
    engine = TradingEngine(mode='paper')
    engine._ensure_connection()
    await engine._evening_watchlist_screening()
asyncio.run(test())
"

로그 확인:
- '[CS] 조건식 실행 완료: N종목'
- 'daily_watchlist 저장: YYYY-MM-DD'
- 텔레그램에 종목명 포함된 메시지

### 7-4. DB 확인
sqlite3 swing.db "SELECT date, stock_count FROM daily_watchlist;"

### 7-5. 아침 로드 시뮬레이션 (수동 스크리닝 버튼)
GUI에서 "스크리닝" 버튼 클릭 → 로그/텔레그램 확인

## 8. 커밋

git add -A
git commit -m "feat: 저녁 조건검색 + 종목명 통합

문제:
- swing_pre_cross 조건식은 0봉전 데이터 사용 → 장전 0건
- 텔레그램 알림에 종목 코드만 표시 (가독성 낮음)

해결:
- 15:40 저녁 조건검색 → 다음날 watchlist DB 저장
- 08:30 아침 스크리닝은 DB 로드만 (조건검색 직접 호출 제거)
- 조건검색 응답의 9001(코드)+302(종목명) 함께 추출
- daily_watchlist 테이블에 stocks JSON으로 저장
- 종목명 캐시(_poll_stock_names) 미리 채움
- 텔레그램 메시지에 종목명 포함

변경:
- src/broker/condition_search.py: execute_condition list[dict] 반환
- src/datastore.py: daily_watchlist 테이블 + save/load 메서드
- src/engine.py: _evening_watchlist_screening 신규
- src/engine.py: _pre_market_screening DB 우선 로드로 변경
- src/engine.py: 15:40 저녁 스크리닝 스케줄 등록"
git push

## 9. 트러블슈팅

### 첫 운영일
- DB 비어있음 → 고정 watchlist 폴백 (정상)
- 그날 15:40 스크리닝 → 다음날부터 정상

### 종목명이 안 나오는 경우
- 응답 키 '302'가 다를 수 있음
- logs에서 'CNSRREQ raw' 확인 후 키 이름 추가

### 15:40 스케줄 안 돌아감
- '스케줄 실행: 저녁스크리닝' 로그 확인
- _make_safe_job 사용 확인
```
