# RISK_SPEC.md — 리스크 관리 명세

## 1. 리스크 계층 구조

```
주문 실행 요청
      │
      ▼
RiskManager.pre_check()    ← 1차: 주문 전 사전 체크
      │
  통과 시
      │
      ▼
PositionSizer.calculate()  ← 2차: 투자 금액 산정
      │
      ▼
OrderManager.execute()     ← 3차: 실제 주문
      │
      ▼
StopManager.update()       ← 4차: 손절가/트레일링스탑 관리
      │
      ▼ (실시간 모니터링)
DailyLimitChecker.check()  ← 5차: 일일 한도 감시
```

---

## 2. 포지션 사이징 (PositionSizer)

### 하프켈리 공식

```python
def calculate(
    capital: float,
    win_rate: float,      # 백테스트 또는 최근 30회 실거래 기준
    avg_win: float,       # 평균 수익률
    avg_loss: float,      # 평균 손실률 (양수 입력)
    method: str = 'half_kelly'
) -> float:
    """
    반환값: 투자 비율 (0.0 ~ MAX_POSITION_RATIO)
    """
    if avg_loss == 0:
        return MIN_POSITION_RATIO

    b = avg_win / avg_loss    # 손익비
    q = 1 - win_rate

    kelly = (win_rate * b - q) / b

    if kelly <= 0:
        # 기댓값 음수 → 최소 비율 또는 0
        return 0.0

    if method == 'full_kelly':
        ratio = kelly
    elif method == 'half_kelly':
        ratio = kelly * 0.5
    elif method == 'quarter_kelly':
        ratio = kelly * 0.25
    else:
        ratio = FIXED_RATIO

    # 상한/하한 적용
    ratio = max(MIN_POSITION_RATIO, min(ratio, MAX_POSITION_RATIO))

    # 금액으로 변환
    invest_amount = capital * ratio
    return invest_amount

# 상수 (config.yaml에서 로드)
MAX_POSITION_RATIO = 0.15   # 종목당 최대 15%
MIN_POSITION_RATIO = 0.03   # 종목당 최소 3%
FIXED_RATIO        = 0.05   # 고정 비율 모드
```

### 신규 전략 초기값 (실거래 데이터 부족 시)
백테스트 결과의 승률/손익비를 초기값으로 사용. 실거래 30회 이후부터 실제 데이터로 갱신.

---

## 3. 손절 & 트레일링스탑 (StopManager)

```python
class StopManager:

    def get_initial_stop(self, entry_price: int, atr: float) -> int:
        """
        초기 손절가 = 진입가 - ATR × 1.5
        최소 손실: -3%, 최대 손실: -7% (ATR 과대 시 상한 적용)
        """
        atr_stop   = entry_price - int(atr * STOP_ATR_MULT)  # 1.5
        pct_stop   = int(entry_price * (1 - MAX_STOP_PCT))   # 0.07
        return max(atr_stop, pct_stop)   # 둘 중 높은 값 (더 타이트한 쪽)

    def update_trailing_stop(
        self,
        position: Position,
        current_price: int,
        atr: float
    ) -> int:
        """
        트레일링스탑 = 기록된 고점 - ATR × 2.0
        수익이 ATR × 1.5 이상 발생한 후에만 활성화 (너무 이른 발동 방지)
        """
        high_since_entry = max(position.high_since_entry, current_price)
        trailing = int(high_since_entry - atr * TRAILING_ATR_MULT)  # 2.0

        # 초기 손절가보다 낮아지면 안 됨 (후퇴 금지)
        trailing = max(trailing, position.stop_price)

        # 활성화 조건 체크
        unrealized_pct = (current_price - position.entry_price) / position.entry_price
        if unrealized_pct < TRAILING_ACTIVATE_PCT:  # 기본 0.03 (3%)
            return position.stop_price              # 아직 초기 손절가 유지

        return trailing
```

---

## 4. 사전 리스크 체크 (RiskManager.pre_check)

```python
@dataclass
class RiskCheckResult:
    approved: bool
    reason: str = ""

def pre_check(self, signal: Signal) -> RiskCheckResult:
    """주문 전 모든 리스크 조건 체크"""

    # 1. 일일 손실 한도 체크
    if self.daily_pnl_pct <= DAILY_LOSS_LIMIT:  # -3%
        return RiskCheckResult(False, "일일 손실 한도 초과")

    # 2. 최대 동시 보유 종목 수
    open_count = self.datastore.count_open_positions()
    if open_count >= MAX_POSITIONS:  # 5
        return RiskCheckResult(False, f"최대 보유 종목 수 초과 ({MAX_POSITIONS})")

    # 3. 동일 종목 재진입 쿨다운
    last_trade = self.datastore.get_last_trade(signal.code)
    if last_trade:
        days_since = (date.today() - last_trade.date).days
        if days_since < REENTRY_COOLDOWN:  # 3 영업일
            return RiskCheckResult(False, f"재진입 쿨다운 중 ({days_since}일 경과)")

    # 4. 장 시간 체크
    if not is_market_open():
        return RiskCheckResult(False, "장 시간 외")

    # 5. MDD 체크
    if self.current_mdd <= MAX_MDD:  # -20%
        return RiskCheckResult(False, "최대 낙폭 초과 — 전략 중단 필요")

    return RiskCheckResult(True)
```

---

## 5. 일일 손실 한도 모니터링

```python
class DailyLimitChecker:
    def on_price_update(self, tick: Tick):
        """실시간 시세 수신 시마다 호출"""
        unrealized = self._calc_unrealized_pnl()
        total_daily_pnl = self.realized_pnl + unrealized

        daily_pct = total_daily_pnl / self.capital_start_of_day

        if daily_pct <= WARNING_THRESHOLD:   # -2%
            if not self._warned:
                self.telegram.send("⚠️ 일일 손실 경고: -2% 도달")
                self._warned = True

        if daily_pct <= HALT_THRESHOLD:      # -3%
            self.engine.halt()               # 매매 중단
            self.telegram.send("🛑 일일 손실 한도 도달: 매매 중단")
```

---

## 6. 리스크 파라미터 요약

| 파라미터 | 기본값 | 설명 |
|---------|--------|------|
| `MAX_POSITION_RATIO` | 0.15 | 종목당 최대 자본 비율 |
| `MIN_POSITION_RATIO` | 0.03 | 종목당 최소 자본 비율 |
| `MAX_POSITIONS` | 5 | 최대 동시 보유 종목 수 |
| `STOP_ATR_MULT` | 1.5 | 초기 손절 ATR 배수 |
| `MAX_STOP_PCT` | 0.07 | 최대 손절 비율 (7%) |
| `TRAILING_ATR_MULT` | 2.0 | 트레일링스탑 ATR 배수 |
| `TRAILING_ACTIVATE_PCT` | 0.03 | 트레일링 활성화 수익률 |
| `DAILY_LOSS_LIMIT` | -0.03 | 일일 손실 한도 |
| `DAILY_LOSS_WARNING` | -0.02 | 일일 손실 경고 임계값 |
| `MAX_MDD` | -0.20 | 전략 중단 MDD |
| `REENTRY_COOLDOWN` | 3 | 동일 종목 재진입 쿨다운 (영업일) |
| `MAX_HOLD_DAYS` | 15 | 최대 보유 기간 (영업일) |
