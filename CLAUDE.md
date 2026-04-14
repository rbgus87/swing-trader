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
  - [ ] KRX 종목 메타 수집
  - [ ] 일봉 OHLCV 수집
  - [ ] 시총·이벤트 수집
  - [ ] 정합성 검증
- [ ] Phase 2: TrendFollowing 전략 신규 설계
- [ ] Phase 3: Engine 4-레이어 재구축
- [ ] Phase 4: TrendFollowing 백테스트·페이퍼
- [ ] Phase 5: MeanReversion 전략 추가

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
