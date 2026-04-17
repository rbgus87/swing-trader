# SURGERY_PROMPTS_V2.md — 인프라 잔여 수정 + 전략 재설계

> Phase A: 인프라 잔여 이슈 4건 (전략 재설계 전 선행)
> Phase B: 전략 재설계 — 기존 5개 → 신규 3개 교체
> Phase C: 검증 + 커밋

---

## Phase A: 인프라 잔여 수정

### 프롬프트 A-1 — INFRA-1 + INFRA-3 + INFRA-5 (안전장치 + 기반)

```
CLAUDE.md를 읽어줘.

인프라 잔여 이슈 3건을 처리해줘.

## INFRA-1: StopManager 초기화 fallback 기본값 통일

src/engine.py 67-72행에서 StopManager 초기화 시 fallback 기본값이
config.yaml과 불일치해. config가 정상 로드되면 문제없지만,
파일 누락 시 잘못된 값이 적용돼.

기존:
self._stop_mgr = StopManager(
    stop_atr_mult=config.get("risk.stop_atr_multiplier", 2.5),
    max_stop_pct=config.get("risk.max_stop_pct", 0.10),
    trailing_atr_mult=config.get("risk.trailing_atr_multiplier", 2.5),
    trailing_activate_pct=config.get("risk.trailing_activate_pct", 0.07),
)

수정 (config.yaml 값과 동일한 fallback):
self._stop_mgr = StopManager(
    stop_atr_mult=config.get("risk.stop_atr_multiplier", 1.5),
    max_stop_pct=config.get("risk.max_stop_pct", 0.07),
    trailing_atr_mult=config.get("risk.trailing_atr_multiplier", 2.0),
    trailing_activate_pct=config.get("risk.trailing_activate_pct", 0.10),
)

## INFRA-3: Paper 모드 이중 안전장치

src/engine.py의 __init__에서, paper 모드일 때 추가 안전장치를 넣어줘:

1. paper 모드이면 로그에 명확히 표시:
   if self.mode == "paper":
       logger.warning("⚠️ PAPER 모드 — 실전 주문 비활성화")

2. _execute_sell과 _check_entry_conditions에서 주문 실행 직전에
   mode 재확인하는 방어 코드는 이미 있으니 (if self.mode == "live") 유지.

3. OrderManager에 paper 모드 플래그 전달 — 이중 방어:
   src/broker/order_manager.py의 __init__에 is_paper: bool = False 파라미터 추가.
   execute_order() 최상단에:
   if self._is_paper:
       logger.info(f"[PAPER] 주문 시뮬레이션: {code} {qty}주")
       return OrderResult(success=True, order_no="PAPER-SIM", message="paper mode")

   src/engine.py __init__에서:
   self._order_mgr = OrderManager(self._kiwoom, account, is_paper=(self.mode == "paper"))

## INFRA-5: DataStore.get_trade_statistics() 구현

src/datastore.py에 메서드 추가:

def get_trade_statistics(self, limit: int = 50) -> dict | None:
    """최근 N건 매도 거래의 승률/평균손익 통계.

    Args:
        limit: 최근 N건 (매도 거래만).

    Returns:
        {"count": int, "win_rate": float, "avg_win": float, "avg_loss": float}
        거래 없으면 None.
    """
    try:
        cursor = self.conn.execute(
            "SELECT pnl_pct FROM trades WHERE side = 'sell' "
            "ORDER BY executed_at DESC LIMIT ?",
            (limit,),
        )
        rows = cursor.fetchall()
        if not rows:
            return None

        pnls = [r[0] for r in rows]
        wins = [p for p in pnls if p > 0]
        losses = [abs(p) for p in pnls if p <= 0]

        return {
            "count": len(pnls),
            "win_rate": len(wins) / len(pnls) if pnls else 0.5,
            "avg_win": sum(wins) / len(wins) if wins else 0.08,
            "avg_loss": sum(losses) / len(losses) if losses else 0.04,
        }
    except Exception:
        return None

수정 후 pytest tests/test_datastore.py -v 실행.
get_trade_statistics 테스트도 추가해줘:
- 거래 없을 때 None 반환
- 매도 거래 3건(2승1패) 넣고 win_rate, avg_win, avg_loss 검증
```

