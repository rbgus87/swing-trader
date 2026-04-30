# scripts/

## 운영 스크립트 (정기 실행)

| 스크립트 | 빈도 | 용도 |
|---------|------|------|
| `daily_run.sh`           | 매일 16:00 이후 | 일일 데이터 갱신 + 시그널 생성 |
| `daily_update.sh`        | 매일       | 시세·시총 갱신 |
| `update_delisting.py`    | 월 1회     | 상장폐지 종목 추가 (data.krx.co.kr 다운로드 후 실행) |

### update_delisting.py — 상장폐지 갱신

```bash
# 1. data.krx.co.kr → 상장폐지현황 → xls 다운로드
# 2. data/raw/delisting/상장폐지현황.xls 에 덮어쓰기
# 3. 실행 (cmd / PowerShell / Git Bash / WSL 모두 동일)
python scripts/update_delisting.py

# 옵션
python scripts/update_delisting.py --skip-candles   # xls 파싱만
python scripts/update_delisting.py --skip-infer     # 추론 생략
```

내부 흐름 (3단계, 모두 멱등):
1. `import_delisted_list.main()`  — xls 파싱 → stocks 테이블 INSERT/UPDATE (cutoff 2014-01-01)
2. `collect_daily_candles.main()` — 새 폐지 종목의 일봉 OHLCV 수집 (생존편향 제거)
3. `infer_delisted.infer_delisted()` — 마지막 거래일 + 임계값으로 `delisted_date` 역산

## 검증 도구

- **`../selftest.py` (루트)** — 환경/의존성 무결성 검증 (~6초)
  ```bash
  python selftest.py             # 직접 실행
  python gui.py --selftest       # GUI 진입 전 검증 후 exit
  ```
  9단계: 모듈 import / 지표 계산 / config / 두 DB / 엔진·백테스터 import / Kiwoom·Telegram 네트워크. exit code 0 = 모두 OK, 1 = FAIL 1건+. 빌드 직후 `python build_exe.py`가 자동으로 호출.

- `run_walk_forward.py` — Walk-Forward 검증 (파라미터 변경 후 / 분기 1회)
- `verify_fdr_capabilities.py`, `verify_krx_split_adjustment.py`, `verify_ticker_reuse.py` — 데이터 정합성 검증

## 일회성 도구

- `reset_paper_positions.py` — paper 모드 포지션·매매·일일 성과 초기화 (전략 사양 변경 시)

## 탐색 스크립트 (참고용)

- `explore_dart_corp_code.py`, `explore_fdr_delisting.py`, `explore_krx_endpoints.py`, `explore_krx_historical_bydd.py` — Phase 1 단계 데이터 소스 탐색 흔적. 새 데이터 소스 검토 시 패턴 참고.

## archived/

개발 과정에서 사용한 실험·비교 스크립트. 실전 운용에 불필요.
파라미터 튜닝이나 전략 비교가 필요하면 참고용으로 사용 가능.
