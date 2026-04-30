"""selftest.py — 환경/의존성 무결성 검증.

운영 시작 또는 빌드 직후 5~10초 안에 환경 전반을 검증해
silent fail (예: hidden import 누락, config 키 변경, DB 마이그레이션 누락) 을
조기 감지.

사용:
    python selftest.py             # 직접 실행
    python gui.py --selftest       # GUI 진입 전 검증

exit code:
    0 = 모든 단계 OK
    1 = FAIL 1건 이상
"""
from __future__ import annotations

import asyncio
import importlib
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Callable

# Windows cp949 파이프 안전 출력
try:
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if sys.stderr and hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_PROJECT_ROOT = Path(__file__).resolve().parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# ANSI 컬러 (Windows 콘솔도 지원)
_GREEN = "\x1b[32m"
_RED = "\x1b[31m"
_YELLOW = "\x1b[33m"
_GRAY = "\x1b[90m"
_RESET = "\x1b[0m"

_NETWORK_TIMEOUT = 5.0

_REQUIRED_MODULES = [
    "pandas", "numpy",
    "PyQt5", "PyQt5.QtCore", "PyQt5.QtWidgets",
    "loguru", "apscheduler", "apscheduler.schedulers.asyncio",
    "httpx", "websockets", "requests",
    "yaml", "ruamel.yaml", "dotenv",
    "holidays", "pytz",
    "pykrx", "FinanceDataReader",
    "pyqtgraph",
]

_REQUIRED_TRADE_TABLES = {
    "positions", "trades", "daily_performance", "signals",
    "daily_portfolio_snapshot", "schema_version",
}
_REQUIRED_DATA_TABLES = {
    "stocks", "daily_candles", "market_cap_history",
    "index_daily", "stock_status_events",
}

# v4 마이그레이션 후 positions 테이블에 있어야 할 v2.5+ 컬럼
_REQUIRED_POSITIONS_COLUMNS = {
    "initial_quantity", "tp2_price", "partial_sold_2",
}

_REQUIRED_CONFIG_KEYS = [
    # (dotted_path, expected_type)
    ("trading.initial_capital", (int, float)),
    ("trading.max_positions", int),
    ("trend_following.tp1_sell_ratio", (int, float)),
    ("trend_following.tp2_atr", (int, float)),
    ("trend_following.tp2_sell_ratio", (int, float)),
    ("trend_following.sizing_mode", str),
    ("trend_following.stop_loss_atr", (int, float)),
    ("trend_following.take_profit_atr", (int, float)),
    ("trend_following.trailing_atr", (int, float)),
    ("trend_following.adx_threshold", (int, float)),
    ("trend_following.relative_strength_threshold", (int, float)),
    ("universe_pool.min_market_cap", (int, float)),
    ("broker.base_url", str),
]


# ──────────────────────────────────────────────────────────────────
# Step results
# ──────────────────────────────────────────────────────────────────
class StepResult:
    OK = "OK"
    FAIL = "FAIL"
    WARN = "WARN"
    SKIP = "SKIP"


def _print_step(idx: int, name: str, status: str, detail: str = "", remedy: str = "") -> None:
    color = {
        StepResult.OK: _GREEN,
        StepResult.FAIL: _RED,
        StepResult.WARN: _YELLOW,
        StepResult.SKIP: _GRAY,
    }.get(status, "")
    tag = f"[{status}]"
    line = f"{color}{tag:<7}{_RESET} {idx:02d}. {name:<30}"
    if detail:
        line += f"  {detail}"
    try:
        print(line)
        if remedy:
            print(f"        {_GRAY}→ {remedy}{_RESET}")
    except UnicodeEncodeError:
        print(line.encode("ascii", "replace").decode("ascii"))
        if remedy:
            print(f"        -> {remedy.encode('ascii', 'replace').decode('ascii')}")


