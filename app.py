"""
app.py — FastAPI application: routes, startup, and background job runner.

Report source: Jira REST API (JQL + per-issue changelog + comments).
The Jira Activity Stream is NOT used — it is a curated feed with server-side
caps and unreliable user filtering under API-token auth. The REST approach
gives complete, authoritative history with field-level change detail.

Routes:
  GET  /                       -> serve index.html
  GET  /api/status             -> verify saved credentials and return auth state
  POST /api/auth/test          -> verify Jira credentials
  POST /api/auth/clear         -> remove saved credentials
  GET  /api/users/search?q=    -> search Jira users
  POST /api/report/start       -> start a report job
  GET  /api/report/{job_id}    -> poll job status / get results
"""

import asyncio
import re as _re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Query, BackgroundTasks
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

import config_store
import jira_client
from jira_client import JiraAuthError
import job_store
import parser as feed_parser
from models import (
    AuthTestRequest, AuthTestResponse, AuthTestUser,
    ReportRow,
    ReportStartRequest, ReportStartResponse,
    ReportStatusResponse, StatusResponse,
    UserSearchResponse,
)

# ------------------------------------------------------------------ app setup

BASE_DIR = Path(__file__).parent

app = FastAPI(title="Jira Audit App")

app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

# ------------------------------------------------------------------ range map

RANGE_MAP = {
    "1d":  timedelta(days=1),
    "2d":  timedelta(days=2),
    "3d":  timedelta(days=3),
    "7d":  timedelta(days=7),
    "30d": timedelta(days=30),
}

# ------------------------------------------------------------------ routes

@app.get("/", response_class=HTMLResponse)
async def serve_index():
    """Serve the single-page frontend."""
    html_path = BASE_DIR / "templates" / "index.html"
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"), status_code=200)


@app.get("/api/status", response_model=StatusResponse)
async def get_status():
    """
    Return current auth status.
    Actually calls Jira /rest/api/3/myself to confirm saved credentials are
    still valid — does not blindly trust the config file.
    """
    cfg = config_store.load_config()
    if not cfg:
        return StatusResponse(authenticated=False)

    try:
        await jira_client.verify_credentials(
            site_url=cfg["site_url"],
            email=cfg["email"],
            api_token=cfg["api_token"],
        )
    except Exception:
        # Credentials revoked, network unreachable, etc. — show setup screen.
        # We don't auto-clear the config so a transient network error doesn't
        # force the user to re-enter their token.
        return StatusResponse(authenticated=False)

    return StatusResponse(
        authenticated=True,
        site_url=cfg.get("site_url"),
        display_name=cfg.get("display_name"),
    )


@app.post("/api/auth/test", response_model=AuthTestResponse)
async def test_auth(body: AuthTestRequest):
    """
    Verify Jira credentials by calling /rest/api/3/myself.
    If valid, save them locally and return the user details.
    """
    try:
        user_info = await jira_client.verify_credentials(
            site_url=body.site_url,
            email=body.email,
            api_token=body.api_token,
        )
    except Exception as exc:
        return AuthTestResponse(ok=False, error=str(exc))

    config_store.save_config(
        site_url=body.site_url,
        email=body.email,
        api_token=body.api_token,
        display_name=user_info["display_name"],
        account_id=user_info["account_id"],
    )
    return AuthTestResponse(
        ok=True,
        user=AuthTestUser(
            display_name=user_info["display_name"],
            account_id=user_info["account_id"],
        ),
    )


@app.post("/api/auth/clear")
async def clear_auth():
    """Delete saved credentials."""
    config_store.clear_config()
    return {"ok": True}


