"""
models.py — Pydantic request/response models for the Tickets Touched Report app.
"""

from pydantic import BaseModel
from typing import Optional, List


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

class AuthTestRequest(BaseModel):
    site_url: str
    email: str
    api_token: str


class AuthTestUser(BaseModel):
    display_name: str
    account_id: str


class AuthTestResponse(BaseModel):
    ok: bool
    user: Optional[AuthTestUser] = None
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

class StatusResponse(BaseModel):
    authenticated: bool
    site_url: Optional[str] = None
    display_name: Optional[str] = None


# ---------------------------------------------------------------------------
# User search
# ---------------------------------------------------------------------------

class UserItem(BaseModel):
    account_id: str
    display_name: str
    avatar_url: Optional[str] = None


class UserSearchResponse(BaseModel):
    items: List[UserItem]


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

class ReportStartRequest(BaseModel):
    account_ids: List[str]
    range_key: str = "7d"              # "1d"|"2d"|"3d"|"7d"|"30d"|"custom"
    start_date: Optional[str] = None   # "YYYY-MM-DD" — required when range_key == "custom"
    end_date:   Optional[str] = None   # "YYYY-MM-DD" — required when range_key == "custom"
    tz_offset_minutes: int = 0         # new Date().getTimezoneOffset() — +240 for UTC-4 (EDT)
    display_names: dict = {}           # {account_id: display_name} — supplied by frontend
    project_keys: List[str] = []       # optional project filter, e.g. ["KAN", "OPS"]


class ReportStartResponse(BaseModel):
    job_id: str


class ReportRow(BaseModel):
    timestamp: str
    user: str
    issue_key: str
    action_type: str
    details: str
    project: str
    issue_url: str


class ReportStatusResponse(BaseModel):
    status: str          # "pending" | "running" | "done" | "error"
    step: Optional[str] = None
    progress: Optional[int] = None
    rows: Optional[List[ReportRow]] = None
    error: Optional[str] = None
    window_start: Optional[str] = None  # UTC ISO string — inclusive lower bound used for filtering
    window_end: Optional[str] = None    # UTC ISO string — exclusive upper bound used for filtering