# ──────────────────────────────────────────────────────────────────
# Steps (sync)
# ──────────────────────────────────────────────────────────────────
def step_imports() -> tuple[str, str, str]:
    missing = []
    for mod in _REQUIRED_MODULES:
        try:
            importlib.import_module(mod)
        except ImportError as e:
            missing.append(f"{mod} ({type(e).__name__})")
    if missing:
        names = " ".join(m.split(" ")[0].split(".")[0] for m in missing)
        return (
            StepResult.FAIL,
            f"누락 {len(missing)}: {missing[0]}" + (f" 외 {len(missing)-1}건" if len(missing) > 1 else ""),
            f"pip install {names}",
        )
    return StepResult.OK, f"{len(_REQUIRED_MODULES)} modules", ""


def step_indicators() -> tuple[str, str, str]:
    """trend_following_v2.calculate_indicators 1회 빌드 — ATR/ADX/MA/MACD 모두 검증."""
    try:
        import numpy as np
        import pandas as pd
        from src.strategy.trend_following_v2 import StrategyParams, calculate_indicators

        rng = np.random.default_rng(42)
        n = 250
        # 단조 증가 + 노이즈 (정상적 추세)
        base = np.cumsum(rng.normal(0.5, 1.0, n)) + 1000
        df = pd.DataFrame({
            "date": pd.date_range("2024-01-01", periods=n, freq="D"),
            "open":  base + rng.normal(0, 0.3, n),
            "high":  base + np.abs(rng.normal(0, 1.0, n)),
            "low":   base - np.abs(rng.normal(0, 1.0, n)),
            "close": base + rng.normal(0, 0.5, n),
            "volume": rng.integers(10000, 100000, n).astype(float),
        })
        df = calculate_indicators(df, StrategyParams())

        # 지표 컬럼이 정상 계산됐는지 — 마지막 행 NaN 아님
        required_cols = ["atr", "adx", "ma20", "ma60", "ma120", "macd_hist"]
        last = df.iloc[-1]
        nan_cols = [c for c in required_cols if c not in df.columns or pd.isna(last.get(c))]
        if nan_cols:
            return (
                StepResult.FAIL,
                f"NaN 컬럼: {nan_cols}",
                "calculate_indicators 함수 또는 컬럼 매핑 확인",
            )
        return (
            StepResult.OK,
            f"ATR={last['atr']:.2f}, ADX={last['adx']:.1f}, MACDh={last['macd_hist']:.2f}",
            "",
        )
    except Exception as e:
        return (
            StepResult.FAIL,
            f"{type(e).__name__}: {str(e)[:80]}",
            "지표 계산 모듈 import / 함수 시그니처 확인",
        )


def _get_dotted(d: dict, path: str):
    cur = d
    for k in path.split("."):
        if not isinstance(cur, dict) or k not in cur:
            return None
        cur = cur[k]
    return cur


