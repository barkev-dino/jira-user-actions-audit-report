"""
parser.py — Post-processing helpers for Tickets Touched Report rows.
"""

from datetime import datetime, timezone
from typing import List
from models import ReportRow


def dedupe_and_sort(rows: List[ReportRow]) -> List[ReportRow]:
    """
    Remove duplicate rows and sort newest-first.

    Dedupe key includes details so two distinct changes at the exact same
    second (e.g. bulk field edits) are not collapsed into one row.
    """
    seen = set()
    unique: List[ReportRow] = []
    for row in rows:
        key = (row.timestamp, row.user, row.issue_key, row.action_type, row.details)
        if key not in seen:
            seen.add(key)
            unique.append(row)

    def _ts_key(row: ReportRow) -> datetime:
        try:
            return datetime.fromisoformat(row.timestamp.replace("Z", "+00:00"))
        except ValueError:
            return datetime.min.replace(tzinfo=timezone.utc)

    return sorted(unique, key=_ts_key, reverse=True)
