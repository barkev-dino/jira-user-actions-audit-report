"""
app.py — FastAPI application for Tickets Touched Report: routes, startup, and background job runner.

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
  GET  /api/schedule           -> get saved schedule config + last-run info
  POST /api/schedule           -> save schedule config (and reschedule APScheduler job)
"""

import asyncio
import csv
import io
import os
import re as _re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, HTTPException, Query, BackgroundTasks
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

import config_store
import jira_client
from jira_client import JiraAuthError
import job_store
import parser as feed_parser
import schedule_store
import groups_store
from models import (
    AuthTestRequest, AuthTestResponse, AuthTestUser,
    ReportRow,
    ReportStartRequest, ReportStartResponse,
    ReportStatusResponse, StatusResponse,
    UserSearchResponse,
)

# ------------------------------------------------------------------ app setup

BASE_DIR    = Path(__file__).parent
# On Fly.io DATA_DIR=/data (persistent volume). Locally falls back to ./reports.
_DATA_DIR   = Path(os.environ.get("DATA_DIR", BASE_DIR))
REPORTS_DIR = _DATA_DIR / "reports"

_scheduler = AsyncIOScheduler()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start APScheduler on startup; shut it down cleanly on exit."""
    REPORTS_DIR.mkdir(exist_ok=True)
    _scheduler.start()
    _apply_schedule()          # load saved config and arm the cron job (if any)
    yield
    _scheduler.shutdown(wait=False)


app = FastAPI(title="Tickets Touched Report", lifespan=lifespan)

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


# ------------------------------------------------------------------ schedule routes

@app.get("/api/schedule")
async def get_schedule():
    """Return saved schedule config, last-run record, and next scheduled fire time."""
    job      = _scheduler.get_job("scheduled_report")
    next_run = None
    if job and job.next_run_time:
        next_run = job.next_run_time.isoformat()
    return {
        "config":   schedule_store.load_schedule(),
        "last_run": schedule_store.load_last_run(),
        "next_run": next_run,
    }


@app.post("/api/schedule")
async def save_schedule(body: dict):
    """Persist schedule config and rearm the APScheduler cron job."""
    required = {"enabled", "run_time", "run_until", "range_key",
                "account_ids", "display_names", "project_keys"}
    if not required.issubset(body.keys()):
        raise HTTPException(status_code=400, detail="Missing required schedule fields")
    try:
        schedule_store.save_schedule(body)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    _apply_schedule()
    return {"ok": True}


# ------------------------------------------------------------------ group routes

@app.get("/api/groups")
async def get_groups():
    """Return all saved user groups."""
    return {"groups": groups_store.load_groups()}


@app.post("/api/groups")
async def save_group(body: dict):
    """
    Create or update a user group.
    Body must include: name, account_ids, display_names, avatar_urls.
    If 'id' is present and matches an existing group, it is updated.
    Otherwise a new group is created (subject to MAX_GROUPS cap).
    """
    name          = body.get("name", "New Group")
    account_ids   = body.get("account_ids", [])
    display_names = body.get("display_names", {})
    avatar_urls   = body.get("avatar_urls", {})
    group_id      = body.get("id")

    try:
        if group_id and groups_store.get_group(group_id):
            group = groups_store.update_group(
                group_id,
                name=name,
                account_ids=account_ids,
                display_names=display_names,
                avatar_urls=avatar_urls,
            )
        else:
            group = groups_store.create_group(name, account_ids, display_names, avatar_urls)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return {"group": group}


@app.delete("/api/groups/{group_id}")
async def delete_group(group_id: str):
    """Delete a user group by ID."""
    groups_store.delete_group(group_id)
    return {"ok": True}


# ------------------------------------------------------------------ background job

async def _run_report_job(
    job_id: str, account_ids: list, range_key: str, cfg: dict,
    start_date: Optional[str] = None, end_date: Optional[str] = None,
    tz_offset_minutes: int = 0, display_names: Optional[dict] = None,
    project_keys: Optional[list] = None,
):
    """
    Background task: Jira REST API audit pipeline.

    Pipeline:
      Phase 0 — Per-user JQL scan using updatedBy() to collect candidate keys.
                Keys are unioned and deduped across all selected users.
                Optional project filter applied here to shrink the set further.
      Phase 1 — Fetch changelog + comments ONCE per unique candidate key.
      Phase 1b— Extract per-user events in-memory (no API calls).
      Phase 2 — Per-user JQL for issues CREATED by that user (user-specific).

    updatedBy(accountId, dateFrom, dateTo) is an Atlassian-documented Jira Cloud
    JQL function covering field changes, status transitions, comments, and creation.
    It is day-granular only — the ±1-day padded JQL dates ensure boundary events
    are not omitted. Python-side _row_in_window remains the exact final filter.
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

        # Optional project filter clause — reused in Phase 0 and Phase 2.
        project_clause = _build_project_clause(project_keys)

        # ── Phase 0: per-user updatedBy() scan, union across users ───────────
        # updatedBy() narrows candidates to issues the user actually touched,
        # replacing a broad "all updated issues" scan.  Running one query per
        # user and unioning the results avoids fetching irrelevant issue histories.
        total_users    = len(account_ids)
        seen_keys: set = set()
        candidate_keys: list = []

        for u_idx, account_id in enumerate(account_ids):
            display_name = id_to_name[account_id]
            pct_base     = 5 + int((u_idx / total_users) * 10)
            job.update(f"Scanning candidates for {display_name}…", pct_base)
            await asyncio.sleep(0)

            jql      = _build_candidate_jql(account_id, jql_from_str, jql_to_str, project_keys)
            user_keys = await _paginate_issue_keys(jql, cfg, job, pct_base)

            for k in user_keys:
                if k not in seen_keys:
                    seen_keys.add(k)
                    candidate_keys.append(k)

        total_issues = len(candidate_keys)

        # ── Phase 1: fetch changelog + comments ONCE per unique issue ─────────
        # For N selected users this replaces N×total_issues API calls with
        # 1×total_issues calls — the dominant cost for multi-user reports.
        issue_histories: dict = {}   # key → (changelog_list, comment_list)
        for idx, key in enumerate(candidate_keys):
            n   = idx + 1
            pct = 15 + int((idx / max(total_issues, 1)) * 65)
            job.update(f"{n}/{total_issues}|{key}|fetching history…", pct)
            await asyncio.sleep(0.1)  # polite throttle — stays well under Jira rate limits

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


