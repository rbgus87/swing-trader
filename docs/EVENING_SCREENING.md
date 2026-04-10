# EVENING_SCREENING.md — 저녁 조건검색 → 다음날 watchlist 생성

> 장마감 후 15:40에 조건검색 실행 → DB에 저장 → 다음날 아침에 로드
> 이유: `swing_pre_cross` 조건식은 오늘 종가/시가/거래량(0봉전) 데이터를 사용하므로
> 장 시작 전에는 0건 반환. 장 마감 후가 유일하게 데이터가 확정되는 시점.

## 운용 흐름

```
[금요일 15:40] 저녁 조건검색 실행
   ↓ 결과 30종목을 DB daily_watchlist에 저장 (date=월요일)
   ↓
[월요일 08:30] 장전 스크리닝
   ↓ DB에서 월요일 watchlist 로드 (어제 저녁 저장된 것)
   ↓ OHLCV 프리로드
   ↓
[월요일 09:25] polling 시작 → 매매
   ↓
[월요일 15:40] 저녁 조건검색 → DB에 화요일 watchlist 저장
```

## 첫 운영일 처리

DB에 아직 저녁 스크리닝 결과가 없는 첫날은 **고정 watchlist 폴백**으로 처리.
이후 매일 저녁 스크리닝이 쌓이면서 자동으로 정상 동작.

---

## 프롬프트

