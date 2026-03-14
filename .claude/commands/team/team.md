# /team - AI 베테랑 개발팀 가동

프로젝트 요청을 분석하고 적절한 전문가 팀을 구성하여 실행합니다.

## 사용법

```bash
/team "요청 내용"                          # 기본 (hybrid 모드)
/team "요청 내용" --mode auto              # 완전 자동 실행
/team "요청 내용" --mode step              # 단계별 확인
/team "요청 내용" --with frontend,backend  # 특정 전문가만
/team "요청 내용" --without devops         # 특정 전문가 제외
```

## 실행 프로세스

### Phase 1: Orchestrator 분석

**활성화**: `skills/team/orchestrator/SKILL.md`

1. 요청 의도 파악
2. 기술 스택 결정 (또는 PROJECT.md 참조)
3. 작업 분해 (Task Breakdown)
4. 필요한 전문가 식별
5. 실행 계획 수립

**출력 형식**:
```markdown
## 📋 프로젝트 분석

### 요구사항 요약
- **핵심 목표**: [목표]
- **범위**: [범위]
- **제약 조건**: [제약]

### 기술 스택
| 영역 | 선택 | 근거 |
|------|------|------|
| Frontend | [기술] | [이유] |
| Backend | [기술] | [이유] |

### 작업 분배
| 순서 | 작업 | 담당 | 의존성 |
|------|------|------|--------|
| 1 | [작업] | @bootstrapper | - |
| 2 | [작업] | @frontend | #1 |
```

**모드별 동작**:
- `auto`: 분석 후 바로 다음 Phase로
- `step`: 사용자 승인 후 진행
- `hybrid`: 기술 스택/구조 결정 시에만 확인

---

### Phase 2: Bootstrapper 설정

**활성화**: `skills/team/bootstrapper/SKILL.md`

**조건**: 새 프로젝트 또는 프레임워크 설정 필요 시

1. 프로젝트 초기화
2. 의존성 설치
3. 설정 파일 구성
4. 폴더 구조 생성
5. 검증 (npm run dev 성공 확인)

**완료 조건**:
- [ ] npm install 성공
- [ ] TypeScript 컴파일 성공
- [ ] npm run dev 성공

---

### Phase 3: 병렬 개발

**동시 활성화 가능한 전문가**:

| 전문가 | Skill 경로 | 작업 |
|--------|-----------|------|
| Designer | `product-designer/SKILL.md` | UX/UI 설계 |
| Frontend | `frontend-architect/SKILL.md` | UI 구현 |
| Backend | `backend-architect/SKILL.md` | API/DB 구현 |
| Performance | `performance-architect/SKILL.md` | 성능 최적화 |
| Accessibility | `accessibility-architect/SKILL.md` | 접근성 검토 |
| Security | `security-engineer/SKILL.md` | 보안 검토 (각 단계 개입) |
| DevOps | `devops-engineer/SKILL.md` | 인프라 설정 |
| QA | `qa-engineer/SKILL.md` | TDD 가이드, 테스트 선행 작성 |

**Security 개입 시점**:
- Bootstrapper 완료 후: 의존성 보안 감사
- Backend 작업 중: API 보안 검토
- Frontend 작업 중: XSS 방지 검토
- DevOps 설정 시: 인프라 보안 검토

**Performance 개입 시점**:
- Frontend 작업 후: 번들 사이즈, 렌더링 최적화
- Backend 작업 후: 쿼리 최적화, 캐싱 전략

**Accessibility 개입 시점**:
- Designer 작업 후: 디자인 접근성 검토
- Frontend 작업 후: WCAG 준수, 키보드 내비게이션

---

### Phase 4: 검증 및 완료

**활성화**: `skills/team/qa-engineer/SKILL.md`

1. 전체 테스트 스위트 실행 (회귀 + E2E)
2. 커버리지 확인
3. 보안 최종 점검 (Security)
4. 완료 보고

**완료 조건**:
- [ ] 모든 테스트 통과
- [ ] 보안 점검 통과
- [ ] 기능 요구사항 충족

---

### Phase 5: 지속적 개선 (자동 실행 시 반복 제한 필수)

> ⚠️ **반복 제한 필수**: `/ralph-loop` 실행 시 `--max-iterations`를 반드시 포함해야 합니다.
> 미설정 시 무제한 반복으로 비용이 폭증할 수 있습니다. 기본 권장값: **10회**.

**진입 조건**: Phase 4 완료 후 자동 또는 수동 진입

**필수 안전 설정**:
- `--max-iterations N`: 최대 반복 횟수 (기본: 10, 최대 권장: 30, **무제한 실행 금지**)
- `--completion-promise 'TEXT'`: 자동 종료 조건 (선택)

```
Phase 4 완료 후 /ralph-loop 자동 실행:
  /ralph-loop "지속적 개선" --max-iterations 10
  /ralph-loop "성능 최적화" --max-iterations 10 --completion-promise 'All optimized'
    │
    ├── 역할 관점 개선 요청
    │   ├── "UI/UX 개선해줘" → Designer
    │   ├── "성능 최적화해줘" → Performance Architect
    │   ├── "접근성 검토해줘" → Accessibility Architect
    │   ├── "보안 점검해줘" → Security Engineer
    │   └── "테스트 보강해줘" → QA Engineer
    │
    ├── 반복 (--max-iterations 범위 내)
    │
    ├── 자동 종료: max-iterations 도달 또는 completion-promise 충족
    └── 수동 조기 종료: /cancel-ralph
```