@app.get("/api/users/search", response_model=UserSearchResponse)
async def search_users(q: str = Query(default="", min_length=2)):
    """Search Jira users matching the query string."""
    cfg = config_store.load_config()
    if not cfg:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        items = await jira_client.search_users(
            site_url=cfg["site_url"],
            email=cfg["email"],
            api_token=cfg["api_token"],
            query=q,
        )
    except JiraAuthError as exc:
        # Return 401 so the frontend can show the auth-failure banner
        raise HTTPException(status_code=401, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return UserSearchResponse(items=items)


@app.post("/api/report/start", response_model=ReportStartResponse)
async def start_report(body: ReportStartRequest, background_tasks: BackgroundTasks):
    """Create a new report job and start it in the background."""
    cfg = config_store.load_config()
    if not cfg:
        raise HTTPException(status_code=401, detail="Not authenticated")

    if not body.account_ids:
        raise HTTPException(status_code=400, detail="account_ids must not be empty")

    if body.range_key == "custom":
        if not body.start_date or not body.end_date:
            raise HTTPException(status_code=400, detail="start_date and end_date are required for custom range")
        try:
            datetime.fromisoformat(body.start_date)
            datetime.fromisoformat(body.end_date)
        except ValueError:
            raise HTTPException(status_code=400, detail="start_date and end_date must be YYYY-MM-DD")
        if body.start_date > body.end_date:
            raise HTTPException(status_code=400, detail="start_date must not be after end_date")
    elif body.range_key not in RANGE_MAP:
        raise HTTPException(status_code=400, detail=f"Unknown range_key: {body.range_key!r}")

    # Light validation: project keys must be alphanumeric (Jira convention)
    if body.project_keys:
        invalid = [k for k in body.project_keys if not _re.match(r'^[A-Za-z0-9_]+$', k)]
        if invalid:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid project key(s): {', '.join(invalid)}",
            )

    job = job_store.create_job()
    background_tasks.add_task(
        _run_report_job,
        job_id=job.job_id,
        account_ids=body.account_ids,
        range_key=body.range_key,
        start_date=body.start_date,
        end_date=body.end_date,
        tz_offset_minutes=body.tz_offset_minutes,
        display_names=body.display_names,
        project_keys=body.project_keys,
        cfg=cfg,
    )
    return ReportStartResponse(job_id=job.job_id)


@app.get("/api/report/{job_id}", response_model=ReportStatusResponse)
async def get_report(job_id: str):
    """Poll a report job for status or final results."""
    job = job_store.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    if job.status == "done":
        return ReportStatusResponse(
            status="done",
            step=job.step,
            progress=job.progress,
            rows=job.rows,
            window_start=job.window_start,
            window_end=job.window_end,
        )
    if job.status == "error":
        return ReportStatusResponse(status="error", error=job.error)

    return ReportStatusResponse(
        status=job.status,
        step=job.step,
        progress=job.progress,
    )


# ------------------------------------------------------------------ background job

async def _run_report_job(
    job_id: str, account_ids: list, range_key: str, cfg: dict,
    start_date: Optional[str] = None, end_date: Optional[str] = None,
    tz_offset_minutes: int = 0, display_names: Optional[dict] = None,
    project_keys: Optional[list] = None,
):
    """
    Background task: Jira REST API audit pipeline.

    Pipeline (optimised):
      Phase 0 — ONE JQL scan across all users to collect candidate issue keys.
                Project filter applied here if provided.
      Phase 1 — Fetch changelog + comments ONCE per unique issue key.
      Phase 1b— Extract per-user events from the cached histories (in-memory).
      Phase 2 — Per-user JQL for issues CREATED by that user (user-specific).

    Why Phase 0 is not narrowed by user:
      Standard Jira Cloud JQL has no "updated by user X" predicate. Proxies like
      `reporter =` or `assignee was` would silently miss status changes / field
      edits made by non-reporter/non-assignee users — producing false negatives in
      an audit. The project filter IS safe to add and directly shrinks the set.
      Python-side _row_in_window + author matching remain the authoritative filter.
    """
    job = job_store.get_job(job_id)
    if job is None:
        return

    try:
        if range_key == "custom":
            from_date = _local_midnight_utc(start_date, tz_offset_minutes)
            to_date   = _local_midnight_utc(end_date,   tz_offset_minutes) + timedelta(days=1)
        else:
            now_utc   = datetime.now(timezone.utc)
            local_now = now_utc - timedelta(minutes=tz_offset_minutes)
            today_str = local_now.strftime("%Y-%m-%d")
            from_str  = (local_now - RANGE_MAP[range_key]).strftime("%Y-%m-%d")
            from_date = _local_midnight_utc(from_str,  tz_offset_minutes)
            to_date   = _local_midnight_utc(today_str, tz_offset_minutes) + timedelta(days=1)

        window_start_str = from_date.strftime("%Y-%m-%dT%H:%M:%SZ")
        window_end_str   = to_date.strftime("%Y-%m-%dT%H:%M:%SZ")

        site_url = cfg["site_url"]

        provided   = display_names or {}
        id_to_name: dict = {
            aid: provided.get(aid) or f"[{aid[:8]}…]"
            for aid in account_ids
        }

        # JQL date strings padded ±1 day to survive Jira's site-timezone evaluation.
        jql_from_str = (from_date - timedelta(days=1)).strftime("%Y-%m-%d")
        jql_to_str   = (to_date   + timedelta(days=1)).strftime("%Y-%m-%d")

        # Optional project filter — appended to every JQL query.
        project_clause = _build_project_clause(project_keys)

        # ── Phase 0: ONE JQL scan for all updated issues ──────────────────────
        # Running once for all users avoids N redundant full-instance scans.
        job.update("Scanning candidate issues…", 5)
        await asyncio.sleep(0)
        candidate_keys = await _collect_updated_keys(
            jql_from_str, jql_to_str, project_clause, cfg, job,
        )
        total_issues = len(candidate_keys)

        # ── Phase 1: fetch changelog + comments ONCE per unique issue ─────────
        # For N selected users this replaces N×total_issues API calls with
        # 1×total_issues calls — the dominant cost for multi-user reports.
        issue_histories: dict = {}   # key → (changelog_list, comment_list)
        for idx, key in enumerate(candidate_keys):
            n   = idx + 1
            pct = 15 + int((idx / max(total_issues, 1)) * 65)
            job.update(f"{n}/{total_issues}|{key}|fetching history…", pct)
            await asyncio.sleep(0)

            histories = await jira_client.fetch_issue_changelog(
                site_url, cfg["email"], cfg["api_token"], key,
            )
            comments  = await _fetch_all_issue_comments(
                site_url, cfg["email"], cfg["api_token"], key,
            )
            issue_histories[key] = (histories, comments)

        # ── Phase 1b: extract per-user events in-memory ───────────────────────
        all_rows: list = []
        for account_id in account_ids:
            display_name = id_to_name[account_id]
            rows = _extract_user_events(
                account_id, display_name, issue_histories, from_date, to_date, site_url,
            )
            all_rows.extend(rows)

        # ── Phase 2: issues CREATED by each user (user-specific JQL) ─────────
        for account_id in account_ids:
            display_name = id_to_name[account_id]
            job.update(f"Fetching issues created by {display_name}…", 82)
            await asyncio.sleep(0)
            created = await _fetch_user_created_issues(
                account_id, display_name,
                from_date, to_date, jql_from_str, jql_to_str,
                project_clause, cfg, job,
            )
            all_rows.extend(created)

        job.update("Building report table…", 98)
        await asyncio.sleep(0)

        final_rows = feed_parser.dedupe_and_sort(all_rows)
        job.finish(final_rows, window_start=window_start_str, window_end=window_end_str)

    except JiraAuthError as exc:
        job.fail(f"AUTH:{exc}")
    except Exception as exc:
        job.fail(str(exc))


