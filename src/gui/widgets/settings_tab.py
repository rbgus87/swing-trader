"""설정 탭 — config.yaml 편집 + .env 관리.

모든 슬라이더 값은 ×1000 스케일로 저장/로드하여 float 정밀도 유지.
.env 파일 쓰기를 지원하여 API 키 변경사항도 정상 저장.
"""

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv, set_key
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QComboBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)


class SettingField:
    """설정 필드 위젯 생성 헬퍼."""

    @staticmethod
    def spin(value: int, min_v: int, max_v: int, suffix: str = "") -> QSpinBox:
        w = QSpinBox()
        w.setRange(min_v, max_v)
        w.setValue(value)
        if suffix:
            w.setSuffix(f" {suffix}")
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
            1, 500, "B"
        )
        form.addRow("최소 거래대금", self.w_min_amount)

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

        backtest = self._config.get("backtest", {})
        self.w_initial_capital = SettingField.spin(
            backtest.get("initial_capital", 1_000_000), 100_000, 100_000_000, "원"
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
        # Trading
        trading = self._config.setdefault("trading", {})
        trading["universe"] = self.w_universe.currentText()
        trading["max_positions"] = self.w_max_positions.value()
        trading["reentry_cooldown_days"] = self.w_reentry_cooldown.value()

        # Screening
        screening = self._config.setdefault("screening", {})
        screening["min_daily_amount"] = self.w_min_amount.value() * 100_000_000
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

        # Backtest
        backtest = self._config.setdefault("backtest", {})
        backtest["initial_capital"] = self.w_initial_capital.value()

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
