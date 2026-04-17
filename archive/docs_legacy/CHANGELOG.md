# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.1.0] - 2026-03-15

### Added
- 프로젝트 초기화 및 기반 모듈 (config, logger, market_calendar)
- 키움 OpenAPI+ 브로커 레이어 (OCX 래퍼, 주문관리, 실시간시세)
- MACD-RSI 스윙 전략 (매수AND/매도OR 신호)
- 리스크 관리 (하프켈리 사이징, ATR 손절, 트레일링스탑)
- vectorbt 백테스트 엔진 (그리드서치, Walk-Forward)
- 텔레그램 알림 (8종 메시지 템플릿)
- TradingEngine 통합 (Paper/Live 모드)
- SQLite 데이터 저장소
- E2E 통합 테스트
- GitHub Actions CI (Windows + Linux)
- ruff/black 코드 품질 도구 설정
