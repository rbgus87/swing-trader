"""로그 탭 — 실시간 로그 뷰어 + 레벨 필터 + 검색."""

import html

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)


class LogTab(QWidget):
    """실시간 로그 뷰어."""

    MAX_LINES = 1000

    # 레벨별 최소 순위 (필터 기준)
    _LEVEL_ORDER = {
        "DEBUG": 0,
        "INFO": 1,
        "WARNING": 2,
        "ERROR": 3,
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self._all_logs: list[tuple[str, str]] = []  # (level, message)
        self._filter_level = "INFO"
        self._search_text = ""
        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        # ── 상단 툴바 ──
        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)

        # 레벨 필터
        toolbar.addWidget(QLabel("Level:"))
        self.combo_level = QComboBox()
        self.combo_level.addItems(["DEBUG", "INFO", "WARNING", "ERROR"])
        self.combo_level.setCurrentText("INFO")
        self.combo_level.setFixedWidth(100)
        self.combo_level.currentTextChanged.connect(self._on_level_changed)
        toolbar.addWidget(self.combo_level)

        # 검색
        toolbar.addWidget(QLabel("Search:"))
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Filter logs... (Ctrl+F)")
        self.search_input.textChanged.connect(self._on_search_changed)
        toolbar.addWidget(self.search_input)

        toolbar.addStretch()

        btn_clear = QPushButton("Clear")
        btn_clear.clicked.connect(self._on_clear)
        toolbar.addWidget(btn_clear)

        layout.addLayout(toolbar)

        # ── 로그 텍스트 ──
        self.log_view = QTextEdit()
        self.log_view.setObjectName("logView")
        self.log_view.setReadOnly(True)
        layout.addWidget(self.log_view)

    def append_log(self, message: str, level: str = "INFO"):
        """로그 메시지 추가."""
        # 로그 메시지에서 레벨 추출
        detected_level = self._detect_level(message)
        if detected_level:
            level = detected_level

        self._all_logs.append((level, message))

        # 최대 보존 라인
        if len(self._all_logs) > self.MAX_LINES:
            self._all_logs = self._all_logs[-self.MAX_LINES:]

        # 필터 통과 시 표시
        if self._passes_filter(level, message):
            self._append_to_view(message, level)

    def _detect_level(self, message: str) -> str | None:
        """메시지에서 로그 레벨 감지."""
        # 형식: [HH:MM:SS] LEVEL    message
        for lvl in ("ERROR", "WARNING", "INFO", "DEBUG"):
            if f"] {lvl}" in message:
                return lvl
        return None

    def _passes_filter(self, level: str, message: str) -> bool:
        """레벨 필터 + 검색어 필터 통과 여부."""
        level_ok = self._LEVEL_ORDER.get(level, 0) >= self._LEVEL_ORDER.get(
            self._filter_level, 0
        )
        search_ok = (
            not self._search_text
            or self._search_text.lower() in message.lower()
        )
        return level_ok and search_ok

    def _append_to_view(self, message: str, level: str = "INFO"):
        """뷰에 메시지 추가 (색상 적용)."""
        color = {
            "ERROR": "#f38ba8",
            "WARNING": "#f9e2af",
            "DEBUG": "#6c7086",
        }.get(level, "#a6adc8")

        safe_msg = html.escape(message)
        self.log_view.append(f'<span style="color:{color}">{safe_msg}</span>')

        # 자동 스크롤
        scrollbar = self.log_view.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _rebuild_view(self):
        """필터 변경 시 뷰 재구성."""
        self.log_view.clear()
        for level, message in self._all_logs:
            if self._passes_filter(level, message):
                self._append_to_view(message, level)

    def _on_level_changed(self, text: str):
        self._filter_level = text
        self._rebuild_view()

    def _on_search_changed(self, text: str):
        self._search_text = text
        self._rebuild_view()

    def _on_clear(self):
        self._all_logs.clear()
        self.log_view.clear()
