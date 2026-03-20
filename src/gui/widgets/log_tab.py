"""로그 탭 — 실시간 로그 뷰어 + 레벨 필터 + 검색 + 프로그레스 바."""

import html
import re

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
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
        "ALL": -1,
        "DEBUG": 0,
        "PROGRESS": 0,
        "INFO": 1,
        "TRADE": 2,
        "WARNING": 3,
        "ERROR": 4,
    }

    # PROGRESS 메시지 형식: "라벨|현재|전체"
    _PROGRESS_RE = re.compile(r"PROGRESS\s+(.+)\|(\d+)\|(\d+)")

    def __init__(self, parent=None):
        super().__init__(parent)
        self._all_logs: list[tuple[str, str]] = []  # (level, message)
        self._filter_level = "ALL"
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
        self.combo_level.addItems(["ALL", "DEBUG", "INFO", "TRADE", "WARNING", "ERROR"])
        self.combo_level.setCurrentText("ALL")
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

        # ── 프로그레스 바 ──
        self._progress_bar = QProgressBar()
        self._progress_bar.setFixedHeight(18)
        self._progress_bar.setTextVisible(True)
        self._progress_bar.setVisible(False)
        layout.addWidget(self._progress_bar)

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

        # PROGRESS 메시지: 프로그레스 바만 업데이트, 텍스트에는 안 쌓음
        if level == "PROGRESS":
            self._try_update_progress(message)
            return

        self._all_logs.append((level, message))

        # 최대 보존 라인
        if len(self._all_logs) > self.MAX_LINES:
            self._all_logs = self._all_logs[-self.MAX_LINES:]

        # "완료" 키워드 시 프로그레스 바 숨김
        if "완료" in message and self._progress_bar.isVisible():
            self._progress_bar.setVisible(False)

        # 필터 통과 시 표시
        if self._passes_filter(level, message):
            self._append_to_view(message, level)

    def _try_update_progress(self, message: str) -> bool:
        """PROGRESS 로그 감지 → 프로그레스 바 업데이트. 매칭 시 True."""
        match = self._PROGRESS_RE.search(message)
        if not match:
            return False

        label = match.group(1)
        current = int(match.group(2))
        total = int(match.group(3))
        pct = current * 100 // total if total > 0 else 0

        self._progress_bar.setMaximum(total)
        self._progress_bar.setValue(current)
        self._progress_bar.setFormat(f"{label} {current}/{total} ({pct}%)")
        self._progress_bar.setVisible(True)
        return True

    def _detect_level(self, message: str) -> str | None:
        """메시지에서 로그 레벨 감지."""
        for lvl in ("ERROR", "WARNING", "TRADE", "PROGRESS", "INFO", "DEBUG"):
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
            "TRADE": "#a6e3a1",
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
        self._progress_bar.setVisible(False)
