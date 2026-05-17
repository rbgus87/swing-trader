"""호가단위 보정 유틸 테스트."""

import pytest

from src.utils.tick_size import adjust_price, get_tick_size


class TestGetTickSize:
    @pytest.mark.parametrize(
        "price,expected",
        [
            (500, 1),
            (999, 1),
            (1000, 5),
            (4999, 5),
            (5000, 10),
            (9999, 10),
            (10000, 50),
            (49999, 50),
            (50000, 100),
            (99999, 100),
            (100000, 500),
            (499999, 500),
            (500000, 1000),
            (1000000, 1000),
        ],
    )
    def test_tick_size_boundaries(self, price, expected):
        assert get_tick_size(price) == expected


class TestAdjustPrice:
    @pytest.mark.parametrize(
        "price,direction,expected",
        [
            # 10,000~50,000 구간: tick=50
            (12345, "down", 12300),
            (12345, "up", 12350),
            (12350, "down", 12350),
            (12350, "up", 12350),
            # 50,000~100,000 구간: tick=100
            (67890, "down", 67800),
            (67890, "up", 67900),
            # 100,000~500,000 구간: tick=500
            (123456, "down", 123000),
            (123456, "up", 123500),
            # 경계값
            (0, "down", 0),
            (50000, "down", 50000),
            (50000, "up", 50000),
            # 500원 미만: tick=1
            (500, "down", 500),
            (999, "up", 999),
            # 1,000~5,000: tick=5
            (1003, "down", 1000),
            (1003, "up", 1005),
        ],
    )
    def test_adjust_price(self, price, direction, expected):
        assert adjust_price(price, direction) == expected

    def test_float_input(self):
        """float 입력도 int 변환 후 보정."""
        assert adjust_price(12345.67, "down") == 12300
        assert adjust_price(12345.67, "up") == 12350

    def test_negative_returns_zero(self):
        assert adjust_price(-100, "down") == 0

    def test_default_direction_is_down(self):
        """방향 미지정 시 기본값 down."""
        assert adjust_price(12345) == 12300
