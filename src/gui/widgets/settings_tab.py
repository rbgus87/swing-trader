"""설정 탭 — config.yaml 편집 + .env 관리.

모든 슬라이더 값은 ×1000 스케일로 저장/로드하여 float 정밀도 유지.
.env 파일 쓰기를 지원하여 API 키 변경사항도 정상 저장.
"""

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv, set_key
from PyQt5.QtCore import Qt, QLocale, QThread, pyqtSignal
from PyQt5.QtWidgets import (
    QComboBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)


class _StockNameWorker(QThread):
    """백그라운드에서 종목 코드 → 종목명 조회."""
    finished = pyqtSignal(dict)

    def __init__(self, codes: list, parent=None):
        super().__init__(parent)
        self._codes = codes

    def run(self):
        result = {}
        try:
            from pykrx import stock
            for code in self._codes:
                try:
                    name = stock.get_market_ticker_name(code)
                    if name:
                        result[code] = name
                except Exception:
                    pass
        except Exception:
            pass
        self.finished.emit(result)


class _StockSearchWorker(QThread):
    """백그라운드에서 전종목 리스트를 캐시 파일로 빌드."""
    finished = pyqtSignal(dict)
    _CACHE_FILE = Path("data/stock_names.json")

    def run(self):
        import json
        cache = {}
        # 1) 캐시 파일이 있으면 로드
        if self._CACHE_FILE.exists():
            try:
                with open(self._CACHE_FILE, "r", encoding="utf-8") as f:
                    cache = json.load(f)
                if cache:
                    self.finished.emit(cache)
                    return
            except Exception:
                pass
        # 2) pykrx에서 전종목 리스트 시도
        try:
            from pykrx import stock
            import datetime
            today = datetime.date.today()
            for delta in range(0, 7):
                d = (today - datetime.timedelta(days=delta)).strftime("%Y%m%d")
                for market in ["KOSPI", "KOSDAQ"]:
                    tickers = stock.get_market_ticker_list(d, market=market)
                    for code in tickers:
                        if code not in cache:
                            name = stock.get_market_ticker_name(code)
                            cache[code] = name
                if cache:
                    break
        except Exception:
            pass
        # 3) 캐시 파일로 저장
        if cache:
            try:
                self._CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
                with open(self._CACHE_FILE, "w", encoding="utf-8") as f:
                    json.dump(cache, f, ensure_ascii=False)
            except Exception:
                pass
        self.finished.emit(cache)


class SettingField:
    """설정 필드 위젯 생성 헬퍼."""

    @staticmethod
    def spin(value: int, min_v: int, max_v: int, suffix: str = "") -> QSpinBox:
        w = QSpinBox()
        w.setRange(min_v, max_v)
        w.setValue(value)
        if suffix:
            w.setSuffix(f" {suffix}")
        locale = QLocale(QLocale.Korean, QLocale.SouthKorea)
        w.setLocale(locale)
        w.setGroupSeparatorShown(True)
        w.setFixedHeight(30)
        return w

    @staticmethod
    def pct_slider(value: float, min_v: float, max_v: float) -> QWidget:
        """퍼센트 슬라이더 + 라벨.

        내부 스케일: value × 1000 (0.05 → 50, 0.10 → 100).
        표시: value × 100 (0.05 → "5.0%").
        """
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)

        slider = QSlider(Qt.Horizontal)
        int_val = int(value * 1000)
        slider.setRange(int(min_v * 1000), int(max_v * 1000))
        slider.setValue(int_val)

        label = QLabel(f"{value * 100:.1f}%")
        label.setFixedWidth(60)
        label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        slider.valueChanged.connect(
            lambda v: label.setText(f"{v / 10:.1f}%")
        )

        layout.addWidget(slider, stretch=1)
        layout.addWidget(label)

        container._slider = slider
        container._label = label
        return container

    @staticmethod
    def float_slider(value: float, min_v: float, max_v: float,
                     scale: int = 10, fmt: str = "{:.1f}") -> QWidget:
        """일반 float 슬라이더 + 라벨.

        Args:
            value: 현재 값.
            min_v, max_v: 범위.
            scale: 내부 정수 변환 배율.
            fmt: 라벨 표시 포맷.
        """
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)

        slider = QSlider(Qt.Horizontal)
        slider.setRange(int(min_v * scale), int(max_v * scale))
        slider.setValue(int(value * scale))

        label = QLabel(fmt.format(value))
        label.setFixedWidth(60)
        label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        slider.valueChanged.connect(
            lambda v: label.setText(fmt.format(v / scale))
        )

        layout.addWidget(slider, stretch=1)
        layout.addWidget(label)

        container._slider = slider
        container._label = label
        container._scale = scale
        return container

    @staticmethod
    def combo(items: list, current: str) -> QComboBox:
        w = QComboBox()
        w.addItems(items)
        w.setCurrentText(current)
        w.setFixedHeight(30)
        return w

    @staticmethod
    def line_edit(value: str, password: bool = False) -> QLineEdit:
        w = QLineEdit(value)
        if password:
            w.setEchoMode(QLineEdit.Password)
        w.setFixedHeight(30)
        return w


