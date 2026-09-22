"""The backtest and the live engine must pick the SAME strikes.

The reference implementation rounded to the nearest 50 in its backtest but
used ceil/floor live, so every backtest number described a different — and
more aggressive — strategy than the one that actually traded. This pins the
two implementations together.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.backtest import round_ce, round_pe  # noqa: E402


def engine_strikes(prev_high: float, prev_low: float) -> tuple[int, int]:
    """Mirrors StrategyEngine.planned_strikes()."""
    return (int(math.ceil(prev_high / 50.0) * 50), int(math.floor(prev_low / 50.0) * 50))


@pytest.mark.parametrize(
    "prev_high,prev_low",
    [
        (23389.15, 23286.60),   # real values from a live session
        (24520.00, 24310.00),   # the case that exposed the reference bug
        (24500.00, 24500.00),   # exactly on a strike boundary
        (23000.10, 22999.90),   # either side of a boundary
        (25049.99, 24950.01),
    ],
)
def test_backtest_and_engine_agree(prev_high, prev_low):
    assert (round_ce(prev_high), round_pe(prev_low)) == engine_strikes(prev_high, prev_low)


def test_ce_is_always_at_or_above_prev_high():
    """A short CE must sit above the prior high, never inside it."""
    for h in (23389.15, 24520.0, 24500.0, 25049.99):
        assert round_ce(h) >= h


def test_pe_is_always_at_or_below_prev_low():
    for l in (23286.6, 24310.0, 24500.0, 22999.9):
        assert round_pe(l) <= l


def test_rounding_is_not_nearest():
    """Guard against someone 'simplifying' this back to round()."""
    # 24520 -> nearest is 24500 (inside the range); correct answer is 24550.
    assert round_ce(24520.0) == 24550
    # 24310 -> nearest is 24300; correct answer is also 24300 here, so use a
    # case where nearest would round *up* into the range.
    assert round_pe(24340.0) == 24300