---

### 프롬프트 A-2 — INFRA-2 (_evaluate_exit 전략별 분기)

```
CLAUDE.md를 읽어줘.

INFRA-2를 처리해줘. 가장 중요한 인프라 이슈야.

## 문제

_evaluate_exit()가 entry_strategy를 무시하고 모든 포지션에 MACD 데드크로스를 일괄 적용.
entry_strategy 필드는 DB에 기록되지만 청산 시 사용하지 않음.
백테스트는 전략별 exit 신호를 사용하므로 백테스트 ↔ 실전 불일치.

## 수정 방향

_evaluate_exit()의 청산 로직을 모든 전략에 공통으로 유지해.
이유: 새로 설계할 전략 3개(momentum_pullback, institutional_flow, disparity_reversion)는
각각 고유한 exit 조건을 가지는데, 그건 Phase B에서 구현해.
지금은 공통 청산 체계를 깔끔하게 정리하는 것이 목표.

## 수정 내용

1. _evaluate_exit()에 entry_strategy 기반 분기 골격 추가:

def _evaluate_exit(self, pos: Position, current_price: int) -> ExitReason | None:
    """종합 청산 판단."""
    from src.strategy.signals import calculate_indicators
    import pandas as pd

    max_hold = config.get("strategy.max_hold_days", 10)

    # 1. 손절가 이탈 (공통 — 모든 전략)
    if self._stop_mgr.is_stopped(pos, current_price):
        return ExitReason.STOP_LOSS

    # 2a. 부분 매도 (공통)
    partial_enabled = config.get("strategy.partial_sell_enabled", False)
    if (
        partial_enabled
        and not getattr(pos, "partial_sold", False)
        and pos.target_price > 0
    ):
        partial_pct = config.get("strategy.partial_target_pct", 0.5)
        target_return_val = (pos.target_price - pos.entry_price) / pos.entry_price
        partial_trigger = pos.entry_price * (1 + target_return_val * partial_pct)
        if current_price >= partial_trigger:
            return ExitReason.PARTIAL_TARGET

    # 2b. 목표가 도달 (공통)
    if pos.target_price > 0 and current_price >= pos.target_price:
        return ExitReason.TARGET_REACHED

    # 3. 전략별 exit 조건
    strategy_exit = self._check_strategy_exit(pos, current_price)
    if strategy_exit:
        return strategy_exit

    # 4. 최대 보유기간 초과 (공통)
    if pos.hold_days >= max_hold:
        return ExitReason.MAX_HOLD

    return None

2. 새 메서드 _check_strategy_exit() 추가:

def _check_strategy_exit(self, pos: Position, current_price: int) -> ExitReason | None:
    """전략별 고유 청산 조건 체크.

    entry_strategy에 따라 분기. 미등록 전략은 MACD 데드크로스 폴백.
    """
    from src.strategy.signals import calculate_indicators
    import pandas as pd

    strategy = pos.entry_strategy

    # 공통 MACD 데드크로스 (수익 +2% 이상)
    # 현재 모든 전략이 이걸 사용. Phase B에서 전략별 분기 추가 예정.
    pnl_pct = (current_price - pos.entry_price) / pos.entry_price
    if pnl_pct >= 0.02:
        try:
            from datetime import timedelta, datetime
            end = datetime.now().strftime("%Y-%m-%d")
            start = (datetime.now() - timedelta(days=40)).strftime("%Y-%m-%d")
            ohlcv = self._ds.get_cached_ohlcv(pos.code, start, end)
            if ohlcv and len(ohlcv) >= 30:
                df = pd.DataFrame(ohlcv)
                df = calculate_indicators(df)
                if len(df) >= 2:
                    prev_hist = df.iloc[-2].get("macd_hist", 0)
                    curr_hist = df.iloc[-1].get("macd_hist", 0)
                    if prev_hist > 0 and curr_hist < 0:
                        return ExitReason.MACD_DEAD
        except Exception:
            pass

    return None

3. 이 구조의 핵심:
   - 공통 청산(손절/목표가/부분매도/최대보유)은 _evaluate_exit에 유지
   - 전략별 고유 청산은 _check_strategy_exit로 분리
   - Phase B에서 새 전략 추가 시 _check_strategy_exit에 분기만 추가하면 됨
   - 현재는 모든 전략이 MACD 데드크로스 사용 (기존 동작 보존)

수정 후 pytest tests/test_engine.py -v 실행.
기존 테스트가 깨지면 안 됨 (동작 변경 없이 구조만 분리).
```

