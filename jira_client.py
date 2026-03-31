"""
jira_client.py — Jira Cloud REST API helpers.

All public functions are async so they can be awaited from FastAPI route
handlers without blocking the event loop.
"""

import asyncio
import base64
import httpx
from typing import List, Optional
from models import UserItem

# Maximum number of retry attempts when Jira returns 429 Too Many Requests.
_MAX_RETRIES = 4
# Default back-off seconds if Jira doesn't send a Retry-After header.
_DEFAULT_BACKOFF = 5.0


async def _with_retry(make_request):
    """
    Call make_request() (an async callable that returns an httpx.Response),
    retrying on 429 with exponential back-off.

    Jira Cloud rate-limits API tokens and returns:
      HTTP 429  +  Retry-After: <seconds>   (honour the header)
      HTTP 429  (no header)                 (fall back to _DEFAULT_BACKOFF, doubled each retry)

    Raises the last httpx exception if all retries are exhausted.
    Re-raises JiraAuthError / ValueError immediately (no retry for auth failures).
    """
    backoff = _DEFAULT_BACKOFF
    for attempt in range(_MAX_RETRIES + 1):
        resp = await make_request()
        if resp.status_code != 429:
            return resp
        if attempt == _MAX_RETRIES:
            return resp   # caller will handle the non-2xx status
        retry_after = resp.headers.get("Retry-After")
        wait = float(retry_after) if retry_after else backoff
        await asyncio.sleep(wait)
        backoff = min(backoff * 2, 60.0)   # cap at 60 s
    return resp  # unreachable but satisfies type checker


class JiraAuthError(ValueError):
    """
    Raised when Jira returns HTTP 401 (invalid/expired credentials) or
    HTTP 403 (valid credentials but insufficient permissions).
    Distinct from ValueError so callers can surface a targeted auth-failure
    message rather than a generic error.
    """


def _basic_auth_header(email: str, api_token: str) -> str:
    """
    Jira Cloud requires Basic auth encoded as base64(email:api_token).
    A bare token (without email) will return 401.
    """
    credentials = f"{email}:{api_token}"
    encoded = base64.b64encode(credentials.encode()).decode()
    return f"Basic {encoded}"


