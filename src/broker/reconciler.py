"""브로커 잔고 ↔ DB 포지션 정합성 검사.

순수 함수 설계 — 외부 I/O 없음. 입력 dict 리스트를 비교해 ReconcileResult 반환.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ReconcileResult:
    """정합성 검사 결과."""

    matched: list[str] = field(default_factory=list)       # 일치 종목코드
    db_only: list[dict] = field(default_factory=list)      # DB에만 있음 (브로커 유실)
    broker_only: list[dict] = field(default_factory=list)  # 브로커에만 있음 (DB 미기록)
    qty_mismatch: list[dict] = field(default_factory=list) # 수량 불일치
    is_clean: bool = True                                  # True = 불일치 없음

    @property
    def summary(self) -> str:
        if self.is_clean:
            return f"✅ 정합성 OK — {len(self.matched)}종목 일치"
        parts = []
        if self.db_only:
            codes = [d["code"] for d in self.db_only]
            parts.append(f"DB에만 존재: {codes}")
        if self.broker_only:
            codes = [d["code"] for d in self.broker_only]
            parts.append(f"브로커에만 존재: {codes}")
        for m in self.qty_mismatch:
            parts.append(f"{m['code']}: DB={m['db_qty']}주 vs 브로커={m['broker_qty']}주")
        return "⚠️ 정합성 불일치 — " + " | ".join(parts)


def reconcile(
    db_positions: list[dict],
    broker_holdings: list[dict],
    code_key_db: str = "code",
    qty_key_db: str = "qty",
    code_key_broker: str = "code",
    qty_key_broker: str = "qty",
) -> ReconcileResult:
    """DB 포지션과 브로커 잔고를 비교한다.

    Args:
        db_positions:    DB에서 조회한 open 포지션 목록. 각 dict에 code/qty 포함.
        broker_holdings: 브로커 API에서 조회한 보유 종목 목록. 각 dict에 code/qty 포함.
        code_key_db:     db_positions dict에서 종목코드 키 이름.
        qty_key_db:      db_positions dict에서 수량 키 이름.
        code_key_broker: broker_holdings dict에서 종목코드 키 이름.
        qty_key_broker:  broker_holdings dict에서 수량 키 이름.

    Returns:
        ReconcileResult — 불일치 항목 분류 + is_clean 플래그.
    """
    result = ReconcileResult()

    db_map: dict[str, int] = {}
    for p in db_positions:
        code = str(p.get(code_key_db, "")).strip()
        qty = int(p.get(qty_key_db, 0))
        if code:
            db_map[code] = qty

    broker_map: dict[str, int] = {}
    for h in broker_holdings:
        code = str(h.get(code_key_broker, "")).strip()
        qty = int(h.get(qty_key_broker, 0))
        if code and qty > 0:
            broker_map[code] = qty

    all_codes = set(db_map.keys()) | set(broker_map.keys())

    for code in sorted(all_codes):
        db_qty = db_map.get(code)
        br_qty = broker_map.get(code)

        if db_qty is not None and br_qty is None:
            result.db_only.append({"code": code, "db_qty": db_qty})
            result.is_clean = False
        elif db_qty is None and br_qty is not None:
            result.broker_only.append({"code": code, "broker_qty": br_qty})
            result.is_clean = False
        elif db_qty != br_qty:
            result.qty_mismatch.append(
                {"code": code, "db_qty": db_qty, "broker_qty": br_qty}
            )
            result.is_clean = False
        else:
            result.matched.append(code)

    return result