# ------------------------------------------------------------------ date helpers

def _local_midnight_utc(date_str: str, tz_offset_minutes: int) -> datetime:
    """
    Convert a local YYYY-MM-DD calendar date to its UTC midnight datetime.

    tz_offset_minutes comes from JS new Date().getTimezoneOffset():
      Positive for UTC-behind zones  (e.g. EDT  UTC-4  → +240)
      Negative for UTC-ahead zones   (e.g. IST  UTC+5:30 → -330)

    Formula:  UTC_midnight = local_midnight + timedelta(minutes=tz_offset_minutes)

    Examples
    --------
    EDT (offset=+240): 2026-03-25 local → 2026-03-25T04:00:00Z
    IST (offset=-330): 2026-03-25 local → 2026-03-24T18:30:00Z
    """
    naive_midnight = datetime.fromisoformat(date_str)        # "2026-03-25" → 00:00 naive
    utc_midnight   = naive_midnight + timedelta(minutes=tz_offset_minutes)
    return utc_midnight.replace(tzinfo=timezone.utc)


def _build_project_clause(project_keys: Optional[list]) -> str:
    """Return a JQL AND fragment for project filtering, or '' if no filter."""
    if not project_keys:
        return ""
    quoted = ", ".join(f'"{k}"' for k in project_keys)
    return f' AND project in ({quoted})'


async def _collect_updated_keys(
    jql_from_str: str, jql_to_str: str,
    project_clause: str, cfg: dict, job,
) -> list:
    """
    Phase 0: paginate JQL to collect all issue keys updated in the padded window.
    Returns a deduped list preserving insertion order.
    Called ONCE for all selected users combined.
    """
    site_url  = cfg["site_url"]
    email     = cfg["email"]
    api_token = cfg["api_token"]
    max_results = 50

    jql = (
        f'updated >= "{jql_from_str}" AND updated <= "{jql_to_str}"'
        f'{project_clause} ORDER BY updated DESC'
    )

    issue_keys: list = []
    next_token: Optional[str] = None
    page = 0

    while True:
        try:
            data = await jira_client.search_issues_page(
                site_url, email, api_token, jql, next_token, max_results,
            )
        except JiraAuthError:
            raise
        except Exception as exc:
            job.update(f"⚠ Search error: {exc}", 5)
            break

        issues = data.get("issues") or []
        if not issues:
            break
        page += 1
        for iss in issues:
            k = iss.get("key")
            if k:
                issue_keys.append(k)

        job.update(f"Scanning issues · page {page} · {len(issue_keys)} found…", 8)
        await asyncio.sleep(0)

        next_token = data.get("nextPageToken")
        if not next_token or len(issues) < max_results:
            break

    # Dedupe preserving order
    seen: set = set()
    unique: list = []
    for k in issue_keys:
        if k not in seen:
            seen.add(k)
            unique.append(k)
    return unique


