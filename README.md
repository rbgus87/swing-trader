# swing-trader

한국 주식 중기 스윙 자동매매 시스템.

## 현재 상태

- **Phase 1 완료** — 데이터 파이프라인 구축 (2026-04-14)
- **Phase 2 진행 중** — TrendFollowing 전략 설계

## 데이터 현황

| 테이블 | 행 수 | 범위 |
|--------|-------|------|
| stocks | 3,444 | Active 2,736 + Delisted 708 |
| daily_candles | 7.03M | 2014~2026, 수정주가 |
| market_cap_history | 7.03M | 2014~2026 |

## 아키텍처

- 4-레이어: Regime Detector → Strategy Router → Strategy Modules → Portfolio Manager
- 동시 보유: 4종목 (전략별 2자리)
- 자본: 500만 원
- 데이터 소스: FDR (일봉) + KRX OpenAPI (메타/시총)

## 실행

```bash
# 데이터 초기화
python db/init_db.py
python src/data_pipeline/collect_stocks_meta.py
python src/data_pipeline/collect_daily_candles.py
python src/data_pipeline/collect_market_cap.py

# 폐지 종목 갱신 (월 1회)
python scripts/update_delisting.py

# 정합성 검증
python src/data_pipeline/data_integrity_check.py
```

## 프로젝트 구조

```
swing-trader/
├── CLAUDE.md              ← 프로젝트 컨텍스트 (핵심 문서)
├── config.yaml            ← 설정
├── db/                    ← 스키마 + 마이그레이션
├── src/
│   ├── data_pipeline/     ← Phase 1 데이터 레이어 (유효)
│   ├── strategy/          ← Phase 2 전략 (진행 중)
│   ├── backtest/          ← Phase 2 백테스트 (진행 중)
│   ├── broker/            ← 키움 API (Phase 4)
│   ├── risk/              ← 리스크 관리 (Phase 3)
│   ├── gui/               ← GUI (후순위)
│   ├── notification/      ← 텔레그램
│   └── utils/             ← 공용 유틸
├── scripts/               ← 운영·검증 스크립트
├── docs/                  ← 인프라 명세
├── data/raw/              ← 수집 원본 파일
└── archive/               ← Phase 0/1 이전 자산
```

## 상세 문서

- `CLAUDE.md` — Phase 0 결정사항, Phase 1 완료 현황, 진행 단계
- `docs/` — 키움·리스크·텔레그램·운영 명세
