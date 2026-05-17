"""브로커 잔고 ↔ DB 포지션 정합성 검사 단위 테스트."""

import pytest

from src.broker.reconciler import ReconcileResult, reconcile


def _db(*codes_qtys: tuple[str, int]) -> list[dict]:
    return [{"code": c, "qty": q} for c, q in codes_qtys]


def _broker(*codes_qtys: tuple[str, int]) -> list[dict]:
    return [{"code": c, "qty": q} for c, q in codes_qtys]


class TestReconcile:
    def test_all_matched(self):
        result = reconcile(
            _db(("005930", 100), ("000660", 50)),
            _broker(("005930", 100), ("000660", 50)),
        )
        assert result.is_clean
        assert sorted(result.matched) == ["000660", "005930"]
        assert result.db_only == []
        assert result.broker_only == []
        assert result.qty_mismatch == []

    def test_db_only(self):
        """DB에만 있고 브로커에 없는 종목 → 실제로는 청산됐을 가능성."""
        result = reconcile(
            _db(("005930", 100), ("000660", 50)),
            _broker(("005930", 100)),
        )
        assert not result.is_clean
        assert result.db_only == [{"code": "000660", "db_qty": 50}]
        assert result.broker_only == []

    def test_broker_only(self):
        """브로커에만 있고 DB에 없는 종목 → 미기록 포지션."""
        result = reconcile(
            _db(("005930", 100)),
            _broker(("005930", 100), ("035720", 30)),
        )
        assert not result.is_clean
        assert result.broker_only == [{"code": "035720", "broker_qty": 30}]
        assert result.db_only == []

    def test_qty_mismatch(self):
        """수량이 서로 다른 경우."""
        result = reconcile(
            _db(("005930", 100)),
            _broker(("005930", 80)),
        )
        assert not result.is_clean
        assert result.qty_mismatch == [
            {"code": "005930", "db_qty": 100, "broker_qty": 80}
        ]
        assert result.matched == []

    def test_mixed_discrepancies(self):
        """여러 불일치가 동시에 발생하는 경우."""
        result = reconcile(
            _db(("005930", 100), ("000660", 50), ("035720", 20)),
            _broker(("005930", 90), ("051910", 10)),
        )
        assert not result.is_clean
        # 005930: qty 불일치
        assert any(m["code"] == "005930" for m in result.qty_mismatch)
        # 000660, 035720: DB에만
        db_only_codes = [d["code"] for d in result.db_only]
        assert "000660" in db_only_codes
        assert "035720" in db_only_codes
        # 051910: 브로커에만
        assert any(b["code"] == "051910" for b in result.broker_only)

    def test_empty_both(self):
        result = reconcile([], [])
        assert result.is_clean
        assert result.matched == []

    def test_empty_db(self):
        """DB 비어 있고 브로커에 잔고 → broker_only."""
        result = reconcile([], _broker(("005930", 100)))
        assert not result.is_clean
        assert len(result.broker_only) == 1

    def test_empty_broker(self):
        """브로커 비어 있고 DB에 포지션 → db_only."""
        result = reconcile(_db(("005930", 100)), [])
        assert not result.is_clean
        assert len(result.db_only) == 1

    def test_broker_qty_zero_excluded(self):
        """브로커 응답에서 수량=0인 항목은 보유로 보지 않음."""
        result = reconcile(
            _db(("005930", 100)),
            [{"code": "005930", "qty": 100}, {"code": "000660", "qty": 0}],
        )
        assert result.is_clean
        assert result.matched == ["005930"]

    def test_summary_clean(self):
        result = reconcile(
            _db(("005930", 100)),
            _broker(("005930", 100)),
        )
        assert "✅" in result.summary
        assert "1종목" in result.summary

    def test_summary_dirty(self):
        result = reconcile(
            _db(("005930", 100), ("000660", 50)),
            _broker(("005930", 80)),
        )
        assert "⚠️" in result.summary
        assert "000660" in result.summary
        assert "005930" in result.summary

    def test_custom_key_names(self):
        """code_key_*, qty_key_* 커스텀 매핑."""
        db_pos = [{"ticker": "005930", "shares": 100}]
        br_pos = [{"symbol": "005930", "amount": 100}]
        result = reconcile(
            db_pos, br_pos,
            code_key_db="ticker", qty_key_db="shares",
            code_key_broker="symbol", qty_key_broker="amount",
        )
        assert result.is_clean
        assert result.matched == ["005930"]

    def test_code_stripped(self):
        """종목코드 앞뒤 공백 자동 제거."""
        result = reconcile(
            [{"code": " 005930 ", "qty": 100}],
            [{"code": "005930", "qty": 100}],
        )
        assert result.is_clean
