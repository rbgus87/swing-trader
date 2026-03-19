"""대시보드 탭 — 보유 포지션 + 요약 + 후보/체결 + 로그.

KoreanQuant 스타일: 요약 한 줄 → 테이블 → 하단 분할 → 로그
"""

import html

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)


# Catppuccin Mocha 색상
_GREEN = "#a6e3a1"
_RED = "#f38ba8"
_YELLOW = "#f9e2af"
_BLUE = "#89b4fa"
_MAUVE = "#cba6f7"
_PEACH = "#fab387"
_TEXT = "#cdd6f4"
_SUBTEXT = "#6c7086"
_SURFACE = "#313244"


class DashboardTab(QWidget):
    """대시보드 탭 — 포트폴리오 + 후보/체결 + 로그."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._max_positions = 3
        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(0)
        layout.setContentsMargins(0, 0, 0, 0)

        # ── 요약 통계 바 ──
        summary_bar = QFrame()
        summary_bar.setObjectName("summaryBar")
        summary_bar.setFixedHeight(36)
        summary_layout = QHBoxLayout(summary_bar)
        summary_layout.setContentsMargins(16, 0, 16, 0)
        summary_layout.setSpacing(24)

        self._lbl_summary = QLabel("투자금: 0원 | 가용자금: 0원 | 포지션: 0/3 | 후보: 0종목")
        self._lbl_summary.setStyleSheet(
            f"color: {_SUBTEXT}; font-size: 12px;"
        )
        summary_layout.addWidget(self._lbl_summary)

        summary_layout.addStretch()

        self._lbl_pnl = QLabel("일일 손익: +0.00%")
        self._lbl_pnl.setStyleSheet(
            f"color: {_GREEN}; font-size: 12px; font-weight: bold;"
        )
        summary_layout.addWidget(self._lbl_pnl)

        self._lbl_mdd = QLabel("MDD: 0.0%")
        self._lbl_mdd.setStyleSheet(f"color: {_SUBTEXT}; font-size: 12px;")
        summary_layout.addWidget(self._lbl_mdd)

        layout.addWidget(summary_bar)

        # ── 메인 영역 (수직 스플리터) ──
        splitter = QSplitter(Qt.Vertical)
        splitter.setHandleWidth(1)
        splitter.setStyleSheet("QSplitter::handle { background-color: #313244; }")

        # --- 상단: 보유 포지션 ---
        pos_widget = QWidget()
        pos_layout = QVBoxLayout(pos_widget)
        pos_layout.setContentsMargins(12, 8, 12, 4)
        pos_layout.setSpacing(4)

        pos_header = QHBoxLayout()
        pos_header.addWidget(self._section_label("보유 종목"))
        pos_header.addStretch()
        pos_layout.addLayout(pos_header)

        self.positions_table = self._make_table(
            ["종목코드", "종목명", "수량", "매수가", "현재가", "평가금액", "수익률", "손절가", "목표가"]
        )
        pos_layout.addWidget(self.positions_table)
        splitter.addWidget(pos_widget)

        # --- 중단: 후보 / 체결 ---
        bottom_widget = QWidget()
        bottom_layout = QHBoxLayout(bottom_widget)
        bottom_layout.setContentsMargins(12, 4, 12, 4)
        bottom_layout.setSpacing(8)

        # 매수 후보
        cand_widget = QVBoxLayout()
        cand_widget.setSpacing(4)
        cand_widget.addWidget(self._section_label("매수 후보"))
        self.candidates_table = self._make_table(["종목코드", "종목명"])
        cand_widget.addWidget(self.candidates_table)
        bottom_layout.addLayout(cand_widget)

        # 당일 체결
        trade_widget = QVBoxLayout()
        trade_widget.setSpacing(4)
        trade_widget.addWidget(self._section_label("당일 체결"))
        self.trades_table = self._make_table(["시간", "종목코드", "구분", "가격", "손익", "사유"])
        trade_widget.addWidget(self.trades_table)
        bottom_layout.addLayout(trade_widget)

        splitter.addWidget(bottom_widget)

        # --- 하단: 로그 ---
        log_widget = QWidget()
        log_layout = QVBoxLayout(log_widget)
        log_layout.setContentsMargins(12, 4, 12, 8)
        log_layout.setSpacing(4)

        log_header = QHBoxLayout()
        log_header.addWidget(self._section_label("로그"))
        log_header.addStretch()

        self._btn_log_clear = QPushButton("지우기")
        self._btn_log_clear.setFixedSize(60, 24)
        self._btn_log_clear.clicked.connect(self._on_log_clear)
        log_header.addWidget(self._btn_log_clear)

        self._btn_autoscroll = QPushButton("자동 스크롤: ON")
        self._btn_autoscroll.setFixedSize(110, 24)
        self._btn_autoscroll.clicked.connect(self._toggle_autoscroll)
        log_header.addWidget(self._btn_autoscroll)

        log_layout.addLayout(log_header)

        self.log_view = QTextEdit()
        self.log_view.setObjectName("logView")
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumHeight(180)
        log_layout.addWidget(self.log_view)

        splitter.addWidget(log_widget)

        # 스플리터 비율 (포지션:후보체결:로그 = 4:3:2)
        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 3)
        splitter.setStretchFactor(2, 2)

        layout.addWidget(splitter)

        # 로그 상태
        self._autoscroll = True
        self._log_lines: list[str] = []

    # ── 헬퍼 ──

    def _section_label(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setStyleSheet(
            f"color: {_SUBTEXT}; font-size: 12px; font-weight: bold; "
            "padding: 2px 0px;"
        )
        return label

    def _make_table(self, headers: list[str]) -> QTableWidget:
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setAlternatingRowColors(True)
        table.setSelectionBehavior(QTableWidget.SelectRows)
        table.setShowGrid(False)
        h = table.horizontalHeader()
        h.setSectionResizeMode(QHeaderView.Stretch)
        h.setDefaultAlignment(Qt.AlignCenter)
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.verticalHeader().setVisible(False)
        return table

    # ── 로그 ──

    def append_log(self, message: str, level: str = "INFO"):
        """로그 메시지 추가 (MainWindow에서 호출)."""
        color = {
            "ERROR": _RED,
            "WARNING": _YELLOW,
            "DEBUG": _SUBTEXT,
        }.get(level, "#a6adc8")

        safe_msg = html.escape(message)
        self.log_view.append(f'<span style="color:{color}">{safe_msg}</span>')

        if self._autoscroll:
            sb = self.log_view.verticalScrollBar()
            sb.setValue(sb.maximum())

    def _on_log_clear(self):
        self.log_view.clear()

    def _toggle_autoscroll(self):
        self._autoscroll = not self._autoscroll
        self._btn_autoscroll.setText(
            "자동 스크롤: ON" if self._autoscroll else "자동 스크롤: OFF"
        )

    # ── 데이터 업데이트 ──

    def update_status(self, status: dict):
        """요약 통계 업데이트."""
        capital = status.get("capital", 0)
        candidates = status.get("candidates", 0)
        self._max_positions = status.get("max_positions", self._max_positions)

        pnl = status.get("daily_pnl_pct", 0.0)
        pnl_color = _GREEN if pnl >= 0 else _RED
        self._lbl_pnl.setText(f"일일 손익: {pnl:+.2f}%")
        self._lbl_pnl.setStyleSheet(
            f"color: {pnl_color}; font-size: 12px; font-weight: bold;"
        )

        mdd = status.get("mdd", 0.0)
        mdd_color = _RED if mdd < -5 else _SUBTEXT
        self._lbl_mdd.setText(f"MDD: {mdd:.1f}%")
        self._lbl_mdd.setStyleSheet(f"color: {mdd_color}; font-size: 12px;")

        # 요약 라인은 positions 업데이트에서 갱신
        self._capital = capital
        self._candidates_count = candidates

    def update_positions(self, positions: list):
        """포지션 테이블 + 요약 통계 업데이트."""
        self.positions_table.setRowCount(len(positions))

        total_eval = 0
        for row, pos in enumerate(positions):
            entry_price = pos.get("entry_price", 0)
            current_price = pos.get("current_price", entry_price)
            qty = pos.get("quantity", 0)
            eval_amount = current_price * qty
            total_eval += eval_amount

            if entry_price > 0:
                pnl_pct = (current_price - entry_price) / entry_price * 100
            else:
                pnl_pct = 0.0

            items = [
                pos.get("code", ""),
                pos.get("name", ""),
                f"{qty:,}",
                f"{entry_price:,}",
                f"{current_price:,}",
                f"{eval_amount:,}",
                f"{pnl_pct:+.2f}%",
                f"{pos.get('stop_price', 0):,}",
                f"{pos.get('target_price', 0):,}",
            ]
            for col, text in enumerate(items):
                item = QTableWidgetItem(text)
                item.setTextAlignment(Qt.AlignCenter)
                if col == 6:  # 수익률
                    color = _GREEN if pnl_pct >= 0 else _RED
                    item.setForeground(QColor(color))
                self.positions_table.setItem(row, col, item)

        # 요약 통계 갱신
        capital = getattr(self, "_capital", 0)
        cand = getattr(self, "_candidates_count", 0)
        self._lbl_summary.setText(
            f"총 평가: {total_eval:,}원 | "
            f"가용자금: {capital:,}원 | "
            f"포지션: {len(positions)}/{self._max_positions} | "
            f"후보: {cand}종목"
        )

    # 청산사유 한글 매핑
    _EXIT_REASON_KR = {
        "stop_loss": "손절",
        "trailing_stop": "트레일링",
        "target_reached": "목표가",
        "partial_target": "부분매도",
        "macd_dead": "MACD역전",
        "max_hold": "보유초과",
        "signal": "매수",
    }

    def update_trades(self, trades: list):
        """체결 내역 테이블 업데이트."""
        self.trades_table.setRowCount(len(trades))
        for row, trade in enumerate(trades):
            time_str = trade.get("executed_at", "")[-8:]
            pnl = trade.get("pnl", 0)
            side = trade.get("side", "")
            side_kr = "매수" if side == "buy" else "매도"
            pnl_str = f"{pnl:+,.0f}" if side == "sell" else ""

            reason_raw = trade.get("reason", "")
            reason_kr = self._EXIT_REASON_KR.get(reason_raw, reason_raw)

            items = [time_str, trade.get("code", ""), side_kr,
                     f"{trade.get('price', 0):,}", pnl_str, reason_kr]

            for col, text in enumerate(items):
                item = QTableWidgetItem(text)
                item.setTextAlignment(Qt.AlignCenter)
                if col == 2:
                    color = _GREEN if side_kr == "매수" else _RED
                    item.setForeground(QColor(color))
                if col == 4 and pnl != 0:
                    item.setForeground(QColor(_GREEN if pnl > 0 else _RED))
                if col == 5 and reason_raw == "partial_target":
                    item.setForeground(QColor("#fab387"))  # 부분매도: 오렌지
                self.trades_table.setItem(row, col, item)

    def update_candidates(self, candidates: list):
        """매수 후보 테이블 업데이트."""
        self.candidates_table.setRowCount(len(candidates))
        for row, cand in enumerate(candidates):
            code_item = QTableWidgetItem(cand.get("code", ""))
            code_item.setTextAlignment(Qt.AlignCenter)
            name_item = QTableWidgetItem(cand.get("name", ""))
            name_item.setTextAlignment(Qt.AlignCenter)
            self.candidates_table.setItem(row, 0, code_item)
            self.candidates_table.setItem(row, 1, name_item)