# ------------------------------------------------------------------ scheduler helpers

def _apply_schedule() -> None:
    """
    Read the saved schedule config and (re)arm the APScheduler cron job.
    Called on startup and every time the schedule is saved via the UI.
    """
    _scheduler.remove_all_jobs()
    cfg = schedule_store.load_schedule()
    if not cfg or not cfg.get("enabled"):
        return

    run_until = cfg.get("run_until", "")
    today_str = datetime.now().strftime("%Y-%m-%d")
    if run_until and run_until < today_str:
        return  # expired — don't arm

    run_time = cfg.get("run_time", "07:00")
    try:
        hour, minute = [int(x) for x in run_time.split(":")]
    except (ValueError, AttributeError):
        hour, minute = 7, 0

    _scheduler.add_job(
        _run_scheduled_report,
        trigger=CronTrigger(hour=hour, minute=minute),
        id="scheduled_report",
        replace_existing=True,
        misfire_grace_time=1800,   # run if server starts within 30 min of scheduled time
    )


async def _run_scheduled_report() -> None:
    """
    APScheduler callback: run the report for saved schedule config,
    save results as a CSV in reports/, and write the last-run record.
    """
    cfg      = schedule_store.load_schedule()
    jira_cfg = config_store.load_config()

    if not cfg or not jira_cfg:
        schedule_store.save_last_run(
            ran_at=datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            rows=0, ok=False,
            error="Missing schedule or Jira config — open the app to configure.",
        )
        return

    # Check run_until again at fire time (user may have updated the date)
    today_str = datetime.now().strftime("%Y-%m-%d")
    if cfg.get("run_until", "") < today_str:
        return  # silently skip expired runs

    ran_at = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    try:
        # Use local timezone offset at fire time (server is local machine)
        local_offset = -int(datetime.now().astimezone().utcoffset().total_seconds() / 60)

        account_ids   = cfg.get("account_ids", [])
        display_names = cfg.get("display_names", {})
        range_key     = cfg.get("range_key", "1d")
        project_keys  = cfg.get("project_keys", [])

        if not account_ids:
            raise ValueError("No users configured in schedule")

        # Compute date window
        if range_key == "custom":
            raise ValueError("Custom date range is not supported for scheduled runs; use a preset.")

        now_utc   = datetime.now(timezone.utc)
        local_now = now_utc - timedelta(minutes=local_offset)
        today_s   = local_now.strftime("%Y-%m-%d")
        from_s    = (local_now - RANGE_MAP[range_key]).strftime("%Y-%m-%d")
        from_date = _local_midnight_utc(from_s,   local_offset)
        to_date   = _local_midnight_utc(today_s,  local_offset) + timedelta(days=1)

        jql_from_str   = (from_date - timedelta(days=1)).strftime("%Y-%m-%d")
        jql_to_str     = (to_date   + timedelta(days=1)).strftime("%Y-%m-%d")
        project_clause = _build_project_clause(project_keys)

        # Run the full pipeline (same as manual run)
        candidate_keys = await _collect_candidate_keys(
            account_ids, jql_from_str, jql_to_str, project_keys, jira_cfg,
        )

        issue_histories: dict = {}
        for key in candidate_keys:
            await asyncio.sleep(0.1)  # polite throttle
            histories = await jira_client.fetch_issue_changelog(
                jira_cfg["site_url"], jira_cfg["email"], jira_cfg["api_token"], key,
            )
            comments = await _fetch_all_issue_comments(
                jira_cfg["site_url"], jira_cfg["email"], jira_cfg["api_token"], key,
            )
            issue_histories[key] = (histories, comments)

        all_rows: list = []
        id_to_name = {
            aid: display_names.get(aid) or f"[{aid[:8]}…]"
            for aid in account_ids
        }
        for account_id in account_ids:
            all_rows.extend(_extract_user_events(
                account_id, id_to_name[account_id],
                issue_histories, from_date, to_date, jira_cfg["site_url"],
            ))

        for account_id in account_ids:
            created = await _fetch_user_created_issues(
                account_id, id_to_name[account_id],
                from_date, to_date, jql_from_str, jql_to_str,
                project_clause, jira_cfg, None,
            )
            all_rows.extend(created)

        final_rows = feed_parser.dedupe_and_sort(all_rows)
        _save_rows_as_csv(final_rows, ran_at)

        schedule_store.save_last_run(
            ran_at=ran_at, rows=len(final_rows), ok=True,
        )

    except Exception as exc:
        schedule_store.save_last_run(
            ran_at=ran_at, rows=0, ok=False, error=str(exc),
        )


