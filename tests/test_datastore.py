"""DataStore CRUD 테스트."""

from src.datastore import DataStore
from src.models import Position, TradeRecord


class TestPositionCRUD:
    def test_insert_and_get_position(self, tmp_db: DataStore):
        """포지션 삽입 및 조회."""
        pos = Position(
            id=0,
            code="005930",
            name="삼성전자",
            entry_date="2025-01-15",
            entry_price=70000,
            quantity=10,
            stop_price=65000,
            target_price=75000,
            updated_at="2025-01-15 09:30:00",
        )
        row_id = tmp_db.insert_position(pos)
        assert row_id >= 1

        positions = tmp_db.get_open_positions()
        assert len(positions) == 1
        assert positions[0]["code"] == "005930"
        assert positions[0]["entry_price"] == 70000

    def test_update_position(self, tmp_db: DataStore):
        """포지션 업데이트."""
        pos = Position(
            id=0,
            code="005930",
            name="삼성전자",
            entry_date="2025-01-15",
            entry_price=70000,
            quantity=10,
            stop_price=65000,
            target_price=75000,
        )
        row_id = tmp_db.insert_position(pos)
        tmp_db.update_position(row_id, status="closed", updated_at="2025-01-16")

        positions = tmp_db.get_open_positions()
        assert len(positions) == 0

    def test_count_open_positions(self, tmp_db: DataStore):
        """열린 포지션 수 카운트."""
        assert tmp_db.count_open_positions() == 0

        for code in ["005930", "000660", "035720"]:
            pos = Position(
                id=0,
                code=code,
                name="테스트",
                entry_date="2025-01-15",
                entry_price=50000,
                quantity=5,
                stop_price=45000,
                target_price=55000,
            )
            tmp_db.insert_position(pos)

        assert tmp_db.count_open_positions() == 3

    def test_update_with_no_kwargs(self, tmp_db: DataStore):
        """kwargs 없이 update 호출 시 아무 일도 일어나지 않음."""
        pos = Position(
            id=0,
            code="005930",
            name="삼성전자",
            entry_date="2025-01-15",
            entry_price=70000,
            quantity=10,
            stop_price=65000,
            target_price=75000,
        )
        row_id = tmp_db.insert_position(pos)
        tmp_db.update_position(row_id)  # 아무 변경 없음
        positions = tmp_db.get_open_positions()
        assert len(positions) == 1


class TestTradeCRUD:
    def test_record_and_get_trade(self, tmp_db: DataStore):
        """매매 기록 삽입 및 조회."""
        trade = TradeRecord(
            code="005930",
            name="삼성전자",
            side="buy",
            price=70000,
            quantity=10,
            amount=700000,
            fee=105.0,
            tax=0.0,
            pnl=0.0,
            pnl_pct=0.0,
            reason="signal",
            executed_at="2025-01-15 09:30:00",
        )
        row_id = tmp_db.record_trade(trade)
        assert row_id >= 1

        last = tmp_db.get_last_trade("005930")
        assert last is not None
        assert last["price"] == 70000
        assert last["side"] == "buy"

    def test_get_last_trade_no_data(self, tmp_db: DataStore):
        """기록 없는 종목 조회 시 None 반환."""
        assert tmp_db.get_last_trade("999999") is None

    def test_get_trades_by_date(self, tmp_db: DataStore):
        """날짜별 매매 기록 조회."""
        for i, ts in enumerate(
            ["2025-01-15 09:30:00", "2025-01-15 10:00:00", "2025-01-16 09:30:00"]
        ):
            trade = TradeRecord(
                code=f"00{i}",
                name="테스트",
                side="buy",
                price=10000,
                quantity=1,
                amount=10000,
                fee=15.0,
                tax=0.0,
                pnl=0.0,
                pnl_pct=0.0,
                reason="test",
                executed_at=ts,
            )
            tmp_db.record_trade(trade)

        trades_15 = tmp_db.get_trades_by_date("2025-01-15")
        assert len(trades_15) == 2

        trades_16 = tmp_db.get_trades_by_date("2025-01-16")
        assert len(trades_16) == 1


class TestDailyPerformance:
    def test_save_and_get_performance(self, tmp_db: DataStore):
        """일일 성과 저장 및 조회."""
        tmp_db.save_daily_performance(
            date="2025-01-15",
            realized_pnl=50000.0,
            unrealized_pnl=-10000.0,
            total_capital=10_050_000.0,
            daily_return=0.005,
            mdd_current=-0.01,
            trade_count=3,
        )

        perf = tmp_db.get_daily_performance("2025-01-15")
        assert perf is not None
        assert perf["realized_pnl"] == 50000.0
        assert perf["trade_count"] == 3

    def test_get_missing_performance(self, tmp_db: DataStore):
        """없는 날짜 조회 시 None."""
        assert tmp_db.get_daily_performance("2099-01-01") is None

    def test_upsert_performance(self, tmp_db: DataStore):
        """같은 날짜로 다시 저장하면 덮어쓰기."""
        tmp_db.save_daily_performance(
            date="2025-01-15",
            realized_pnl=50000.0,
            unrealized_pnl=0.0,
            total_capital=10_050_000.0,
            daily_return=0.005,
            mdd_current=-0.01,
            trade_count=3,
        )
        tmp_db.save_daily_performance(
            date="2025-01-15",
            realized_pnl=100000.0,
            unrealized_pnl=0.0,
            total_capital=10_100_000.0,
            daily_return=0.01,
            mdd_current=-0.005,
            trade_count=5,
        )
        perf = tmp_db.get_daily_performance("2025-01-15")
        assert perf["realized_pnl"] == 100000.0
        assert perf["trade_count"] == 5


class TestOHLCVCache:
    def test_cache_and_get_ohlcv(self, tmp_db: DataStore):
        """OHLCV 캐시 저장 및 조회."""
        records = [
            {
                "date": "2025-01-13",
                "open": 69000,
                "high": 71000,
                "low": 68500,
                "close": 70000,
                "volume": 1000000,
                "amount": 70000000000,
            },
            {
                "date": "2025-01-14",
                "open": 70000,
                "high": 72000,
                "low": 69500,
                "close": 71000,
                "volume": 1200000,
                "amount": 85000000000,
            },
        ]
        tmp_db.cache_ohlcv("005930", records)

        cached = tmp_db.get_cached_ohlcv("005930", "2025-01-13", "2025-01-14")
        assert len(cached) == 2
        assert cached[0]["close"] == 70000
        assert cached[1]["close"] == 71000

    def test_cache_date_range_filter(self, tmp_db: DataStore):
        """날짜 범위 필터링."""
        records = [
            {"date": f"2025-01-{d}", "open": 100, "high": 110, "low": 90, "close": 105, "volume": 100, "amount": 10000}
            for d in range(10, 16)
        ]
        tmp_db.cache_ohlcv("005930", records)

        cached = tmp_db.get_cached_ohlcv("005930", "2025-01-12", "2025-01-14")
        assert len(cached) == 3  # 12, 13, 14