---

### 프롬프트 A-3 — Phase A 검증 + 커밋

```
Phase A 수정이 끝났으니 전체 검증해줘.

1. 전체 테스트:
   pytest tests/ -v --tb=short
   (test_engine의 test_screening_failure_sends_error는 기존 타임아웃 이슈 — 무시 가능)

2. StopManager fallback 확인:
   grep -n "config.get.*risk\.\|StopManager" src/engine.py | head -10
   → 모든 fallback이 config.yaml과 일치해야 함

3. Paper 안전장치 확인:
   grep -n "is_paper\|PAPER" src/broker/order_manager.py
   → is_paper 플래그와 시뮬레이션 로직 존재

4. get_trade_statistics 확인:
   grep -n "get_trade_statistics" src/datastore.py
   → 메서드 존재

5. _check_strategy_exit 확인:
   grep -n "_check_strategy_exit\|entry_strategy" src/engine.py
   → 분기 골격 존재

커밋:
git add -A
git commit -m "fix: 인프라 잔여 수정 — StopManager fallback, Paper 안전장치, 전략별 exit 골격

- INFRA-1: StopManager fallback 기본값을 config.yaml과 통일
- INFRA-2: _evaluate_exit에서 _check_strategy_exit 분리 (전략별 분기 골격)
- INFRA-3: Paper 모드 OrderManager 이중 안전장치
- INFRA-5: DataStore.get_trade_statistics() 구현"
```

---

## Phase B: 전략 재설계

### 프롬프트 B-1 — 기존 전략 비활성화 + 신규 전략 A (momentum_pullback)

