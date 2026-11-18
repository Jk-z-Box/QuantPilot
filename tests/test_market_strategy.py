import pytest

from quantpilot.market import decimal
from quantpilot.strategy import ema, rsi


def test_flat_shared_indicators():
    assert ema([100] * 60, 20) == 100
    assert rsi([100] * 60) == 50
    assert rsi(list(range(1, 100))) == 100


@pytest.mark.parametrize("value", ["nan", "inf", "-inf", "invalid"])
def test_shared_decimal_rejects_invalid(value):
    with pytest.raises(ValueError):
        decimal(value)