def _extract_user_events(
    account_id: str,
    display_name: str,
    issue_histories: dict,
    from_date: datetime,
    to_date: datetime,
    site_url: str,
) -> list:
    """
    Phase 1b (in-memory, no API calls): scan pre-fetched changelogs and comments
    for events belonging to account_id within [from_date, to_date).
    issue_histories: {key: (changelog_list, comment_list)}
    """
    rows: list = []
    for key, (histories, comment_list) in issue_histories.items():
        project   = key.rsplit("-", 1)[0] if "-" in key else key
        issue_url = f"{site_url.rstrip('/')}/browse/{key}"

        for history in histories:
            if ((history.get("author") or {}).get("accountId")) != account_id:
                continue
            ts = _normalize_ts(history.get("created", ""))
            if not _row_in_window(ts, from_date, to_date):
                continue
            items = [i for i in (history.get("items") or [])
                     if (i.get("field") or "").lower() != "resolution"]
            if not items:
                continue
            rows.append(ReportRow(
                timestamp=ts, user=display_name, issue_key=key,
                action_type=_classify_changelog_action(items),
                details=_format_changelog_items(items),
                project=project, issue_url=issue_url,
            ))

        for comment in comment_list:
            if ((comment.get("author") or {}).get("accountId")) != account_id:
                continue
            ts = _normalize_ts(comment.get("created", ""))
            if not _row_in_window(ts, from_date, to_date):
                continue
            body_text = _extract_adf_text(comment.get("body") or {})
            preview   = body_text[:150].rstrip()
            if len(body_text) > 150:
                preview += "…"
            rows.append(ReportRow(
                timestamp=ts, user=display_name, issue_key=key,
                action_type="commented",
                details=f"Comment: {preview}" if preview else "Added comment",
                project=project, issue_url=issue_url,
            ))

    return rows


async def _fetch_user_created_issues(
    account_id: str,
    display_name: str,
    from_date: datetime,
    to_date: datetime,
    jql_from_str: str,
    jql_to_str: str,
    project_clause: str,
    cfg: dict,
    job,
) -> list:
    """
    Phase 2: paginate JQL for issues CREATED by this user in the padded window.
    This is already user-specific so runs per-user (but is typically very fast).
    """
    site_url  = cfg["site_url"]
    email     = cfg["email"]
    api_token = cfg["api_token"]
    max_results = 50
    rows: list  = []

    jql = (
        f'reporter = "{account_id}" AND '
        f'created >= "{jql_from_str}" AND created <= "{jql_to_str}"'
        f'{project_clause} ORDER BY created DESC'
    )
    next_token: Optional[str] = None

    while True:
        try:
            data = await jira_client.search_created_issues_page(
                site_url, email, api_token, jql, next_token, max_results,
            )
        except JiraAuthError:
            raise
        except Exception:
            break

        issues = data.get("issues") or []
        if not issues:
            break

        for issue in issues:
            key = issue.get("key", "")
            if not key:
                continue
            fields  = issue.get("fields") or {}
            ts      = _normalize_ts(fields.get("created", ""))
            if not _row_in_window(ts, from_date, to_date):
                continue
            summary = fields.get("summary") or ""
            project = key.rsplit("-", 1)[0] if "-" in key else key
            rows.append(ReportRow(
                timestamp=ts, user=display_name, issue_key=key,
                action_type="created",
                details=f"Created: {summary}" if summary else "Created issue",
                project=project,
                issue_url=f"{site_url.rstrip('/')}/browse/{key}",
            ))

        next_token = data.get("nextPageToken")
        if not next_token or len(issues) < max_results:
            break

    return rows


async def _fetch_all_issue_comments(
    site_url: str, email: str, api_token: str, issue_key: str
) -> list:
    """Paginate through all comments for an issue."""
    all_comments: list = []
    start = 0
    while True:
        data = await jira_client.fetch_issue_comments(
            site_url, email, api_token, issue_key, start, 100
        )
        comments = data.get("comments") or []
        all_comments.extend(comments)
        if len(comments) < 100 or (start + len(comments)) >= data.get("total", 0):
            break
        start += len(comments)
    return all_comments