```
CLAUDE.md를 읽어줘. 그리고 docs/SURGERY_GUIDE.md의 Phase 4 전략 재설계 섹션을 읽어줘.

전략 재설계를 시작해. 기존 전략을 삭제하지 않고 비활성화하고, 새 전략을 추가하는 방식이야.

## 1. 기존 전략 비활성화

src/strategy/__init__.py에서 기존 전략 import를 주석 처리:

# 기존 전략 (비활성 — v2 전략으로 교체됨)
# from src.strategy.golden_cross_strategy import GoldenCrossStrategy
# from src.strategy.macd_rsi_strategy import MacdRsiStrategy
# from src.strategy.bb_bounce_strategy import BbBounceStrategy
# from src.strategy.breakout_strategy import BreakoutStrategy
# from src.strategy.macd_pullback_strategy import MacdPullbackStrategy
# from src.strategy.stoch_reversal_strategy import StochReversalStrategy
# from src.strategy.volume_breakout_strategy import VolumeBreakoutStrategy

파일 자체는 삭제하지 마. 나중에 비교 참고용으로 남겨둬.

## 2. momentum_pullback 전략 구현

src/strategy/momentum_pullback_strategy.py 신규 생성:

"""모멘텀 + 눌림목 스윙 전략.

엣지: 모멘텀 프리미엄 — 최근 잘 간 종목이 계속 간다 (학술적 anomaly).
진입: 60일 모멘텀 상위 종목이 3~5일 눌림목 후 반등할 때.
청산: 목표가/손절/최대보유/모멘텀 이탈.

핵심 차이 (기존 macd_pullback 대비):
- MACD 크로스(후행 지표) 대신 가격 자체의 pullback(N일 하락)을 사용
- 모멘텀 필터를 스크리닝이 아닌 전략 진입 조건에 내장
"""

import numpy as np
import pandas as pd
from src.strategy.base_strategy import BaseStrategy, register_strategy
from src.strategy.signals import calculate_indicators


@register_strategy
class MomentumPullbackStrategy(BaseStrategy):
    """모멘텀 + 눌림목 스윙 전략."""

    name = "momentum_pullback"
    category = "trend"

    def check_screening_entry(self, df: pd.DataFrame) -> bool:
        """장전 스크리닝: 60일 모멘텀 양수 + 최근 눌림목 + 반등 시작."""
        momentum_period = self.params.get("momentum_period", 60)
        pullback_days = self.params.get("pullback_days", 3)
        rsi_pullback_threshold = self.params.get("rsi_pullback_threshold", 30)

        if len(df) < momentum_period + 5:
            return False
        latest = df.iloc[-1]

        # 1. 60일 모멘텀 양수 (상승 추세 종목)
        momentum = (latest["close"] - df.iloc[-momentum_period]["close"]) / df.iloc[-momentum_period]["close"]
        if momentum <= 0:
            return False

        # 2. 종가 > 20일선 (추세 유지)
        if latest["close"] <= latest.get("sma20", 0):
            return False

        # 3. 최근 N일 중 하락일이 과반 (눌림목 확인)
        recent = df.iloc[-pullback_days:]
        down_days = sum(1 for i in range(len(recent)) if recent.iloc[i]["close"] < recent.iloc[i]["open"])
        if down_days < pullback_days // 2 + 1:
            # 대안: RSI가 pullback 수준이면 통과
            if latest.get("rsi", 50) > rsi_pullback_threshold:
                return False

        # 4. 당일 양봉 (반등 시작)
        if latest["close"] <= latest["open"]:
            return False

        # 5. 거래량 확인
        volume_multiplier = self.params.get("volume_multiplier", 1.0)
        if latest["volume"] < latest.get("volume_sma20", 0) * volume_multiplier:
            return False

        return True

    def check_realtime_entry(
        self, df_daily: pd.DataFrame, df_60m: pd.DataFrame | None = None
    ) -> bool:
        """장중 진입: 눌림목 반등 확인 + 모멘텀 유지."""
        momentum_period = self.params.get("momentum_period", 60)
        pullback_days = self.params.get("pullback_days", 3)

        if len(df_daily) < momentum_period + 5:
            return False
        latest = df_daily.iloc[-1]

        # 1. 60일 모멘텀 양수
        past = df_daily.iloc[-momentum_period]
        momentum = (latest["close"] - past["close"]) / past["close"]
        if momentum <= 0:
            return False

        # 2. 종가 > 20일선
        if latest["close"] <= latest.get("sma20", 0):
            return False

        # 3. 최근 N일 눌림 후 당일 반등
        if len(df_daily) >= pullback_days + 1:
            pullback_start = df_daily.iloc[-(pullback_days + 1)]
            pullback_end = df_daily.iloc[-2]
            pullback_pct = (pullback_end["close"] - pullback_start["close"]) / pullback_start["close"]
            if pullback_pct > -0.02:
                return False  # 최소 2% 이상 눌림 필요

        # 4. 당일 양봉 + 전일 대비 반등
        if latest["close"] <= df_daily.iloc[-2]["close"]:
            return False

        # 5. 거래량 확인
        volume_multiplier = self.params.get("volume_multiplier", 1.0)
        if latest["volume"] < latest.get("volume_sma20", 0) * volume_multiplier:
            return False

        return True

    def generate_backtest_signals(
        self, df: pd.DataFrame
    ) -> tuple[pd.Series, pd.Series]:
        """백테스트 시그널: 모멘텀 + 눌림목 반등 entry / 모멘텀 이탈 exit."""
        p = self.params
        momentum_period = p.get("momentum_period", 60)
        pullback_days = p.get("pullback_days", 3)
        rsi_pullback = p.get("rsi_pullback_threshold", 30)
        volume_multiplier = p.get("volume_multiplier", 1.0)

        df_ind = calculate_indicators(df)
        if df_ind.empty:
            return pd.Series(dtype=bool), pd.Series(dtype=bool)

        # Entry 조건
        # 1. 60일 모멘텀 양수
        momentum = df_ind["close"].pct_change(momentum_period)
        cond_momentum = momentum > 0

        # 2. 종가 > SMA20
        cond_above_sma = df_ind["close"] > df_ind["sma20"]

        # 3. 최근 N일 하락 (pullback)
        rolling_return = df_ind["close"].pct_change(pullback_days)
        cond_pullback = rolling_return < -0.02  # 최소 2% 하락

        # 4. 당일 양봉 (반등)
        cond_bullish = df_ind["close"] > df_ind["open"]

        # 5. 5일 RSI 과매도 (대안 pullback 신호)
        rsi5 = df_ind["close"].rolling(5).apply(
            lambda x: 100 - 100 / (1 + (x.diff().clip(lower=0).mean() / (-x.diff().clip(upper=0).mean() + 1e-10)))
            if len(x) > 1 else 50
        )
        cond_rsi_oversold = rsi5 < rsi_pullback

        # 6. 거래량
        cond_vol = df_ind["volume"] >= df_ind["volume_sma20"] * volume_multiplier

        # 눌림목 OR RSI 과매도
        cond_pullback_or_rsi = cond_pullback | cond_rsi_oversold

        raw_entries = cond_momentum & cond_above_sma & cond_pullback_or_rsi & cond_bullish & cond_vol

        # Exit 조건
        # 1. 모멘텀 이탈: 60일 모멘텀 음전환
        cond_momentum_exit = momentum < 0
        # 2. 20일선 이탈
        cond_below_sma = df_ind["close"] < df_ind["sma20"]

        raw_exits = cond_momentum_exit | cond_below_sma

        # Look-ahead bias 방지
        entries = raw_entries.shift(1).fillna(False).astype(bool)
        exits = raw_exits.shift(1).fillna(False).astype(bool)
        entries.index = df_ind.index
        exits.index = df_ind.index

        return entries, exits

## 3. __init__.py에 신규 전략 등록

src/strategy/__init__.py에 추가:
from src.strategy.momentum_pullback_strategy import MomentumPullbackStrategy  # noqa: F401

## 4. config.yaml 전략 파라미터 추가 (strategy 섹션 하단)

  # 모멘텀 + 눌림목 전략 (momentum_pullback)
  momentum_period: 60           # 모멘텀 측정 기간 (거래일)
  pullback_days: 3              # 눌림목 확인 기간 (거래일)
  rsi_pullback_threshold: 30    # 눌림목 RSI 기준

수정 후 pytest tests/test_strategy.py -v 실행.
새 전략 관련 테스트도 최소 3개 추가:
- check_screening_entry: 모멘텀 양수 + 눌림 + 양봉 → True
- check_screening_entry: 모멘텀 음수 → False
- generate_backtest_signals: entries/exits가 boolean Series이고 look-ahead bias 없음 (shift 적용 확인)
```

