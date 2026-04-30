@echo off
chcp 65001 >nul
echo ============================================
echo   SwingTrader exe 빌드
echo ============================================
echo.

:: PyInstaller 설치 확인
python -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo [1/2] PyInstaller 설치 중...
    pip install pyinstaller
) else (
    echo [1/2] PyInstaller 확인 완료
)

:: 빌드 실행
echo [2/2] 빌드 시작...
echo.
python build_exe.py

if errorlevel 1 (
    echo.
    echo ============================================
    echo   빌드 실패! 위 에러 메시지를 확인하세요.
    echo ============================================
    pause
    exit /b 1
)

echo.
echo ============================================
echo   실행 파일: SwingTrader.exe (프로젝트 루트)
echo              dist\SwingTrader.exe
echo.
echo   실행 전 확인사항 (exe와 같은 폴더에 배치):
echo     1. .env             (API 키)
echo     2. config.yaml      (전략 설정)
echo     3. swing_data.db    (시세 DB — Phase 1 데이터)
echo     4. swing_trade.db   (운영 DB — 자동 생성됨)
echo.
echo   설치 후 환경 검증: SwingTrader.exe --selftest
echo ============================================
pause
