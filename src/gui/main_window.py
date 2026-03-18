"""메인 윈도우 — 좌측 사이드바 + 우측 탭 레이아웃.

KoreanQuant 스타일 참고:
- 좌측: 전략 설정 + 엔진 제어
- 우측: 대시보드(포트폴리오) / 설정 탭
"""

import ctypes
import sys
from datetime import datetime
from pathlib import Path

from PyQt5.QtCore import QTimer, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QIcon, QPixmap, QPainter, QFont
from PyQt5.QtWidgets import (
    QAction,
    QApplication,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QStatusBar,
    QSystemTrayIcon,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from loguru import logger

from src.gui.widgets.dashboard_tab import DashboardTab
from src.gui.widgets.log_tab import LogTab
from src.gui.widgets.settings_tab import SettingsTab
from src.gui.workers.engine_worker import EngineWorker


class MainWindow(QMainWindow):
    """swing-trader 메인 윈도우."""

    _log_signal = pyqtSignal(str, str)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Swing Trader")
        self.setMinimumSize(1100, 720)
        self.resize(1280, 800)

        self._worker: EngineWorker | None = None

        self._init_ui()
        self._apply_theme()
        self._apply_dark_titlebar()
        self._setup_tray()
        self._setup_loguru_sink()
        self._setup_refresh_timer()

    def _init_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ══════════════════════════════════════
        # 좌측 사이드바
        # ══════════════════════════════════════
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(220)
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(16, 16, 16, 16)
        sidebar_layout.setSpacing(12)

        # 앱 타이틀
        title = QLabel("Swing\nTrader")
        title.setObjectName("appTitle")
        sidebar_layout.addWidget(title)

        # ── 모드 선택 ──
        sidebar_layout.addWidget(self._sidebar_section("모드"))

        self.combo_mode = QComboBox()
        self.combo_mode.addItems(["paper", "live"])
        self.combo_mode.setCurrentText("paper")
        sidebar_layout.addWidget(self.combo_mode)

        self.lbl_mode_badge = QLabel("모의투자")
        self.lbl_mode_badge.setObjectName("modeBadge")
        self.lbl_mode_badge.setAlignment(Qt.AlignCenter)
        self.lbl_mode_badge.setStyleSheet(
            "background-color: #40a02b; color: #fff; border-radius: 4px; "
            "padding: 4px 0; font-size: 11px; font-weight: bold;"
        )
        sidebar_layout.addWidget(self.lbl_mode_badge)

        # ── 스케줄러 / 엔진 상태 ──
        sidebar_layout.addWidget(self._sidebar_section("엔진"))

        self._lbl_engine_status = QLabel("대기 중")
        self._lbl_engine_status.setObjectName("engineStatus")
        self._lbl_engine_status.setStyleSheet(
            "color: #6c7086; font-size: 12px; padding: 4px 0;"
        )
        sidebar_layout.addWidget(self._lbl_engine_status)

        self._lbl_schedule_info = QLabel("")
        self._lbl_schedule_info.setStyleSheet(
            "color: #45475a; font-size: 10px; padding: 0;"
        )
        self._lbl_schedule_info.setWordWrap(True)
        sidebar_layout.addWidget(self._lbl_schedule_info)

        # ── 제어 버튼 ──
        btn_row1 = QHBoxLayout()
        btn_row1.setSpacing(8)

        self.btn_start = QPushButton("시작")
        self.btn_start.setObjectName("startBtn")
        self.btn_start.setCursor(Qt.PointingHandCursor)
        btn_row1.addWidget(self.btn_start)

        self.btn_stop = QPushButton("중지")
        self.btn_stop.setObjectName("stopBtn")
        self.btn_stop.setEnabled(False)
        self.btn_stop.setCursor(Qt.PointingHandCursor)
        btn_row1.addWidget(self.btn_stop)

        sidebar_layout.addLayout(btn_row1)

        btn_row2 = QHBoxLayout()
        btn_row2.setSpacing(8)

        self.btn_halt = QPushButton("매매 중단")
        self.btn_halt.setObjectName("haltBtn")
        self.btn_halt.setEnabled(False)
        self.btn_halt.setCursor(Qt.PointingHandCursor)
        btn_row2.addWidget(self.btn_halt)

        sidebar_layout.addLayout(btn_row2)

        # 구분선
        sidebar_layout.addWidget(self._hline())

        # ── 수동 실행 ──
        sidebar_layout.addWidget(self._sidebar_section("수동 실행"))

        btn_manual_row1 = QHBoxLayout()
        btn_manual_row1.setSpacing(8)

        self.btn_reconnect = QPushButton("연결확인")
        self.btn_reconnect.setObjectName("manualBtn")
        self.btn_reconnect.setEnabled(False)
        self.btn_reconnect.setCursor(Qt.PointingHandCursor)
        self.btn_reconnect.setToolTip("키움 API 연결 확인/재연결")
        btn_manual_row1.addWidget(self.btn_reconnect)

        self.btn_daily_reset = QPushButton("일일리셋")
        self.btn_daily_reset.setObjectName("manualBtn")
        self.btn_daily_reset.setEnabled(False)
        self.btn_daily_reset.setCursor(Qt.PointingHandCursor)
        self.btn_daily_reset.setToolTip("PnL 초기화, ATR캐시 클리어, hold_days 갱신")
        btn_manual_row1.addWidget(self.btn_daily_reset)

        sidebar_layout.addLayout(btn_manual_row1)

        btn_manual_row2 = QHBoxLayout()
        btn_manual_row2.setSpacing(8)

        self.btn_screening = QPushButton("스크리닝")
        self.btn_screening.setObjectName("manualBtn")
        self.btn_screening.setEnabled(False)
        self.btn_screening.setCursor(Qt.PointingHandCursor)
        self.btn_screening.setToolTip("즉시 장전 스크리닝 실행")
        btn_manual_row2.addWidget(self.btn_screening)

        self.btn_report = QPushButton("리포트")
        self.btn_report.setObjectName("manualBtn")
        self.btn_report.setEnabled(False)
        self.btn_report.setCursor(Qt.PointingHandCursor)
        self.btn_report.setToolTip("즉시 일간 리포트 발송")
        btn_manual_row2.addWidget(self.btn_report)

        sidebar_layout.addLayout(btn_manual_row2)

        btn_manual_row3 = QHBoxLayout()
        btn_manual_row3.setSpacing(8)

        self.btn_refresh_60m = QPushButton("60분봉")
        self.btn_refresh_60m.setObjectName("manualBtn")
        self.btn_refresh_60m.setEnabled(False)
        self.btn_refresh_60m.setCursor(Qt.PointingHandCursor)
        self.btn_refresh_60m.setToolTip("60분봉 데이터 즉시 갱신 (진입 타이밍 판단용)")
        btn_manual_row3.addWidget(self.btn_refresh_60m)

        sidebar_layout.addLayout(btn_manual_row3)

        # 구분선
        sidebar_layout.addWidget(self._hline())

        # ── 연결 상태 ──
        conn_layout = QHBoxLayout()
        conn_layout.setSpacing(6)
        self._lbl_conn_dot = QLabel("\u25cf")
        self._lbl_conn_dot.setStyleSheet("color: #f38ba8; font-size: 10px;")
        conn_layout.addWidget(self._lbl_conn_dot)
        self._lbl_conn_text = QLabel("미연결")
        self._lbl_conn_text.setStyleSheet("color: #6c7086; font-size: 11px;")
        conn_layout.addWidget(self._lbl_conn_text)
        conn_layout.addStretch()
        sidebar_layout.addLayout(conn_layout)

        sidebar_layout.addStretch()

        root.addWidget(sidebar)

        # ══════════════════════════════════════
        # 우측 메인 영역
        # ══════════════════════════════════════
        right = QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(0)

        # 탭
        self.tabs = QTabWidget()
        self.dashboard_tab = DashboardTab()
        self.log_tab = LogTab()
        self.settings_tab = SettingsTab()

        self.tabs.addTab(self.dashboard_tab, "대시보드")
        self.tabs.addTab(self.log_tab, "로그")
        self.tabs.addTab(self.settings_tab, "설정")

        right.addWidget(self.tabs)

        # 상태바
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self._lbl_status_left = QLabel()
        self._lbl_status_time = QLabel()
        self.status_bar.addWidget(self._lbl_status_left, 1)
        self.status_bar.addPermanentWidget(self._lbl_status_time)

        root.addLayout(right, stretch=1)

        # ── 시그널 연결 ──
        self.btn_start.clicked.connect(self._on_start)
        self.btn_stop.clicked.connect(self._on_stop)
        self.btn_halt.clicked.connect(self._on_halt)
        self.btn_screening.clicked.connect(self._on_screening)
        self.btn_report.clicked.connect(self._on_report)
        self.btn_reconnect.clicked.connect(self._on_reconnect)
        self.btn_daily_reset.clicked.connect(self._on_daily_reset)
        self.btn_refresh_60m.clicked.connect(self._on_refresh_60m)

    # ── 사이드바 헬퍼 ──

    def _sidebar_section(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setStyleSheet(
            "color: #6c7086; font-size: 10px; font-weight: bold; "
            "letter-spacing: 2px; padding: 4px 0 0 0; "
            "border-bottom: 1px solid #313244; margin-bottom: 2px;"
        )
        return label

    def _hline(self) -> QFrame:
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setStyleSheet("color: #313244;")
        line.setFixedHeight(1)
        return line

    # ── 테마 ──

    def _apply_theme(self):
        # PyInstaller exe: _MEIPASS 내부 또는 소스 기준 탐색
        if getattr(sys, "frozen", False):
            base = Path(sys._MEIPASS)
        else:
            base = Path(__file__).parent
        qss_path = base / "src" / "gui" / "styles" / "theme.qss"
        if not qss_path.exists():
            qss_path = Path(__file__).parent / "styles" / "theme.qss"
        if qss_path.exists():
            with open(qss_path, "r", encoding="utf-8") as f:
                self.setStyleSheet(f.read())

    def _apply_dark_titlebar(self):
        """Windows 타이틀바를 다크 모드로 변경."""
        try:
            hwnd = int(self.winId())
            DWMWA_USE_IMMERSIVE_DARK_MODE = 20
            value = ctypes.c_int(1)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE,
                ctypes.byref(value), ctypes.sizeof(value),
            )
        except Exception:
            pass  # Windows 10 이전 버전 등에서는 무시

    # ── 로그 ──

    def _setup_loguru_sink(self):
        self._log_signal.connect(self._dispatch_log)

        def gui_sink(message):
            record = message.record
            level = record["level"].name
            time_str = record["time"].strftime("%H:%M:%S")
            text = f"[{time_str}] {level:8s} {record['message']}"
            self._log_signal.emit(text, level)

        self._loguru_sink_id = logger.add(gui_sink, level="DEBUG", format="{message}")

    def _dispatch_log(self, text: str, level: str):
        """로그를 대시보드 + 로그탭 양쪽에 전달."""
        self.dashboard_tab.append_log(text, level)
        self.log_tab.append_log(text, level)

    # ── 타이머 ──

    def _setup_refresh_timer(self):
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh_status_bar)
        self._timer.start(1000)

    def _refresh_status_bar(self):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._lbl_status_time.setText(f"  {now}  ")

    # ── 엔진 제어 ──

    def _on_start(self):
        mode = self.combo_mode.currentText()

        if mode == "live":
            reply = QMessageBox.warning(
                self, "실거래 모드",
                "실거래 모드는 실제 주문이 실행됩니다.\n계속하시겠습니까?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return

        self._worker = EngineWorker(mode=mode)
        self._connect_worker_signals()

        self._worker.start()
        self.btn_start.setEnabled(False)
        self.btn_start.setText("시작 중...")
        self.combo_mode.setEnabled(False)

    def _connect_worker_signals(self):
        s = self._worker.signals
        s.started.connect(self._on_engine_started)
        s.stopped.connect(self._on_engine_stopped)
        s.error.connect(self._on_engine_error)
        s.status_updated.connect(self._on_status_updated)
        s.positions_updated.connect(self._on_positions_updated)
        s.trades_updated.connect(self._on_trades_updated)
        s.candidates_updated.connect(self._on_candidates_updated)

    def _on_stop(self):
        if self._worker:
            self._worker.signals.request_stop.emit()
            self.btn_stop.setEnabled(False)
            self.btn_stop.setText("중지 중...")

    def _on_halt(self):
        if not self._worker:
            return
        if self.btn_halt.text() == "매매 중단":
            self._worker.signals.request_halt.emit()
        else:
            self._worker.signals.request_resume.emit()

    def _on_screening(self):
        if self._worker:
            self._worker.signals.request_screening.emit()
            logger.info("수동 스크리닝 요청")

    def _on_report(self):
        if self._worker:
            self._worker.signals.request_report.emit()
            logger.info("수동 리포트 요청")

    def _on_reconnect(self):
        if self._worker:
            self._worker.signals.request_reconnect.emit()
            logger.info("수동 연결 확인 요청")

    def _on_daily_reset(self):
        if self._worker:
            self._worker.signals.request_daily_reset.emit()
            logger.info("수동 일일 리셋 요청")

    def _on_refresh_60m(self):
        if self._worker:
            self._worker.signals.request_refresh_60m.emit()
            logger.info("수동 60분봉 갱신 요청")

    def _on_engine_started(self):
        # 연결 상태
        self._lbl_conn_dot.setStyleSheet("color: #a6e3a1; font-size: 10px;")
        self._lbl_conn_text.setText("연결됨")
        self._lbl_conn_text.setStyleSheet("color: #a6e3a1; font-size: 11px;")

        # 모드 배지
        mode = self.combo_mode.currentText()
        if mode == "live":
            self.lbl_mode_badge.setText("실거래")
            self.lbl_mode_badge.setStyleSheet(
                "background-color: #d20f39; color: #fff; border-radius: 4px; "
                "padding: 4px 0; font-size: 11px; font-weight: bold;"
            )
        else:
            self.lbl_mode_badge.setText("모의투자")
            self.lbl_mode_badge.setStyleSheet(
                "background-color: #40a02b; color: #fff; border-radius: 4px; "
                "padding: 4px 0; font-size: 11px; font-weight: bold;"
            )

        # 엔진 상태
        self._lbl_engine_status.setText("실행 중")
        self._lbl_engine_status.setStyleSheet(
            "color: #a6e3a1; font-size: 12px; font-weight: bold; padding: 4px 0;"
        )

        # 스케줄 정보
        from src.utils.config import config
        st = config.get("schedule.screening_time", "08:30")
        rt = config.get("schedule.daily_report_time", "16:00")
        self._lbl_schedule_info.setText(
            f"{st} 스크리닝 | {rt} 리포트"
        )

        # 버튼
        self.btn_start.setText("시작")
        self.btn_stop.setEnabled(True)
        self.btn_halt.setEnabled(True)
        self.btn_screening.setEnabled(True)
        self.btn_report.setEnabled(True)
        self.btn_reconnect.setEnabled(True)
        self.btn_daily_reset.setEnabled(True)
        self.btn_refresh_60m.setEnabled(True)
        self._lbl_status_left.setText("스케줄러: 실행 중")

    def _on_engine_stopped(self):
        self._lbl_conn_dot.setStyleSheet("color: #f38ba8; font-size: 10px;")
        self._lbl_conn_text.setText("미연결")
        self._lbl_conn_text.setStyleSheet("color: #6c7086; font-size: 11px;")

        self.lbl_mode_badge.setText("중지됨")
        self.lbl_mode_badge.setStyleSheet(
            "background-color: #45475a; color: #6c7086; border-radius: 4px; "
            "padding: 4px 0; font-size: 11px; font-weight: bold;"
        )

        self._lbl_engine_status.setText("대기 중")
        self._lbl_engine_status.setStyleSheet(
            "color: #6c7086; font-size: 12px; padding: 4px 0;"
        )
        self._lbl_schedule_info.setText("")

        self.btn_start.setEnabled(True)
        self.btn_start.setText("시작")
        self.btn_stop.setEnabled(False)
        self.btn_stop.setText("중지")
        self.btn_halt.setEnabled(False)
        self.btn_halt.setText("매매 중단")
        self.btn_screening.setEnabled(False)
        self.btn_report.setEnabled(False)
        self.btn_reconnect.setEnabled(False)
        self.btn_daily_reset.setEnabled(False)
        self.btn_refresh_60m.setEnabled(False)
        self.combo_mode.setEnabled(True)
        self._lbl_status_left.setText("스케줄러: 중지")
        self._worker = None

    def _on_engine_error(self, error: str):
        QMessageBox.critical(self, "엔진 오류", error)

    def _on_status_updated(self, status: dict):
        self.dashboard_tab.update_status(status)

        # 사이드바 엔진 상태 동기화
        halted = status.get("halted", False)
        running = status.get("running", False)

        if halted:
            self._lbl_engine_status.setText("매매 중단됨")
            self._lbl_engine_status.setStyleSheet(
                "color: #f9e2af; font-size: 12px; font-weight: bold; padding: 4px 0;"
            )
            self.btn_halt.setText("매매 재개")
        elif running:
            self._lbl_engine_status.setText("실행 중")
            self._lbl_engine_status.setStyleSheet(
                "color: #a6e3a1; font-size: 12px; font-weight: bold; padding: 4px 0;"
            )
            self.btn_halt.setText("매매 중단")

        self.btn_halt.setEnabled(running)

    def _on_positions_updated(self, positions: list):
        self.dashboard_tab.update_positions(positions)

    def _on_trades_updated(self, trades: list):
        self.dashboard_tab.update_trades(trades)

    def _on_candidates_updated(self, candidates: list):
        self.dashboard_tab.update_candidates(candidates)

    # ── 시스템 트레이 ──

    def _make_tray_icon(self) -> QIcon:
        """트레이용 아이콘 생성 (RT 텍스트)."""
        size = 64
        pixmap = QPixmap(size, size)
        pixmap.fill(QColor(0, 0, 0, 0))

        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)

        # 배경 원
        painter.setBrush(QColor("#89b4fa"))
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(2, 2, size - 4, size - 4)

        # RT 텍스트
        painter.setPen(QColor("#11111b"))
        font = QFont("Segoe UI", 22, QFont.Bold)
        painter.setFont(font)
        painter.drawText(pixmap.rect(), Qt.AlignCenter, "ST")

        painter.end()
        return QIcon(pixmap)

    def _setup_tray(self):
        """시스템 트레이 아이콘 설정."""
        tray_icon = self._make_tray_icon()

        self._tray = QSystemTrayIcon(self)
        self._tray.setIcon(tray_icon)
        self._tray.setToolTip("Swing Trader")
        self.setWindowIcon(tray_icon)

        # 트레이 메뉴
        tray_menu = QMenu()

        action_show = QAction("열기", self)
        action_show.triggered.connect(self._tray_show)
        tray_menu.addAction(action_show)

        tray_menu.addSeparator()

        action_quit = QAction("종료", self)
        action_quit.triggered.connect(self._tray_quit)
        tray_menu.addAction(action_quit)

        self._tray.setContextMenu(tray_menu)
        self._tray.activated.connect(self._on_tray_activated)

    def _tray_show(self):
        """트레이에서 창 복원."""
        self.showNormal()
        self.activateWindow()

    def _tray_quit(self):
        """트레이에서 완전 종료."""
        self._cleanup_and_quit()

    def _on_tray_activated(self, reason):
        """트레이 아이콘 더블클릭 시 창 복원."""
        if reason == QSystemTrayIcon.DoubleClick:
            self._tray_show()

    def closeEvent(self, event):
        """닫기 버튼 동작:
        - 엔진 구동 중: 트레이로 최소화
        - 엔진 미구동: 프로그램 종료
        """
        if self._worker and self._worker.isRunning():
            # 트레이로 최소화
            event.ignore()
            self.hide()
            self._tray.show()
            self._tray.showMessage(
                "Swing Trader",
                "엔진이 구동 중입니다. 트레이에서 실행됩니다.",
                QSystemTrayIcon.Information,
                2000,
            )
        else:
            # 프로그램 종료
            event.accept()
            self._cleanup_and_quit()

    def _cleanup_and_quit(self):
        """종료 시 모든 리소스 정리 + QApplication 종료."""
        if getattr(self, "_cleanup_done", False):
            return
        self._cleanup_done = True

        # 타이머 중지
        self._timer.stop()

        # loguru GUI sink 제거 (참조 해제)
        if hasattr(self, "_loguru_sink_id"):
            try:
                logger.remove(self._loguru_sink_id)
            except ValueError:
                pass

        # 워커 스레드 정리
        if self._worker and self._worker.isRunning():
            self._worker.signals.request_stop.emit()
            if not self._worker.wait(5000):
                logger.warning("EngineWorker 5초 내 미종료 — 강제 terminate")
                self._worker.terminate()
                self._worker.wait(2000)
        self._worker = None

        # 트레이 숨김
        self._tray.hide()

        # QApplication 이벤트 루프 종료
        QApplication.quit()


def run_gui():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    # aboutToQuit 시그널로 정리 보장
    window = MainWindow()
    app.aboutToQuit.connect(window._cleanup_and_quit)

    window.show()
    sys.exit(app.exec_())