**관련 커맨드**:
- `/ralph-loop`: 지속적 개선 루프 진입 (**`--max-iterations 10` 기본 포함 필수**)
- `/cancel-ralph`: 개선 루프 수동 조기 종료

---

## 실행 모드

### `--mode auto` (완전 자동)
```
분석 → 설정 → 개발 → 테스트 → 완료 → 지속적 개선
        (모든 단계 자동 진행, Phase 5는 --max-iterations 10 기본 포함)
```

### `--mode step` (단계별 확인)
```
분석 → [확인] → 설정 → [확인] → 개발 → [확인] → 테스트 → [확인] → 완료
```

### `--mode hybrid` (기본값, 중요 결정만 확인)
```
분석 → [기술 스택 확인] → 설정 → 개발 → [구조 변경 시 확인] → 테스트 → 완료
```

---

## 플래그 레퍼런스

> 이 섹션은 `/team` 커맨드의 모든 지원 플래그에 대한 공식 문서입니다.

### 실행 모드 플래그

| 플래그 | 값 | 설명 | 기본값 |
|--------|-----|------|--------|
| `--mode` | `auto` | 완전 자동 실행, 끝까지 진행 | - |
| `--mode` | `step` | 매 Phase마다 사용자 승인 요청 | - |
| `--mode` | `hybrid` | 기술 스택/구조 변경 시에만 확인 | ✅ 기본값 |

### 전문가 지정 플래그

| 플래그 | 사용법 | 설명 |
|--------|--------|------|
| `--with` | `--with frontend,backend` | 지정된 전문가만 활성화 |
| `--without` | `--without devops,qa` | 지정된 전문가 제외 |

**사용 가능한 역할 값:**

| 역할 | 설명 | 제외 가능 |
|------|------|----------|
| `orchestrator` | 분석, 조율 (항상 활성화) | ❌ |
| `bootstrapper` | 프로젝트 초기화, 환경 설정 | ✅ |
| `designer` | UX/UI 설계, 와이어프레임 | ✅ |
| `frontend` | 프론트엔드 개발, UI 구현 | ✅ |
| `backend` | 백엔드 개발, API/DB 구현 | ✅ |
| `performance` | 성능 최적화, Web Vitals | ✅ |
| `accessibility` | 접근성, WCAG 준수 | ✅ |
| `security` | 보안 검토, 취약점 점검 | ✅ |
| `devops` | CI/CD, 인프라, 배포 | ✅ |
| `qa` | 테스트, 품질 검증 | ✅ |

### 출력 제어 플래그

| 플래그 | 설명 |
|--------|------|
| `--verbose` | 상세 출력 (각 단계 상세 로그) |
| `--quiet` | 최소 출력 (결과만 표시) |

### 플래그 조합 예시

```bash
# 빠른 프로토타입 (테스트/배포 제외)
/team "대시보드 만들어줘" --mode auto --without qa,devops

# 보안 중점 개발
/team "결제 시스템 구현해줘" --mode step --with backend,security

# API만 설계
/team "REST API 설계해줘" --with backend,security

# 프론트엔드만 작업 (기존 백엔드 연동)
/team "대시보드 UI 개선해줘" --with designer,frontend

# 성능 최적화 중점
/team "이커머스 사이트 만들어줘" --with frontend,backend,performance

# 접근성 필수 프로젝트 (공공기관 등)
/team "민원 접수 시스템 만들어줘" --with frontend,backend,accessibility

# 완전체 (모든 전문가 활성화)
/team "SaaS 플랫폼 만들어줘" --mode step --verbose

# 상세 로그로 학습 목적 실행
/team "블로그 만들어줘" --mode step --verbose
```

### 플래그 호환성

| 조합 | 결과 |
|------|------|
| `--with` + `--without` | `--with`가 우선 (지정된 역할만 활성화) |
| `--verbose` + `--quiet` | `--quiet`가 우선 |
| `--without orchestrator` | 무시됨 (orchestrator는 항상 활성화) |

---

## 출력 형식

### 진행 상황

```
🎯 Phase 1: Orchestrator 분석
   ✅ 요구사항 분석 완료
   ✅ 기술 스택 결정: Nuxt.js + Supabase
   ✅ 작업 분배 완료 (8개 태스크)

🔧 Phase 2: Bootstrapper 설정
   🔄 프로젝트 초기화 중...
   ✅ npx nuxi@latest init 완료
   🔄 의존성 설치 중...
```

### 완료 보고

```markdown
## ✅ 작업 완료

### 프로젝트: [프로젝트명]

### 완료된 작업
- ✅ 프로젝트 초기화
- ✅ UI 컴포넌트 구현 (5개)
- ✅ API 엔드포인트 구현 (3개)
- ✅ 보안 검토 통과
- ✅ 테스트 통과 (커버리지 85%)

### 생성된 파일
- `components/ui/Button.vue`
- `server/api/users/index.ts`
- ...

### 다음 단계
1. 환경변수 설정 (.env)
2. 데이터베이스 마이그레이션
3. 배포 설정
```

---

## 참조

- 팀 빠른 참조: `TEAM-QUICK.md`
- 팀 상세 가이드: `TEAM-DETAILED.md`
- 프로젝트 설정: `PROJECT.md`
- 플러그인 가이드: `PLUGINS.md`
- 각 전문가 상세: `skills/team/[역할]/SKILL.md`
