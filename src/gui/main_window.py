"""메인 윈도우 — 좌측 사이드바 + 우측 탭 레이아웃.

4-레이어 엔진(orchestrator) 연결 버전:
  - 일일 실행: orchestrator.run() 1회 트리거
  - 10초 타이머로 DB에서 positions/signals/snapshot 직접 조회
  - 실시간 폴링 없음 (EOD 배치 구조)
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

from src.data_pipeline.db import get_combined_db, get_data_db, get_trade_db
from src.utils.config import config
from src.gui.widgets.dashboard_tab import DashboardTab
from src.gui.widgets.log_tab import LogTab
from src.gui.widgets.settings_tab import SettingsTab
from src.gui.widgets.trade_history_tab import TradeHistoryTab
from src.gui.workers.engine_worker import EngineWorker
from src.gui.workers.daily_run_worker import DailyRunWorker


MAX_POSITIONS = 4


class MainWindow(QMainWindow):
    """swing-trader 메인 윈도우."""

    _log_signal = pyqtSignal(str, str)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Swing Trader")
        self.setMinimumSize(1100, 720)
        self.resize(1280, 800)

        self._live: EngineWorker | None = None  # 실시간 엔진
        self._daily: DailyRunWorker | None = None  # 일일 실행 (데이터 갱신)

        self._init_ui()
        self._apply_theme()
        self._apply_dark_titlebar()
        self._setup_tray()
        self._setup_loguru_sink()
        self._ensure_swing_db_tables()
        self._setup_refresh_timer()
        # 초기 DB 로드
        self._refresh_from_db()

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

        ver_label = QLabel("v2.5")
        ver_label.setStyleSheet(
            "color: #45475a; font-size: 10px; margin-top: -8px; padding: 0;"
        )
        sidebar_layout.addWidget(ver_label)

        # ── 모드 선택 ──
        sidebar_layout.addWidget(self._sidebar_section("모드"))

        self.combo_mode = QComboBox()
        self.combo_mode.addItems(["paper", "live"])
        self.combo_mode.setCurrentText("paper")
        sidebar_layout.addWidget(self.combo_mode)

        # ── 엔진 상태 ──
        sidebar_layout.addWidget(self._sidebar_section("엔진"))

        self._lbl_engine_status = QLabel("대기 중")
        self._lbl_engine_status.setObjectName("engineStatus")
        self._lbl_engine_status.setStyleSheet(
            "color: #6c7086; font-size: 12px; padding: 4px 0;"
        )
        sidebar_layout.addWidget(self._lbl_engine_status)

        self._lbl_last_run = QLabel("")
        self._lbl_last_run.setStyleSheet(
            "color: #45475a; font-size: 10px; padding: 0;"
        )
        self._lbl_last_run.setWordWrap(True)
        sidebar_layout.addWidget(self._lbl_last_run)

        # ── 전략 요약 ──
        sidebar_layout.addWidget(self._sidebar_section("전략"))

        self._lbl_strategy_name = QLabel("TF v2.5")
        self._lbl_strategy_name.setObjectName("strategyName")
        sidebar_layout.addWidget(self._lbl_strategy_name)

        self._lbl_regime = QLabel("시장: -")
        self._lbl_regime.setObjectName("sidebarInfo")
        sidebar_layout.addWidget(self._lbl_regime)

        self._lbl_sidebar_pos = QLabel(f"포지션: 0/{MAX_POSITIONS}")
        self._lbl_sidebar_pos.setObjectName("sidebarInfo")
        self._lbl_sidebar_pos.setStyleSheet(
            "color: #89b4fa; font-size: 11px; padding: 1px 0;"
        )
        sidebar_layout.addWidget(self._lbl_sidebar_pos)

        self._lbl_sidebar_pnl = QLabel("수익률: +0.00%")
        self._lbl_sidebar_pnl.setObjectName("sidebarInfo")
        self._lbl_sidebar_pnl.setStyleSheet(
            "color: #a6e3a1; font-size: 11px; padding: 1px 0;"
        )
        sidebar_layout.addWidget(self._lbl_sidebar_pnl)

        # ── 제어 버튼 ──
        # 실시간 엔진 (TradingEngine)
        btn_live_row = QHBoxLayout()
        btn_live_row.setSpacing(8)
        self.btn_start = QPushButton("▶ 시작")
        self.btn_start.setObjectName("startBtn")
        self.btn_start.setCursor(Qt.PointingHandCursor)
        self.btn_start.setToolTip("실시간 엔진 시작 (TradingEngine + v2.5 전략)")
        btn_live_row.addWidget(self.btn_start)

        self.btn_stop = QPushButton("■ 중지")
        self.btn_stop.setObjectName("stopBtn")
        self.btn_stop.setEnabled(False)
        self.btn_stop.setCursor(Qt.PointingHandCursor)
        btn_live_row.addWidget(self.btn_stop)
        sidebar_layout.addLayout(btn_live_row)

        # EOD 배치 + 수동 DB 조회
        btn_row1 = QHBoxLayout()
        btn_row1.setSpacing(8)

        self.btn_daily_run = QPushButton("🔄 일일 실행")
        self.btn_daily_run.setObjectName("manualBtn")
        self.btn_daily_run.setCursor(Qt.PointingHandCursor)
        self.btn_daily_run.setToolTip("orchestrator를 1회 실행 (EOD 배치)")
        btn_row1.addWidget(self.btn_daily_run)

        sidebar_layout.addLayout(btn_row1)

        btn_row2 = QHBoxLayout()
        btn_row2.setSpacing(8)

        self.btn_refresh = QPushButton("🔃 새로고침")
        self.btn_refresh.setObjectName("manualBtn")
        self.btn_refresh.setCursor(Qt.PointingHandCursor)
        self.btn_refresh.setToolTip("DB에서 최신 데이터 재조회")
        btn_row2.addWidget(self.btn_refresh)

        sidebar_layout.addLayout(btn_row2)

        # 구분선
        sidebar_layout.addWidget(self._hline())

        # ── 파라미터 요약 ──
        sidebar_layout.addWidget(self._sidebar_section("파라미터"))

        self._lbl_params = QLabel(
            "SL ATR×2.0\nTP1 ATR×2.0 (30%)\nTP2 ATR×4.0 (30%)\n"
            "Trail ATR×4.0\nHold 20일\nSizing: equity/4"
        )
        self._lbl_params.setStyleSheet(
            "color: #a6adc8; font-size: 10px; padding: 0; "
            "line-height: 1.4;"
        )
        self._lbl_params.setWordWrap(True)
        sidebar_layout.addWidget(self._lbl_params)

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
        self.trade_history_tab = TradeHistoryTab()
        self.log_tab = LogTab()
        self.settings_tab = SettingsTab()

        self.tabs.addTab(self.dashboard_tab, "\U0001F4C8 대시보드")
        self.tabs.addTab(self.trade_history_tab, "\U0001F4CB 매매 이력")
        self.tabs.addTab(self.log_tab, "\U0001F4DD 로그")
        self.tabs.addTab(self.settings_tab, "\u2699 설정")

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
        self.btn_daily_run.clicked.connect(self._on_daily_run)
        self.btn_refresh.clicked.connect(self._refresh_from_db)
        self.btn_start.clicked.connect(self._on_live_start)
        self.btn_stop.clicked.connect(self._on_live_stop)

    # ── 사이드바 헬퍼 ──

    def _sidebar_section(self, text: str) -> QLabel:
        label = QLabel(text.upper())
        label.setStyleSheet(
            "color: #585b70; font-size: 10px; font-weight: bold; "
            "letter-spacing: 3px; padding: 6px 0 2px 0; "
            "border: none;"
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
        try:
            hwnd = int(self.winId())
            DWMWA_USE_IMMERSIVE_DARK_MODE = 20
            value = ctypes.c_int(1)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE,
                ctypes.byref(value), ctypes.sizeof(value),
            )
        except Exception:
            pass

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
        self.dashboard_tab.append_log(text, level)
        self.log_tab.append_log(text, level)

    # ── 타이머 ──

    def _setup_refresh_timer(self):
        """1초 타이머: 상태바 시계 | 10초 타이머: DB 재조회."""
        self._clock_timer = QTimer(self)
        self._clock_timer.timeout.connect(self._refresh_status_bar)
        self._clock_timer.start(1000)

        self._db_timer = QTimer(self)
        self._db_timer.timeout.connect(self._refresh_from_db)
        self._db_timer.start(10_000)

    def _refresh_status_bar(self):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._lbl_status_time.setText(f"  {now}  ")

    # ── DB 조회 ──

    def _ensure_swing_db_tables(self):
        """swing_trade.db에 필요 테이블이 없으면 생성 — 10초 타이머 DEBUG 방지.

        DataStore 인스턴스화로 positions/trades 등 매매 핵심 테이블을 만들고,
        snapshot/signals(GUI 표시용)는 명시적으로 보장.
        """
        try:
            from src.datastore import DataStore
            ds = DataStore()
            ds.create_tables()  # positions/trades/daily_performance/ohlcv_cache/...
            ds.close()
        except Exception as e:
            logger.warning(f"DataStore 테이블 보장 실패: {e}")

        try:
            with get_trade_db() as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS signals (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        date DATE NOT NULL,
                        ticker TEXT NOT NULL,
                        signal_type TEXT NOT NULL,
                        strategy TEXT DEFAULT 'TF',
                        price REAL,
                        reason TEXT,
                        executed INTEGER DEFAULT 0,
                        executed_at DATETIME,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS daily_portfolio_snapshot (
                        date DATE PRIMARY KEY,
                        cash REAL NOT NULL,
                        portfolio_value REAL NOT NULL,
                        positions_count INTEGER NOT NULL,
                        breadth REAL,
                        gate_status TEXT,
                        mdd REAL DEFAULT 0,
                        peak_capital REAL DEFAULT 0,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                # 기존 DB 호환: mdd/peak_capital 컬럼 추가 (없으면)
                import sqlite3
                for col, defn in (
                    ("mdd", "REAL DEFAULT 0"),
                    ("peak_capital", "REAL DEFAULT 0"),
                ):
                    try:
                        conn.execute(
                            f"ALTER TABLE daily_portfolio_snapshot "
                            f"ADD COLUMN {col} {defn}"
                        )
                    except sqlite3.OperationalError:
                        pass
        except Exception as e:
            logger.warning(f"swing_trade.db 보조 테이블 보장 실패: {e}")

    def _refresh_from_db(self):
        """DB에서 최신 데이터를 조회해 UI 업데이트.

        - positions: swing_trade.db (DataStore 스키마: code/quantity/소문자 status)
        - signals: swing_trade.db (orchestrator 스키마)
        - daily_portfolio_snapshot: swing_trade.db
        - daily_candles, stocks: swing_data.db
        """
        positions: list[dict] = []
        signals_list: list[dict] = []
        snapshot: dict | None = None

        try:
            with get_combined_db() as conn:
                # positions (DataStore 스키마: status='open'|'selling')
                try:
                    rows = conn.execute(
                        "SELECT p.*, s.name AS stock_name FROM trade.positions p "
                        "LEFT JOIN stocks s ON s.ticker = p.code "
                        "WHERE p.status IN ('open', 'selling') "
                        "ORDER BY p.entry_date"
                    ).fetchall()
                    positions = [dict(r) for r in rows]
                    for p in positions:
                        cur = conn.execute(
                            "SELECT close FROM daily_candles "
                            "WHERE ticker = ? ORDER BY date DESC LIMIT 1",
                            (p['code'],),
                        ).fetchone()
                        p['current_price'] = (
                            cur['close'] if cur else p.get('entry_price', 0)
                        )
                except Exception as e:
                    logger.debug(f"positions 조회 스킵: {e}")

                # signals (orchestrator 스키마)
                try:
                    rows = conn.execute(
                        "SELECT s.*, st.name FROM trade.signals s "
                        "LEFT JOIN stocks st ON st.ticker = s.ticker "
                        "ORDER BY s.id DESC LIMIT 50"
                    ).fetchall()
                    signals_list = [dict(r) for r in rows]
                except Exception as e:
                    logger.debug(f"signals 조회 스킵: {e}")

                # snapshot
                try:
                    row = conn.execute(
                        "SELECT * FROM trade.daily_portfolio_snapshot "
                        "ORDER BY date DESC LIMIT 1"
                    ).fetchone()
                    snapshot = dict(row) if row else None
                except Exception as e:
                    logger.debug(f"snapshot 조회 스킵: {e}")
        except Exception as e:
            logger.warning(f"DB 연결 실패: {e}")
            return

        self._update_dashboard(positions, signals_list, snapshot)
        self._update_sidebar(positions, snapshot)
        self._update_status_bar(snapshot)

    def _update_dashboard(self, positions: list, signals_list: list,
                          snapshot: dict | None):
        """DashboardTab 데이터 주입 — positions는 DataStore 스키마."""
        pos_dicts = []
        today = datetime.now().date()
        for p in positions:
            entry_date = p.get('entry_date')
            hold_days = p.get('hold_days') or 0
            if not hold_days and entry_date:
                try:
                    ed = datetime.strptime(str(entry_date), "%Y-%m-%d").date()
                    hold_days = max(0, (today - ed).days)
                except Exception:
                    hold_days = 0
            code = p.get('code') or ''
            name = p.get('stock_name') or p.get('name') or code
            pos_dicts.append({
                "code": code,
                "name": name,
                "entry_strategy": p.get('entry_strategy') or 'TF',
                "hold_days": hold_days,
                "quantity": p.get('quantity', 0),
                "entry_price": p.get('entry_price', 0),
                "current_price": p.get('current_price', 0),
                "stop_price": p.get('stop_price', 0),
                "tp1_price": p.get('target_price', 0),
                "tp1_triggered": p.get('partial_sold', 0),
                "tp2_price": p.get('tp2_price', 0),
                "tp2_triggered": p.get('partial_sold_2', 0),
                "atr_at_entry": 0,
                "highest_since_entry": p.get('high_since_entry', 0),
            })
        self.dashboard_tab.update_positions(pos_dicts)

        # 당일 ENTRY 신호 → 후보로 표시
        entry_signals = [s for s in signals_list if s.get('signal_type') == 'ENTRY']
        cand_dicts = []
        for s in entry_signals[:20]:
            cand_dicts.append({
                "code": s.get('ticker', ''),
                "name": s.get('name') or s.get('ticker', ''),
                "price": s.get('price', 0),
                "change_pct": 0,
                "score": 0,
                "reason": s.get('reason', ''),
                "date": s.get('date', ''),
            })
        self.dashboard_tab.update_candidates(cand_dicts)

        # 당일 EXIT 신호 → 체결 영역에 간단 표시
        exit_signals = [s for s in signals_list if s.get('signal_type') == 'EXIT']
        trade_dicts = []
        for s in exit_signals[:20]:
            trade_dicts.append({
                "executed_at": s.get('date', ''),
                "code": s.get('ticker', ''),
                "name": s.get('name') or s.get('ticker', ''),
                "side": "sell",
                "quantity": 0,
                "price": s.get('price', 0),
                "pnl": 0,
                "reason": s.get('reason', ''),
            })
        self.dashboard_tab.update_trades(trade_dicts)

        # 상태 카드 (daily_pnl_pct/mdd는 비율 — dashboard_tab에서 ×100 표시)
        cash = snapshot['cash'] if snapshot else 0
        pv = snapshot['portfolio_value'] if snapshot else 0
        initial = int(config.get(
            "trading.initial_capital",
            config.get("backtest.initial_capital", 10_000_000),
        ))
        pnl_ratio = ((pv - initial) / initial) if pv and initial else 0.0
        mdd_ratio = (snapshot.get('mdd') or 0.0) if snapshot else 0.0
        self.dashboard_tab.update_status({
            "capital": int(cash or 0),
            "candidates": len(entry_signals),
            "max_positions": MAX_POSITIONS,
            "daily_pnl_pct": pnl_ratio,
            "mdd": float(mdd_ratio),
            "portfolio_value": int(pv or 0),
        })

    def _update_sidebar(self, positions: list, snapshot: dict | None):
        """사이드바 요약 정보 업데이트."""
        self._lbl_sidebar_pos.setText(
            f"포지션: {len(positions)}/{MAX_POSITIONS}"
        )

        if snapshot:
            gate = snapshot.get('gate_status') or 'UNKNOWN'
            breadth = snapshot.get('breadth') or 0.0
            if gate == 'OPEN':
                text = f"🟢 OPEN (breadth {breadth:.0%})"
                color = "#a6e3a1"
            elif gate == 'CLOSED':
                text = f"🔴 CLOSED (breadth {breadth:.0%})"
                color = "#f38ba8"
            else:
                text = f"⚪ {gate}"
                color = "#6c7086"
            self._lbl_regime.setText(f"시장: {text}")
            self._lbl_regime.setStyleSheet(
                f"color: {color}; font-size: 11px; "
                f"font-weight: bold; padding: 2px 0;"
            )

            # 누적 수익률
            pv = snapshot.get('portfolio_value') or 0
            initial = int(config.get(
                "trading.initial_capital",
                config.get("backtest.initial_capital", 10_000_000),
            ))
            pnl = ((pv - initial) / initial * 100) if pv and initial else 0.0
            pnl_color = "#a6e3a1" if pnl >= 0 else "#f38ba8"
            self._lbl_sidebar_pnl.setText(f"수익률: {pnl:+.2f}%")
            self._lbl_sidebar_pnl.setStyleSheet(
                f"color: {pnl_color}; font-size: 11px; padding: 1px 0;"
            )

            # 최근 실행
            last_date = snapshot.get('date', '')
            if last_date:
                self._lbl_last_run.setText(f"마지막 실행: {last_date}")

    def _update_status_bar(self, snapshot: dict | None):
        mode = self.combo_mode.currentText().upper()
        strategy = "TF v2.5"
        next_td = self._next_trading_date()
        if snapshot:
            gate = snapshot.get('gate_status') or 'UNKNOWN'
            breadth = snapshot.get('breadth') or 0.0
            date = snapshot.get('date', '')
            gate_icon = "🟢" if gate == 'OPEN' else (
                "🔴" if gate == 'CLOSED' else "⚪"
            )
            self._lbl_status_left.setText(
                f"{mode} | {strategy} | {gate_icon} {gate} ({breadth:.0%}) | "
                f"마지막 실행: {date} | 다음: {next_td}"
            )
        else:
            self._lbl_status_left.setText(
                f"{mode} | {strategy} | 데이터 없음 | 다음: {next_td}"
            )

    def _next_trading_date(self) -> str:
        """근사 다음 거래일 (주말 건너뛰기). 공휴일은 반영하지 않음."""
        from datetime import timedelta
        d = datetime.now().date() + timedelta(days=1)
        while d.weekday() >= 5:  # 5=토, 6=일
            d += timedelta(days=1)
        return d.strftime("%Y-%m-%d")

    # ── 일일 실행 ──

    def _on_daily_run(self):
        """일일 실행 — 데이터 갱신(신규상장/일봉/시총/지수) + 시그널 생성."""
        if self._daily and self._daily.isRunning():
            QMessageBox.information(
                self, "실행 중",
                "일일 실행이 진행 중입니다. 완료를 기다려 주세요."
            )
            return

        mode = self.combo_mode.currentText()
        if mode == "live":
            reply = QMessageBox.warning(
                self, "실거래 모드",
                "실거래 모드는 실제 주문이 실행됩니다.\n계속하시겠습니까?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return

        self._daily = DailyRunWorker()
        self._daily.log_signal.connect(self._on_daily_log)
        self._daily.step_signal.connect(self._on_daily_step)
        self._daily.finished_signal.connect(self._on_daily_finished)

        self.btn_daily_run.setEnabled(False)
        self.btn_daily_run.setText("⏳ 실행 중…")
        self._lbl_engine_status.setText("일일 실행 중")
        self._lbl_engine_status.setStyleSheet(
            "color: #f9e2af; font-size: 12px; "
            "font-weight: bold; padding: 4px 0;"
        )
        self._daily.start()

    def _on_daily_log(self, message: str, level: str):
        self._dispatch_log(message, level)

    def _on_daily_step(self, current: int, total: int, label: str):
        self.btn_daily_run.setText(f"⏳ {label}")

    def _on_daily_finished(self, success: bool, summary: str):
        if success:
            self._dispatch_log(f"✅ {summary}", "INFO")
        else:
            self._dispatch_log(f"❌ {summary}", "ERROR")
            QMessageBox.critical(self, "일일 실행 실패", summary)

        self.btn_daily_run.setEnabled(True)
        self.btn_daily_run.setText("🔄 일일 실행")
        self._lbl_engine_status.setText("대기 중")
        self._lbl_engine_status.setStyleSheet(
            "color: #6c7086; font-size: 12px; padding: 4px 0;"
        )
        self._daily = None
        self._refresh_from_db()

    def _on_engine_error(self, error: str):
        QMessageBox.critical(self, "엔진 오류", error)

    # ── 실시간 엔진 (TradingEngine) ──

    def _on_live_start(self):
        if self._live and self._live.isRunning():
            QMessageBox.information(self, "실행 중", "실시간 엔진이 이미 실행 중입니다.")
            return

        mode = self.combo_mode.currentText()
        if mode == "live":
            reply = QMessageBox.warning(
                self, "실거래 모드",
                "실거래 모드는 실제 주문이 실행됩니다.\n계속하시겠습니까?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return

        self._live = EngineWorker(mode=mode)
        s = self._live.signals
        s.started.connect(self._on_live_started)
        s.stopped.connect(self._on_live_stopped)
        s.error.connect(self._on_engine_error)
        s.status_updated.connect(self._on_live_status)
        s.positions_updated.connect(self._on_live_positions)
        s.trades_updated.connect(self._on_live_trades)
        s.candidates_updated.connect(self._on_live_candidates)

        self.btn_start.setEnabled(False)
        self.btn_start.setText("시작 중...")
        self.combo_mode.setEnabled(False)
        self._live.start()

    def _on_live_stop(self):
        if self._live:
            self._live.signals.request_stop.emit()
            self.btn_stop.setEnabled(False)
            self.btn_stop.setText("중지 중...")

    def _on_live_started(self):
        # 엔진 ON: signal emit(swing_legacy.db)만 사용 → DB 타이머(swing.db) 중지
        if hasattr(self, "_db_timer") and self._db_timer.isActive():
            self._db_timer.stop()

        # 수익 곡선 세션 리셋
        if hasattr(self.dashboard_tab, "reset_equity"):
            self.dashboard_tab.reset_equity()

        self._lbl_engine_status.setText("실시간 실행 중")
        self._lbl_engine_status.setStyleSheet(
            "color: #a6e3a1; font-size: 12px; "
            "font-weight: bold; padding: 4px 0;"
        )
        self.btn_start.setText("▶ 시작")
        self.btn_stop.setEnabled(True)

    def _on_live_stopped(self):
        # 엔진 OFF: swing.db 스냅샷 표시로 복귀
        if hasattr(self, "_db_timer") and not self._db_timer.isActive():
            self._db_timer.start(10_000)
        self._refresh_from_db()

        self._lbl_engine_status.setText("대기 중")
        self._lbl_engine_status.setStyleSheet(
            "color: #6c7086; font-size: 12px; padding: 4px 0;"
        )
        self.btn_start.setEnabled(True)
        self.btn_start.setText("▶ 시작")
        self.btn_stop.setEnabled(False)
        self.btn_stop.setText("■ 중지")
        self.combo_mode.setEnabled(True)
        self._live = None

    def _on_live_status(self, status: dict):
        self.dashboard_tab.update_status(status)
        pnl = status.get("daily_pnl_pct", 0.0) * 100
        pnl_color = "#a6e3a1" if pnl >= 0 else "#f38ba8"
        self._lbl_sidebar_pnl.setText(f"수익률: {pnl:+.2f}%")
        self._lbl_sidebar_pnl.setStyleSheet(
            f"color: {pnl_color}; font-size: 11px; padding: 1px 0;"
        )

        # breadth / gate 반영
        breadth = status.get("breadth", 0.0) or 0.0
        gate_open = bool(status.get("gate_open", False))
        if gate_open:
            text = f"🟢 OPEN (breadth {breadth:.0%})"
            color = "#a6e3a1"
        else:
            text = f"🔴 CLOSED (breadth {breadth:.0%})"
            color = "#f38ba8"
        self._lbl_regime.setText(f"시장: {text}")
        self._lbl_regime.setStyleSheet(
            f"color: {color}; font-size: 11px; "
            f"font-weight: bold; padding: 2px 0;"
        )

    def _on_live_positions(self, positions: list):
        # TradingEngine Position → dashboard 포맷
        pos_dicts = []
        for p in positions:
            pos_dicts.append({
                "code": p.get("code", ""),
                "name": p.get("name", "") or p.get("code", ""),
                "entry_strategy": p.get("entry_strategy", "TF"),
                "hold_days": p.get("hold_days", 0),
                "quantity": p.get("quantity", 0),
                "entry_price": p.get("entry_price", 0),
                "current_price": p.get("current_price", 0),
                "stop_price": p.get("stop_price", 0),
                "tp1_price": p.get("tp1_price", 0),
                "tp1_triggered": p.get("tp1_triggered", 0),
                "atr_at_entry": p.get("atr_at_entry", 0),
                "highest_since_entry": p.get("highest_since_entry", 0),
            })
        self.dashboard_tab.update_positions(pos_dicts)
        self._lbl_sidebar_pos.setText(
            f"포지션: {len(positions)}/{MAX_POSITIONS}"
        )

    def _on_live_trades(self, trades: list):
        self.dashboard_tab.update_trades(trades)

    def _on_live_candidates(self, candidates: list):
        self.dashboard_tab.update_candidates(candidates)

    # ── 시스템 트레이 ──

    def _make_tray_icon(self) -> QIcon:
        size = 64
        pixmap = QPixmap(size, size)
        pixmap.fill(QColor(0, 0, 0, 0))

        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setBrush(QColor("#89b4fa"))
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(2, 2, size - 4, size - 4)

        painter.setPen(QColor("#11111b"))
        font = QFont("Segoe UI", 22, QFont.Bold)
        painter.setFont(font)
        painter.drawText(pixmap.rect(), Qt.AlignCenter, "ST")
        painter.end()
        return QIcon(pixmap)

    def _setup_tray(self):
        tray_icon = self._make_tray_icon()

        self._tray = QSystemTrayIcon(self)
        self._tray.setIcon(tray_icon)
        self._tray.setToolTip("Swing Trader")
        self.setWindowIcon(tray_icon)

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
        self.showNormal()
        self.activateWindow()

    def _tray_quit(self):
        self._cleanup_and_quit()

    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.DoubleClick:
            self._tray_show()

    def closeEvent(self, event):
        engine_running = (
            (self._live is not None and self._live.isRunning())
            or (self._daily is not None and self._daily.isRunning())
        )

        if engine_running:
            event.ignore()
            self.hide()
            self._tray.show()
            self._tray.showMessage(
                "Swing Trader",
                "엔진이 구동 중입니다. 트레이에서 실행됩니다.",
                QSystemTrayIcon.Information,
                2000,
            )
            return

        reply = QMessageBox.question(
            self,
            "종료 확인",
            "프로그램을 종료하시겠습니까?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            event.accept()
            self._cleanup_and_quit()
        else:
            event.ignore()

    def _cleanup_and_quit(self):
        if getattr(self, "_cleanup_done", False):
            return
        self._cleanup_done = True

        if hasattr(self, "_clock_timer"):
            self._clock_timer.stop()
        if hasattr(self, "_db_timer"):
            self._db_timer.stop()

        if hasattr(self, "_loguru_sink_id"):
            try:
                logger.remove(self._loguru_sink_id)
            except ValueError:
                pass

        if self._live and self._live.isRunning():
            self._live.signals.request_stop.emit()
            if not self._live.wait(5000):
                logger.warning("EngineWorker 5초 내 미종료 — 강제 terminate")
                self._live.terminate()
                self._live.wait(2000)
        self._live = None

        if self._daily and self._daily.isRunning():
            self._daily.cancel()
            if not self._daily.wait(5000):
                logger.warning("DailyRunWorker 5초 내 미종료 — 강제 terminate")
                self._daily.terminate()
                self._daily.wait(2000)
        self._daily = None

        self._tray.hide()
        QApplication.quit()


def run_gui():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    window = MainWindow()
    app.aboutToQuit.connect(window._cleanup_and_quit)

    window.show()
    sys.exit(app.exec_())
