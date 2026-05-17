@echo off
:: Windows 작업 스케줄러에 swing-trader 자동 시작 등록
:: 관리자 권한으로 실행 필요 (우클릭 → 관리자 권한으로 실행)

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo 관리자 권한이 필요합니다. 우클릭 후 "관리자 권한으로 실행"을 선택하세요.
    pause
    exit /b 1
)

set TASK_NAME=SwingTraderAutoStart
set PROJECT_DIR=%~dp0..
set BAT_FILE=%~dp0auto_start.bat

:: 기존 작업 삭제 (있을 경우)
schtasks /delete /tn "%TASK_NAME%" /f >nul 2>&1

:: 작업 등록: 로그온 시 실행, 로그온 없이 실행, 재시작 시 실행
schtasks /create ^
  /tn "%TASK_NAME%" ^
  /tr "cmd /c \"%BAT_FILE%\"" ^
  /sc onlogon ^
  /ru "%USERNAME%" ^
  /rl HIGHEST ^
  /f

if %errorlevel% equ 0 (
    echo 작업 스케줄러 등록 완료: %TASK_NAME%
    echo 로그온 시 자동 시작됩니다.
) else (
    echo 등록 실패. 관리자 권한을 확인하세요.
)

pause
