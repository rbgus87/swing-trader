# PROJECT.md - 스윙 자동매매 시스템 프로젝트 설정

## 프로젝트 정보

```yaml
project_name: "swing-trader"
description: "KOSPI/KOSDAQ 국내 주식 스윙매매 자동화 시스템 (키움 OpenAPI+ 기반)"
version: "0.1.0"
project_type: "fullstack"
platforms: [desktop, web]        # PyQt5 데스크톱 + 향후 Streamlit 대시보드
```

---

## 기술 스택

```yaml
tech_stack:
  language: "Python"
  python_version: "3.11+"
  packaging: "pip"               # pip + requirements.txt

  backend:
    framework: "none"            # 프레임워크 없이 순수 Python
    event_loop: "PyQt5"          # 키움 OCX 이벤트 처리용
    scheduler: "APScheduler"     # 장 시작/마감 작업 스케줄링
    database: "SQLite"           # 매매일지, 포지션, OHLCV 캐시
    config:
      format: "yaml"             # config.yaml (PyYAML)
      secrets: "dotenv"          # .env (python-dotenv)
    logging: "loguru"            # 파일 + 콘솔 동시 출력

  data:
    broker_api: "키움 OpenAPI+ OCX"   # Windows 전용
    market_data: "pykrx"              # 전종목 일봉/재무 데이터
    indicators: "pandas-ta"           # 기술적 지표 계산
    dataframe: "pandas"
    backtest: "vectorbt"              # 파라미터 최적화 포함

  notification:
    telegram: "requests"         # Telegram Bot API 직접 호출

  frontend:                      # 향후 확장
    dashboard: "Streamlit"       # 선택적 웹 대시보드 (Out of Scope)
```

---

## 플랫폼 제약사항

```yaml
platform_constraints:
  os: "Windows"                  # 키움 OCX 필수 → Windows 전용
  architecture: "x86_64"         # 32bit OCX 호환 필요
  threading: "single"            # OCX → PyQt5 메인 스레드만 허용
  python_arch: "32bit"           # 키움 OCX 32비트 호환 (또는 64bit with 64bit OCX)
```

---

## 팀 설정

```yaml
team_config:
  disabled_roles:
    - designer                   # UI 디자인 불필요 (CLI + PyQt5 이벤트루프)
    - accessibility              # 데스크톱 자동매매 도구, 접근성 N/A
  auto_security_review: true     # 실거래 시스템 → 보안 검토 필수
  default_mode: "auto"
```

## 프로젝트 컨벤션

```yaml
conventions:
  code_style:
    python:
      formatter: "black"
      linter: "ruff"
      type_checker: "none"       # 필요 시 mypy 추가

  naming:
    money: "int"                 # 원화 금액은 항상 int
    ratio: "float"               # 비율은 0.0~1.0 float
    timezone: "Asia/Seoul"       # pytz 또는 zoneinfo

  commit:
    format: "conventional"

  branching:
    strategy: "github-flow"
    main: "main"
    feature: "feature/*"
```

## 환경변수

```yaml
env_vars:
  required:
    - KIWOOM_ACCOUNT             # 키움 계좌번호
    - TELEGRAM_BOT_TOKEN         # 텔레그램 봇 토큰
    - TELEGRAM_CHAT_ID           # 텔레그램 채팅 ID
  optional:
    - CONFIG_PATH                # config.yaml 경로 (기본: ./config.yaml)
    - LOG_LEVEL                  # 로그 레벨 (기본: INFO)
    - DEBUG                      # 디버그 모드
```

## 핵심 안전 규칙

```yaml
safety_rules:
  # 주문 실행 전 반드시 RiskManager.pre_check() 통과
  order_requires_risk_check: true
  # 일일 손실 -3% 초과 시 자동 halt
  daily_loss_auto_halt: true
  # 실거래 모드에서 텔레그램 알림 없이 주문 불가
  live_requires_telegram: true
  # 장 시간 외 자동 주문 차단 (09:00~15:30)
  market_hours_only: true
  # 키움 API 요청 제한: TR 5건/초, 주문 5건/초
  rate_limiting: true
```

## 문서화 설정

```yaml
documentation:
  language: "ko"
  specs_dir: "docs/"             # PRD, ARCHITECTURE, *_SPEC.md
  auto_generate:
    readme: true
    changelog: true
  format:
    code: "docstring"            # Google style docstring
```
