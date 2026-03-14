# TELEGRAM_SPEC.md — 텔레그램 알림 명세

## 1. 봇 설정

```python
# src/notification/telegram_bot.py
import requests

class TelegramBot:
    def __init__(self, token: str, chat_id: str):
        self._token   = token
        self._chat_id = chat_id
        self._base    = f"https://api.telegram.org/bot{token}"

    def send(self, message: str, parse_mode: str = "HTML") -> bool:
        """동기 직접 호출 (python-telegram-bot 라이브러리 사용 안 함)"""
        try:
            resp = requests.post(
                f"{self._base}/sendMessage",
                json={
                    "chat_id":    self._chat_id,
                    "text":       message,
                    "parse_mode": parse_mode,
                },
                timeout=5
            )
            return resp.status_code == 200
        except Exception as e:
            logger.error(f"텔레그램 전송 실패: {e}")
            return False
```

---

## 2. 알림 메시지 템플릿

### 매수 신호 발생
```
📊 <b>매수 신호</b>
종목: 삼성전자 (005930)
현재가: 74,500원
신호 강도: ★★★★☆

━━ 지표 ━━
RSI: 52.3
MACD 히스토그램: +0.82 (양전환)
거래량: 평균 대비 1.8배
20일선 대비: +2.1%

━━ 진입 계획 ━━
목표가: 80,460원 (+8.0%)
손절가: 72,200원 (-3.1%)
손익비: 2.6
```

### 매수 체결
```
✅ <b>매수 체결</b>
종목: 삼성전자 (005930)
체결가: 74,600원
수량: 13주
투자금: 969,800원 (자본의 9.7%)

손절가: 72,200원 (-3.2%)
목표가: 80,568원 (+8.0%)
```

### 매도 체결 — 수익
```
💰 <b>매도 체결 (목표 달성)</b>
종목: 삼성전자 (005930)
체결가: 80,700원
보유: 8일

수익: +80,600원 (+8.2%)
세후 수익: +76,800원 (+7.8%)
```

### 매도 체결 — 손실
```
🔴 <b>매도 체결 (손절)</b>
종목: 카카오 (035720)
체결가: 42,100원
보유: 3일

손실: -28,500원 (-3.2%)
사유: 손절가 이탈
```

### 일일 손실 경고
```
⚠️ <b>일일 손실 경고</b>
현재 일일 손익: -2.1%
한도: -3.0%
남은 여유: 0.9%p

보유 포지션 확인 권장
```

### 매매 중단 (Halt)
```
🛑 <b>매매 중단</b>
일일 손실 한도(-3%) 초과
현재 손익: -3.2%

당일 신규 주문 중단됨
내일 09:00 자동 재개
```

### 일간 리포트 (16:00)
```
📈 <b>일간 리포트</b> — 2024-11-15

━━ 당일 매매 ━━
매수: 2건 | 매도: 1건
실현 손익: +45,200원 (+0.45%)

━━ 포지션 현황 ━━
보유 종목: 3종목
평가 손익: +128,000원

━━ 누적 성과 ━━
기준자본: 10,000,000원
현재자본: 10,523,400원
누적 수익률: +5.23%
현재 MDD: -3.8%
```

### 시스템 오류
```
🚨 <b>시스템 오류</b>
오류: ConnectionError
위치: kiwoom_api.py:142

자동 재연결 시도 중... (1/5)
```

---

## 3. 알림 우선순위 및 쿨다운

| 알림 종류 | 우선순위 | 쿨다운 |
|---------|---------|--------|
| 시스템 오류 | 긴급 | 없음 |
| 매매 중단 | 긴급 | 없음 |
| 손절 발동 | 높음 | 없음 |
| 체결 알림 | 높음 | 없음 |
| 일일 손실 경고 | 보통 | 1회/일 |
| 매수 신호 | 보통 | 동일 종목 1시간 |
| 일간 리포트 | 낮음 | 1회/일 |

```python
class TelegramBot:
    def __init__(self, ...):
        self._cooldowns: dict[str, datetime] = {}

    def send_with_cooldown(self, key: str, message: str, cooldown_sec: int):
        """동일 키 메시지 쿨다운 적용"""
        now = datetime.now()
        if key in self._cooldowns:
            if (now - self._cooldowns[key]).seconds < cooldown_sec:
                return  # 쿨다운 중 — 무시
        self._cooldowns[key] = now
        self.send(message)
```

---

## 4. 환경 변수

```env
# .env
TELEGRAM_TOKEN=1234567890:ABCDE-xxxxxxxxxxxxxxxxxxxxxxxxxxx
TELEGRAM_CHAT_ID=987654321
```

```python
# 사용
from dotenv import load_dotenv
import os

load_dotenv()
bot = TelegramBot(
    token   = os.getenv('TELEGRAM_TOKEN'),
    chat_id = os.getenv('TELEGRAM_CHAT_ID')
)
```