async def verify_credentials(site_url: str, email: str, api_token: str) -> dict:
    """
    Call GET /rest/api/3/myself to confirm the credentials are valid.

    Returns a dict with keys: display_name, account_id.
    Raises ValueError with a human-readable message on any failure.
    """
    url = f"{site_url.rstrip('/')}/rest/api/3/myself"
    headers = {
        "Authorization": _basic_auth_header(email, api_token),
        "Accept": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await _with_retry(lambda: client.get(url, headers=headers))
    except httpx.ConnectError:
        raise ValueError(f"Could not connect to {site_url} — check the site URL")
    except httpx.TimeoutException:
        raise ValueError(f"Request to {site_url} timed out")

    if resp.status_code == 401:
        raise ValueError("Invalid credentials — check your email and API token")
    if resp.status_code == 403:
        raise ValueError("Access denied — the API token may not have sufficient scope")
    if not resp.is_success:
        raise ValueError(f"Jira returned HTTP {resp.status_code}")

    try:
        data = resp.json()
    except Exception:
        raise ValueError("Jira returned an unexpected response (not JSON)")

    display_name = data.get("displayName") or ""
    account_id   = data.get("accountId")  or ""
    if not account_id:
        raise ValueError("Jira response missing accountId — unexpected API format")

    return {"display_name": display_name, "account_id": account_id}


async def search_users(site_url: str, email: str, api_token: str, query: str) -> List[UserItem]:
    """
    Search Jira users by display name / email fragment.

    Calls GET /rest/api/3/user/search?query=<query>
    Returns a list of UserItem objects.
    Raises ValueError on auth failures or network errors so callers can surface
    a real error message rather than silently returning an empty list.
    """
    url = f"{site_url.rstrip('/')}/rest/api/3/user/search"
    headers = {
        "Authorization": _basic_auth_header(email, api_token),
        "Accept": "application/json",
    }
    params = {"query": query, "maxResults": 20}

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await _with_retry(lambda: client.get(url, headers=headers, params=params))
    except httpx.ConnectError:
        raise ValueError(f"Cannot connect to {site_url}")
    except httpx.TimeoutException:
        raise ValueError("User search timed out")
    except Exception as exc:
        raise ValueError(f"User search request failed: {exc}") from exc

    if resp.status_code == 401:
        raise JiraAuthError("Credentials rejected — your API token may be invalid or revoked")
    if resp.status_code == 403:
        raise JiraAuthError("Permission denied searching Jira users — check your token's scopes")
    if not resp.is_success:
        raise ValueError(f"Jira user search returned HTTP {resp.status_code}")

    try:
        data = resp.json()
    except Exception:
        raise ValueError("Unexpected response from Jira user search (not JSON)")

    # Guard: Jira should return a list
    if not isinstance(data, list):
        return []

    items: List[UserItem] = []
    for user in data:
        if not isinstance(user, dict):
            continue
        display_name = user.get("displayName") or ""
        account_id   = user.get("accountId")   or ""
        if not display_name or not account_id:
            continue

        avatar_urls = user.get("avatarUrls") or {}
        if isinstance(avatar_urls, dict):
            avatar_url: Optional[str] = (
                avatar_urls.get("48x48") or
                avatar_urls.get("32x32") or
                next((v for v in avatar_urls.values() if v), None)
            )
        else:
            avatar_url = None

        items.append(UserItem(
            account_id=account_id,
            display_name=display_name,
            avatar_url=avatar_url,
        ))
    return items


async def search_issues_page(
    site_url: str, email: str, api_token: str,
    jql: str, next_page_token: Optional[str] = None, max_results: int = 50,
) -> dict:
    """
    Single page of JQL search results using /rest/api/3/search/jql.
    Uses cursor-based pagination via nextPageToken.
    Returns raw Jira JSON including 'issues' and 'nextPageToken'.
    """
    url = f"{site_url.rstrip('/')}/rest/api/3/search/jql"
    headers = {
        "Authorization": _basic_auth_header(email, api_token),
        "Accept": "application/json",
    }
    # Only fetch the issue key — changelog and comments are fetched per-issue
    params = [
        ("jql", jql),
        ("maxResults", str(max_results)),
        ("fields", "summary"),
    ]
    if next_page_token:
        params.append(("nextPageToken", next_page_token))
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await _with_retry(lambda: client.get(url, headers=headers, params=params))
        if resp.status_code in (401, 403):
            raise JiraAuthError(
                f"Jira credentials rejected during issue search (HTTP {resp.status_code})"
            )
        if not resp.is_success:
            body = resp.text[:500]
            raise ValueError(f"Jira search HTTP {resp.status_code}: {body}")
        return resp.json()
    except (ValueError, JiraAuthError):
        raise
    except Exception as exc:
        raise ValueError(f"Jira search request failed: {exc}") from exc


async def search_created_issues_page(
    site_url: str, email: str, api_token: str,
    jql: str, next_page_token: Optional[str] = None, max_results: int = 50,
) -> dict:
    """Search for issues created by a user (just summary + created timestamp)."""
    url = f"{site_url.rstrip('/')}/rest/api/3/search/jql"
    headers = {
        "Authorization": _basic_auth_header(email, api_token),
        "Accept": "application/json",
    }
    params = [
        ("jql", jql),
        ("maxResults", str(max_results)),
        ("fields", "summary"),
        ("fields", "created"),
    ]
    if next_page_token:
        params.append(("nextPageToken", next_page_token))
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await _with_retry(lambda: client.get(url, headers=headers, params=params))
        if resp.status_code in (401, 403):
            raise JiraAuthError(
                f"Jira credentials rejected during created-issues search (HTTP {resp.status_code})"
            )
        if not resp.is_success:
            raise ValueError(f"HTTP {resp.status_code}")
        return resp.json()
    except (ValueError, JiraAuthError):
        raise
    except Exception as exc:
        raise ValueError(f"Created-issues search failed: {exc}") from exc


async def fetch_issue_comments(
    site_url: str, email: str, api_token: str,
    issue_key: str, start_at: int = 0, max_results: int = 100,
) -> dict:
    """
    Fetch a page of comments for a specific issue.
    Returns raw Jira JSON: {total, startAt, maxResults, comments: [...]}
    """
    url = f"{site_url.rstrip('/')}/rest/api/3/issue/{issue_key}/comment"
    headers = {
        "Authorization": _basic_auth_header(email, api_token),
        "Accept": "application/json",
    }
    params = {"startAt": start_at, "maxResults": max_results, "orderBy": "created"}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await _with_retry(lambda: client.get(url, headers=headers, params=params))
        if resp.status_code in (401, 403):
            raise JiraAuthError(
                f"Jira credentials rejected fetching comments for {issue_key} (HTTP {resp.status_code})"
            )
        resp.raise_for_status()
        return resp.json()
    except JiraAuthError:
        raise
    except Exception:
        return {"total": 0, "comments": []}


async def fetch_issue_changelog(
    site_url: str, email: str, api_token: str, issue_key: str
) -> list:
    """
    Fetch the complete changelog for an issue via /rest/api/3/issue/{key}/changelog.
    Paginates until all history entries are retrieved.
    Returns a list of history dicts: [{created, author, items: [...]}]
    Never raises — returns [] on error.
    """
    url = f"{site_url.rstrip('/')}/rest/api/3/issue/{issue_key}/changelog"
    headers = {
        "Authorization": _basic_auth_header(email, api_token),
        "Accept": "application/json",
    }
    histories = []
    start = 0
    while True:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await _with_retry(
                    lambda: client.get(
                        url, headers=headers,
                        params={"startAt": start, "maxResults": 100},
                    )
                )
            if resp.status_code in (401, 403):
                raise JiraAuthError(
                    f"Jira credentials rejected fetching changelog for {issue_key} (HTTP {resp.status_code})"
                )
            resp.raise_for_status()
            data = resp.json()
        except JiraAuthError:
            raise
        except Exception:
            break
        values = data.get("values") or []
        histories.extend(values)
        if data.get("isLast", True) or len(values) == 0:
            break
        start += len(values)
    return histories
