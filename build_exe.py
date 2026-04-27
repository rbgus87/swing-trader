"""PyInstaller 빌드 스크립트

실행: python build_exe.py
결과: dist/SwingTrader.exe
"""

import PyInstaller.__main__
import os
import shutil
import sys

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))


def build() -> None:
    args = [
        os.path.join(PROJECT_ROOT, "gui.py"),
        "--name=SwingTrader",
        "--onefile",
        "--windowed",                    # 콘솔 창 숨김
        "--noconfirm",
        # 프로젝트 모듈 포함
        f"--paths={PROJECT_ROOT}",
        # config.yaml 등 데이터 파일 포함
        f"--add-data={os.path.join(PROJECT_ROOT, 'config.yaml')};.",
        f"--add-data={os.path.join(PROJECT_ROOT, 'src', 'gui', 'styles', 'theme.qss')};src/gui/styles",
        # 히든 임포트 (동적 import 되는 모듈)
        # ── 공통 ──
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
        "--hidden-import=matplotlib.backends.backend_agg",
        # 불필요한 패키지 제외
        "--exclude-module=streamlit",
        "--exclude-module=tkinter",
        "--exclude-module=pytest",
        "--exclude-module=numba",
        "--exclude-module=IPython",
        "--exclude-module=jupyter",
        "--exclude-module=notebook",
        # 빌드 디렉토리
        f"--distpath={os.path.join(PROJECT_ROOT, 'dist')}",
        f"--workpath={os.path.join(PROJECT_ROOT, 'build')}",
        f"--specpath={PROJECT_ROOT}",
    ]

    print("=" * 50)
    print("Swing Trader - exe 빌드 시작")
    print("=" * 50)

    PyInstaller.__main__.run(args)

    exe_path = os.path.join(PROJECT_ROOT, "dist", "SwingTrader.exe")
    if os.path.exists(exe_path):
        size_mb = os.path.getsize(exe_path) / (1024 * 1024)
        print(f"\n빌드 완료: {exe_path} ({size_mb:.1f} MB)")

        # 프로젝트 루트에 exe 복사
        root_exe = os.path.join(PROJECT_ROOT, "SwingTrader.exe")
        shutil.copy2(exe_path, root_exe)
        print(f"루트에 복사: {root_exe}")
    else:
        print("\n빌드 실패!")
        sys.exit(1)


if __name__ == "__main__":
    build()
