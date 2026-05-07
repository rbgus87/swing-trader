"""PyInstaller 빌드 스크립트

실행: python build_exe.py
결과: dist/SwingTrader/SwingTrader.exe (onedir)

onedir 채택 이유:
  - onefile은 매 실행 시 임시 폴더로 압축해제 → 시작 ~10초 + 디스크 IO
  - onedir은 압축 단계 없음 → 빌드/시작 모두 빠름
  - PROJECT_ROOT 결정 로직(src/data_pipeline/__init__.py, src/utils/config.py)에
    onedir 폴백을 추가했으므로 dist/SwingTrader/SwingTrader.exe 직접 실행 시
    상위 디렉토리에서 config.yaml/swing_*.db 자동 발견.

빌드 직후 자동으로 `SwingTrader.exe --selftest` 를 호출해 환경/의존성 무결성을
검증한다. selftest FAIL 시 exit 1 — 운영 투입 차단.
"""

import PyInstaller.__main__
import os
import shutil
import subprocess
import sys
import time

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))


def build() -> None:
    args = [
        os.path.join(PROJECT_ROOT, "gui.py"),
        "--name=SwingTrader",
        "--onedir",
        "--windowed",                    # 콘솔 창 숨김
        "--noconfirm",
        # 프로젝트 모듈 포함
        f"--paths={PROJECT_ROOT}",
        # config.yaml 등 데이터 파일 포함
        f"--add-data={os.path.join(PROJECT_ROOT, 'config.yaml')};.",
        f"--add-data={os.path.join(PROJECT_ROOT, 'src', 'gui', 'styles', 'theme.qss')};src/gui/styles",
        # 히든 임포트 (동적 import 되는 모듈)
        # ── 공통 ──
        "--hidden-import=selftest",
        "--hidden-import=src.datastore",
        "--hidden-import=src.models",
        "--hidden-import=src.utils.config",
        "--hidden-import=src.utils.logger",
        "--hidden-import=src.utils.market_calendar",
        # ── 실시간 엔진 (TradingEngine) ──
        "--hidden-import=src.trading_engine",
        "--hidden-import=src.strategy.trend_following_v2",
        # ── 데이터 파이프라인 (일일 실행용) ──
        "--hidden-import=src.data_pipeline",
        "--hidden-import=src.data_pipeline.db",
        "--hidden-import=src.data_pipeline.collect_daily_candles",
        "--hidden-import=src.data_pipeline.collect_market_cap",
        "--hidden-import=src.data_pipeline.collect_index_daily",
        "--hidden-import=src.data_pipeline.collect_stocks_meta",
        "--hidden-import=src.data_pipeline.detect_new_listings",
        "--hidden-import=src.data_pipeline.fdr_client",
        "--hidden-import=src.data_pipeline.krx_client",
        "--hidden-import=src.data_pipeline.rate_limiter",
        # ── 브로커 ──
        "--hidden-import=src.broker.kiwoom_api",
        "--hidden-import=src.broker.rest_client",
        "--hidden-import=src.broker.ws_client",
        "--hidden-import=src.broker.order_manager",
        "--hidden-import=src.broker.realtime_data",
        "--hidden-import=src.broker.tr_codes",
        # ── 리스크/알림 ──
        "--hidden-import=src.risk.risk_manager",
        "--hidden-import=src.risk.position_sizer",
        "--hidden-import=src.risk.stop_manager",
        "--hidden-import=src.notification.telegram_bot",
        # ── GUI ──
        "--hidden-import=src.gui.main_window",
        "--hidden-import=src.gui.widgets.dashboard_tab",
        "--hidden-import=src.gui.widgets.settings_tab",
        "--hidden-import=src.gui.widgets.log_tab",
        "--hidden-import=src.gui.widgets.trade_history_tab",
        "--hidden-import=src.gui.workers.engine_worker",
        "--hidden-import=src.gui.workers.daily_run_worker",
        "--hidden-import=src.gui.workers.signals",
        # ── 레거시 참조(런타임 가드용) ──
        "--hidden-import=src.strategy.market_regime",
        # ── 외부 라이브러리 ──
        "--hidden-import=PyQt5",
        "--hidden-import=PyQt5.QtCore",
        "--hidden-import=PyQt5.QtGui",
        "--hidden-import=PyQt5.QtWidgets",
        "--hidden-import=PyQt5.sip",
        "--hidden-import=apscheduler.schedulers.asyncio",
        "--hidden-import=apscheduler.triggers.cron",
        "--hidden-import=apscheduler.triggers.date",
        "--hidden-import=apscheduler.triggers.interval",
        "--hidden-import=apscheduler.jobstores.memory",
        "--hidden-import=apscheduler.executors.pool",
        "--hidden-import=apscheduler.executors.asyncio",
        "--hidden-import=pykrx",
        "--hidden-import=pykrx.stock",
        # pykrx __init__이 NanumBarunGothic.ttf 데이터 파일을 importlib.resources로
        # 로드하므로 --collect-all로 datas 포함 (그렇지 않으면 import 자체 실패)
        "--collect-all=pykrx",
        "--hidden-import=FinanceDataReader",
        "--hidden-import=yfinance",
        "--hidden-import=pyqtgraph",
        "--hidden-import=ruamel.yaml",
        "--hidden-import=loguru",
        "--hidden-import=yaml",
        "--hidden-import=dotenv",
        "--hidden-import=httpx",
        "--hidden-import=websockets",
        "--hidden-import=holidays",
        "--hidden-import=requests",
        "--hidden-import=matplotlib.backends.backend_agg",  # pykrx __init__이 plt 로드
        # 불필요한 패키지 제외
        "--exclude-module=streamlit",
        "--exclude-module=tkinter",
        "--exclude-module=pytest",
        "--exclude-module=numba",
        "--exclude-module=IPython",
        "--exclude-module=jupyter",
        "--exclude-module=notebook",
        "--exclude-module=sklearn",
        "--exclude-module=cv2",
        "--exclude-module=tensorflow",
        "--exclude-module=torch",
        # PIL/scipy는 matplotlib 의존성으로 pykrx 로드 시 필요 → 제외 금지
        # 빌드 디렉토리
        f"--distpath={os.path.join(PROJECT_ROOT, 'dist')}",
        f"--workpath={os.path.join(PROJECT_ROOT, 'build')}",
        f"--specpath={PROJECT_ROOT}",
    ]

    print("=" * 50)
    print("Swing Trader - exe 빌드 시작 (onedir)")
    print("=" * 50)

    build_started = time.time()
    PyInstaller.__main__.run(args)
    build_elapsed = time.time() - build_started

    # onedir: dist/SwingTrader/SwingTrader.exe
    dist_dir = os.path.join(PROJECT_ROOT, "dist", "SwingTrader")
    exe_path = os.path.join(dist_dir, "SwingTrader.exe")
    if not os.path.exists(exe_path):
        print("\n빌드 실패!")
        sys.exit(1)

    # 폴더 전체 사이즈 측정
    total_size = 0
    for root, _, files in os.walk(dist_dir):
        for f in files:
            try:
                total_size += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    size_mb = total_size / (1024 * 1024)
    exe_mb = os.path.getsize(exe_path) / (1024 * 1024)
    print(f"\n빌드 완료: {exe_path}")
    print(f"  exe 단독: {exe_mb:.1f} MB / 폴더 합계: {size_mb:.1f} MB")
    print(f"  빌드 소요 시간: {build_elapsed:.1f}s")

    # 루트의 구버전 onefile exe 정리 (있으면 혼란 방지)
    legacy_root_exe = os.path.join(PROJECT_ROOT, "SwingTrader.exe")
    if os.path.exists(legacy_root_exe):
        try:
            os.remove(legacy_root_exe)
            print(f"구버전 루트 exe 제거: {legacy_root_exe}")
        except OSError as e:
            print(f"  (구버전 exe 제거 실패: {e})")

    # 빌드 직후 selftest 자동 실행 — silent failure 조기 차단
    # onedir: dist/SwingTrader/SwingTrader.exe 직접 실행 + cwd=PROJECT_ROOT.
    # PROJECT_ROOT 탐색 로직이 exe_dir → exe_dir.parent → exe_dir.parent.parent 순으로
    # config.yaml을 찾으므로 swing-trader/ 가 root로 인식됨.
    print("\n" + "=" * 50)
    print("빌드된 exe selftest 실행")
    print("=" * 50)
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        result = subprocess.run(
            [exe_path, "--selftest"],
            capture_output=True,
            text=True,
            timeout=60,
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=PROJECT_ROOT,
        )
    except subprocess.TimeoutExpired:
        print("*** selftest 60초 타임아웃 — 운영 투입 금지 ***")
        sys.exit(1)

    print(result.stdout)
    if result.returncode != 0:
        if result.stderr:
            print("STDERR:")
            print(result.stderr)
        print("\n*** 빌드된 exe selftest FAIL ***")
        print("운영 투입 금지. 빌드 옵션 재검토 필요.")
        sys.exit(1)
    print("*** 빌드 + selftest 모두 통과 ***")
    print(f"\n실행: {exe_path}")


if __name__ == "__main__":
    build()
