# Archive

Phase 1 재설계(2026-04-14) 시점에 격리된 자산.
무엇이 왜 여기 있는지, 언제 정리 가능한지 기록.

## 디렉토리 구조

- `swing.db.20260414.bak` — Phase 1 직전 라이브 DB. **절대 삭제 금지.**
- `strategies_legacy/` — 전략 10개 (golden_cross, disparity_reversion, 실험 8개)
- `backtest_legacy/` — strategy_compare, tune_adaptive
- `broker_deferred/` — condition_search.py (반자동 매매 어시스턴트 프로젝트로 이관 예정)
- `tests_legacy/` — 전략 의존 테스트 4개
- `docs_legacy/` — 옛 CLAUDE.md, config.yaml, superpowers/plans
- `data_legacy/` — 옛 data/ 내용 (cache 제외)
- `reports_legacy/` — 과거 백테스트 리포트

## 사용 정책

### 참고는 OK
새 전략 설계 시 "이전엔 어떻게 했나" 참고용으로 읽기 — 권장.

### import 금지
`from archive.xxx import ...` 절대 금지.
새 코드가 archive를 의존하면 Phase 1 재설계 의미가 사라짐.

### 코드 재사용 정책
필요한 로직이 있으면:
1. 명시적 결정 (Archi와 논의)
2. archive에서 src/ 적절한 위치로 **복사** (이동 아님)
3. 새 인터페이스에 맞게 적응
4. archive 원본은 그대로 보존

## 정리 시점

- **2026-07-14 이후** (3개월 후): 사용 흔적 없으면 통째로 git rm 검토
- **예외:** broker_deferred/condition_search.py — 반자동 매매 어시스턴트 프로젝트 시작 시 이관
- **예외:** swing.db.20260414.bak — 영구 보존

## 복구 방법

```bash
# 파일 history 추적
git log --all --follow -- archive/strategies_legacy/golden_cross_strategy.py

# 특정 시점으로 복구
git checkout <commit> -- <원본경로>
```
