"""
schedule_store.py — Persist and load the scheduled report configuration.

Config file: ~/.jira_audit_schedule.json
Last-run log: ~/.jira_audit_last_run.json

Schedule config shape:
{
  "enabled":       true,
  "run_time":      "07:00",          # HH:MM local time
  "run_until":     "2026-05-05",     # YYYY-MM-DD — scheduler skips if today > this
  "account_ids":   ["abc123", ...],
  "display_names": {"abc123": "Alice Smith", ...},
  "project_keys":  ["KAN", "OPS"],   # [] means all projects
  "range_key":     "1d"              # same keys as the main report form
}

Last-run log shape:
{
  "last_run_at":   "2026-03-30T07:00:00",  # local ISO, no tz
  "last_run_rows": 45,
  "last_run_ok":   true,
  "last_error":    null
}
"""

import json
import os
from pathlib import Path
from typing import Optional

_DATA_DIR      = Path(os.environ.get("DATA_DIR", Path.home()))
SCHEDULE_PATH  = _DATA_DIR / ".jira_audit_schedule.json"
LAST_RUN_PATH  = _DATA_DIR / ".jira_audit_last_run.json"

SCHEDULE_KEYS = {"enabled", "run_time", "run_until", "account_ids",
                 "display_names", "project_keys", "range_key"}


# ---------------------------------------------------------------------------
# Schedule config
# ---------------------------------------------------------------------------

def load_schedule() -> Optional[dict]:
    """Return saved schedule config or None if missing / invalid."""
    try:
        data = json.loads(SCHEDULE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, Exception):
        return None
    if not isinstance(data, dict):
        return None
    return data


def save_schedule(config: dict) -> None:
    """Persist schedule config atomically."""
    tmp = SCHEDULE_PATH.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(config, indent=2), encoding="utf-8")
        tmp.replace(SCHEDULE_PATH)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        raise


def clear_schedule() -> None:
    try:
        SCHEDULE_PATH.unlink(missing_ok=True)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Last-run log
# ---------------------------------------------------------------------------

def load_last_run() -> dict:
    """Return last-run info, or a blank record if not found."""
    blank = {"last_run_at": None, "last_run_rows": None,
             "last_run_ok": None, "last_error": None}
    try:
        data = json.loads(LAST_RUN_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else blank
    except Exception:
        return blank


def save_last_run(*, ran_at: str, rows: int, ok: bool, error: Optional[str] = None) -> None:
    """Write a last-run record."""
    record = {
        "last_run_at":   ran_at,
        "last_run_rows": rows,
        "last_run_ok":   ok,
        "last_error":    error,
    }
    tmp = LAST_RUN_PATH.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(record, indent=2), encoding="utf-8")
        tmp.replace(LAST_RUN_PATH)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
