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
    return HTMLResponse(content=html_path.read_text(), status_code=200)


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
):
    """
    Background task: uses the Jira REST API (JQL + changelog + comments) to
    fetch complete, paginated activity for each user within the date range.
    """
    job = job_store.get_job(job_id)
    if job is None:
        return

    try:
        if range_key == "custom":
            # Local calendar-day boundaries → UTC.
            # from_date = start of selected start date in local tz
            # to_date   = start of the day AFTER selected end date (exclusive upper bound)
            from_date = _local_midnight_utc(start_date, tz_offset_minutes)
            to_date   = _local_midnight_utc(end_date,   tz_offset_minutes) + timedelta(days=1)
        else:
            # Preset: snap to local calendar midnight boundaries so "1 day" means
            # today's full local day, "7 days" means the last 7 full local days, etc.
            now_utc   = datetime.now(timezone.utc)
            local_now = now_utc - timedelta(minutes=tz_offset_minutes)
            today_str = local_now.strftime("%Y-%m-%d")
            from_str  = (local_now - RANGE_MAP[range_key]).strftime("%Y-%m-%d")
            from_date = _local_midnight_utc(from_str,  tz_offset_minutes)
            to_date   = _local_midnight_utc(today_str, tz_offset_minutes) + timedelta(days=1)

        # Record the exact UTC window so the frontend can display it.
        window_start_str = from_date.strftime("%Y-%m-%dT%H:%M:%SZ")
        window_end_str   = to_date.strftime("%Y-%m-%dT%H:%M:%SZ")

        site_url    = cfg["site_url"]
        total_users = len(account_ids)

        # Use display names supplied by the frontend (already known from user-search).
        # This avoids a redundant Jira API call per selected user.
        provided   = display_names or {}
        id_to_name: dict = {
            aid: provided.get(aid) or f"[{aid[:8]}…]"
            for aid in account_ids
        }

        all_rows: list = []

        for idx, account_id in enumerate(account_ids):
            display_name  = id_to_name[account_id]
            base_progress = 10 + int((idx / total_users) * 80)
            progress_span = int(80 / total_users)

            job.update(f"Fetching activity for {display_name}…", base_progress)
            await asyncio.sleep(0)

            user_rows = await _fetch_user_activity_rest(
                account_id, display_name, from_date, to_date,
                cfg, job, base_progress, progress_span,
            )
            all_rows.extend(user_rows)

        job.update("Building report table…", 98)
        await asyncio.sleep(0)

        final_rows = feed_parser.dedupe_and_sort(all_rows)
        job.finish(final_rows, window_start=window_start_str, window_end=window_end_str)

    except JiraAuthError as exc:
        # Prefix lets the frontend distinguish auth failures from other errors
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


async def _fetch_user_activity_rest(
    account_id: str,
    display_name: str,
    from_date: datetime,
    to_date: datetime,
    cfg: dict,
    job,
    base_progress: int,
    progress_span: int,
) -> list:
    """
    Three-phase fetch:
      Phase 0 — paginate JQL to collect all issue keys updated in the window.
      Phase 1 — for each key fetch full changelog + comments, emit N/M progress.
      Phase 2 — separately fetch issues created by this user in the window.
    """
    site_url  = cfg["site_url"]
    email     = cfg["email"]
    api_token = cfg["api_token"]
    max_results = 50
    rows: list  = []

    # JQL date strings padded ±1 day to survive Jira's site-timezone interpretation.
    # Jira evaluates date-only JQL in the site's configured timezone (not UTC), so
    # a late-evening local event may appear as the next calendar day in Jira's JQL.
    # The padding ensures we always scan a superset; _row_in_window is the authoritative filter.
    jql_from_str = (from_date - timedelta(days=1)).strftime("%Y-%m-%d")
    jql_to_str   = (to_date   + timedelta(days=1)).strftime("%Y-%m-%d")

    # ── Phase 0: collect all issue keys updated in the window ─────────────────
    jql_updated = (
        f'updated >= "{jql_from_str}" AND updated <= "{jql_to_str}" '
        f'ORDER BY updated DESC'
    )
    issue_keys: list = []
    next_token: Optional[str] = None
    scan_page = 0

    while True:
        try:
            data = await jira_client.search_issues_page(
                site_url, email, api_token, jql_updated, next_token, max_results,
            )
        except JiraAuthError:
            raise  # let _run_report_job catch it and set the AUTH: error
        except Exception as exc:
            job.update(f"⚠ Search error: {exc}", base_progress)
            break

        issues = data.get("issues") or []
        if not issues:
            break
        scan_page += 1
        for iss in issues:
            k = iss.get("key")
            if k:
                issue_keys.append(k)

        next_token = data.get("nextPageToken")
        job.update(
            f"Scanning {display_name} · page {scan_page} · {len(issue_keys)} issues found…",
            base_progress + 3,
        )
        await asyncio.sleep(0)
        if not next_token or len(issues) < max_results:
            break

    # Dedupe preserving order
    seen_set: set = set()
    unique_keys: list = []
    for k in issue_keys:
        if k not in seen_set:
            seen_set.add(k)
            unique_keys.append(k)
    issue_keys  = unique_keys
    total_issues = len(issue_keys)

    # ── Phase 1: audit each issue's changelog and comments ───────────────────
    for idx, key in enumerate(issue_keys):
        n         = idx + 1
        project   = key.rsplit("-", 1)[0] if "-" in key else key
        issue_url = f"{site_url.rstrip('/')}/browse/{key}"
        pct       = base_progress + 10 + int((idx / max(total_issues, 1)) * (progress_span - 20))

        job.update(f"{n}/{total_issues}|{key}|fetching changelog…", pct)
        await asyncio.sleep(0)
        histories = await jira_client.fetch_issue_changelog(site_url, email, api_token, key)

        cl_matches = 0
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
            cl_matches += 1

        job.update(
            f"{n}/{total_issues}|{key}|fetching comments"
            f" ({len(histories)} cl, {cl_matches} matched)…",
            pct,
        )
        await asyncio.sleep(0)
        comment_list = await _fetch_all_issue_comments(site_url, email, api_token, key)

        cm_matches = 0
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
            cm_matches += 1

        job.update(
            f"{n}/{total_issues}|{key}|done"
            f" · cl:{cl_matches} cm:{cm_matches} · {len(rows)} events so far",
            pct,
        )
        await asyncio.sleep(0)

    # ── Phase 2: issues CREATED by this user in the window ───────────────────
    jql_created = (
        f'reporter = "{account_id}" AND '
        f'created >= "{jql_from_str}" AND created <= "{jql_to_str}" '
        f'ORDER BY created DESC'
    )
    next_token2: Optional[str] = None
    while True:
        try:
            data2 = await jira_client.search_created_issues_page(
                site_url, email, api_token, jql_created, next_token2, max_results,
            )
        except JiraAuthError:
            raise  # let _run_report_job catch it and set the AUTH: error
        except Exception:
            break
        issues2 = data2.get("issues") or []
        if not issues2:
            break
        for issue in issues2:
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
        next_token2 = data2.get("nextPageToken")
        if not next_token2 or len(issues2) < max_results:
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