```
CLAUDE.md를 읽어줘.
그리고 docs/EVENING_SCREENING.md를 읽어줘.

조건검색 실행 시점을 장 시작 전(08:30)에서 장 마감 후(15:40)로 변경해줘.
장 마감 후 조건검색 결과를 DB에 저장하고, 다음날 아침 스크리닝이
DB에서 watchlist를 로드하는 구조로 바꾼다.

이유: swing_pre_cross 조건식은 0봉전(오늘) 시가/종가/거래량 데이터를 사용하므로
장 시작 전에는 0건 반환. 장 마감 후가 데이터 확정되는 유일한 시점.

## 1. src/datastore.py — daily_watchlist 테이블 추가

### 1-1. 테이블 스키마 추가

create_tables() 메서드 안에 새 테이블 추가:

CREATE TABLE IF NOT EXISTS daily_watchlist (
    date TEXT PRIMARY KEY,              -- YYYY-MM-DD
    codes TEXT NOT NULL,                 -- JSON array: ["005930", ...]
    source TEXT DEFAULT 'condition_search',
    stock_count INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_daily_watchlist_date
ON daily_watchlist(date);

### 1-2. 저장/로드 메서드 추가

save_daily_performance 메서드 근처에 다음 두 메서드 추가:

def save_daily_watchlist(
    self, date: str, codes: list[str],
    source: str = "condition_search",
) -> None:
    """날짜별 watchlist 저장. 같은 날짜면 덮어쓰기.

    Args:
        date: 대상 날짜 (YYYY-MM-DD)
        codes: 종목 코드 리스트
        source: 소스 태그 (기본 'condition_search')
    """
    import json
    self._ensure_connection()
    try:
        self.conn.execute(
            "INSERT OR REPLACE INTO daily_watchlist "
            "(date, codes, source, stock_count) VALUES (?, ?, ?, ?)",
            (date, json.dumps(codes), source, len(codes)),
        )
        self.conn.commit()
        logger.info(f"daily_watchlist 저장: {date} ({len(codes)}종목, source={source})")
    except Exception as e:
        logger.error(f"daily_watchlist 저장 실패 ({date}): {e}")
        raise


def load_daily_watchlist(self, date: str) -> list[str] | None:
    """특정 날짜 watchlist 로드.

    Args:
        date: 대상 날짜 (YYYY-MM-DD)

    Returns:
        종목 코드 리스트. 없으면 None.
    """
    import json
    self._ensure_connection()
    try:
        cursor = self.conn.execute(
            "SELECT codes FROM daily_watchlist WHERE date = ?",
            (date,),
        )
        row = cursor.fetchone()
        if row and row[0]:
            codes = json.loads(row[0])
            logger.info(f"daily_watchlist 로드: {date} ({len(codes)}종목)")
            return codes
        return None
    except Exception as e:
        logger.error(f"daily_watchlist 로드 실패 ({date}): {e}")
        return None

## 2. src/engine.py — _evening_watchlist_screening 메서드 추가

_post_market_cleanup 메서드 근처에 새 메서드 추가:

async def _evening_watchlist_screening(self):
    """장마감 후 조건검색 실행 → 다음 거래일 watchlist를 DB에 저장.

    실행 시점: 15:40 (장마감 10분 후, 데이터 확정)
    대상 키: 다음 영업일 날짜 (주말 건너뜀)
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

    # 토큰 확인 (만료 대비)
    try:
        await asyncio.to_thread(self._ensure_connection)
    except Exception as e:
        logger.warning(f"저녁 스크리닝 토큰 갱신 실패: {e}")

    # 조건검색 실행
    try:
        from src.broker.condition_search import run_condition_search
        codes = await run_condition_search(
            ws_url=ws_url,
            access_token=self._kiwoom.access_token,
            condition_name=condition_name,
        )
    except Exception as e:
        logger.error(f"저녁 조건검색 호출 실패: {e}", exc_info=True)
        codes = []

    if not codes:
        logger.warning("저녁 조건검색 결과 없음 → 저장 스킵 (다음날 폴백 예정)")
        try:
            self._telegram.send(
                "⚠️ 저녁 스크리닝 결과 없음\n"
                "내일 아침 고정 watchlist 사용 예정"
            )
        except Exception:
            pass
        return

    # 상한 적용
    if len(codes) > max_stocks:
        logger.info(
            f"저녁 조건검색 {len(codes)}개 → 상위 {max_stocks}개 제한"
        )
        codes = codes[:max_stocks]

    # 다음 영업일 계산 (주말 건너뛰기)
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
            codes=codes,
            source="condition_search",
        )
        logger.info(
            f"저녁 조건검색 완료: {len(codes)}종목 → {next_date_str} watchlist 저장"
        )
        try:
            self._telegram.send(
                f"🌙 저녁 스크리닝 완료\n"
                f"대상일: {next_date_str}\n"
                f"매칭: {len(codes)}종목\n"
                f"샘플: {', '.join(codes[:5])}"
                + ("..." if len(codes) > 5 else "")
            )
        except Exception:
            pass
    except Exception as e:
        logger.error(f"watchlist DB 저장 실패: {e}", exc_info=True)

## 3. src/engine.py — _pre_market_screening 수정

기존 _pre_market_screening의 watchlist 로드 부분 수정.
조건검색 모드일 때 DB 우선 로드로 변경:

watchlist_mode = config.get("watchlist_mode", "fixed")

if watchlist_mode == "condition":
    # 1. 오늘 날짜로 DB에서 먼저 시도 (어제 저녁 저장된 것)
    from datetime import datetime
    today_str = datetime.now().strftime("%Y-%m-%d")
    saved_codes = self._ds.load_daily_watchlist(today_str)

    if saved_codes:
        logger.info(
            f"watchlist 모드: DB 로드 (어제 저녁 저장) — {len(saved_codes)}종목"
        )
        watchlist = saved_codes
        self._candidates = set(saved_codes)
        try:
            self._telegram.send(
                f"📋 watchlist 로드 완료\n"
                f"소스: 어제 저녁 조건검색\n"
                f"종목: {len(watchlist)}개"
            )
        except Exception:
            pass
    else:
        # 2. DB에 없으면 폴백
        logger.warning(
            f"오늘 날짜({today_str}) watchlist가 DB에 없음 "
            "(첫 운영일 또는 저녁 스크리닝 실패)"
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

# 이하 OHLCV 프리로드 로직 그대로 (watchlist 변수 사용)

**중요**: 기존에 _pre_market_screening에서 조건검색을 직접 호출하던 코드
(run_condition_search 호출 블록)는 **제거**해야 함.
아침 스크리닝에서는 더 이상 조건검색을 직접 호출하지 않고 DB만 읽음.

## 4. src/engine.py — 스케줄 등록

start() 메서드에서 _post_market_cleanup 스케줄 등록 직후에 추가:

# 저녁 조건검색 스케줄 (15:40 — 장마감 10분 후)
# 다음 거래일 watchlist를 DB에 저장
self._scheduler.add_job(
    self._make_safe_job(self._evening_watchlist_screening, "저녁스크리닝"),
    "cron",
    hour=15, minute=40,
    misfire_grace_time=3600,
)

## 5. config.yaml — 스케줄 시간 추가

schedule 섹션에 evening_screening_time 추가:

schedule:
  screening_time: "08:30"           # 장전 스크리닝 (DB 로드)
  polling_start_time: "09:25"
  polling_stop_time: "15:35"
  evening_screening_time: "15:40"   # 신규: 저녁 조건검색 (다음날 watchlist 생성)
  daily_report_time: "16:00"

(주의: config에만 추가하고 engine.py의 스케줄러는 hour=15, minute=40 하드코딩 OK.
config에서 동적 로드하려면 parse 로직 추가 필요하지만 이번엔 단순화)

## 6. 검증

### 6-1. DB 테이블 생성 확인

python -c "
from src.datastore import DataStore
ds = DataStore()
ds.create_tables()
cursor = ds.conn.execute(\"SELECT name FROM sqlite_master WHERE type='table' AND name='daily_watchlist'\")
print('daily_watchlist table:', cursor.fetchone())
"

### 6-2. 저장/로드 단위 테스트

python -c "
from src.datastore import DataStore
ds = DataStore()
ds.save_daily_watchlist('2026-04-10', ['005930', '000660', '005380'])
loaded = ds.load_daily_watchlist('2026-04-10')
print('Loaded:', loaded)
assert loaded == ['005930', '000660', '005380']
print('OK')
"

### 6-3. 수동 테스트 시나리오

**Step 1: 저녁 스크리닝 수동 트리거**
프로그램 시작 후, 파이썬 인터프리터나 디버그 스크립트로:

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
- '저녁 조건검색 시작'
- '[CS] 조건식 실행 완료: N종목'
- 'daily_watchlist 저장: YYYY-MM-DD (N종목, source=condition_search)'
- '저녁 조건검색 완료'
- 텔레그램 알림

**Step 2: DB 확인**
sqlite3 swing.db "SELECT date, stock_count, source FROM daily_watchlist;"

**Step 3: 아침 스크리닝 로드 확인**
GUI에서 "스크리닝" 버튼 클릭 → 로그:
- 'watchlist 모드: DB 로드 (어제 저녁 저장) — N종목'

### 6-4. 폴백 테스트
DB에서 오늘 날짜 삭제 후 수동 스크리닝:
sqlite3 swing.db "DELETE FROM daily_watchlist WHERE date='2026-04-10';"

로그:
- 'DB에 없음 → 고정 watchlist 폴백'

## 7. 커밋

git add -A
git commit -m "feat: 저녁 조건검색 → 다음날 watchlist DB 저장

문제:
- swing_pre_cross 조건식은 0봉전(오늘) 데이터 사용
- 장 시작 전(08:30)에는 오늘 데이터 없어 0건 반환
- 조건검색이 의미있는 결과를 내는 유일한 시점은 장 마감 후

해결:
- 15:40 (장마감 10분 후) 저녁 조건검색 스케줄 추가
- 결과를 daily_watchlist 테이블에 다음 영업일 날짜로 저장
- 08:30 아침 스크리닝은 DB에서 오늘 날짜 watchlist 로드
- DB에 없으면 (첫 운영일 또는 실패) 고정 watchlist 폴백

변경:
- src/datastore.py: daily_watchlist 테이블 + save/load 메서드
- src/engine.py: _evening_watchlist_screening 추가
- src/engine.py: _pre_market_screening DB 우선 로드로 변경
- src/engine.py: 15:40 저녁 스크리닝 스케줄 등록
- config.yaml: evening_screening_time 추가"
git push

## 8. 트러블슈팅

### 문제: 첫 운영일 아침에 DB 비어있음
- 정상 동작. 고정 watchlist 폴백으로 진행.
- 그날 15:40에 저녁 스크리닝 실행되면 다음날부터 정상.

### 문제: 15:40에 저녁 스크리닝 안 돌아감
- logs에서 "스케줄 실행: 저녁스크리닝" 확인
- 없으면 스케줄 등록 실패 → _make_safe_job 사용 확인

### 문제: 저장은 됐는데 아침에 로드 안 됨
- 날짜 포맷 불일치 의심 (YYYY-MM-DD 통일)
- sqlite3 swing.db "SELECT date FROM daily_watchlist ORDER BY date DESC LIMIT 5"
- 오늘 날짜와 일치하는지 확인
```
