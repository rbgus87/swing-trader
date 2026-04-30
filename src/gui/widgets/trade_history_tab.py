"""매매 이력 탭 — Catppuccin Mocha 다크 테마."""

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

# ── Catppuccin Mocha palette ──
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

# 사유 한글 매핑 (v2.6 + legacy)
_EXIT_REASON_KR = {
    "STOP_LOSS": "손절",
    "TAKE_PROFIT_1": "TP1 분할(10%)",
    "TAKE_PROFIT_2": "TP2 분할(10%)",
    "TRAILING": "트레일링",
    "TREND_EXIT": "추세이탈",
    "TIME_EXIT": "시간청산",
    "FINAL_CLOSE": "강제청산",
    "stop_loss": "손절",
    "partial_target": "TP1 분할(10%)",
    "partial_target_2": "TP2 분할(10%)",
    "target_reached": "목표가",
    "trailing_stop": "트레일링",
    "trend_exit": "추세이탈",
    "max_hold": "시간청산",
    "macd_dead": "데드크로스",
    "signal": "매수신호",
}

# 전략 한글 매핑 (v2.6)
_STRATEGY_KR = {"TF": "추세추종 v2.6", "TF_v2.3": "추세추종 v2.6"}


class TradeHistoryTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._trades: list[dict] = []
        self._stats: dict[str, QLabel] = {}
        self._init_ui()
        self._load_data()

    # ── UI 구성 ──

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(0)
        layout.setContentsMargins(0, 0, 0, 0)

        # 상단 필터 바
        filter_bar = QWidget()
        filter_bar.setStyleSheet(f"background-color: {_MANTLE};")
        filter_layout = QHBoxLayout(filter_bar)
        filter_layout.setContentsMargins(12, 8, 12, 8)
        filter_layout.setSpacing(8)

        filter_layout.addWidget(QLabel("기간:"))
        self._combo_period = QComboBox()
        self._combo_period.addItems(["전체", "1주", "1개월", "3개월"])
        self._combo_period.currentTextChanged.connect(self._on_filter_changed)
        filter_layout.addWidget(self._combo_period)

        filter_layout.addWidget(QLabel("전략:"))
        self._combo_strategy = QComboBox()
        self._combo_strategy.addItems(["전체", "TF"])
        self._combo_strategy.currentTextChanged.connect(self._on_filter_changed)
        filter_layout.addWidget(self._combo_strategy)

        filter_layout.addStretch()

        self._btn_refresh = QPushButton("새로고침")
        self._btn_refresh.setObjectName("manualBtn")
        self._btn_refresh.clicked.connect(self._load_data)
        filter_layout.addWidget(self._btn_refresh)

        layout.addWidget(filter_bar)

        # 중단 테이블
        table_widget = QWidget()
        table_layout = QVBoxLayout(table_widget)
        table_layout.setContentsMargins(12, 8, 12, 4)
        table_layout.setSpacing(4)

        table_header = QHBoxLayout()
        table_header.addWidget(self._section_label("매매 이력"))
        table_header.addStretch()
        self._lbl_trade_count = QLabel("0건")
        self._lbl_trade_count.setStyleSheet(f"color: {_SUBTEXT}; font-size: 11px;")
        table_header.addWidget(self._lbl_trade_count)
        table_layout.addLayout(table_header)

        self._table = QTableWidget(0, 10)
        self._table.setHorizontalHeaderLabels(
            ["일시", "종목코드", "종목명", "구분", "수량", "가격", "수익률", "손익(원)", "전략", "청산사유"]
        )
        self._table.setAlternatingRowColors(True)
        self._table.setSelectionBehavior(QTableWidget.SelectRows)
        self._table.setShowGrid(False)
        h = self._table.horizontalHeader()
        h.setSectionResizeMode(QHeaderView.Stretch)
        h.setDefaultAlignment(Qt.AlignCenter)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.verticalHeader().setVisible(False)
        table_layout.addWidget(self._table)

        layout.addWidget(table_widget, stretch=1)

        # 하단 통계 카드
        stat_area = QWidget()
        stat_area.setStyleSheet(f"background-color: {_MANTLE};")
        stat_layout = QHBoxLayout(stat_area)
        stat_layout.setContentsMargins(12, 10, 12, 10)
        stat_layout.setSpacing(10)

        stat_defs = [
            ("total", "총 거래", "0건", _TEXT),
            ("winrate", "승률", "0.0%", _BLUE),
            ("avg_win", "평균 수익", "+0.00%", _GREEN),
            ("avg_loss", "평균 손실", "-0.00%", _RED),
            ("total_pnl", "총 손익", "0원", _TEXT),
            ("pf", "손익비", "0.00", _YELLOW),
        ]
        for key, label, default, color in stat_defs:
            card, val_label, _ = self._make_stat_card(label, default, color)
            self._stats[key] = val_label
            stat_layout.addWidget(card)

        layout.addWidget(stat_area)

    # ── 헬퍼 ──

    def _make_stat_card(self, label: str, value: str, color: str):
        card = QFrame()
        card.setObjectName("historyStatCard")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(10, 6, 10, 6)
        card_layout.setSpacing(2)

        lbl_value = QLabel(value)
        lbl_value.setStyleSheet(f"color: {color}; font-size: 16px; font-weight: bold;")
        lbl_value.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        card_layout.addWidget(lbl_value)

        lbl_label = QLabel(label.upper())
        lbl_label.setStyleSheet(f"color: {_SUBTEXT}; font-size: 10px; letter-spacing: 1px;")
        lbl_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        card_layout.addWidget(lbl_label)

        return card, lbl_value, lbl_label

    def _section_label(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("sectionHeader")
        return label

    # ── 데이터 ──

    def _load_data(self):
        """매매 기록 로드 — CLOSED 포지션은 매수+매도 2행, OPEN은 매수 1행.

        - swing_trade.db.positions: 보유 + 청산 이력
        - swing_trade.db.trades(side='sell'): 매도 가격/손익/사유 매칭
        - swing_data.db.stocks: 종목명 매핑
        """
        trades: list[dict] = []
        try:
            from src.data_pipeline.db import get_data_db, get_trade_db

            stock_names: dict[str, str] = {}
            try:
                with get_data_db() as sconn:
                    for r in sconn.execute(
                        "SELECT ticker, name FROM stocks"
                    ).fetchall():
                        stock_names[r['ticker']] = r['name']
            except Exception:
                pass

            with get_trade_db() as conn:
                # OPEN/SELLING 포지션 — 매수 행만 (보유 중)
                open_rows = conn.execute(
                    "SELECT * FROM positions "
                    "WHERE status IN ('open', 'selling') "
                    "ORDER BY entry_date DESC"
                ).fetchall()
                for p in open_rows:
                    d = dict(p)
                    code = d.get('code', '')
                    trades.append({
                        "executed_at": str(d.get('entry_date') or ''),
                        "code": code,
                        "name": d.get('name') or stock_names.get(code, code),
                        "side": "buy",
                        "quantity": int(d.get('quantity') or 0),
                        "price": int(d.get('entry_price') or 0),
                        "pnl": 0,
                        "pnl_pct": 0,
                        "entry_strategy": d.get('entry_strategy') or 'TF',
                        "reason": "보유 중",
                    })

                # CLOSED 포지션 — 매수 행 + 매도 행 (exit_date 기준 최신순)
                closed_rows = conn.execute(
                    "SELECT * FROM positions WHERE status = 'closed' "
                    "ORDER BY entry_date DESC LIMIT 500"
                ).fetchall()

                # 매도 trade를 code+entry_date로 매칭 (재진입 고려)
                for p in closed_rows:
                    d = dict(p)
                    code = d.get('code', '')
                    entry_date = str(d.get('entry_date') or '')
                    name = d.get('name') or stock_names.get(code, code)
                    entry_price = int(d.get('entry_price') or 0)
                    strategy = d.get('entry_strategy') or 'TF'

                    sell = conn.execute(
                        "SELECT * FROM trades "
                        "WHERE code = ? AND side = 'sell' AND executed_at >= ? "
                        "ORDER BY executed_at ASC LIMIT 1",
                        (code, entry_date),
                    ).fetchone()

                    if sell:
                        sd = dict(sell)
                        exit_at = str(sd.get('executed_at') or '')
                        exit_price = int(sd.get('price') or 0)
                        pnl = float(sd.get('pnl') or 0)
                        pnl_pct = float(sd.get('pnl_pct') or 0)
                        reason = sd.get('reason') or ''
                        sell_qty = int(sd.get('quantity') or d.get('quantity') or 0)
                    else:
                        exit_at = ''
                        exit_price = 0
                        pnl = 0.0
                        pnl_pct = 0.0
                        reason = ''
                        sell_qty = int(d.get('quantity') or 0)

                    # 매수 행
                    trades.append({
                        "executed_at": entry_date,
                        "code": code,
                        "name": name,
                        "side": "buy",
                        "quantity": sell_qty,
                        "price": entry_price,
                        "pnl": 0,
                        "pnl_pct": 0,
                        "entry_strategy": strategy,
                        "reason": "",
                    })
                    # 매도 행
                    trades.append({
                        "executed_at": exit_at,
                        "code": code,
                        "name": name,
                        "side": "sell",
                        "quantity": sell_qty,
                        "price": exit_price,
                        "pnl": pnl,
                        "pnl_pct": pnl_pct,
                        "entry_strategy": strategy,
                        "reason": reason,
                    })
        except Exception:
            import traceback
            traceback.print_exc()
            trades = []
        self._trades = trades
        self._apply_filters()

    def _on_filter_changed(self):
        self._apply_filters()

    def _apply_filters(self):
        """필터 적용 후 테이블 + 통계 갱신."""
        filtered = list(self._trades)

        # 기간 필터
        period = self._combo_period.currentText()
        if period != "전체":
            from datetime import datetime, timedelta

            now = datetime.now()
            if period == "1주":
                cutoff = now - timedelta(days=7)
            elif period == "1개월":
                cutoff = now - timedelta(days=30)
            elif period == "3개월":
                cutoff = now - timedelta(days=90)
            else:
                cutoff = None
            if cutoff:
                cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M:%S")
                filtered = [t for t in filtered if (t.get("executed_at") or "") >= cutoff_str]

        # 전략 필터
        strategy = self._combo_strategy.currentText()
        if strategy != "전체":
            filtered = [t for t in filtered if t.get("entry_strategy") == strategy]

        self._update_table(filtered)
        self._update_stats(filtered)

    def _update_table(self, trades: list[dict]):
        self._table.setRowCount(len(trades))
        for row, trade in enumerate(trades):
            time_str = trade.get("executed_at", "")[:16]  # YYYY-MM-DD HH:MM
            side = trade.get("side", "")
            side_kr = "매수" if side == "buy" else "매도"
            pnl = trade.get("pnl", 0) or 0
            pnl_pct = trade.get("pnl_pct", 0) or 0
            reason_raw = trade.get("reason", "")
            reason_kr = _EXIT_REASON_KR.get(reason_raw, reason_raw)
            strategy_kr = _STRATEGY_KR.get(
                trade.get("entry_strategy", ""), trade.get("entry_strategy", "")
            )

            items = [
                time_str,
                trade.get("code", ""),
                trade.get("name", ""),
                side_kr,
                f"{trade.get('quantity', 0):,}",
                f"{trade.get('price', 0):,}",
                f"{pnl_pct * 100:+.2f}%" if side == "sell" else "",
                f"{pnl:+,.0f}" if side == "sell" else "",
                strategy_kr,
                reason_kr,
            ]
            for col, text in enumerate(items):
                item = QTableWidgetItem(str(text))
                item.setTextAlignment(Qt.AlignCenter)
                if col == 3:  # 구분
                    color = _GREEN if side_kr == "매수" else _RED
                    item.setForeground(QColor(color))
                if col == 6 and side == "sell":  # 수익률
                    item.setForeground(QColor(_GREEN if pnl_pct > 0 else _RED))
                if col == 7 and side == "sell":  # 손익
                    item.setForeground(QColor(_GREEN if pnl > 0 else _RED))
                self._table.setItem(row, col, item)

        self._lbl_trade_count.setText(f"{len(trades)}건")

    def _update_stats(self, trades: list[dict]):
        sells = [t for t in trades if t.get("side") == "sell"]
        if not sells:
            self._stats["total"].setText("0건")
            self._stats["winrate"].setText("0.0%")
            self._stats["avg_win"].setText("+0.00%")
            self._stats["avg_loss"].setText("-0.00%")
            self._stats["total_pnl"].setText("0원")
            self._stats["pf"].setText("0.00")
            return

        wins = [t for t in sells if (t.get("pnl") or 0) > 0]
        losses = [t for t in sells if (t.get("pnl") or 0) <= 0]
        total_pnl = sum(t.get("pnl", 0) or 0 for t in sells)
        win_rate = len(wins) / len(sells) * 100 if sells else 0
        avg_win = sum((t.get("pnl_pct") or 0) for t in wins) / len(wins) * 100 if wins else 0
        avg_loss = sum(abs(t.get("pnl_pct") or 0) for t in losses) / len(losses) * 100 if losses else 0
        loss_sum = abs(sum((t.get("pnl") or 0) for t in losses))
        profit_factor = (sum((t.get("pnl") or 0) for t in wins) / loss_sum) if loss_sum > 0 else 0

        self._stats["total"].setText(f"{len(sells)}건")
        self._stats["winrate"].setText(f"{win_rate:.1f}%")
        self._stats["avg_win"].setText(f"+{avg_win:.2f}%")
        self._stats["avg_loss"].setText(f"-{avg_loss:.2f}%")

        pnl_color = _GREEN if total_pnl >= 0 else _RED
        self._stats["total_pnl"].setText(f"{total_pnl:+,.0f}원")
        self._stats["total_pnl"].setStyleSheet(
            f"color: {pnl_color}; font-size: 16px; font-weight: bold;"
        )

        self._stats["pf"].setText(f"{profit_factor:.2f}")

    def _update_stat(self, key: str, text: str):
        if key in self._stats:
            self._stats[key].setText(text)