def step_config(yaml_path: Path) -> tuple[str, str, str]:
    try:
        import yaml
        if not yaml_path.exists():
            return StepResult.FAIL, f"{yaml_path.name} 없음", "config.yaml 생성 필요"
        with open(yaml_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception as e:
        return StepResult.FAIL, f"yaml 로드 실패: {e}", "config.yaml 문법 확인"

    missing = []
    type_errors = []
    for key, expected_type in _REQUIRED_CONFIG_KEYS:
        v = _get_dotted(data, key)
        if v is None:
            missing.append(key)
        elif not isinstance(v, expected_type):
            type_errors.append(f"{key}={v!r} ({type(v).__name__})")

    if missing:
        return StepResult.FAIL, f"키 누락: {missing[:3]}{'...' if len(missing) > 3 else ''}", "config.yaml 보충"
    if type_errors:
        return StepResult.FAIL, f"타입 오류: {type_errors[:2]}", "config.yaml 값 확인"

    # sizing_mode 값 화이트리스트 검증
    sm = _get_dotted(data, "trend_following.sizing_mode")
    if sm not in ("cash", "equity"):
        return (
            StepResult.WARN,
            f"sizing_mode={sm!r} (예상: 'cash' 또는 'equity')",
            "v2.6: sizing_mode를 'equity'로 권장",
        )

    return StepResult.OK, f"{len(_REQUIRED_CONFIG_KEYS)} keys, sizing={sm}", ""


def step_trade_db() -> tuple[str, str, str]:
    """swing_trade.db: 테이블 존재 + positions v4 컬럼 검증."""
    try:
        from src.data_pipeline import TRADE_DB_PATH
        p = Path(TRADE_DB_PATH)
    except Exception as e:
        return StepResult.FAIL, f"TRADE_DB_PATH import 실패: {e}", "src/data_pipeline/__init__.py 확인"

    if not p.exists():
        return (
            StepResult.WARN,
            f"DB 없음 ({p.name})",
            "GUI 또는 main.py 1회 실행으로 자동 생성됨",
        )

    try:
        conn = sqlite3.connect(str(p))
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        # positions 컬럼
        cols = {r[1] for r in conn.execute("PRAGMA table_info(positions)").fetchall()}
        # schema_version
        try:
            ver_row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
            schema_ver = ver_row[0] if ver_row and ver_row[0] is not None else 0
        except sqlite3.OperationalError:
            schema_ver = 0
        conn.close()
    except sqlite3.Error as e:
        return StepResult.FAIL, f"SQLite 오류: {e}", "DB 락/파일 무결성 확인"

    missing_tables = _REQUIRED_TRADE_TABLES - tables
    if missing_tables:
        return (
            StepResult.FAIL,
            f"테이블 누락: {sorted(missing_tables)}",
            "DataStore.create_tables() 자동 마이그레이션 트리거",
        )

    missing_cols = _REQUIRED_POSITIONS_COLUMNS - cols
    if missing_cols:
        return (
            StepResult.FAIL,
            f"positions 컬럼 누락 (v2.5+): {sorted(missing_cols)}, schema={schema_ver}",
            "DataStore() init 시 v4 마이그레이션 실행",
        )

    return (
        StepResult.OK,
        f"{len(tables)} tables, schema v{schema_ver}, positions v2.5+ OK",
        "",
    )


def step_data_db() -> tuple[str, str, str]:
    """swing_data.db: Phase 1 데이터 테이블 검증."""
    try:
        from src.data_pipeline import DATA_DB_PATH
        p = Path(DATA_DB_PATH)
    except Exception as e:
        return StepResult.FAIL, f"DATA_DB_PATH import 실패: {e}", ""

    if not p.exists():
        return (
            StepResult.FAIL,
            f"DB 없음 ({p.name})",
            "Phase 1 데이터 파이프라인 실행 필요 (collect_stocks_meta.py 등)",
        )

    try:
        conn = sqlite3.connect(str(p))
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        # 행 수 sanity
        n_stocks = conn.execute("SELECT COUNT(*) FROM stocks").fetchone()[0]
        n_candles = conn.execute("SELECT COUNT(*) FROM daily_candles").fetchone()[0]
        conn.close()
    except sqlite3.Error as e:
        return StepResult.FAIL, f"SQLite 오류: {e}", ""

    missing_tables = _REQUIRED_DATA_TABLES - tables
    if missing_tables:
        return (
            StepResult.FAIL,
            f"테이블 누락: {sorted(missing_tables)}",
            "Phase 1 데이터 수집 스크립트 재실행",
        )

    if n_stocks < 100 or n_candles < 100_000:
        return (
            StepResult.WARN,
            f"데이터 부족 (stocks={n_stocks}, candles={n_candles:,})",
            "scripts/daily_run.sh 또는 Phase 1 backfill 실행",
        )

    return (
        StepResult.OK,
        f"{len(tables)} tables, stocks={n_stocks:,}, candles={n_candles:,}",
        "",
    )


def step_engine_init() -> tuple[str, str, str]:
    """TradingEngine 클래스 import + StrategyParams 인스턴스화 (실제 인스턴스화는 안 함 — 키움 API 호출 회피)."""
    try:
        from src.trading_engine import TradingEngine  # noqa: F401
        from src.strategy.trend_following_v2 import StrategyParams
        from src.utils.config import config

        # config에서 v2.6 파라미터 읽어 StrategyParams 빌드
        params = StrategyParams(
            tp1_sell_ratio=float(config.get("trend_following.tp1_sell_ratio", 0.10)),
            tp2_atr=float(config.get("trend_following.tp2_atr", 4.0)),
            tp2_sell_ratio=float(config.get("trend_following.tp2_sell_ratio", 0.10)),
        )
        # sanity: tp2_atr이 양수면 TP2 활성
        tp2_active = params.tp2_atr > 0 and params.tp2_sell_ratio > 0
        sizing = config.get("trend_following.sizing_mode", "?")
        return (
            StepResult.OK,
            f"TP1={params.tp1_sell_ratio:.0%}, TP2={params.tp2_sell_ratio:.0%} "
            f"({'on' if tp2_active else 'off'}), sizing={sizing}",
            "",
        )
    except Exception as e:
        return (
            StepResult.FAIL,
            f"{type(e).__name__}: {str(e)[:80]}",
            "src.trading_engine import 체인 / config 키 확인",
        )


def step_backtester_smoke() -> tuple[str, str, str]:
    """백테스터 import 및 데이터클래스 sanity (실제 백테스트는 무거움 — 스킵)."""
    try:
        from src.backtest.portfolio_backtester import (
            PortfolioTradeResult, run_portfolio_backtest,  # noqa: F401
        )
        # PortfolioTradeResult가 v2.5+ initial_shares 필드 가지는지
        fields = PortfolioTradeResult.__dataclass_fields__
        if "initial_shares" not in fields:
            return (
                StepResult.FAIL,
                "PortfolioTradeResult.initial_shares 누락",
                "src/backtest/portfolio_backtester.py 마이그레이션 (v2.5)",
            )
        return StepResult.OK, "import OK + initial_shares 필드 존재", ""
    except Exception as e:
        return (
            StepResult.FAIL,
            f"{type(e).__name__}: {str(e)[:80]}",
            "백테스터 import 체인 확인",
        )


# ──────────────────────────────────────────────────────────────────
# Steps (async — 네트워크)
# ──────────────────────────────────────────────────────────────────
async def step_kiwoom_token() -> tuple[str, str, str]:
    """키움 API /oauth2/token 호출. .env에 키 없으면 SKIP."""
    appkey = os.getenv("KIWOOM_APPKEY", "")
    secret = os.getenv("KIWOOM_SECRETKEY", "")
    if not appkey or not secret:
        return (
            StepResult.SKIP,
            ".env에 KIWOOM_APPKEY/SECRETKEY 미설정",
            "운영 시 .env 보충 필요",
        )

    try:
        from src.utils.config import config
        from src.broker.rest_client import KiwoomRestClient

        base_url = config.get("broker.base_url", "https://api.kiwoom.com")
        client = KiwoomRestClient(base_url, appkey, secret)
        token = await asyncio.wait_for(
            client.authenticate(), timeout=_NETWORK_TIMEOUT,
        )
        if not token:
            return StepResult.FAIL, "토큰 빈 값", "키움 API 키 확인"
        return StepResult.OK, f"token len={len(token)}", ""
    except asyncio.TimeoutError:
        return StepResult.WARN, f"timeout {_NETWORK_TIMEOUT}s — 오프라인?", ""
    except Exception as e:
        cls = type(e).__name__
        if "Connect" in cls or "OSError" in cls or "Timeout" in cls:
            return StepResult.WARN, f"네트워크: {cls}", ""
        return StepResult.FAIL, f"{cls}: {str(e)[:80]}", "키움 API 응답/키 확인"


async def step_telegram() -> tuple[str, str, str]:
    """Telegram getMe 호출. 토큰 없으면 SKIP."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not token:
        return (
            StepResult.SKIP,
            ".env에 TELEGRAM_BOT_TOKEN 미설정",
            "알림 사용 시 .env 보충",
        )

    try:
        import httpx
        url = f"https://api.telegram.org/bot{token}/getMe"
        async with httpx.AsyncClient(timeout=_NETWORK_TIMEOUT) as c:
            resp = await c.get(url)
        data = resp.json()
        if not data.get("ok"):
            return (
                StepResult.FAIL,
                f"getMe ok=False: {data.get('description', '')[:60]}",
                "TELEGRAM_BOT_TOKEN 갱신",
            )
        bot_name = data.get("result", {}).get("username", "?")
        return StepResult.OK, f"@{bot_name}", ""
    except asyncio.TimeoutError:
        return StepResult.WARN, f"timeout {_NETWORK_TIMEOUT}s — 오프라인?", ""
    except Exception as e:
        cls = type(e).__name__
        if "Connect" in cls or "OSError" in cls:
            return StepResult.WARN, f"네트워크: {cls}", ""
        return StepResult.FAIL, f"{cls}: {str(e)[:80]}", ""


# ──────────────────────────────────────────────────────────────────
# Runner
# ──────────────────────────────────────────────────────────────────
def run_selftest() -> int:
    """selftest 진입점. exit code 반환 (0=성공, 1=FAIL 1건 이상)."""
    # loguru 노이즈 억제 — selftest 출력 사이에 INFO 로그 끼지 않게
    try:
        from loguru import logger as _lg
        _lg.remove()
        _lg.add(sys.stderr, level="ERROR")
    except Exception:
        pass

    print(f"{_GRAY}=== swing-trader selftest (v2.6) ==={_RESET}")
    started = time.time()
    results: list[tuple[int, str, str]] = []

    def _run_sync(idx: int, name: str, fn: Callable[[], tuple[str, str, str]]) -> str:
        status, detail, remedy = fn()
        _print_step(idx, name, status, detail, remedy)
        results.append((idx, name, status))
        return status

    # 동기 검증
    _run_sync(1, "핵심 모듈 import", step_imports)
    _run_sync(2, "지표 계산 (ATR/ADX/MACD)", step_indicators)
    yaml_path = _PROJECT_ROOT / "config.yaml"
    _run_sync(3, "Config 무결성", lambda: step_config(yaml_path))
    _run_sync(4, "swing_trade.db (운영 DB)", step_trade_db)
    _run_sync(5, "swing_data.db (시세 DB)", step_data_db)
    _run_sync(6, "TradingEngine import", step_engine_init)
    _run_sync(7, "Backtester import", step_backtester_smoke)

    # 비동기 네트워크 검증
    async def _run_async() -> None:
        for idx, name, fn in [
            (8, "Kiwoom 토큰 발급", step_kiwoom_token),
            (9, "Telegram 봇", step_telegram),
        ]:
            try:
                status, detail, remedy = await asyncio.wait_for(
                    fn(), timeout=_NETWORK_TIMEOUT + 1.0,
                )
            except asyncio.TimeoutError:
                status, detail, remedy = (
                    StepResult.WARN,
                    f"전체 타임아웃 {_NETWORK_TIMEOUT + 1.0}s",
                    "",
                )
            _print_step(idx, name, status, detail, remedy)
            results.append((idx, name, status))

    # Windows 이벤트 루프 정책 (Python 3.13까지만 — 3.14+는 deprecated)
    if sys.platform == "win32" and sys.version_info < (3, 14):
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        except Exception:
            pass
    try:
        asyncio.run(_run_async())
    except Exception as e:
        print(f"{_RED}네트워크 검증 자체 실패: {type(e).__name__}: {e}{_RESET}")

    # 요약
    elapsed = time.time() - started
    ok = sum(1 for _, _, s in results if s == StepResult.OK)
    fail = sum(1 for _, _, s in results if s == StepResult.FAIL)
    warn = sum(1 for _, _, s in results if s == StepResult.WARN)
    skip = sum(1 for _, _, s in results if s == StepResult.SKIP)
    total = len(results)
    print(f"{_GRAY}---{_RESET}")
    print(
        f"통과: {_GREEN}{ok}{_RESET} / {total}  "
        f"FAIL: {_RED}{fail}{_RESET}  "
        f"WARN: {_YELLOW}{warn}{_RESET}  "
        f"SKIP: {_GRAY}{skip}{_RESET}  "
        f"({elapsed:.1f}s)"
    )
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(run_selftest())