def _save_rows_as_csv(rows: list, ran_at: str) -> None:
    """
    Write report rows to reports/tickets_touched_YYYY-MM-DD_HH-MM.csv atomically.
    Each run gets its own file. Writes to a .tmp first then renames so the file
    is either complete or absent, never partially written.
    """
    REPORTS_DIR.mkdir(exist_ok=True)
    # ran_at is "2026-03-30T07:00:00" — convert to "2026-03-30_07-00" for the filename
    file_ts  = ran_at[:16].replace("T", "_").replace(":", "-")
    out_path = REPORTS_DIR / f"tickets_touched_{file_ts}.csv"
    tmp_path = out_path.with_suffix(".tmp")
    headers  = ["Timestamp", "User", "Issue Key", "Action Type",
                "Details", "Project", "Issue URL"]
    buf = io.StringIO()
    writer = csv.writer(buf, quoting=csv.QUOTE_ALL)
    writer.writerow(headers)
    for row in rows:
        writer.writerow([
            row.timestamp, row.user, row.issue_key,
            row.action_type, row.details, row.project, row.issue_url,
        ])
    tmp_path.write_text(buf.getvalue(), encoding="utf-8")
    tmp_path.replace(out_path)  # atomic on POSIX; best-effort on Windows


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


def _build_candidate_jql(
    account_id: str,
    jql_from_str: str,
    jql_to_str: str,
    project_keys: Optional[list],
) -> str:
    """
    Build a JQL query using updatedBy() to find candidate issues for one user.

    updatedBy(user, dateFrom, dateTo) is an Atlassian-documented Jira Cloud JQL
    function that matches issues where the specified user:
      - created the issue
      - updated any field
      - created, edited, or deleted a comment
    This covers all action types this app audits.

    Day-granular precision only: jql_from_str / jql_to_str are already padded by
    ±1 day so boundary events are not missed at the JQL level.  Python-side
    _row_in_window performs the exact timestamp inclusion check.

    With project filter:   project in ("KAN", "OPS") AND issuekey in updatedBy(...)
    Without project filter: issuekey in updatedBy(...)
    """
    upd = f'issuekey in updatedBy("{account_id}", "{jql_from_str}", "{jql_to_str}")'
    if project_keys:
        quoted = ", ".join(f'"{k}"' for k in project_keys)
        return f'project in ({quoted}) AND {upd} ORDER BY updated DESC'
    return f'{upd} ORDER BY updated DESC'


async def _paginate_issue_keys(
    jql: str, cfg: dict, job, progress_pct: int
) -> list:
    """
    Generic JQL paginator — returns a deduped list of issue keys from a JQL query.
    Used by Phase 0 for each user's updatedBy() candidate scan.
    """
    site_url    = cfg["site_url"]
    email       = cfg["email"]
    api_token   = cfg["api_token"]
    max_results = 50
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
            job.update(f"⚠ Search error: {exc}", progress_pct)
            break

        issues = data.get("issues") or []
        if not issues:
            break
        page += 1
        for iss in issues:
            k = iss.get("key")
            if k:
                issue_keys.append(k)

        job.update(f"page {page} · {len(issue_keys)} found…", progress_pct)
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


async def _collect_candidate_keys(
    account_ids: list,
    jql_from_str: str,
    jql_to_str: str,
    project_keys: Optional[list],
    cfg: dict,
) -> list:
    """
    Job-free version of Phase 0 for the scheduler (no progress updates needed).
    Runs one updatedBy() JQL per user, unions and dedupes the result.
    """
    site_url    = cfg["site_url"]
    email       = cfg["email"]
    api_token   = cfg["api_token"]
    max_results = 50
    seen: set   = set()
    unique: list = []

    for account_id in account_ids:
        jql        = _build_candidate_jql(account_id, jql_from_str, jql_to_str, project_keys)
        next_token: Optional[str] = None
        while True:
            try:
                data = await jira_client.search_issues_page(
                    site_url, email, api_token, jql, next_token, max_results,
                )
            except JiraAuthError:
                raise
            except Exception:
                break
            issues = data.get("issues") or []
            for iss in issues:
                k = iss.get("key")
                if k and k not in seen:
                    seen.add(k)
                    unique.append(k)
            next_token = data.get("nextPageToken")
            if not next_token or len(issues) < max_results:
                break

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
