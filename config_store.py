"""
config_store.py — Save and load Jira credentials to/from a local JSON file.

The config file lives at ~/.jira_audit_config.json so it persists across
restarts but stays out of the project directory (no accidental commits).
"""

import json
import os
from pathlib import Path
from typing import Optional

# DATA_DIR is set to /data on Fly.io (persistent volume).
# Falls back to home directory for local use.
_DATA_DIR   = Path(os.environ.get("DATA_DIR", Path.home()))
CONFIG_PATH = _DATA_DIR / ".jira_audit_config.json"

REQUIRED_KEYS = {"site_url", "email", "api_token", "display_name", "account_id"}


def save_config(site_url: str, email: str, api_token: str, display_name: str, account_id: str) -> None:
    """Persist credentials to disk atomically via a temp file."""
    data = {
        "site_url": site_url,
        "email": email,
        "api_token": api_token,
        "display_name": display_name,
        "account_id": account_id,
    }
    tmp = CONFIG_PATH.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(CONFIG_PATH)  # atomic on POSIX; best-effort on Windows
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        raise


def load_config() -> Optional[dict]:
    """
    Return saved config dict, or None if not found / unreadable / incomplete.
    Never raises — always returns dict or None.
    """
    try:
        text = CONFIG_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except Exception:
        return None

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None

    # Reject configs that are missing any required key
    if not isinstance(data, dict) or not REQUIRED_KEYS.issubset(data.keys()):
        return None

    return data


def clear_config() -> None:
    """Delete the saved config file. No-op if already gone."""
    try:
        CONFIG_PATH.unlink(missing_ok=True)
    except Exception:
        pass
