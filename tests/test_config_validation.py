"""Settings must save what you typed, or say plainly why they didn't.

Two real bugs are pinned here:
  * a time of "HH:MM:SS" (which browsers send once a step is involved) was
    dropped, and because the UI saves every field in one patch, the whole
    save 400'd and nothing changed;
  * invalid fields were discarded in silence, so a save reported success
    while quietly keeping the old value.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.strategies_store import normalize_time, validate_patch  # noqa: E402


# -- time -----------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("09:33", "09:33"),
        ("9:33", "09:33"),        # some inputs drop the leading zero
        ("09:33:00", "09:33"),    # the form browsers send with a step set
        ("23:59", "23:59"),
        ("00:00", "00:00"),
        (" 10:15 ", "10:15"),
    ],
)
def test_accepted_time_formats(raw, expected):
    assert normalize_time(raw) == expected


@pytest.mark.parametrize("raw", ["", "  ", "25:00", "10:60", "abc", "10", None])
def test_rejected_time_formats(raw):
    assert normalize_time(raw) is None


def test_time_with_seconds_saves_instead_of_being_dropped():
    clean, rejected = validate_patch({"auto_entry_time": "11:22:00"})
    assert clean["auto_entry_time"] == "11:22"
    assert not rejected


def test_a_bad_time_does_not_discard_the_other_settings():
    """The whole-form save must not be lost because one field is wrong."""
    clean, rejected = validate_patch(
        {"auto_entry_time": "99:99", "profit_target": "2500", "lots": "2"}
    )
    assert clean == {"profit_target": 2500.0, "lots": 2}
    assert "auto_entry_time" in rejected


# -- amounts ---------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [("2500", 2500.0), ("2500.5", 2500.5), ("1,800", 1800.0), (3000, 3000.0)],
)
def test_amount_formats(raw, expected):
    clean, rejected = validate_patch({"profit_target": raw})
    assert clean["profit_target"] == expected
    assert not rejected


def test_lots_accepts_a_float_string_from_a_number_input():
    clean, _ = validate_patch({"lots": "2.0"})
    assert clean["lots"] == 2


def test_empty_amount_is_reported_not_silently_ignored():
    clean, rejected = validate_patch({"profit_target": ""})
    assert "profit_target" not in clean
    assert "profit_target" in rejected


@pytest.mark.parametrize("key", ["lots", "profit_target", "loss_limit"])
def test_non_positive_values_are_rejected(key):
    clean, rejected = validate_patch({key: "0"})
    assert key not in clean
    assert key in rejected


# -- reporting -------------------------------------------------------------

def test_unknown_keys_are_reported():
    clean, rejected = validate_patch({"not_a_setting": 1})
    assert not clean
    assert "not_a_setting" in rejected


def test_bad_mode_is_reported_with_the_offending_value():
    clean, rejected = validate_patch({"mode": "wat"})
    assert "mode" not in clean
    assert "wat" in rejected["mode"]


def test_monitor_is_a_valid_mode():
    clean, rejected = validate_patch({"mode": "monitor"})
    assert clean["mode"] == "monitor"
    assert not rejected


def test_a_full_realistic_form_save():
    """Everything the dashboard sends in one go."""
    clean, rejected = validate_patch({
        "lots": "1", "profit_target": "2250", "loss_limit": "2000",
        "mode": "monitor", "loss_limit_basis": "exit",
        "trail_enabled": True, "trail_activate_at": "1500", "trail_by": "750",
        "lock_profit_enabled": False, "lock_profit_trigger": "2000",
        "lock_profit_lock_at": "1000", "auto_entry_enabled": True,
        "auto_entry_time": "09:33:00", "auto_entry_expiry": "weekly_current",
        "eod_squareoff_time": "15:15", "vix_max": "20",
    })
    assert not rejected
    assert clean["auto_entry_time"] == "09:33"
    assert clean["eod_squareoff_time"] == "15:15"
    assert clean["profit_target"] == 2250.0
    assert clean["lots"] == 1