---

### 프롬프트 B-2 — 신규 전략 B (institutional_flow)

```
CLAUDE.md를 읽어줘.

institutional_flow 전략을 구현해줘.

src/strategy/institutional_flow_strategy.py 신규 생성:

"""수급 기반 스윙 전략.

엣지: 외국인/기관의 정보 우위 — 대형 자금이 먼저 움직인다.
한국 시장에서 특히 강력. 정보 비대칭 + 대형 자금 분할 체결로 설명.

진입: 외국인/기관 연속 순매수 종목 + 추세 확인
청산: 외국인 순매도 전환 / 목표가 / 손절 / 최대보유
"""

핵심 로직:

class InstitutionalFlowStrategy(BaseStrategy):
    name = "institutional_flow"
    category = "trend"

    check_screening_entry:
      1. 종가 > 20일선 (추세 유지)
      2. ADX > params.get("adx_threshold", 20) (방향성)
      3. 거래량 > 20일 평균 * params.get("volume_multiplier", 1.0)
      4. 기관/외국인 수급 체크는 screener나 engine 레벨에서 하므로
         여기서는 기술적 조건만 체크. 수급은 signal_score에 반영.
         단, 이 전략의 check_screening_entry가 통과하려면
         외부에서 수급 필터를 거친 종목만 들어와야 함.
         → 스크리닝에서 수급 필터를 추가하는 것은 별도 작업 (프롬프트 B-4에서)

    check_realtime_entry:
      1. 종가 > 20일선
      2. ADX > threshold
      3. 당일 양봉
      4. 거래량 > 20일 평균

    generate_backtest_signals:
      Entry: 종가 > SMA20 + ADX > threshold + 양봉 + 거래량
      Exit: 종가 < SMA20 (추세 이탈) 또는 RSI > 70 (과열)

      주의: 백테스트에서 기관/외국인 순매수 데이터를 일별로 조회하면 너무 느려.
      백테스트용 entry에는 기술적 조건만 사용하고,
      "수급이 좋은 종목을 넣었다"는 가정으로 종목 선정 단계에서 필터.

## __init__.py에 추가

from src.strategy.institutional_flow_strategy import InstitutionalFlowStrategy  # noqa: F401

## _check_strategy_exit에 수급 exit 분기 추가

src/engine.py의 _check_strategy_exit()에 분기 추가:

if strategy == "institutional_flow":
    # 수급 이탈: 외국인 2일 연속 순매도 시 청산
    try:
        from src.strategy.signals import get_institutional_net_buying
        inst_net, foreign_net = get_institutional_net_buying(pos.code, days=2)
        if foreign_net < 0:
            return ExitReason.MACD_DEAD  # 기존 enum 재사용 (또는 새 enum 추가)
    except Exception:
        pass

ExitReason에 FLOW_EXIT = "flow_exit" 추가:
src/models.py의 ExitReason enum에:
FLOW_EXIT = "flow_exit"

수정 후 pytest tests/test_strategy.py -v 실행.
테스트 추가:
- check_screening_entry: ADX > threshold + 양봉 + 거래량 → True
- check_screening_entry: ADX < threshold → False
- generate_backtest_signals: entries/exits가 boolean Series
```

