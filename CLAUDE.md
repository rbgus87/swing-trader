# CLAUDE.md — swing-trader

## 현재 상태: Phase 1 완료 (2026-04-18). Phase 2 진입 준비.

이 프로젝트는 전면 재설계 중입니다. 기존 전략과 운영 방식은 모두 archive되었고,
Phase 0 기획에 따라 데이터 파이프라인부터 새로 구축합니다.

## Phase 1: 데이터 파이프라인 ✅ 완료 (2026-04-14)

### 최종 데이터 현황

| 테이블 | 행 수 | 범위 | 비고 |
|--------|-------|------|------|
| stocks | 3,444 | - | Active 2,736 + Delisted 708 |
| daily_candles | 7.03M | 2014-01~2026-04 | 수정주가, FDR 기반 |
| market_cap_history | 7.03M | 2014-01~2026-04 | KRX bydd_trd 기반 |
| stock_status_events | 135 | - | ADMIN 80 + WARNING 50 + PRIOR 5 |
| collection_log | 3,389 | - | SUCCESS 3,211 + PARTIAL 178 |

### 데이터 소스

- 일봉 OHLCV: FinanceDataReader (Naver 기반 수정주가)
- 종목 메타/시총: KRX OpenAPI (openapi.krx.co.kr)
- 폐지 종목: data.krx.co.kr 수동 다운로드 (상장폐지현황.xls)

### 알려진 한계

- 폐지 종목 market='UNKNOWN' (KRX 메타 미제공, SQLite ALTER COLUMN 미지원)
- sector 전종목 NULL (KRX OpenAPI 미제공, Phase 5 이후 보강)
- 폐지 종목 2% 누락 (KRX historical snapshot 미반환 소형주)
- 생존편향 제거 완료 (2014~2026, 708종목 폐지 이력 포함)
- 티커 재사용 0건 확인 (003620/097230 = 단순 상호변경)

### 갱신 절차

- 일봉/시총 일일 갱신: Phase 2 이후 구축 (Step 4b)
- 폐지 종목 갱신: 월 1회 `bash scripts/update_delisting.sh`

### 정합성 검증 (Step 4a)

- 수정주가 연속성: 3종목 분할 케이스 PASS
- Universe Pool 시뮬레이션: 43종목 (2020-01-02 기준, 시총 5조+거래대금 100억)
- 지표 계산 (SMA/RSI/ATR): 5종목 inf/NaN/zero 없음 PASS
- ERROR급 이상치: 0건

## Phase 0 결정사항 (불변)

- 시스템 정체성: 중기 스윙, 완전 자동, 자본 500만원
- 알파 본체: 종목 선별 (시장 타이밍 아님)
- 시장 국면: Soft Weight 가드레일만
- 아키텍처: 단일 프로세스, 4-레이어 (Regime → Router → Strategy → PM)
- 동시 보유: 4종목, 전략별 2자리 고정
- 데이터: 자체 스크리너 only, 전종목 일봉 12년 신규 수집
- 개발 순서: TrendFollowing 먼저, MeanReversion 후

## 진행 단계

- [x] Phase 0: 기획·아키텍처 결정
- [x] Phase 1: 데이터 파이프라인 ✅
- [x] Phase 2: TrendFollowing 전략 설계 ✅ (2026-04-18)
- [ ] Phase 3: Engine 4-레이어 재구축 ← 다음
- [ ] Phase 4: 페이퍼 트레이딩
- [ ] Phase 5: 보완 전략 추가

## Phase 2: TrendFollowing 전략 설계 ✅ 완료 (2026-04-18)

### 확정 전략 — TrendFollowing v1

**사양**:
- Universe: 시총 3조+, 거래대금 50억+, 60일 갱신
- 스크리너: MA5>MA20>MA60 + ADX≥20
- 진입: 60일 신고가 + 거래량 1.5x → 익일 시가
- 청산: SL ATR×1.5 / TP1 ATR×2.0(50%) / Trail ATR×3.0 / 15일 / MA5<MA20
- 가드레일: breadth ≥ 0.40 (Universe MA200 기반)
- 비용: 왕복 0.31%
- 포트폴리오: 4종목 동시, 종목당 자본 25%, 최소 30만원