# ------------------------------------------------------------------ REST helpers

def _extract_adf_text(node) -> str:
    """Recursively extract plain text from an Atlassian Document Format (ADF) node."""
    if not isinstance(node, dict):
        return ""
    if node.get("type") == "text":
        return node.get("text", "")
    children = node.get("content") or []
    parts = [_extract_adf_text(c) for c in children]
    sep = " " if node.get("type") in (
        "paragraph", "heading", "blockquote", "codeBlock",
        "bulletList", "orderedList", "listItem", "panel",
    ) else ""
    return sep.join(p for p in parts if p)


# Jira internal field names that indicate a work log operation.
# These appear in changelog items when time is logged on an issue.
_WORKLOG_FIELDS = frozenset({
    "timespent",
    "worklogid",
    "timeestimate",
    "remainingestimate",
    "timeoriginalestimate",
})


def _classify_changelog_action(items: list) -> str:
    """
    Determine the best action_type label from a list of changelog items.

    Precedence (highest to lowest):
      status      -> status_change  (includes transitions to Resolved/Closed)
      assignee    -> assigned
      attachment  -> attachment
      link        -> linked
      worklog     -> logged_work
      (anything)  -> updated
    """
    has_worklog = False
    for item in items:
        field = (item.get("field") or "").lower()
        if field == "status":
            return "status_change"
        if field == "assignee":
            return "assigned"
        if field == "attachment":
            return "attachment"
        if field in ("link", "issuelinks"):
            return "linked"
        if field in _WORKLOG_FIELDS:
            # Don't return immediately — a higher-priority field may still
            # appear later in the items list.
            has_worklog = True
    if has_worklog:
        return "logged_work"
    return "updated"


def _normalize_ts(iso_str: str) -> str:
    """
    Normalize an ISO-8601 timestamp to UTC Z format.
    Handles Jira's '-0400' offset style (no colon) which Python ≤3.10 rejects.
    """
    fixed = _re.sub(r'([+-])(\d{2})(\d{2})$', r'\1\2:\3', iso_str)
    try:
        dt = datetime.fromisoformat(fixed)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return iso_str


def _format_changelog_items(items: list) -> str:
    """Convert a list of changelog items into a human-readable Details string."""
    parts = []
    for item in items:
        field  = (item.get("field") or "").lower()
        from_s = item.get("fromString") or ""
        to_s   = item.get("toString")   or ""

        if field == "resolution":
            continue  # always paired with a status change; suppress as noise
        elif field == "issueparentassociation":
            parts.append(f"Parent → {to_s or from_s}")
        elif field == "status":
            if from_s and to_s:
                parts.append(f"Status: {from_s} → {to_s}")
            else:
                parts.append(f"Status → {to_s or from_s}")
        elif field in ("duedate", "due date"):
            parts.append(f"Due Date → {to_s[:10]}" if to_s else "Due Date cleared")
        elif field == "description":
            parts.append("Description updated")
        elif field == "summary":
            label = to_s[:80] + ("…" if len(to_s) > 80 else "")
            parts.append(f"Summary → {label}")
        elif field == "assignee":
            parts.append(f"Assignee → {to_s or 'Unassigned'}")
        elif field == "priority":
            parts.append(f"Priority → {to_s}")
        elif field in ("story points", "story point estimate", "story_points", "customfield_10016"):
            parts.append(f"Story Points → {to_s}")
        elif field == "labels":
            parts.append(f"Labels → {to_s or '(none)'}")
        elif field == "sprint":
            parts.append(f"Sprint → {to_s or '(none)'}")
        elif field == "attachment":
            fname = to_s or from_s
            parts.append(f"Attachment: {fname}" if fname else "Attachment added")
        elif field in ("link", "issuelinks"):
            parts.append(f"Linked: {to_s or from_s}")
        else:
            label = (item.get("field") or field).replace("_", " ").title()
            if to_s:
                val = to_s[:80] + ("…" if len(to_s) > 80 else "")
                parts.append(f"{label} → {val}")
            elif from_s:
                parts.append(f"{label} cleared")
            else:
                parts.append(f"{label} updated")

    return ", ".join(parts) if parts else "Updated"


def _row_in_window(timestamp_str: str, from_date: datetime, to_date: datetime) -> bool:
    """Return True if the timestamp falls within [from_date, to_date)."""
    try:
        dt = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
        return from_date <= dt < to_date
    except ValueError:
        return False