---

### 프롬프트 B-3 — 신규 전략 C (disparity_reversion)

```
CLAUDE.md를 읽어줘.

disparity_reversion 전략을 구현해줘.

src/strategy/disparity_reversion_strategy.py 신규 생성:

"""이격도 기반 평균회귀 전략.

엣지: 단기 과매도 반등 — 극단적 이탈은 평균으로 돌아온다.
bb_bounce 대체. 이격도(종가/SMA20)가 BB보다 직관적이고 파라미터 적음.

진입: 이격도 < 93% + RSI 과매도 + 양봉 반등
청산: 이격도 100% 복귀(20일선 터치) / 추가 하락 손절 / 최대보유 7일
"""

핵심 로직:

class DisparityReversionStrategy(BaseStrategy):
    name = "disparity_reversion"
    category = "mean_reversion"

    check_screening_entry:
      1. 이격도 계산: disparity = close / sma20 * 100
      2. disparity < params.get("disparity_entry", 93) — 7% 이상 이탈
      3. RSI(14) < params.get("rsi_oversold", 25)
      4. 60일 이동평균 상승 중: sma60 > sma60[5일전] (장기 추세 생존 확인)
      5. 당일 양봉 (바닥 확인)

    check_realtime_entry:
      1. 이격도 < 93%
      2. RSI < 30 (약간 완화)
      3. 당일 양봉
      4. 거래량 증가 (전일 대비)

    generate_backtest_signals:
      Entry: 이격도 < entry_threshold + RSI < oversold + 양봉 + SMA60 상승
      Exit: 이격도 >= 100 (20일선 복귀) 또는 이격도 < 88% (추가 하락)

## __init__.py에 추가

from src.strategy.disparity_reversion_strategy import DisparityReversionStrategy  # noqa: F401

## config.yaml 전략 파라미터 추가

  # 이격도 평균회귀 전략 (disparity_reversion)
  disparity_entry: 93           # 진입 이격도 (93% = 20일선 대비 -7%)
  disparity_exit: 100           # 청산 이격도 (100% = 20일선 터치)
  disparity_stop: 88            # 손절 이격도 (88% = 추가 -5% 하락)

## _check_strategy_exit에 이격도 exit 분기 추가

src/engine.py의 _check_strategy_exit()에 분기 추가:

if strategy == "disparity_reversion":
    try:
        from datetime import timedelta, datetime
        end = datetime.now().strftime("%Y-%m-%d")
        start = (datetime.now() - timedelta(days=40)).strftime("%Y-%m-%d")
        ohlcv = self._ds.get_cached_ohlcv(pos.code, start, end)
        if ohlcv and len(ohlcv) >= 20:
            df = pd.DataFrame(ohlcv)
            df = calculate_indicators(df)
            if not df.empty:
                latest = df.iloc[-1]
                sma20 = latest.get("sma20", 0)
                if sma20 > 0:
                    disparity = latest["close"] / sma20 * 100
                    # 이격도 100% 복귀 → 청산
                    if disparity >= config.get("strategy.disparity_exit", 100):
                        return ExitReason.TARGET_REACHED  # 목표 도달로 처리
                    # 이격도 추가 하락 → 손절
                    if disparity <= config.get("strategy.disparity_stop", 88):
                        return ExitReason.STOP_LOSS
    except Exception:
        pass

ExitReason에 DISPARITY_EXIT = "disparity_exit" 추가.

수정 후 pytest tests/test_strategy.py -v 실행.
테스트 추가:
- check_screening_entry: 이격도 < 93 + RSI < 25 + 양봉 → True
- check_screening_entry: 이격도 > 93 → False
- generate_backtest_signals: entries/exits 검증
```

