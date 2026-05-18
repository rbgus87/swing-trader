"""설정 탭 — config.yaml 편집 + .env 관리 (v2.7 전용).

v2.7 정리:
  - 5개 탭: 매매 / 전략 / 리스크 / 스케줄 / API
  - 종목관리/스크리닝 탭 제거 (v1 레거시 — 동적 Universe 사용)
  - 전략 탭: TrendFollowing v2.7 파라미터 직접 편집

슬라이더 값은 ×1000 또는 ×scale 정수로 저장하여 float 정밀도 유지.
"""

import os
from pathlib import Path

from ruamel.yaml import YAML
from dotenv import load_dotenv, set_key
from PyQt5.QtCore import Qt, QLocale
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
        """일반 float 슬라이더 + 라벨."""
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
    """설정 탭 — config.yaml + .env 편집 (v2.7 전용)."""

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

        # v2.7 서브탭 — 5개
        self.sub_tabs = QTabWidget()
        self.sub_tabs.addTab(self._build_trading_tab(), "\U0001F4B0 매매")
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

    # ── 매매 서브탭 ──

    def _build_trading_tab(self) -> QWidget:
        w = QWidget()
        scroll = self._wrap_scroll(w)
        form = QFormLayout(w)
        form.setSpacing(12)
        form.setContentsMargins(16, 16, 16, 16)

        trading = self._config.get("trading", {})

        form.addRow(self._make_separator("기본 설정"))

        self.w_initial_capital = SettingField.spin(
            trading.get("initial_capital", 10_000_000),
            100_000, 1_000_000_000, "원",
        )
        form.addRow("초기 자본", self.w_initial_capital)

        self.w_max_positions = SettingField.spin(
            trading.get("max_positions", 6), 1, 10, "종목"
        )
        form.addRow("최대 보유 종목", self.w_max_positions)

        self.w_universe = SettingField.combo(
            ["kospi", "kosdaq", "kospi_kosdaq"],
            trading.get("universe", "kospi_kosdaq"),
        )
        form.addRow("투자 유니버스", self.w_universe)

        form.addRow(self._make_separator("Universe (동적, 읽기 전용)"))
        universe_pool = self._config.get("universe_pool", {})
        min_cap = int(universe_pool.get("min_market_cap", 3_000_000_000_000) / 1_000_000_000_000)
        min_amt = int(universe_pool.get("min_trading_value", 5_000_000_000) / 100_000_000)
        refresh = universe_pool.get("refresh_interval_days", 60)
        info = QLabel(
            f"• 시총 {min_cap}조원 이상 / 거래대금 {min_amt}억원 이상\n"
            f"• {refresh}일마다 재계산 (분기별)\n"
            "• v2.7 단일 전략(TrendFollowing)이 매일 동적으로 진입 후보 산출"
        )
        info.setStyleSheet(
            "color: #a6adc8; font-size: 11px; line-height: 1.4; "
            "padding: 8px; background-color: #1e1e2e; border-radius: 4px;"
        )
        info.setWordWrap(True)
        form.addRow(info)

        form.addRow(self._make_separator("진입 시간대"))
        self.w_entry_start = SettingField.line_edit(
            trading.get("entry_start_time", "09:30"),
        )
        form.addRow("진입 시작 시각", self.w_entry_start)

        self.w_entry_end = SettingField.line_edit(
            trading.get("entry_end_time", "15:00"),
        )
        form.addRow("진입 종료 시각", self.w_entry_end)

        return scroll

    # ── 전략 서브탭 ──

    def _build_strategy_tab(self) -> QWidget:
        w = QWidget()
        scroll = self._wrap_scroll(w)
        form = QFormLayout(w)
        form.setSpacing(12)
        form.setContentsMargins(16, 16, 16, 16)

        tf = self._config.get("trend_following", {})

        # v2.7 전략 요약 (읽기 전용)
        form.addRow(self._make_separator("현재 전략 (TrendFollowing v2.7)"))
        summary = QLabel(
            "• 진입: 추세추종 (완전 정배열 + MA60 +5~+20% + MACD hist > 0\n"
            "        + 시장별 상대강도 +5%p + ADX≥20)\n"
            "• 가드레일: breadth ≥ 0.40 (Universe MA200)\n"
            "• Universe: 시총 3조+, 거래대금 50억+, 60일 재계산\n"
            "• 포트폴리오: equity / max_positions (균등 + 복리)"
        )
        summary.setStyleSheet(
            "color: #a6e3a1; font-size: 11px; line-height: 1.5; "
            "padding: 8px; background-color: #1e1e2e; border-radius: 4px;"
        )
        summary.setWordWrap(True)
        form.addRow(summary)

        # 청산 파라미터 — 편집 가능
        form.addRow(self._make_separator("청산 (편집)"))

        self.w_sl_atr = SettingField.float_slider(
            float(tf.get("stop_loss_atr", 2.0)), 0.5, 4.0, scale=10, fmt="{:.1f}x",
        )
        form.addRow("SL (ATR 배수)", self.w_sl_atr)

        self.w_tp1_atr = SettingField.float_slider(
            float(tf.get("take_profit_atr", 2.0)), 0.5, 5.0, scale=10, fmt="{:.1f}x",
        )
        form.addRow("TP1 거리 (ATR)", self.w_tp1_atr)

        self.w_tp1_ratio = SettingField.pct_slider(
            float(tf.get("tp1_sell_ratio", 0.10)), 0.0, 1.0,
        )
        form.addRow("TP1 매도 비율", self.w_tp1_ratio)

        self.w_tp2_atr = SettingField.float_slider(
            float(tf.get("tp2_atr", 4.0)), 0.0, 8.0, scale=10, fmt="{:.1f}x",
        )
        form.addRow("TP2 거리 (ATR)", self.w_tp2_atr)

        self.w_tp2_ratio = SettingField.pct_slider(
            float(tf.get("tp2_sell_ratio", 0.10)), 0.0, 1.0,
        )
        form.addRow("TP2 매도 비율", self.w_tp2_ratio)

        self.w_trail_atr = SettingField.float_slider(
            float(tf.get("trailing_atr", 4.0)), 1.0, 8.0, scale=10, fmt="{:.1f}x",
        )
        form.addRow("Trail (ATR 배수)", self.w_trail_atr)

        self.w_hold_days = SettingField.spin(
            int(tf.get("max_hold_days", 20)), 1, 60, "일",
        )
        form.addRow("최대 보유일", self.w_hold_days)

        # 사이징
        form.addRow(self._make_separator("포지션 사이징"))
        self.w_sizing_mode = SettingField.combo(
            ["equity", "cash"], tf.get("sizing_mode", "equity"),
        )
        form.addRow("사이징 모드", self.w_sizing_mode)

        sizing_info = QLabel(
            "• equity: total_equity / max_positions (균등 + 복리, v2.5 권장)\n"
            "• cash:   cash × (1 / max_positions) (현금 기반, v2.4 호환)"
        )
        sizing_info.setStyleSheet(
            "color: #6c7086; font-size: 10px; line-height: 1.4; padding: 4px;"
        )
        sizing_info.setWordWrap(True)
        form.addRow(sizing_info)

        return scroll

    # ── 리스크 서브탭 ──

    def _build_risk_tab(self) -> QWidget:
        w = QWidget()
        scroll = self._wrap_scroll(w)
        form = QFormLayout(w)
        form.setSpacing(12)
        form.setContentsMargins(16, 16, 16, 16)

        risk = self._config.get("risk", {})

        form.addRow(self._make_separator("포지션 한도"))
        self.w_min_pos_amount = SettingField.spin(
            int(risk.get("min_position_amount", 1_000_000)),
            100_000, 100_000_000, "원",
        )
        form.addRow("최소 포지션 금액", self.w_min_pos_amount)

        form.addRow(self._make_separator("일일 손실 한도"))
        self.w_daily_loss_limit = SettingField.pct_slider(
            abs(float(risk.get("daily_loss_limit", -0.03))), 0.01, 0.10,
        )
        form.addRow("일일 손실 한도 (-)", self.w_daily_loss_limit)

        self.w_daily_loss_warning = SettingField.pct_slider(
            abs(float(risk.get("daily_loss_warning", -0.02))), 0.01, 0.10,
        )
        form.addRow("일일 손실 경고 (-)", self.w_daily_loss_warning)

        form.addRow(self._make_separator("최대 낙폭 (MDD)"))
        self.w_max_mdd = SettingField.pct_slider(
            abs(float(risk.get("max_mdd", -0.20))), 0.05, 0.50,
        )
        form.addRow("최대 MDD (-)", self.w_max_mdd)

        info = QLabel(
            "• v2.7 사이징은 trend_following.sizing_mode가 결정 (전략 탭 참고)\n"
            "• SL / Trail 등 청산 ATR 배수는 전략 탭에서 관리"
        )
        info.setStyleSheet(
            "color: #6c7086; font-size: 10px; line-height: 1.4; padding: 6px;"
        )
        info.setWordWrap(True)
        form.addRow(info)

        return scroll

    # ── 스케줄 서브탭 ──

    def _build_schedule_tab(self) -> QWidget:
        w = QWidget()
        scroll = self._wrap_scroll(w)
        form = QFormLayout(w)
        form.setSpacing(12)
        form.setContentsMargins(16, 16, 16, 16)

        schedule = self._config.get("schedule", {})

        form.addRow(self._make_separator("일일 작업"))
        self.w_screening_time = SettingField.line_edit(
            schedule.get("screening_time", "08:30"),
        )
        form.addRow("스크리닝 시각", self.w_screening_time)

        self.w_report_time = SettingField.line_edit(
            schedule.get("daily_report_time", "16:00"),
        )
        form.addRow("리포트 시각", self.w_report_time)

        self.w_reconnect_time = SettingField.line_edit(
            schedule.get("reconnect_time", "08:45"),
        )
        form.addRow("재연결 시각", self.w_reconnect_time)

        form.addRow(self._make_separator("폴링"))
        self.w_polling_start = SettingField.line_edit(
            schedule.get("polling_start_time", "09:00"),
        )
        form.addRow("폴링 시작 시각", self.w_polling_start)

        self.w_polling_stop = SettingField.line_edit(
            schedule.get("polling_stop_time", "15:35"),
        )
        form.addRow("폴링 종료 시각", self.w_polling_stop)

        self.w_polling_interval = SettingField.spin(
            int(schedule.get("polling_interval", 10)), 1, 300, "초",
        )
        form.addRow("폴링 주기", self.w_polling_interval)

        return scroll

    # ── API 서브탭 ──

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
        """초기화 — config.yaml 다시 로드 후 UI 재구축."""
        self._load_config()
        self._build_content()

    def _collect_config(self):
        """위젯 값들을 self._config 딕셔너리에 수집 (v2.7 사양)."""

        # ── trading ──
        trading = self._config.setdefault("trading", {})
        trading["initial_capital"] = self.w_initial_capital.value()
        trading["max_positions"] = self.w_max_positions.value()
        trading["universe"] = self.w_universe.currentText()
        trading["entry_start_time"] = self.w_entry_start.text()
        trading["entry_end_time"] = self.w_entry_end.text()

        # ── trend_following (v2.7 청산 파라미터 + 사이징) ──
        tf = self._config.setdefault("trend_following", {})
        tf["stop_loss_atr"] = self.w_sl_atr._slider.value() / self.w_sl_atr._scale
        tf["take_profit_atr"] = self.w_tp1_atr._slider.value() / self.w_tp1_atr._scale
        tf["tp1_sell_ratio"] = self.w_tp1_ratio._slider.value() / 1000
        tf["tp2_atr"] = self.w_tp2_atr._slider.value() / self.w_tp2_atr._scale
        tf["tp2_sell_ratio"] = self.w_tp2_ratio._slider.value() / 1000
        tf["trailing_atr"] = self.w_trail_atr._slider.value() / self.w_trail_atr._scale
        tf["max_hold_days"] = self.w_hold_days.value()
        tf["sizing_mode"] = self.w_sizing_mode.currentText()

        # ── risk (v2.7 사용 항목만) ──
        risk = self._config.setdefault("risk", {})
        risk["min_position_amount"] = self.w_min_pos_amount.value()
        risk["daily_loss_limit"] = -(self.w_daily_loss_limit._slider.value() / 1000)
        risk["daily_loss_warning"] = -(self.w_daily_loss_warning._slider.value() / 1000)
        risk["max_mdd"] = -(self.w_max_mdd._slider.value() / 1000)

        # ── schedule ──
        schedule = self._config.setdefault("schedule", {})
        schedule["screening_time"] = self.w_screening_time.text()
        schedule["daily_report_time"] = self.w_report_time.text()
        schedule["reconnect_time"] = self.w_reconnect_time.text()
        schedule["polling_start_time"] = self.w_polling_start.text()
        schedule["polling_stop_time"] = self.w_polling_stop.text()
        schedule["polling_interval"] = self.w_polling_interval.value()

    # ── 유틸 ──

    def _make_separator(self, text: str) -> QLabel:
        label = QLabel(f"──  {text}")
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
