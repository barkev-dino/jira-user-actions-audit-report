"""
groups_store.py — Persistence for saved user groups.

Groups are stored in ~/.jira_audit_groups.json as a list of dicts.
Each group has:
  id            str   — timestamp-based unique ID
  name          str   — user-editable display name
  account_ids   list  — ordered list of Jira account IDs
  display_names dict  — {account_id: display_name}
  avatar_urls   dict  — {account_id: avatar_url | null}

Maximum of MAX_GROUPS groups are stored. Oldest group is NOT auto-deleted —
the UI enforces the cap and prevents creation beyond the limit.
"""

import json
import os
import time
from pathlib import Path
from typing import List, Optional

_DATA_DIR   = Path(os.environ.get("DATA_DIR", Path.home()))
GROUPS_PATH = _DATA_DIR / ".jira_audit_groups.json"
MAX_GROUPS  = 5


def load_groups() -> List[dict]:
    """Return the saved groups list, or [] if none saved."""
    try:
        return json.loads(GROUPS_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save_all(groups: List[dict]) -> None:
    """Atomically write the full groups list."""
    tmp = GROUPS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(groups, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(GROUPS_PATH)


def get_group(group_id: str) -> Optional[dict]:
    """Return a single group by ID, or None if not found."""
    return next((g for g in load_groups() if g.get("id") == group_id), None)


def create_group(name: str, account_ids: List[str],
                 display_names: dict, avatar_urls: dict) -> dict:
    """
    Create a new group and persist it. Returns the new group dict.
    Raises ValueError if the MAX_GROUPS cap has been reached.
    """
    groups = load_groups()
    if len(groups) >= MAX_GROUPS:
        raise ValueError(f"Maximum of {MAX_GROUPS} groups allowed. Delete one first.")
    group = {
        "id":            str(int(time.time() * 1000)),  # ms timestamp as ID
        "name":          name or "New Group",
        "account_ids":   account_ids,
        "display_names": display_names,
        "avatar_urls":   avatar_urls,
    }
    groups.append(group)
    _save_all(groups)
    return group


def update_group(group_id: str, name: Optional[str] = None,
                 account_ids: Optional[List[str]] = None,
                 display_names: Optional[dict] = None,
                 avatar_urls: Optional[dict] = None) -> dict:
    """
    Update an existing group's fields. Only supplied (non-None) fields are changed.
    Returns the updated group dict. Raises ValueError if group not found.
    """
    groups = load_groups()
    for g in groups:
        if g.get("id") == group_id:
            if name          is not None: g["name"]          = name
            if account_ids   is not None: g["account_ids"]   = account_ids
            if display_names is not None: g["display_names"] = display_names
            if avatar_urls   is not None: g["avatar_urls"]   = avatar_urls
            _save_all(groups)
            return g
    raise ValueError(f"Group {group_id!r} not found")


def delete_group(group_id: str) -> None:
    """Delete a group by ID. No-op if not found."""
    groups = [g for g in load_groups() if g.get("id") != group_id]
    _save_all(groups)
