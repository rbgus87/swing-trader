@echo off
:: swing-trader 자동 시작 스크립트
:: Windows 작업 스케줄러 또는 시작프로그램에 등록해서 사용
:: 프로세스가 종료되면 60초 대기 후 재시작 (무한 루프)

setlocal

:: 프로젝트 루트 (이 배치 파일 위치의 부모 디렉터리)
set PROJECT_DIR=%~dp0..

:: Python 실행 파일 경로 (.venv 또는 시스템 PATH)
if exist "%PROJECT_DIR%\.venv\Scripts\python.exe" (
    set PYTHON="%PROJECT_DIR%\.venv\Scripts\python.exe"
) else (
    set PYTHON=python
)

:: 로그 파일
set LOG_DIR=%PROJECT_DIR%\logs
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"
set RESTART_LOG=%LOG_DIR%\restart.log

:LOOP
echo %date% %time% — 엔진 시작 >> "%RESTART_LOG%"
cd /d "%PROJECT_DIR%"
%PYTHON% main.py
set EXIT_CODE=%ERRORLEVEL%
echo %date% %time% — 엔진 종료 (exit=%EXIT_CODE%), 60초 후 재시작 >> "%RESTART_LOG%"

:: 정상 종료(exit 0)도 재시작 (watchdog 역할)
timeout /t 60 /nobreak >nul
goto LOOP
