"""대시보드 탭 — 스탯 카드 + 보유 포지션 + 후보/체결 + 차트 + 로그.

v4 리디자인: 카드형 요약 통계, 차트 영역, 개선된 레이아웃
"""

import html

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor, QFont
from PyQt5.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
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
_BASE = "#1e1e2e"
_MANTLE = "#181825"
_CRUST = "#11111b"


class DashboardTab(QWidget):
    """대시보드 탭 — 스탯 카드 + 포트폴리오 + 차트 + 로그."""

    def __init__(self, parent=None):
        super().__init__(parent)
        from src.utils.config import config
        self._max_positions = config.get("trading.max_positions", 8)
        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(0)
        layout.setContentsMargins(0, 0, 0, 0)

        # ── 스탯 카드 영역 ──
        stat_area = QWidget()
        stat_area.setStyleSheet(f"background-color: {_MANTLE};")
        stat_layout = QHBoxLayout(stat_area)
        stat_layout.setContentsMargins(12, 10, 12, 10)
        stat_layout.setSpacing(10)

        self._stat_eval = self._make_stat_card("💰 총 평가", "0원", _TEXT)
        self._stat_avail = self._make_stat_card("💵 가용자금", "0원", _TEXT)
        self._stat_pos = self._make_stat_card("📊 포지션", f"0/{self._max_positions}", _BLUE)
        self._stat_cand = self._make_stat_card("🔍 후보", "0종목", _SUBTEXT)
        self._stat_pnl = self._make_stat_card("📈 일일 손익", "+0.00%", _GREEN)
        self._stat_mdd = self._make_stat_card("📉 MDD", "0.0%", _SUBTEXT)

        for card, _, _ in [
            self._stat_eval, self._stat_avail, self._stat_pos,
            self._stat_cand, self._stat_pnl, self._stat_mdd,
        ]:
            stat_layout.addWidget(card)

        layout.addWidget(stat_area)

        # ── 메인 영역 (수직 스플리터) ──
        splitter = QSplitter(Qt.Vertical)
        splitter.setHandleWidth(1)

        # --- 상단: 보유 포지션 ---
        pos_widget = QWidget()
        pos_layout = QVBoxLayout(pos_widget)
        pos_layout.setContentsMargins(12, 8, 12, 4)
        pos_layout.setSpacing(4)

        pos_header = QHBoxLayout()
        pos_header.addWidget(self._section_label("보유 종목"))
        pos_header.addStretch()
        self._lbl_pos_count = QLabel("0종목")
        self._lbl_pos_count.setStyleSheet(f"color: {_SUBTEXT}; font-size: 11px;")
        pos_header.addWidget(self._lbl_pos_count)
        pos_layout.addLayout(pos_header)

        self.positions_table = self._make_table(
            ["종목코드", "종목명", "전략", "보유일", "수량", "매수가", "현재가", "평가금액", "수익률", "손절가"]
        )
        pos_layout.addWidget(self.positions_table)
        splitter.addWidget(pos_widget)

        # --- 중단: 후보 / 체결 (좌우 분할) ---
        mid_widget = QWidget()
        mid_layout = QHBoxLayout(mid_widget)
        mid_layout.setContentsMargins(12, 4, 12, 4)
        mid_layout.setSpacing(8)

        # 매수 후보
        cand_widget = QVBoxLayout()
        cand_widget.setSpacing(4)
        cand_header = QHBoxLayout()
        cand_header.addWidget(self._section_label("매수 후보"))
        cand_header.addStretch()
        self._lbl_cand_count = QLabel("0건")
        self._lbl_cand_count.setStyleSheet(f"color: {_SUBTEXT}; font-size: 11px;")
        cand_header.addWidget(self._lbl_cand_count)
        cand_widget.addLayout(cand_header)
        self.candidates_table = self._make_table(["종목코드", "종목명", "현재가", "등락률", "점수"])
        cand_widget.addWidget(self.candidates_table)
        mid_layout.addLayout(cand_widget)

        # 당일 체결
        trade_widget = QVBoxLayout()
        trade_widget.setSpacing(4)
        trade_header = QHBoxLayout()
        trade_header.addWidget(self._section_label("당일 체결"))
        trade_header.addStretch()
        self._lbl_trade_count = QLabel("0건")
        self._lbl_trade_count.setStyleSheet(f"color: {_SUBTEXT}; font-size: 11px;")
        trade_header.addWidget(self._lbl_trade_count)
        trade_widget.addLayout(trade_header)
        self.trades_table = self._make_table(["시간", "종목코드", "종목명", "구분", "수량", "가격", "손익", "사유"])
        trade_widget.addWidget(self.trades_table)
        mid_layout.addLayout(trade_widget)

        splitter.addWidget(mid_widget)

        # --- 하단: 차트 + 로그 (좌우 분할) ---
        bottom_widget = QWidget()
        bottom_layout = QHBoxLayout(bottom_widget)
        bottom_layout.setContentsMargins(12, 4, 12, 8)
        bottom_layout.setSpacing(8)

        # 차트 영역 (pyqtgraph가 없으면 플레이스홀더)
        chart_area = QVBoxLayout()
        chart_area.setSpacing(4)
        chart_area.addWidget(self._section_label("수익 곡선"))

        self._chart_container = QFrame()
        self._chart_container.setObjectName("chartContainer")
        self._chart_container.setMinimumHeight(120)
        chart_inner = QVBoxLayout(self._chart_container)
        chart_inner.setContentsMargins(4, 4, 4, 4)

        try:
            import pyqtgraph as pg
            pg.setConfigOptions(antialias=True)

            self._chart_widget = pg.PlotWidget()
            self._chart_widget.setBackground(_CRUST)
            self._chart_widget.showGrid(x=False, y=True, alpha=0.1)
            self._chart_widget.getAxis("left").setPen(pg.mkPen(_SUBTEXT))
            self._chart_widget.getAxis("bottom").setPen(pg.mkPen(_SUBTEXT))
            self._chart_widget.getAxis("left").setTextPen(pg.mkPen(_SUBTEXT))
            self._chart_widget.getAxis("bottom").setTextPen(pg.mkPen(_SUBTEXT))
            self._chart_widget.setLabel("left", "수익률 (%)")
            self._chart_widget.hideAxis("bottom")

            # 빈 곡선
            self._equity_curve = self._chart_widget.plot(
                [], [], pen=pg.mkPen(color=_BLUE, width=2)
            )
            self._zero_line = self._chart_widget.addLine(
                y=0, pen=pg.mkPen(color=_SUBTEXT, width=1, style=Qt.DashLine)
            )

            chart_inner.addWidget(self._chart_widget)
            self._has_chart = True
        except ImportError:
            placeholder = QLabel("pyqtgraph 미설치\npip install pyqtgraph")
            placeholder.setAlignment(Qt.AlignCenter)
            placeholder.setStyleSheet(f"color: {_SUBTEXT}; font-size: 11px;")
            chart_inner.addWidget(placeholder)
            self._has_chart = False

        chart_area.addWidget(self._chart_container)
        bottom_layout.addLayout(chart_area, stretch=1)

        # 미니 로그 영역
        log_area = QVBoxLayout()
        log_area.setSpacing(4)
        log_area.addWidget(self._section_label("로그"))

        self._mini_log = QTextEdit()
        self._mini_log.setObjectName("miniLog")
        self._mini_log.setReadOnly(True)
        self._mini_log.setMinimumHeight(120)
        log_area.addWidget(self._mini_log)

        bottom_layout.addLayout(log_area, stretch=1)
        splitter.addWidget(bottom_widget)

        # 스플리터 비율 (포지션:후보체결:차트로그 = 4:3:3)
        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 3)
        splitter.setStretchFactor(2, 3)

        layout.addWidget(splitter)

        # 미니 로그 상태
        self._mini_log_lines: list[str] = []

        # 차트 데이터
        self._equity_data: list[float] = []

    # ── 스탯 카드 ──

    def _make_stat_card(self, label: str, value: str, color: str):
        """스탯 카드 위젯 생성. (card, value_label, label_label) 반환."""
        card = QFrame()
        card.setObjectName("statCard")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(10, 6, 10, 6)
        card_layout.setSpacing(2)

        lbl_value = QLabel(value)
        lbl_value.setObjectName("statValue")
        lbl_value.setStyleSheet(f"color: {color}; font-size: 18px; font-weight: bold;")
        lbl_value.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        card_layout.addWidget(lbl_value)

        lbl_label = QLabel(label.upper())
        lbl_label.setObjectName("statLabel")
        lbl_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        card_layout.addWidget(lbl_label)

        return card, lbl_value, lbl_label

    # ── 헬퍼 ──

    def _section_label(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("sectionHeader")
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
        """로그 메시지 추가 (MainWindow에서 호출). 최근 10줄만 유지."""
        color = {
            "ERROR": _RED,
            "WARNING": _YELLOW,
            "DEBUG": _SUBTEXT,
            "TRADE": _GREEN,
        }.get(level, "#a6adc8")

        safe_msg = html.escape(message)
        self._mini_log_lines.append(f'<span style="color:{color}">{safe_msg}</span>')
        if len(self._mini_log_lines) > 10:
            self._mini_log_lines = self._mini_log_lines[-10:]
        self._mini_log.setHtml("<br>".join(self._mini_log_lines))

        sb = self._mini_log.verticalScrollBar()
        sb.setValue(sb.maximum())

    # ── 데이터 업데이트 ──

    def update_status(self, status: dict):
        """요약 통계 업데이트."""
        capital = status.get("capital", 0)
        candidates = status.get("candidates", 0)
        self._max_positions = status.get("max_positions", self._max_positions)

        # 가용자금 카드
        _, avail_val, _ = self._stat_avail
        avail_val.setText(f"{capital:,}원")

        # 후보 카드
        _, cand_val, _ = self._stat_cand
        cand_val.setText(f"{candidates}종목")

        # 포지션 카드 (positions_update에서도 갱신)
        # 일일 손익 카드
        pnl = status.get("daily_pnl_pct", 0.0)
        pnl_color = _GREEN if pnl >= 0 else _RED
        _, pnl_val, _ = self._stat_pnl
        pnl_val.setText(f"{pnl:+.2f}%")
        pnl_val.setStyleSheet(
            f"color: {pnl_color}; font-size: 18px; font-weight: bold;"
        )

        # MDD 카드
        mdd = status.get("mdd", 0.0)
        mdd_color = _RED if mdd < -5 else _SUBTEXT
        _, mdd_val, _ = self._stat_mdd
        mdd_val.setText(f"{mdd:.1f}%")
        mdd_val.setStyleSheet(
            f"color: {mdd_color}; font-size: 18px; font-weight: bold;"
        )

        # 내부 저장
        self._capital = capital
        self._candidates_count = candidates

        # 차트 데이터 업데이트
        if self._has_chart and pnl != 0.0:
            self._equity_data.append(pnl)
            self._update_chart()

    def _update_chart(self):
        """수익 곡선 차트 업데이트."""
        if not self._has_chart or not self._equity_data:
            return
        import pyqtgraph as pg

        # 누적 수익률
        cumulative = []
        total = 0.0
        for d in self._equity_data:
            total += d
            cumulative.append(total)

        self._equity_curve.setData(
            list(range(len(cumulative))), cumulative,
            pen=pg.mkPen(color=_BLUE, width=2)
        )

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

            strategy_kr = {
                "golden_cross": "골든크로스",
                "disparity_reversion": "이격도",
            }
            entry_strat = pos.get("entry_strategy", "")
            strat_display = strategy_kr.get(entry_strat, entry_strat)

            hold_days = pos.get("hold_days", 0)
            items = [
                pos.get("code", ""),
                pos.get("name", ""),
                strat_display,
                f"D+{hold_days}",
                f"{qty:,}",
                f"{entry_price:,}",
                f"{current_price:,}",
                f"{eval_amount:,}",
                f"{pnl_pct:+.2f}%",
                f"{pos.get('stop_price', 0):,}",
            ]
            for col, text in enumerate(items):
                item = QTableWidgetItem(text)
                item.setTextAlignment(Qt.AlignCenter)
                if col == 8:  # 수익률 (was 7, now 8)
                    color = _GREEN if pnl_pct >= 0 else _RED
                    item.setForeground(QColor(color))
                if col == 2:  # 전략 컬럼 색상
                    if entry_strat == "golden_cross":
                        item.setForeground(QColor(_GREEN))
                    elif entry_strat == "disparity_reversion":
                        item.setForeground(QColor(_YELLOW))
                self.positions_table.setItem(row, col, item)

        # 스탯 카드 갱신
        _, eval_val, _ = self._stat_eval
        eval_val.setText(f"{total_eval:,}원")

        _, pos_val, _ = self._stat_pos
        pos_val.setText(f"{len(positions)}/{self._max_positions}")
        pos_color = _BLUE if len(positions) < self._max_positions else _YELLOW
        pos_val.setStyleSheet(
            f"color: {pos_color}; font-size: 18px; font-weight: bold;"
        )

        # 카운트 라벨
        self._lbl_pos_count.setText(f"{len(positions)}종목")

    # 청산사유 한글 매핑
    _EXIT_REASON_KR = {
        "stop_loss": "손절",
        "trailing_stop": "트레일링",
        "target_reached": "목표가",
        "partial_target": "부분매도",
        "macd_dead": "데드크로스",
        "max_hold": "보유초과",
        "flow_exit": "수급이탈",
        "disparity_exit": "이격도청산",
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

            items = [time_str, trade.get("code", ""), trade.get("name", ""), side_kr,
                     f"{trade.get('quantity', 0):,}", f"{trade.get('price', 0):,}", pnl_str, reason_kr]

            for col, text in enumerate(items):
                item = QTableWidgetItem(text)
                item.setTextAlignment(Qt.AlignCenter)
                if col == 3:  # 구분
                    color = _GREEN if side_kr == "매수" else _RED
                    item.setForeground(QColor(color))
                if col == 6 and pnl != 0:  # 손익
                    item.setForeground(QColor(_GREEN if pnl > 0 else _RED))
                if col == 7 and reason_raw == "partial_target":  # 사유
                    item.setForeground(QColor(_PEACH))
                self.trades_table.setItem(row, col, item)

        self._lbl_trade_count.setText(f"{len(trades)}건")

    def update_candidates(self, candidates: list):
        """매수 후보 테이블 업데이트."""
        self.candidates_table.setRowCount(len(candidates))
        for row, cand in enumerate(candidates):
            price = cand.get("price", 0)
            change_pct = cand.get("change_pct", 0)
            score = cand.get("score", 0)

            items = [
                cand.get("code", ""),
                cand.get("name", ""),
                f"{price:,}" if price else "",
                f"{change_pct:+.2f}%" if change_pct else "",
                f"{score:.1f}" if score else "",
            ]
            for col, text in enumerate(items):
                item = QTableWidgetItem(text)
                item.setTextAlignment(Qt.AlignCenter)
                if col == 3 and change_pct:  # 등락률 — 한국식: 양수 빨강, 음수 파랑
                    color = _RED if change_pct > 0 else _BLUE
                    item.setForeground(QColor(color))
                self.candidates_table.setItem(row, col, item)
        self._lbl_cand_count.setText(f"{len(candidates)}건")
