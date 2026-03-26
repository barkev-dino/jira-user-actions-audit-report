"""
Tests for timezone-aware date window calculation in app.py.
Run: cd /Users/b/jira_user_audit_report && python3 -m pytest tests/test_dates.py -v
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from datetime import datetime, timezone, timedelta
from app import _local_midnight_utc, _row_in_window


# ── _local_midnight_utc ───────────────────────────────────────────────────────

def test_local_midnight_utc_edt():
    """EDT (UTC-4): local midnight 2026-03-25 → 04:00 UTC."""
    result = _local_midnight_utc("2026-03-25", tz_offset_minutes=240)
    assert result == datetime(2026, 3, 25, 4, 0, 0, tzinfo=timezone.utc)

def test_local_midnight_utc_ist():
    """IST (UTC+5:30): local midnight 2026-03-25 → 2026-03-24T18:30 UTC."""
    result = _local_midnight_utc("2026-03-25", tz_offset_minutes=-330)
    assert result == datetime(2026, 3, 24, 18, 30, 0, tzinfo=timezone.utc)

def test_local_midnight_utc_zero():
    """UTC: local midnight is UTC midnight."""
    result = _local_midnight_utc("2026-03-25", tz_offset_minutes=0)
    assert result == datetime(2026, 3, 25, 0, 0, 0, tzinfo=timezone.utc)

def test_local_midnight_utc_pst():
    """PST (UTC-8): local midnight 2026-03-25 → 08:00 UTC."""
    result = _local_midnight_utc("2026-03-25", tz_offset_minutes=480)
    assert result == datetime(2026, 3, 25, 8, 0, 0, tzinfo=timezone.utc)


# ── _row_in_window ────────────────────────────────────────────────────────────

def test_row_in_window_exact_start():
    """Event at exactly from_date IS included (inclusive lower bound)."""
    from_date = datetime(2026, 3, 25, 4, 0, 0, tzinfo=timezone.utc)
    to_date   = datetime(2026, 3, 26, 4, 0, 0, tzinfo=timezone.utc)
    assert _row_in_window("2026-03-25T04:00:00Z", from_date, to_date) is True

def test_row_in_window_just_before_end():
    """Event one second before to_date IS included."""
    from_date = datetime(2026, 3, 25, 4, 0, 0, tzinfo=timezone.utc)
    to_date   = datetime(2026, 3, 26, 4, 0, 0, tzinfo=timezone.utc)
    assert _row_in_window("2026-03-26T03:59:59Z", from_date, to_date) is True

def test_row_in_window_at_exclusive_end():
    """Event at exactly to_date is NOT included (exclusive upper bound)."""
    from_date = datetime(2026, 3, 25, 4, 0, 0, tzinfo=timezone.utc)
    to_date   = datetime(2026, 3, 26, 4, 0, 0, tzinfo=timezone.utc)
    assert _row_in_window("2026-03-26T04:00:00Z", from_date, to_date) is False

def test_row_in_window_late_evening_edt():
    """10 PM EDT on Mar 25 (= 2026-03-26T02:00Z) is inside window for EDT user selecting Mar 25."""
    # EDT user selects Mar 25: from = 2026-03-25T04:00Z, to = 2026-03-26T04:00Z
    from_date = datetime(2026, 3, 25, 4, 0, 0, tzinfo=timezone.utc)
    to_date   = datetime(2026, 3, 26, 4, 0, 0, tzinfo=timezone.utc)
    # 10 PM EDT = UTC-4, so 10 PM local = 02:00 UTC next day
    assert _row_in_window("2026-03-26T02:00:00Z", from_date, to_date) is True

def test_row_in_window_invalid_ts():
    """Garbage timestamp returns False gracefully."""
    from_date = datetime(2026, 3, 25, 0, 0, tzinfo=timezone.utc)
    to_date   = datetime(2026, 3, 26, 0, 0, tzinfo=timezone.utc)
    assert _row_in_window("not-a-date", from_date, to_date) is False
