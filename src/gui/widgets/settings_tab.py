"""설정 탭 — config.yaml 편집 + .env 관리.

모든 슬라이더 값은 ×1000 스케일로 저장/로드하여 float 정밀도 유지.
.env 파일 쓰기를 지원하여 API 키 변경사항도 정상 저장.
"""

import os
from pathlib import Path

import yaml
from ruamel.yaml import YAML
from dotenv import load_dotenv, set_key
from PyQt5.QtCore import Qt, QLocale, QThread, pyqtSignal
from PyQt5.QtGui import QColor
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
            from data.provider import get_provider
            provider = get_provider()
            for code in self._codes:
                try:
                    name = provider.get_stock_name(code)
                    if name and name != code:
                        result[code] = name
                except Exception:
                    pass
        except Exception:
            pass
        self.finished.emit(result)


class _StockSearchWorker(QThread):
    """백그라운드에서 전종목 리스트를 KRX에서 다운로드 + 캐시."""
    finished = pyqtSignal(dict)
    _CACHE_FILE = Path("data/stock_names.json")
    _CACHE_MAX_AGE_HOURS = 24  # 캐시 유효기간 (시간)

    def __init__(self, force_refresh: bool = False, parent=None):
        super().__init__(parent)
        self._force_refresh = force_refresh

    def run(self):
        import json
        import time

        # 1) 캐시 파일이 있고, 유효기간 내면 로드
        if not self._force_refresh and self._CACHE_FILE.exists():
            try:
                age_hours = (time.time() - self._CACHE_FILE.stat().st_mtime) / 3600
                if age_hours < self._CACHE_MAX_AGE_HOURS:
                    with open(self._CACHE_FILE, "r", encoding="utf-8") as f:
                        cache = json.load(f)
                    if cache:
                        self.finished.emit(cache)
                        return
            except Exception:
                pass

        # 2) KRX에서 전종목 다운로드 (KOSPI + KOSDAQ)
        cache = self._fetch_from_krx()

        # 3) KRX 실패 시 기존 캐시 파일 폴백
        if not cache and self._CACHE_FILE.exists():
            try:
                with open(self._CACHE_FILE, "r", encoding="utf-8") as f:
                    cache = json.load(f)
            except Exception:
                pass

        # 4) 캐시 파일 저장
        if cache:
            try:
                self._CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
                with open(self._CACHE_FILE, "w", encoding="utf-8") as f:
                    json.dump(cache, f, ensure_ascii=False)
            except Exception:
                pass

        self.finished.emit(cache)

    def _fetch_from_krx(self) -> dict:
        """KRX kind.krx.co.kr에서 KOSPI+KOSDAQ 전종목 다운로드."""
        import requests
        import pandas as pd
        from io import StringIO

        results = {}
        try:
            for mtype in ("stockMkt", "kosdaqMkt"):
                resp = requests.get(
                    "https://kind.krx.co.kr/corpgeneral/corpList.do",
                    params={"method": "download", "marketType": mtype},
                    timeout=15,
                )
                resp.encoding = "euc-kr"
                df = pd.read_html(StringIO(resp.text))[0]
                # 종목코드(3번째 컬럼) + 회사명(1번째 컬럼)
                df["code"] = df.iloc[:, 2].apply(lambda x: str(x).zfill(6))
                df["name"] = df.iloc[:, 0]
                for _, row in df.iterrows():
                    results[row["code"]] = row["name"]
        except Exception:
            pass
        return results


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
            ryaml = YAML()
            ryaml.preserve_quotes = True
            with open(self._config_path, "r", encoding="utf-8") as f:
                self._config = ryaml.load(f) or {}

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

        # 서브탭 (아이콘 텍스트로 직관적 구분)
        self.sub_tabs = QTabWidget()
        self.sub_tabs.addTab(self._build_watchlist_tab(), "\U0001F50D 종목관리")
        self.sub_tabs.addTab(self._build_trading_tab(), "\U0001F4B0 매매")
        self.sub_tabs.addTab(self._build_screening_tab(), "\U0001F4CA 스크리닝")
        self.sub_tabs.addTab(self._build_strategy_tab(), "\U0001F3AF 전략")
        self.sub_tabs.addTab(self._build_risk_tab(), "\U0001F6E1 리스크")
        self.sub_tabs.addTab(self._build_schedule_tab(), "\U000023F0 스케줄")
        self.sub_tabs.addTab(self._build_api_tab(), "\U0001F511 API")
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

    _HOVER_STYLE = """
        QTableWidget::item:hover {
            background-color: #313244;
        }
        QTableWidget::item:selected {
            background-color: #45475a;
        }
    """

    def _build_watchlist_tab(self) -> QWidget:
        w = QWidget()
        root = QVBoxLayout(w)
        root.setSpacing(8)
        root.setContentsMargins(12, 12, 12, 12)

        # ═══ 좌우 분할 ═══
        columns = QHBoxLayout()
        columns.setSpacing(12)

        # ── 왼쪽: 종목 검색 ──
        left = QVBoxLayout()
        left.setSpacing(4)

        # 1행: 검색 입력 + 버튼
        search_row = QHBoxLayout()
        search_row.setSpacing(4)
        self.w_search_input = QLineEdit()
        self.w_search_input.setPlaceholderText("코드 또는 종목명")
        self.w_search_input.setFixedHeight(28)
        self.w_search_input.returnPressed.connect(self._on_search_stock)
        search_row.addWidget(self.w_search_input, stretch=1)

        btn_search = QPushButton("검색")
        btn_search.setFixedHeight(28)
        btn_search.setFixedWidth(52)
        btn_search.setStyleSheet("font-size: 11px; padding: 2px 8px;")
        btn_search.clicked.connect(self._on_search_stock)
        search_row.addWidget(btn_search)
        left.addLayout(search_row)

        # 2행: 상태 텍스트
        self.w_search_status = QLabel("더블클릭으로 추가")
        self.w_search_status.setFixedHeight(18)
        self.w_search_status.setStyleSheet("color: #a6adc8; font-size: 11px;")
        left.addWidget(self.w_search_status)

        # 3행: 검색 결과 테이블
        self.w_search_results = QTableWidget(0, 1)
        self.w_search_results.setHorizontalHeaderLabels(["검색 종목 (0건)"])
        self.w_search_results.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.w_search_results.verticalHeader().setVisible(False)
        self.w_search_results.verticalHeader().setDefaultSectionSize(24)
        self.w_search_results.setEditTriggers(QTableWidget.NoEditTriggers)
        self.w_search_results.setSelectionBehavior(QTableWidget.SelectRows)
        self.w_search_results.setMouseTracking(True)
        self.w_search_results.setStyleSheet(self._HOVER_STYLE)
        self.w_search_results.doubleClicked.connect(self._on_search_double_click)
        left.addWidget(self.w_search_results, stretch=1)

        columns.addLayout(left, stretch=1)

        # ── 오른쪽: 감시 종목 ──
        right = QVBoxLayout()
        right.setSpacing(4)

        watchlist = self._config.get("watchlist", [])

        # 1행: 캐시 갱신 + 전체 삭제 버튼
        action_row = QHBoxLayout()
        action_row.setSpacing(4)
        self.btn_refresh_cache = QPushButton("캐시 갱신")
        self.btn_refresh_cache.setFixedHeight(28)
        self.btn_refresh_cache.setStyleSheet("font-size: 11px; padding: 2px 8px;")
        self.btn_refresh_cache.clicked.connect(self._on_refresh_cache)
        action_row.addWidget(self.btn_refresh_cache)
        action_row.addStretch()
        btn_clear = QPushButton("전체 삭제")
        btn_clear.setFixedHeight(28)
        btn_clear.setStyleSheet("font-size: 11px; padding: 2px 8px;")
        btn_clear.clicked.connect(self._on_clear_watchlist)
        action_row.addWidget(btn_clear)
        right.addLayout(action_row)

        # 2행: 상태 텍스트
        self.w_watchlist_status = QLabel("Del 키로 삭제")
        self.w_watchlist_status.setFixedHeight(18)
        self.w_watchlist_status.setStyleSheet("color: #a6adc8; font-size: 11px;")
        right.addWidget(self.w_watchlist_status)

        # 3행: 감시 종목 테이블
        self.w_watchlist_table = QTableWidget(0, 1)
        self.w_watchlist_label = None  # 더 이상 separator 사용 안 함
        self.w_watchlist_table.setHorizontalHeaderLabels(
            [f"감시 종목 ({len(watchlist)}개)"]
        )
        self.w_watchlist_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.w_watchlist_table.verticalHeader().setVisible(False)
        self.w_watchlist_table.verticalHeader().setDefaultSectionSize(24)
        self.w_watchlist_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.w_watchlist_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.w_watchlist_table.setMouseTracking(True)
        self.w_watchlist_table.setStyleSheet(self._HOVER_STYLE)
        right.addWidget(self.w_watchlist_table, stretch=1)

        columns.addLayout(right, stretch=1)

        root.addLayout(columns, stretch=1)

        # 초기화
        self._stock_cache = {}
        self._saved_watchlist_codes = set(watchlist)  # 저장된 원본 코드 (변경 감지용)
        self._populate_watchlist(watchlist)
        self._start_name_lookup(watchlist)
        self._start_full_cache_load()

        # Del 키 바인딩
        self.w_watchlist_table.keyPressEvent = self._watchlist_key_press

        return w

    def _on_refresh_cache(self):
        """종목 캐시 수동 갱신 (KRX에서 강제 다운로드)."""
        self.btn_refresh_cache.setEnabled(False)
        self.btn_refresh_cache.setText("갱신 중...")
        self._start_full_cache_load(force_refresh=True)

    def _on_search_double_click(self, index):
        """검색 결과 더블클릭 → watchlist에 추가."""
        row = index.row()
        item = self.w_search_results.item(row, 0)
        if not item:
            return
        text = item.text()
        code = text.split()[0]
        name = text.split("  ", 1)[1] if "  " in text else ""
        self._add_to_watchlist(code, name)

    def _watchlist_key_press(self, event):
        """watchlist에서 Del/Backspace 키 → 선택 종목 삭제."""
        from PyQt5.QtCore import Qt as QtKey
        if event.key() in (QtKey.Key_Delete, QtKey.Key_Backspace):
            rows = self.w_watchlist_table.selectionModel().selectedRows()
            if rows:
                item = self.w_watchlist_table.item(rows[0].row(), 0)
                if item:
                    code = item.text().split()[0]
                    self._remove_from_watchlist(code)
        else:
            QTableWidget.keyPressEvent(self.w_watchlist_table, event)

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

    def _start_full_cache_load(self, force_refresh: bool = False):
        """전종목 검색용 캐시를 백그라운드 로드."""
        self._search_worker = _StockSearchWorker(
            force_refresh=force_refresh, parent=self
        )
        self._search_worker.finished.connect(self._on_full_cache_loaded)
        self._search_worker.start()
        self.w_search_status.setStyleSheet("color: #f9e2af; font-size: 11px;")
        msg = "KRX에서 종목 다운로드 중..." if force_refresh else "종목 데이터 로딩 중..."
        self.w_search_status.setText(msg)

    def _on_full_cache_loaded(self, cache: dict):
        """전종목 캐시 로드 완료."""
        self._stock_cache.update(cache)
        if cache:
            self.w_search_status.setStyleSheet("color: #a6e3a1; font-size: 11px;")
            self.w_search_status.setText(f"{len(cache)}종목 준비 완료")
            # watchlist 종목명도 갱신
            for row in range(self.w_watchlist_table.rowCount()):
                item = self.w_watchlist_table.item(row, 0)
                if item:
                    text = item.text()
                    code = text.split(" ")[0]
                    if code in cache and cache[code] not in text:
                        item.setText(f"{code}  {cache[code]}")
        # 캐시 갱신 버튼 복원
        if hasattr(self, "btn_refresh_cache"):
            self.btn_refresh_cache.setEnabled(True)
            self.btn_refresh_cache.setText("캐시 갱신")
        else:
            self.w_search_status.setText("종목코드로 검색 가능")

    def _on_search_stock(self):
        """종목 검색."""
        query = self.w_search_input.text().strip()
        if not query:
            return

        self.w_search_results.setRowCount(0)
        results = []

        # 캐시에서 코드/종목명 검색 (대소문자 무시)
        query_lower = query.lower()
        if self._stock_cache:
            for code, name in self._stock_cache.items():
                if query_lower in code.lower() or query_lower in name.lower():
                    results.append((code, name))
                if len(results) >= 50:
                    break

        # 캐시에 없고 6자리 코드면 DataProvider 개별 조회
        if not results and len(query) == 6 and query.isdigit():
            try:
                from data.provider import get_provider
                name = get_provider().get_stock_name(query)
                if name and name != query:
                    results.append((query, name))
                    self._stock_cache[query] = name
                else:
                    results.append((query, ""))
            except Exception:
                results.append((query, ""))

        if not results:
            self.w_search_status.setStyleSheet("color: #f38ba8; font-size: 11px;")
            self.w_search_status.setText("검색 결과 없음")
            self.w_search_results.setHorizontalHeaderLabels(["검색 종목 (0건)"])
            return

        self.w_search_status.setStyleSheet("color: #a6adc8; font-size: 11px;")
        self.w_search_status.setText("더블클릭으로 추가")
        for code, name in results:
            row = self.w_search_results.rowCount()
            self.w_search_results.insertRow(row)

            display = f"{code}  {name}" if name else code
            self.w_search_results.setItem(row, 0, QTableWidgetItem(display))

        self.w_search_results.setHorizontalHeaderLabels(
            [f"검색 종목 ({len(results)}건)"]
        )

    def _add_to_watchlist(self, code: str, name: str):
        """감시 종목에 추가."""
        # 중복 체크
        for row in range(self.w_watchlist_table.rowCount()):
            item = self.w_watchlist_table.item(row, 0)
            if item and item.text().startswith(code):
                self.w_search_status.setStyleSheet("color: #f9e2af; font-size: 11px;")
                self.w_search_status.setText(f"{code} 이미 등록됨")
                return

        if not name:
            name = self._stock_cache.get(code, "")

        row = self.w_watchlist_table.rowCount()
        self.w_watchlist_table.insertRow(row)

        display = f"{code}  {name}" if name else code
        item = QTableWidgetItem(display)

        # 새로 추가된 종목 (아직 미저장) → 초록색 텍스트 + [NEW] 표시
        if code not in self._saved_watchlist_codes:
            item.setForeground(QColor("#a6e3a1"))
            item.setText(f"[NEW] {display}")

        self.w_watchlist_table.setItem(row, 0, item)

        self._update_watchlist_count()
        self.w_search_status.setStyleSheet("color: #a6e3a1; font-size: 11px;")
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
        self._update_watchlist_count()

    def _update_watchlist_count(self):
        """감시 종목 수 업데이트 (테이블 헤더에 표시)."""
        count = self.w_watchlist_table.rowCount()
        self.w_watchlist_table.setHorizontalHeaderLabels(
            [f"감시 종목 ({count}개)"]
        )

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

        self.w_reentry_cooldown_trend = SettingField.spin(
            trading.get("reentry_cooldown_trend_days", 1), 0, 30, "일"
        )
        form.addRow("추세 유지 시 쿨다운", self.w_reentry_cooldown_trend)

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
            ["golden_cross", "disparity_reversion", "adaptive"],
            strategy.get("type", "adaptive"),
        )
        form.addRow("전략 유형", self.w_strategy_type)

        # MACD
        form.addRow(self._make_separator("고급 설정 (MACD)"))
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

        # 이격도 (Disparity Reversion)
        form.addRow(self._make_separator("이격도 평균회귀"))

        self.w_disparity_entry = SettingField.spin(
            strategy.get("disparity_entry", 96), 85, 99, "%"
        )
        form.addRow("진입 이격도", self.w_disparity_entry)

        self.w_disparity_stop = SettingField.spin(
            strategy.get("disparity_stop", 88), 80, 95, "%"
        )
        form.addRow("손절 이격도", self.w_disparity_stop)

        self.w_disparity_max_hold = SettingField.spin(
            strategy.get("disparity_max_hold", 7), 3, 15, "일"
        )
        form.addRow("최대 보유 (이격도)", self.w_disparity_max_hold)

        # 국면별 포지션 스케일링
        form.addRow(self._make_separator("국면별 스케일링"))

        regime_scale = strategy.get("regime_position_scale", {})
        self.w_scale_trending = SettingField.pct_slider(
            regime_scale.get("trending", 1.0), 0.0, 1.0
        )
        form.addRow("추세장 스케일", self.w_scale_trending)

        self.w_scale_sideways = SettingField.pct_slider(
            regime_scale.get("sideways", 0.7), 0.0, 1.0
        )
        form.addRow("횡보장 스케일", self.w_scale_sideways)

        # 부분 매도
        form.addRow(self._make_separator("부분 매도"))
        self.w_partial_sell_enabled = SettingField.combo(
            ["true", "false"],
            "true" if strategy.get("partial_sell_enabled", True) else "false",
        )
        form.addRow("부분 매도 활성화", self.w_partial_sell_enabled)

        self.w_partial_target_pct = SettingField.pct_slider(
            strategy.get("partial_target_pct", 0.5), 0.1, 0.9
        )
        form.addRow("목표 달성률 트리거", self.w_partial_target_pct)

        self.w_partial_sell_ratio = SettingField.pct_slider(
            strategy.get("partial_sell_ratio", 0.5), 0.1, 0.9
        )
        form.addRow("매도 비율", self.w_partial_sell_ratio)

        # watchlist 자동 갱신
        form.addRow(self._make_separator("watchlist 자동 갱신"))

        wl_refresh = self._config.get("watchlist_refresh", {})
        self.w_wl_enabled = SettingField.combo(
            ["true", "false"],
            "true" if wl_refresh.get("enabled", True) else "false",
        )
        form.addRow("자동 갱신", self.w_wl_enabled)

        self.w_wl_min_cap = SettingField.spin(
            int(wl_refresh.get("min_market_cap", 5_000_000_000_000) / 1_000_000_000_000),
            1, 50, "조"
        )
        form.addRow("최소 시가총액", self.w_wl_min_cap)

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
        """config.yaml 저장 (주석 보존)."""
        ryaml = YAML()
        ryaml.preserve_quotes = True
        ryaml.width = 4096
        with open(self._config_path, "w", encoding="utf-8") as f:
            ryaml.dump(self._config, f)

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
        """초기화 — 종목관리 탭에서는 감시 종목만 복원, 그 외는 전체 리셋."""
        current_tab = self.sub_tabs.currentIndex()
        tab_name = self.sub_tabs.tabText(current_tab)

        if tab_name == "종목관리":
            # 감시 종목만 config에서 다시 로드
            self._load_config()
            watchlist = self._config.get("watchlist", [])
            self._saved_watchlist_codes = set(watchlist)
            self._populate_watchlist(watchlist)
            self._start_name_lookup(watchlist)
            self._update_watchlist_count()
            self.w_watchlist_status.setStyleSheet("color: #a6e3a1; font-size: 11px;")
            self.w_watchlist_status.setText("감시 종목 초기화 완료")
        else:
            self._load_config()
            self._build_content()

    def _collect_config(self):
        """위젯 값들을 self._config 딕셔너리에 수집."""
        # Watchlist — 셀 텍스트에서 코드(앞 6자리) 추출
        watchlist = []
        for row in range(self.w_watchlist_table.rowCount()):
            item = self.w_watchlist_table.item(row, 0)
            if item:
                text = item.text()
                if text.startswith("[NEW] "):
                    text = text[6:]
                code = text.split()[0]  # "005930  삼성전자" → "005930"
                watchlist.append(code)
        self._config["watchlist"] = watchlist

        # 저장 후 원본 갱신 → [NEW] 표시 제거
        self._saved_watchlist_codes = set(watchlist)
        for row in range(self.w_watchlist_table.rowCount()):
            item = self.w_watchlist_table.item(row, 0)
            if item:
                text = item.text()
                if text.startswith("[NEW] "):
                    item.setText(text[6:])  # "[NEW] " 제거
                item.setForeground(QColor("#cdd6f4"))  # 기본 텍스트 색상

        # Trading
        trading = self._config.setdefault("trading", {})
        trading["universe"] = self.w_universe.currentText()
        trading["max_positions"] = self.w_max_positions.value()
        trading["reentry_cooldown_days"] = self.w_reentry_cooldown.value()
        trading["reentry_cooldown_trend_days"] = self.w_reentry_cooldown_trend.value()

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
        strategy["partial_sell_enabled"] = self.w_partial_sell_enabled.currentText() == "true"
        strategy["partial_target_pct"] = self.w_partial_target_pct._slider.value() / 1000
        strategy["partial_sell_ratio"] = self.w_partial_sell_ratio._slider.value() / 1000

        # 이격도 파라미터
        strategy["disparity_entry"] = self.w_disparity_entry.value()
        strategy["disparity_stop"] = self.w_disparity_stop.value()
        strategy["disparity_max_hold"] = self.w_disparity_max_hold.value()

        # watchlist 자동 갱신
        wl_refresh = self._config.setdefault("watchlist_refresh", {})
        wl_refresh["enabled"] = self.w_wl_enabled.currentText() == "true"
        wl_refresh["min_market_cap"] = self.w_wl_min_cap.value() * 1_000_000_000_000

        # 국면별 스케일링
        strategy.setdefault("regime_position_scale", {})
        strategy["regime_position_scale"]["trending"] = self.w_scale_trending._slider.value() / 1000
        strategy["regime_position_scale"]["sideways"] = self.w_scale_sideways._slider.value() / 1000
        strategy["regime_position_scale"]["bearish"] = 0.0

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
        label = QLabel(f"\u2500\u2500  {text}")
        label.setStyleSheet(
            "color: #89b4fa; font-weight: bold; font-size: 12px; "
            "padding: 8px 0 4px 0; margin-top: 4px;"
        )
        return label

    def _wrap_scroll(self, widget: QWidget) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidget(widget)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        return scroll
