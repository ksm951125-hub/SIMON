import pytest

from detector import calculate_change_pct, is_drop


@pytest.mark.parametrize(
    ("previous_close", "close", "expected", "detected"),
    [
        (100, 89, -11.0, True),
        (100, 91, -9.0, False),
        (100, 90, -10.0, True),
    ],
)
def test_drop_boundary(previous_close, close, expected, detected):
    change = calculate_change_pct(previous_close, close)
    assert change == pytest.approx(expected)
    assert is_drop(change) is detected


def test_invalid_close_rejected():
    with pytest.raises(ValueError):
        calculate_change_pct(0, 90)