---

### 프롬프트 B-4 — config.yaml 전략 매핑 교체 + regime_position_scale

```
CLAUDE.md를 읽어줘.

config.yaml의 전략 매핑을 신규 전략으로 교체하고,
국면별 포지션 스케일링을 구현해줘.

## 1. config.yaml 전략 매핑 교체

strategy 섹션의 regime_strategy를 수정:

  regime_strategy:
    trending:                          # 추세장
      - momentum_pullback              #   모멘텀 눌림목 반등
      - institutional_flow             #   수급 기반 진입
    sideways: disparity_reversion      # 횡보장 — 이격도 평균회귀
    # bearish: 매수 차단 (MarketRegime 게이트)

## 2. 국면별 포지션 스케일링 구현

config.yaml에 추가:

  # 국면별 포지션 사이즈 스케일링
  regime_position_scale:
    trending: 1.0                # 추세장: 100% 사이즈
    sideways: 0.5                # 횡보장: 50% 사이즈 (리스크 축소)
    bearish: 0.0                 # 약세장: 매수 차단 (기존 게이트와 동일)

src/engine.py의 _check_entry_conditions에서 포지션 사이징 직전에 스케일 적용:

# 기존 (547행 부근):
win_rate = config.get("risk.default_win_rate", 0.5)
avg_win = config.get("risk.default_avg_win", 0.08)
avg_loss = config.get("risk.default_avg_loss", 0.04)
invest_amount = self._sizer.calculate(
    capital=capital, win_rate=win_rate, avg_win=avg_win, avg_loss=avg_loss
)

# 수정:
win_rate = config.get("risk.default_win_rate", 0.5)
avg_win = config.get("risk.default_avg_win", 0.08)
avg_loss = config.get("risk.default_avg_loss", 0.04)
invest_amount = self._sizer.calculate(
    capital=capital, win_rate=win_rate, avg_win=avg_win, avg_loss=avg_loss
)

# 국면별 포지션 스케일링
regime = self._market_regime.regime_type
scale_map = config.get("strategy.regime_position_scale", {})
scale = scale_map.get(regime, 1.0)
invest_amount = int(invest_amount * scale)

## 3. 백테스트에도 동일 스케일링 적용

src/backtest/engine.py의 run_portfolio_backtest에서 진입 시:
position_budget 계산 직후에:

# 국면별 스케일링
regime_scale = {"trending": 1.0, "sideways": 0.5, "bearish": 0.0}
scale = regime_scale.get(current_regime, 1.0)
position_budget = position_budget * scale

## 4. 최대 보유일 전략별 차별화

disparity_reversion은 평균회귀라 보유기간을 짧게 가져야 해.
_check_strategy_exit에서 max_hold 오버라이드 추가:

if strategy == "disparity_reversion":
    disparity_max_hold = config.get("strategy.disparity_max_hold", 7)
    if pos.hold_days >= disparity_max_hold:
        return ExitReason.MAX_HOLD

수정 후:
1. pytest tests/ -v --tb=short
2. 기존 전략이 __init__.py에서 주석 처리되어 있으니,
   기존 전략을 참조하는 테스트가 실패할 수 있음 → 해당 테스트도 주석 처리하거나
   새 전략에 맞게 수정

커밋하지 마. Phase C에서 전체 검증 후 한 번에 커밋해.
```

---

### 프롬프트 B-5 — Phase B 전체 검증 + 커밋