class SettingsTab(QWidget):
    """설정 탭 — config.yaml + .env 편집."""

    def __init__(self, config_path: str = "config.yaml", parent=None):
        super().__init__(parent)
        self._config_path = Path(config_path)
        self._env_path = Path(".env")
        self._config: dict = {}
        self._load_config()
        self._init_ui()

    def _load_config(self):
        if self._config_path.exists():
            with open(self._config_path, "r", encoding="utf-8") as f:
                self._config = yaml.safe_load(f) or {}

    def _init_ui(self):
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(12, 12, 12, 12)
        self._build_content()

    def _build_content(self):
        """서브탭 + 하단 버튼 빌드. reset 시 재호출."""
        # 기존 위젯 모두 제거
        while self._layout.count():
            child = self._layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()
            elif child.layout():
                self._clear_layout(child.layout())

        # 서브탭
        self.sub_tabs = QTabWidget()
        self.sub_tabs.addTab(self._build_watchlist_tab(), "종목관리")
        self.sub_tabs.addTab(self._build_trading_tab(), "매매")
        self.sub_tabs.addTab(self._build_screening_tab(), "스크리닝")
        self.sub_tabs.addTab(self._build_strategy_tab(), "전략")
        self.sub_tabs.addTab(self._build_risk_tab(), "리스크")
        self.sub_tabs.addTab(self._build_schedule_tab(), "스케줄")
        self.sub_tabs.addTab(self._build_api_tab(), "API 설정")
        self._layout.addWidget(self.sub_tabs)

        # 하단 버튼
        btn_widget = QWidget()
        btn_layout = QHBoxLayout(btn_widget)
        btn_layout.setContentsMargins(0, 0, 0, 0)
        btn_layout.addStretch()

        self.btn_reset = QPushButton("초기화")
        self.btn_reset.clicked.connect(self._on_reset)
        btn_layout.addWidget(self.btn_reset)

        self.btn_apply = QPushButton("적용")
        self.btn_apply.setObjectName("startBtn")
        self.btn_apply.clicked.connect(self._on_save)
        btn_layout.addWidget(self.btn_apply)

        self._layout.addWidget(btn_widget)

    def _clear_layout(self, layout):
        """레이아웃 내 위젯 재귀 삭제."""
        while layout.count():
            child = layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()
            elif child.layout():
                self._clear_layout(child.layout())

    # ── Watchlist 서브탭 ──

    def _build_watchlist_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setSpacing(8)
        layout.setContentsMargins(16, 16, 16, 16)

        # -- 종목 검색 영역 --
        layout.addWidget(self._make_separator("종목 검색"))

        search_row = QHBoxLayout()
        self.w_search_input = QLineEdit()
        self.w_search_input.setPlaceholderText("종목코드 또는 종목명 (예: 삼성, 005930)")
        self.w_search_input.setFixedHeight(30)
        self.w_search_input.returnPressed.connect(self._on_search_stock)
        search_row.addWidget(self.w_search_input, stretch=1)

        btn_search = QPushButton("검색")
        btn_search.setFixedHeight(30)
        btn_search.setFixedWidth(60)
        btn_search.clicked.connect(self._on_search_stock)
        search_row.addWidget(btn_search)
        layout.addLayout(search_row)

        # 검색 상태
        self.w_search_status = QLabel("")
        self.w_search_status.setStyleSheet("color: #6c7086; font-size: 11px;")
        layout.addWidget(self.w_search_status)

        # 검색 결과 테이블 (2컬럼: 종목코드+종목명 | 추가버튼)
        self.w_search_results = QTableWidget(0, 2)
        self.w_search_results.setHorizontalHeaderLabels(["종목", ""])
        self.w_search_results.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.w_search_results.horizontalHeader().setSectionResizeMode(1, QHeaderView.Fixed)
        self.w_search_results.setColumnWidth(1, 44)
        self.w_search_results.setMaximumHeight(160)
        self.w_search_results.verticalHeader().setVisible(False)
        self.w_search_results.verticalHeader().setDefaultSectionSize(30)
        self.w_search_results.setEditTriggers(QTableWidget.NoEditTriggers)
        self.w_search_results.setSelectionBehavior(QTableWidget.SelectRows)
        layout.addWidget(self.w_search_results)

        # -- 감시 종목 리스트 --
        watchlist = self._config.get("watchlist", [])
        self.w_watchlist_label = self._make_separator(f"감시 종목 ({len(watchlist)}개)")
        layout.addWidget(self.w_watchlist_label)

        # 감시 종목 테이블 (2컬럼: 종목코드+종목명 | 삭제버튼)
        self.w_watchlist_table = QTableWidget(0, 2)
        self.w_watchlist_table.setHorizontalHeaderLabels(["종목", ""])
        self.w_watchlist_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.w_watchlist_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Fixed)
        self.w_watchlist_table.setColumnWidth(1, 44)
        self.w_watchlist_table.verticalHeader().setVisible(False)
        self.w_watchlist_table.verticalHeader().setDefaultSectionSize(30)
        self.w_watchlist_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.w_watchlist_table.setSelectionBehavior(QTableWidget.SelectRows)
        layout.addWidget(self.w_watchlist_table, stretch=1)

        # 전체 삭제 버튼
        clear_row = QHBoxLayout()
        clear_row.addStretch()
        btn_clear = QPushButton("전체 삭제")
        btn_clear.setFixedHeight(28)
        btn_clear.clicked.connect(self._on_clear_watchlist)
        clear_row.addWidget(btn_clear)
        layout.addLayout(clear_row)

        # 초기화: watchlist 코드만 먼저 표시, 종목명은 비동기 로드
        self._stock_cache = {}
        self._populate_watchlist(watchlist)
        self._start_name_lookup(watchlist)
        # 전종목 검색용 캐시도 백그라운드 로드
        self._start_full_cache_load()

        return w

    def _start_name_lookup(self, codes: list):
        """watchlist 종목명을 백그라운드에서 조회."""
        if not codes:
            return
        self._name_worker = _StockNameWorker(codes, parent=self)
        self._name_worker.finished.connect(self._on_names_loaded)
        self._name_worker.start()

    def _on_names_loaded(self, names: dict):
        """종목명 조회 완료 → 테이블 업데이트."""
        self._stock_cache.update(names)
        for row in range(self.w_watchlist_table.rowCount()):
            item = self.w_watchlist_table.item(row, 0)
            if item:
                code = item.text().split(" ")[0]  # "005930" or "005930  삼성전자"
                if code in names:
                    item.setText(f"{code}  {names[code]}")

    def _start_full_cache_load(self):
        """전종목 검색용 캐시를 백그라운드 로드."""
        self._search_worker = _StockSearchWorker(parent=self)
        self._search_worker.finished.connect(self._on_full_cache_loaded)
        self._search_worker.start()
        self.w_search_status.setText("종목 데이터 로딩 중...")

    def _on_full_cache_loaded(self, cache: dict):
        """전종목 캐시 로드 완료."""
        self._stock_cache.update(cache)
        if cache:
            self.w_search_status.setText(f"검색 가능 ({len(cache)}종목)")
            # watchlist 종목명도 갱신
            for row in range(self.w_watchlist_table.rowCount()):
                item = self.w_watchlist_table.item(row, 0)
                if item:
                    text = item.text()
                    code = text.split(" ")[0]
                    if code in cache and cache[code] not in text:
                        item.setText(f"{code}  {cache[code]}")
        else:
            self.w_search_status.setText("종목코드로 검색 가능")

    def _on_search_stock(self):
        """종목 검색."""
        query = self.w_search_input.text().strip()
        if not query:
            return

        self.w_search_results.setRowCount(0)
        results = []

        # 캐시에서 코드/종목명 검색
        if self._stock_cache:
            for code, name in self._stock_cache.items():
                if query in code or query in name:
                    results.append((code, name))
                if len(results) >= 15:
                    break

        # 캐시에 없고 6자리 코드면 pykrx 개별 조회
        if not results and len(query) == 6 and query.isdigit():
            try:
                from pykrx import stock
                name = stock.get_market_ticker_name(query)
                if name:
                    results.append((query, name))
                    self._stock_cache[query] = name
            except Exception:
                results.append((query, ""))

        if not results:
            self.w_search_status.setText("검색 결과 없음")
            return

        self.w_search_status.setText(f"{len(results)}건")
        for code, name in results:
            row = self.w_search_results.rowCount()
            self.w_search_results.insertRow(row)

            display = f"{code}  {name}" if name else code
            self.w_search_results.setItem(row, 0, QTableWidgetItem(display))

            btn = QPushButton("+")
            btn.setFixedSize(30, 24)
            btn.clicked.connect(lambda _, c=code, n=name: self._add_to_watchlist(c, n))
            self.w_search_results.setCellWidget(row, 1, btn)

    def _add_to_watchlist(self, code: str, name: str):
        """감시 종목에 추가."""
        # 중복 체크
        for row in range(self.w_watchlist_table.rowCount()):
            item = self.w_watchlist_table.item(row, 0)
            if item and item.text().startswith(code):
                self.w_search_status.setText(f"{code} 이미 등록됨")
                return

        if not name:
            name = self._stock_cache.get(code, "")

        row = self.w_watchlist_table.rowCount()
        self.w_watchlist_table.insertRow(row)

        display = f"{code}  {name}" if name else code
        self.w_watchlist_table.setItem(row, 0, QTableWidgetItem(display))

        btn = QPushButton("X")
        btn.setFixedSize(30, 24)
        btn.clicked.connect(lambda _, c=code: self._remove_from_watchlist(c))
        self.w_watchlist_table.setCellWidget(row, 1, btn)

        self._update_watchlist_count()
        self.w_search_status.setText(f"{code} {name} 추가됨")

    def _remove_from_watchlist(self, code: str):
        """감시 종목에서 제거."""
        for row in range(self.w_watchlist_table.rowCount()):
            item = self.w_watchlist_table.item(row, 0)
            if item and item.text().startswith(code):
                self.w_watchlist_table.removeRow(row)
                break
        self._update_watchlist_count()

    def _on_clear_watchlist(self):
        """감시 종목 전체 삭제."""
        if self.w_watchlist_table.rowCount() == 0:
            return
        reply = QMessageBox.question(
            self, "확인", "감시 종목을 모두 삭제하시겠습니까?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            self.w_watchlist_table.setRowCount(0)
            self._update_watchlist_count()

    def _populate_watchlist(self, codes: list):
        """config에서 로드한 종목 코드로 테이블 채우기 (이름은 비동기 로드)."""
        self.w_watchlist_table.setRowCount(0)
        for code in codes:
            row = self.w_watchlist_table.rowCount()
            self.w_watchlist_table.insertRow(row)
            self.w_watchlist_table.setItem(row, 0, QTableWidgetItem(code))

            btn = QPushButton("X")
            btn.setFixedSize(30, 24)
            btn.clicked.connect(lambda _, c=code: self._remove_from_watchlist(c))
            self.w_watchlist_table.setCellWidget(row, 1, btn)
        self._update_watchlist_count()

    def _update_watchlist_count(self):
        """감시 종목 수 업데이트."""
        count = self.w_watchlist_table.rowCount()
        self.w_watchlist_label.setText(f"감시 종목 ({count}개)")

    # ── Trading 서브탭 ──

    def _build_trading_tab(self) -> QWidget:
        w = QWidget()
        scroll = self._wrap_scroll(w)
        form = QFormLayout(w)
        form.setSpacing(12)
        form.setContentsMargins(16, 16, 16, 16)

        trading = self._config.get("trading", {})

        self.w_universe = SettingField.combo(
            ["kospi", "kosdaq", "kospi_kosdaq"],
            trading.get("universe", "kospi_kosdaq"),
        )
        form.addRow("투자 유니버스", self.w_universe)

        self.w_max_positions = SettingField.spin(
            trading.get("max_positions", 3), 1, 10, "종목"
        )
        form.addRow("최대 보유 종목", self.w_max_positions)

        self.w_reentry_cooldown = SettingField.spin(
            trading.get("reentry_cooldown_days", 3), 0, 30, "일"
        )
        form.addRow("재진입 쿨다운", self.w_reentry_cooldown)

        return scroll

    # ── Screening 서브탭 ──

    def _build_screening_tab(self) -> QWidget:
        w = QWidget()
        scroll = self._wrap_scroll(w)
        form = QFormLayout(w)
        form.setSpacing(12)
        form.setContentsMargins(16, 16, 16, 16)

        screening = self._config.get("screening", {})

        self.w_min_amount = SettingField.spin(
            int(screening.get("min_daily_amount", 5_000_000_000) / 100_000_000),
            1, 500, "억원"
        )
        form.addRow("최소 거래대금", self.w_min_amount)

        self.w_min_market_cap = SettingField.spin(
            int(screening.get("min_market_cap", 30_000_000_000) / 100_000_000),
            1, 10000, "억원"
        )
        form.addRow("최소 시가총액", self.w_min_market_cap)

        self.w_min_price = SettingField.spin(
            screening.get("min_price", 1000), 100, 50000, "원"
        )
        form.addRow("최소 주가", self.w_min_price)

        self.w_max_price = SettingField.spin(
            screening.get("max_price", 500000), 10000, 2000000, "원"
        )
        form.addRow("최대 주가", self.w_max_price)

        self.w_top_n = SettingField.spin(
            screening.get("top_n", 30), 5, 100, "종목"
        )
        form.addRow("후보 수", self.w_top_n)

        return scroll

    # ── Strategy 서브탭 ──

    def _build_strategy_tab(self) -> QWidget:
        w = QWidget()
        scroll = self._wrap_scroll(w)
        form = QFormLayout(w)
        form.setSpacing(12)
        form.setContentsMargins(16, 16, 16, 16)

        strategy = self._config.get("strategy", {})

        self.w_strategy_type = SettingField.combo(
            ["golden_cross", "macd_rsi"],
            strategy.get("type", "golden_cross"),
        )
        form.addRow("전략 유형", self.w_strategy_type)

        # MACD
        form.addRow(self._make_separator("MACD"))
        self.w_macd_fast = SettingField.spin(strategy.get("macd_fast", 12), 2, 50)
        form.addRow("단기 기간", self.w_macd_fast)
        self.w_macd_slow = SettingField.spin(strategy.get("macd_slow", 26), 10, 100)
        form.addRow("장기 기간", self.w_macd_slow)
        self.w_macd_signal = SettingField.spin(strategy.get("macd_signal", 9), 2, 30)
        form.addRow("시그널 기간", self.w_macd_signal)

        # RSI
        form.addRow(self._make_separator("RSI"))
        self.w_rsi_period = SettingField.spin(strategy.get("rsi_period", 14), 5, 30)
        form.addRow("기간", self.w_rsi_period)
        self.w_rsi_min = SettingField.spin(strategy.get("rsi_entry_min", 40), 10, 60)
        form.addRow("진입 하한", self.w_rsi_min)
        self.w_rsi_max = SettingField.spin(strategy.get("rsi_entry_max", 65), 50, 90)
        form.addRow("진입 상한", self.w_rsi_max)

        # Target
        form.addRow(self._make_separator("청산"))
        self.w_target_return = SettingField.pct_slider(
            strategy.get("target_return", 0.10), 0.02, 0.30
        )
        form.addRow("목표 수익률", self.w_target_return)

        self.w_max_hold = SettingField.spin(strategy.get("max_hold_days", 15), 1, 60, "일")
        form.addRow("최대 보유일", self.w_max_hold)

        self.w_adx_threshold = SettingField.spin(strategy.get("adx_threshold", 20), 10, 50)
        form.addRow("ADX 기준", self.w_adx_threshold)

        return scroll

    # ── Risk 서브탭 ──

    def _build_risk_tab(self) -> QWidget:
        w = QWidget()
        scroll = self._wrap_scroll(w)
        form = QFormLayout(w)
        form.setSpacing(12)
        form.setContentsMargins(16, 16, 16, 16)

        risk = self._config.get("risk", {})

        form.addRow(self._make_separator("포지션 사이징"))
        self.w_max_pos_ratio = SettingField.pct_slider(
            risk.get("max_position_ratio", 0.15), 0.05, 0.50
        )
        form.addRow("최대 비중", self.w_max_pos_ratio)

        self.w_min_pos_ratio = SettingField.pct_slider(
            risk.get("min_position_ratio", 0.03), 0.01, 0.20
        )
        form.addRow("최소 비중", self.w_min_pos_ratio)

        self.w_sizing_method = SettingField.combo(
            ["half_kelly", "quarter_kelly", "fixed"],
            risk.get("sizing_method", "half_kelly"),
        )
        form.addRow("사이징 방식", self.w_sizing_method)

        form.addRow(self._make_separator("손절"))
        self.w_stop_atr = SettingField.float_slider(
            risk.get("stop_atr_multiplier", 2.5), 0.5, 5.0, scale=10, fmt="{:.1f}x"
        )
        form.addRow("ATR 배수", self.w_stop_atr)

        self.w_max_stop = SettingField.pct_slider(
            risk.get("max_stop_pct", 0.10), 0.03, 0.20
        )
        form.addRow("최대 손절폭", self.w_max_stop)

        form.addRow(self._make_separator("일일 한도"))
        self.w_daily_loss_limit = SettingField.pct_slider(
            abs(risk.get("daily_loss_limit", -0.03)), 0.01, 0.10
        )
        form.addRow("일일 손실 한도", self.w_daily_loss_limit)

        self.w_daily_loss_warning = SettingField.pct_slider(
            abs(risk.get("daily_loss_warning", -0.02)), 0.01, 0.10
        )
        form.addRow("일일 손실 경고", self.w_daily_loss_warning)

        return scroll

    # ── Schedule 서브탭 ──

    def _build_schedule_tab(self) -> QWidget:
        w = QWidget()
        scroll = self._wrap_scroll(w)
        form = QFormLayout(w)
        form.setSpacing(12)
        form.setContentsMargins(16, 16, 16, 16)

        schedule = self._config.get("schedule", {})

        self.w_screening_time = SettingField.line_edit(
            schedule.get("screening_time", "08:30")
        )
        form.addRow("스크리닝 시각", self.w_screening_time)

        self.w_report_time = SettingField.line_edit(
            schedule.get("daily_report_time", "16:00")
        )
        form.addRow("리포트 시각", self.w_report_time)

        self.w_reconnect_time = SettingField.line_edit(
            schedule.get("reconnect_time", "08:45")
        )
        form.addRow("재연결 시각", self.w_reconnect_time)

        trading = self._config.get("trading", {})
        self.w_initial_capital = SettingField.spin(
            trading.get("initial_capital", 3_000_000), 100_000, 100_000_000, "원"
        )
        form.addRow("초기 투자금", self.w_initial_capital)

        return scroll

    # ── API & Alerts 서브탭 ──

    def _build_api_tab(self) -> QWidget:
        w = QWidget()
        scroll = self._wrap_scroll(w)
        form = QFormLayout(w)
        form.setSpacing(12)
        form.setContentsMargins(16, 16, 16, 16)

        load_dotenv()

        form.addRow(self._make_separator("키움 API"))
        self.w_appkey = SettingField.line_edit(
            os.getenv("KIWOOM_APPKEY", ""), password=True
        )
        form.addRow("App Key", self.w_appkey)

        self.w_secretkey = SettingField.line_edit(
            os.getenv("KIWOOM_SECRETKEY", ""), password=True
        )
        form.addRow("Secret Key", self.w_secretkey)

        self.w_account = SettingField.line_edit(os.getenv("KIWOOM_ACCOUNT", ""))
        form.addRow("계좌번호", self.w_account)

        form.addRow(self._make_separator("텔레그램"))
        self.w_tg_token = SettingField.line_edit(
            os.getenv("TELEGRAM_BOT_TOKEN", ""), password=True
        )
        form.addRow("봇 토큰", self.w_tg_token)

        self.w_tg_chat = SettingField.line_edit(os.getenv("TELEGRAM_CHAT_ID", ""))
        form.addRow("채팅 ID", self.w_tg_chat)

        form.addRow(self._make_separator("시스템"))
        self.w_log_level = SettingField.combo(
            ["DEBUG", "INFO", "WARNING", "ERROR"],
            os.getenv("LOG_LEVEL", "INFO"),
        )
        form.addRow("로그 레벨", self.w_log_level)

        return scroll

    # ── 저장/리셋 ──

    def _on_save(self):
        """설정을 config.yaml + .env에 저장."""
        try:
            self._collect_config()
            self._save_yaml()
            self._save_env()
            QMessageBox.information(
                self, "저장 완료",
                "설정이 저장되었습니다.\n엔진을 재시작하면 적용됩니다."
            )
        except Exception as e:
            QMessageBox.critical(self, "오류", f"저장 실패: {e}")

    def _save_yaml(self):
        """config.yaml 저장."""
        with open(self._config_path, "w", encoding="utf-8") as f:
            yaml.dump(
                self._config, f,
                default_flow_style=False,
                allow_unicode=True,
                sort_keys=False,
            )

    def _save_env(self):
        """API 키를 .env 파일에 저장."""
        env_path = str(self._env_path.resolve())

        # .env 파일이 없으면 생성
        if not self._env_path.exists():
            self._env_path.touch()

        env_vars = {
            "KIWOOM_APPKEY": self.w_appkey.text(),
            "KIWOOM_SECRETKEY": self.w_secretkey.text(),
            "KIWOOM_ACCOUNT": self.w_account.text(),
            "TELEGRAM_BOT_TOKEN": self.w_tg_token.text(),
            "TELEGRAM_CHAT_ID": self.w_tg_chat.text(),
            "LOG_LEVEL": self.w_log_level.currentText(),
        }

        for key, value in env_vars.items():
            set_key(env_path, key, value)

    def _on_reset(self):
        """config.yaml에서 다시 로드 후 위젯 재생성."""
        self._load_config()
        self._build_content()

    def _collect_config(self):
        """위젯 값들을 self._config 딕셔너리에 수집."""
        # Watchlist — 셀 텍스트에서 코드(앞 6자리) 추출
        watchlist = []
        for row in range(self.w_watchlist_table.rowCount()):
            item = self.w_watchlist_table.item(row, 0)
            if item:
                code = item.text().split()[0]  # "005930  삼성전자" → "005930"
                watchlist.append(code)
        self._config["watchlist"] = watchlist

        # Trading
        trading = self._config.setdefault("trading", {})
        trading["universe"] = self.w_universe.currentText()
        trading["max_positions"] = self.w_max_positions.value()
        trading["reentry_cooldown_days"] = self.w_reentry_cooldown.value()

        # Screening
        screening = self._config.setdefault("screening", {})
        screening["min_daily_amount"] = self.w_min_amount.value() * 100_000_000
        screening["min_market_cap"] = self.w_min_market_cap.value() * 100_000_000
        screening["min_price"] = self.w_min_price.value()
        screening["max_price"] = self.w_max_price.value()
        screening["top_n"] = self.w_top_n.value()

        # Strategy
        strategy = self._config.setdefault("strategy", {})
        strategy["type"] = self.w_strategy_type.currentText()
        strategy["macd_fast"] = self.w_macd_fast.value()
        strategy["macd_slow"] = self.w_macd_slow.value()
        strategy["macd_signal"] = self.w_macd_signal.value()
        strategy["rsi_period"] = self.w_rsi_period.value()
        strategy["rsi_entry_min"] = self.w_rsi_min.value()
        strategy["rsi_entry_max"] = self.w_rsi_max.value()
        strategy["target_return"] = self.w_target_return._slider.value() / 1000
        strategy["max_hold_days"] = self.w_max_hold.value()
        strategy["adx_threshold"] = self.w_adx_threshold.value()

        # Risk
        risk = self._config.setdefault("risk", {})
        risk["max_position_ratio"] = self.w_max_pos_ratio._slider.value() / 1000
        risk["min_position_ratio"] = self.w_min_pos_ratio._slider.value() / 1000
        risk["sizing_method"] = self.w_sizing_method.currentText()
        # stop_atr_multiplier: float_slider (scale=10)
        risk["stop_atr_multiplier"] = self.w_stop_atr._slider.value() / self.w_stop_atr._scale
        risk["max_stop_pct"] = self.w_max_stop._slider.value() / 1000
        risk["daily_loss_limit"] = -(self.w_daily_loss_limit._slider.value() / 1000)
        risk["daily_loss_warning"] = -(self.w_daily_loss_warning._slider.value() / 1000)

        # Schedule
        schedule = self._config.setdefault("schedule", {})
        schedule["screening_time"] = self.w_screening_time.text()
        schedule["daily_report_time"] = self.w_report_time.text()
        schedule["reconnect_time"] = self.w_reconnect_time.text()

        # Trading — 초기 투자금
        trading["initial_capital"] = self.w_initial_capital.value()

    # ── 유틸 ──

    def _make_separator(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setStyleSheet(
            "color: #89b4fa; font-weight: bold; font-size: 12px; "
            "border-bottom: 1px solid #313244; padding-bottom: 4px; margin-top: 8px;"
        )
        return label

    def _wrap_scroll(self, widget: QWidget) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidget(widget)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        return scroll