**백테스트 성과 (2014~2026, 12년)**:
- PF 0.97 / CAGR +1.4% / MDD 41% / WR 49% / 864건

### 실험 이력

**TrendFollowing**:
- v0 baseline (SL 2.0/TP 3.0/Trail 2.5): PF 0.85, 대형주 손실
- v0.1 파라미터 (SL 1.5/TP 2.0/Trail 3.0): PF 0.97로 개선 → 기준 확정
- 포트폴리오 엔진 버그 수정: 100만원 배분 데드락 → `cash × (1/max_positions)`·최소 30만원
- 가드레일 breadth 50% → 40%: 2020 반등 포착
- 모멘텀 필터 (60일 수익률 상위 50%): 후행 편향 → 폐기
- Squeeze 필터 (BB 밴드폭 하위 20%): 과제한 + 강세장 역편향 → 폐기
- 시총 1조 / 중형주(2000억~10조): 노이즈·MDD 악화 → 폐기

**MeanReversion**:
- v0 (RSI<30 + BB 하단 + MA60 위): 12년 6건, 판단 불가
- v0.1 (RSI<40 + BB 중심선 아래 + 1일 반등): PF 0.83, 단독 엣지 없음
- v0.2 (2일 연속 반등): PF 0.71, PANIC 반감하나 CAGR 악화
- TF+MR 합산 (TF 2슬롯 + MR 2슬롯): PF 0.70, CAGR −7.4%, TF 단독보다 악화
- **결론: 대형주 Universe에서 평균회귀 구조적 부적합** → 폐기
- 코드 → `archive/strategies_legacy/mean_reversion/`

### 핵심 교훈

1. 종목 선별이 전략보다 중요 (단일 종목 PF 2.0+ vs 포트폴리오 PF 0.97)
2. 필터 추가는 거래 수 감소 → 통계 약화 → 역효과
3. 시장 가드레일(breadth)은 하락장 방어에 유효
4. PF 0.97은 실전 운용 시작 후 개선이 합리적

### 다음 단계

- Phase 3: 엔진 4-레이어 재구축
- Phase 4: 페이퍼 트레이딩
- Phase 5: 보완 전략 재검토 (실전 데이터 기반)

## Phase 1 Step 1b-3 메모

상장폐지 종목 수집 경로 탐색 결과:
- KRX OpenAPI (sto/, dis/, gen/ 네임스페이스): 18개 후보 경로 모두 404. 폐지 엔드포인트 부재 확인.
- DART corpCode.xml: 폐지일 필드 없음. 폐지 법인은 마스터에서 제거되어 이력 추적 불가.
- FDR StockListing('KRX-DELISTING'): 소스에 필요한 모든 필드 존재(DELIST_DD, DELIST_RSN_DSC, TO_ISU_SRT_CD) 확인. 단 2024~2025 KRX 정책 변경으로 비인증 접근 차단, 현재 LOGOUT 에러.
- KRX Data Marketplace MDCSTAT23801: 인증된 접근 시 완벽한 데이터 제공 가능 (별도 계정 + 상품 신청 필요).

채택 방침 (2026-04-14): Step 2 일봉 수집 후 "최종 거래일 + 임계값" 역산으로 delisted_date 근사. 정확도는 월 단위. Universe 필터링 목적에는 충분. KRX Marketplace 계정 확보 시 재수집 권장.

티커 재사용 감지도 현재 자동화 불가. 수동 관리 또는 KRX Marketplace 확보 후 TO_ISU_SRT_CD 필드 활용으로 해결 예정.

## Archive 정책

archive/strategies_legacy/ 의 10개 전략은 **참고 자료**입니다.
- 새 전략 설계 시 "이전엔 어떻게 했나" 참고용
- 절대 import 하지 말 것
- 코드 재사용은 명시적 결정 후 src/strategy/로 복사 (이동 아님)

## 옛 절대규칙 무효화

이전 CLAUDE.md에 있던 다음 규칙들은 Phase 1 재설계로 무효:
- "golden_cross / disparity_reversion 확정 전략 보호" → 둘 다 archive
- "adaptive 모드 보존" → 4-레이어 구조로 대체
- (기타 옛 규칙들)