```
Phase B 전략 재설계가 끝났으니 전체 검증해줘.

1. 전략 등록 확인:
   python -c "from src.strategy import available_strategies; print(available_strategies())"
   → ['momentum_pullback', 'institutional_flow', 'disparity_reversion'] 만 출력되어야 함
   (기존 전략은 __init__.py에서 주석 처리됨)

2. config.yaml 확인:
   grep -A 5 "regime_strategy:" config.yaml
   → trending: [momentum_pullback, institutional_flow], sideways: disparity_reversion

3. 전체 테스트:
   pytest tests/ -v --tb=short
   - 기존 전략 참조하는 테스트 실패는 정상 → 해당 테스트를 새 전략으로 업데이트하거나 스킵
   - 신규 전략 테스트는 전부 통과해야 함

4. 기존 전략 파일 존재 확인 (삭제 안 됨):
   ls src/strategy/golden_cross_strategy.py src/strategy/bb_bounce_strategy.py
   → 존재해야 함 (비활성이지만 참고용 보존)

5. 전략별 exit 분기 확인:
   grep -n "institutional_flow\|disparity_reversion\|momentum_pullback" src/engine.py
   → _check_strategy_exit에 분기 존재

6. regime_position_scale 확인:
   grep -n "regime_position_scale\|scale" src/engine.py src/backtest/engine.py | head -10

모든 확인 완료 후 커밋:
git add -A
git commit -m "feat: 전략 재설계 v2 — momentum_pullback, institutional_flow, disparity_reversion

Phase B 전략 재설계:
- 기존 5전략 비활성화 (코드 보존, import 주석 처리)
- momentum_pullback: 60일 모멘텀 상위 + 3~5일 눌림목 반등 진입
- institutional_flow: 수급 기반 + 추세 확인 진입, 외국인 순매도 시 청산
- disparity_reversion: 이격도 93% 미만 과매도 반등, 100% 복귀 청산
- 국면별 포지션 스케일링 (trending 100% / sideways 50%)
- _check_strategy_exit 전략별 분기 구현
- ExitReason에 FLOW_EXIT, DISPARITY_EXIT 추가"

git push
```

---

### 프롬프트 B-6 — CLAUDE.md + SURGERY_GUIDE 최종 갱신

```
수술과 전략 재설계가 완료됐으니 문서를 최종 갱신해줘.

1. 루트 CLAUDE.md:
   - 수술 상태 표에서 INFRA-1~5 항목 추가하고 "완료"로 표시
   - Phase 4 전략 재설계 테이블을 "완료"로 갱신
   - 핵심 아키텍처 다이어그램의 Strategy 부분을 새 3개 전략으로 교체
   - 비용 모델 섹션 아래에 "전략 구조" 요약 추가:
     trending: momentum_pullback + institutional_flow
     sideways: disparity_reversion
     bearish: 매수 차단
     포지션 스케일: trending 100% / sideways 50%

2. docs/CLAUDE.md:
   - "알려진 이슈 & 주의사항"에 추가:
     - 기존 전략 7개는 src/strategy/에 파일 존재하지만 __init__.py에서 비활성.
       재활성화하려면 import 주석 해제 + config.yaml regime_strategy 수정.
     - institutional_flow 전략의 수급 exit(외국인 순매도)은 pykrx API 호출.
       API 장애 시 graceful하게 스킵 (기존 MACD 데드크로스 폴백).

커밋:
git add -A
git commit -m "docs: 전략 재설계 완료 반영 — CLAUDE.md, 전략 구조 갱신"
git push
```

---

## 최종 확인 체크리스트

- [ ] `python -c "from src.strategy import available_strategies; print(available_strategies())"` → 3개 전략
- [ ] `pytest tests/ -v` → 핵심 테스트 전부 통과
- [ ] `grep -rn "regime_position_scale" config.yaml src/engine.py` → 스케일링 존재
- [ ] `grep -n "_check_strategy_exit" src/engine.py` → 전략별 분기 존재
- [ ] `grep -n "get_trade_statistics" src/datastore.py` → 메서드 존재
- [ ] `grep -n "is_paper" src/broker/order_manager.py` → Paper 안전장치 존재
- [ ] `ls src/strategy/golden_cross_strategy.py` → 기존 파일 보존됨
- [ ] `cat CLAUDE.md` → 전략 재설계 완료 상태
