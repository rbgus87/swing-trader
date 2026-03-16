"""스윙 자동매매 대시보드.

Usage: streamlit run dashboard.py
"""

import os
import sqlite3
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st
import yaml

# Page config
st.set_page_config(
    page_title="스윙 자동매매",
    page_icon="📈",
    layout="wide",
)

DB_PATH = "trading.db"
CONFIG_PATH = "config.yaml"


def load_config():
    """config.yaml 로드."""
    if Path(CONFIG_PATH).exists():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    return {}


def get_db_connection():
    """SQLite DB 연결 (읽기 전용)."""
    if not Path(DB_PATH).exists():
        return None
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def query_df(conn, sql):
    """SQL 쿼리 → DataFrame."""
    if conn is None:
        return pd.DataFrame()
    try:
        return pd.read_sql_query(sql, conn)
    except Exception:
        return pd.DataFrame()


# Load config
config = load_config()

# --- Sidebar ---
with st.sidebar:
    st.title("⚙️ 설정")

    mode = config.get("trading", {}).get("mode", "paper")
    st.info(f"모드: **{mode.upper()}**")

    st.subheader("전략")
    strategy_type = config.get("strategy", {}).get("type", "golden_cross")
    st.write(f"전략: {strategy_type}")
    st.write(f"최대 보유: {config.get('trading', {}).get('max_positions', 3)}종목")
    st.write(
        f"목표 수익: {config.get('strategy', {}).get('target_return', 0.10) * 100:.0f}%"
    )
    st.write(f"최대 보유일: {config.get('strategy', {}).get('max_hold_days', 15)}일")

    st.subheader("종목 리스트")
    watchlist = config.get("watchlist", [])
    st.write(f"{len(watchlist)}종목 감시 중")
    with st.expander("종목 코드"):
        for code in watchlist:
            st.code(code)

    st.subheader("DB 상태")
    if Path(DB_PATH).exists():
        mtime = os.path.getmtime(DB_PATH)
        st.success(f"✅ {DB_PATH}")
        st.caption(
            f"최종 수정: {datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M:%S')}"
        )
    else:
        st.warning("⚠️ DB 파일 없음 (엔진 미실행)")

    # Auto refresh
    auto_refresh = st.checkbox("자동 새로고침 (30초)", value=False)
    if auto_refresh:
        import time

        time.sleep(30)
        st.rerun()

# --- Main Content ---
st.title("📈 스윙 자동매매 대시보드")

conn = get_db_connection()

if conn is None:
    st.warning("⚠️ trading.db 파일이 없습니다. 엔진을 먼저 실행하세요:")
    st.code("python main.py --mode paper")
    st.stop()

# --- Portfolio Summary ---
st.header("포트폴리오 요약")

positions_df = query_df(conn, "SELECT * FROM positions WHERE status = 'open'")
trades_df = query_df(conn, "SELECT * FROM trades ORDER BY executed_at DESC")
today = datetime.now().strftime("%Y-%m-%d")
today_trades = query_df(
    conn, f"SELECT * FROM trades WHERE executed_at LIKE '{today}%'"
)
perf_df = query_df(
    conn, "SELECT * FROM daily_performance ORDER BY date DESC LIMIT 1"
)

col1, col2, col3, col4 = st.columns(4)

with col1:
    initial = config.get("backtest", {}).get("initial_capital", 1_000_000)
    if not perf_df.empty:
        total_capital = perf_df.iloc[0].get("total_capital", initial)
    else:
        total_capital = initial
    st.metric("총 자산", f"{int(total_capital):,}원")

with col2:
    if not today_trades.empty and "pnl" in today_trades.columns:
        sell_trades = today_trades[today_trades["side"] == "sell"]
        today_pnl = sell_trades["pnl"].sum() if not sell_trades.empty else 0
        st.metric("오늘 손익", f"{int(today_pnl):+,}원")
    else:
        st.metric("오늘 손익", "0원")

with col3:
    max_pos = config.get("trading", {}).get("max_positions", 3)
    st.metric("보유 종목", f"{len(positions_df)} / {max_pos}")

with col4:
    st.metric("총 거래", f"{len(trades_df)}건")

# --- Open Positions ---
st.header("📊 보유 포지션")

if positions_df.empty:
    st.info("현재 보유 중인 포지션이 없습니다.")
else:
    display_cols = [
        "code",
        "name",
        "entry_date",
        "entry_price",
        "quantity",
        "stop_price",
        "target_price",
        "status",
    ]
    available = [c for c in display_cols if c in positions_df.columns]
    st.dataframe(positions_df[available], use_container_width=True, hide_index=True)

# --- Recent Trades ---
st.header("📋 최근 거래")

if trades_df.empty:
    st.info("매매 기록이 없습니다.")
else:
    display_cols = [
        "executed_at",
        "code",
        "name",
        "side",
        "price",
        "quantity",
        "amount",
        "pnl",
        "pnl_pct",
        "reason",
    ]
    available = [c for c in display_cols if c in trades_df.columns]
    recent = trades_df[available].head(20)
    st.dataframe(recent, use_container_width=True, hide_index=True)

# --- Performance Chart ---
st.header("📈 일별 성과")

perf_all = query_df(conn, "SELECT * FROM daily_performance ORDER BY date")
if perf_all.empty:
    st.info(
        "일별 성과 데이터가 없습니다. 엔진 실행 후 장 마감(16:00) 이후 생성됩니다."
    )
else:
    perf_all["date"] = pd.to_datetime(perf_all["date"])

    tab1, tab2 = st.tabs(["자산 곡선", "일별 수익률"])

    with tab1:
        st.line_chart(perf_all.set_index("date")["total_capital"])

    with tab2:
        st.bar_chart(perf_all.set_index("date")["daily_return"])

# --- Footer ---
st.divider()
st.caption(
    f"realtime-trader 대시보드 | DB: {DB_PATH} | "
    f"마지막 조회: {datetime.now().strftime('%H:%M:%S')}"
)

conn.close()
