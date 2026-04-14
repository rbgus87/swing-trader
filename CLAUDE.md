# CLAUDE.md — swing-trader

## 현재 상태: Phase 1 재설계 진행 중 (시작: 2026-04-14)

이 프로젝트는 전면 재설계 중입니다. 기존 전략과 운영 방식은 모두 archive되었고,
Phase 0 기획에 따라 데이터 파이프라인부터 새로 구축합니다.

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
- [ ] Phase 1: 데이터 파이프라인 (현재 진행)
  - [x] 백업 + 코드 정리
  - [x] Step 1a: 종목 메타 수집 (2,773종목, KOSPI 950 + KOSDAQ 1,823)
  - [x] Step 1b-1: base_info 보강 (listed_date, ISIN 100%)
  - [x] Step 1b-2: sector 정정 (market_division 컬럼 신설)
  - [x] Step 1b-2b: market_division cleanup + FOREIGN type 도입
  - [ ] Step 1b-3: 상장폐지 데이터 — KRX Data Marketplace 차단 이슈로 Step 2에서 역산 처리 예정
  - [ ] Step 2: 일봉 OHLCV 수집 (FDR 기반, 12년, 2,773종목)
  - [ ] Step 3: 시총 이력 수집 (KRX OpenAPI)
  - [ ] Step 4: 상태 이벤트 확장 + 정합성 검증
  - [ ] Step 1b-3 후보강: KRX Marketplace 계정 확보 시 폐지 데이터 교체
- [ ] Phase 2: TrendFollowing 전략 신규 설계
- [ ] Phase 3: Engine 4-레이어 재구축
- [ ] Phase 4: TrendFollowing 백테스트·페이퍼
- [ ] Phase 5: MeanReversion 전략 추가

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
