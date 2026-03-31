# Jira API Design — Tickets Touched Report

This document describes every Jira Cloud REST API call the app makes, the
pagination strategy for each, the two-pass filtering design, action
classification logic, ADF parsing, and timezone handling. It is intended for
anyone who wants to reproduce the pipeline in a standalone service, a different
language, or a different framework.

---

## Contents

1. [Authentication](#1-authentication)
2. [API Overview](#2-api-overview)
3. [Pipeline Phases](#3-pipeline-phases)
   - [Phase 0 — Candidate key collection (updatedBy JQL)](#phase-0--candidate-key-collection)
   - [Phase 1 — Per-issue changelog fetch](#phase-1--per-issue-changelog-fetch)
   - [Phase 1b — In-memory event extraction](#phase-1b--in-memory-event-extraction)
   - [Phase 2 — Created-issues scan](#phase-2--created-issues-scan)
4. [Timezone and Date Window Handling](#4-timezone-and-date-window-handling)
5. [Action Classification](#5-action-classification)
6. [Details Formatting](#6-details-formatting)
7. [ADF Text Extraction](#7-adf-text-extraction)
8. [Timestamp Normalisation](#8-timestamp-normalisation)
9. [Deduplication and Sort](#9-deduplication-and-sort)
10. [Why Not the Activity Stream](#10-why-not-the-activity-stream)

---

## 1. Authentication

Jira Cloud REST APIs under API-token auth use **HTTP Basic authentication**:

```
Authorization: Basic base64(email:api_token)
```

The token alone (without the email prefix) returns `401`. Always encode
`email:api_token` as a single base64 string.

**Scope required**: The API token must belong to an account with at least
read access to the projects being audited. No special admin scopes are needed
for the calls this app makes.

**Credential verification** (`GET /rest/api/3/myself`): Called on every page
load to confirm that saved credentials are still valid. Returns `accountId`
and `displayName`. A `401` means the token has been revoked; a `403` means
insufficient permissions.

```
GET https://<site>.atlassian.net/rest/api/3/myself
Authorization: Basic <base64(email:token)>
Accept: application/json
```

Response (relevant fields):

```json
{
  "accountId":   "5f3e...",
  "displayName": "Alice Smith",
  "emailAddress": "alice@example.com"
}
```

---

## 2. API Overview

| Endpoint | Method | Purpose |
|---|---|---|
| `/rest/api/3/myself` | GET | Verify credentials, get accountId / displayName |
| `/rest/api/3/user/search` | GET | Search users by display name or email fragment |
| `/rest/api/3/search/jql` | GET | JQL issue search (Phase 0 candidate scan and Phase 2 created scan) |
| `/rest/api/3/issue/{key}/changelog` | GET | Complete field-change history for one issue |
| `/rest/api/3/issue/{key}/comment` | GET | All comments for one issue |

---

## 3. Pipeline Phases

### Phase 0 — Candidate Key Collection

**Goal**: Produce a deduplicated list of issue keys that at least one of the
selected users touched during the date window.

**Why per-user**: A single broad JQL like `updated >= X AND updated <= Y`
returns every issue updated in the window regardless of who updated it. On a
large Jira instance that can be tens of thousands of issues. Filtering
per-user with `updatedBy()` dramatically reduces the candidate set.

#### The `updatedBy()` JQL function

`updatedBy(accountId, dateFrom, dateTo)` is an Atlassian-documented Jira
Cloud JQL function. It matches issues where the specified user performed any
of:

- Created the issue
- Updated any field
- Created, edited, or deleted a comment

**Day-granular precision**: The function interprets dates at day granularity
in the Jira site's configured timezone, not the caller's timezone. See
[Section 4](#4-timezone-and-date-window-handling) for how the ±1 day padding
compensates for this.

**JQL template (without project filter)**:

```
issuekey in updatedBy("<accountId>", "<YYYY-MM-DD>", "<YYYY-MM-DD>")
ORDER BY updated DESC
```

**JQL template (with project filter)**:

```
project in ("KAN", "OPS") AND
issuekey in updatedBy("<accountId>", "<YYYY-MM-DD>", "<YYYY-MM-DD>")
ORDER BY updated DESC
```

Project keys are comma-joined and quoted. Jira project keys are
case-insensitive but conventionally uppercase.

One query is run per selected user. Results are unioned (deduplicated by
issue key, preserving insertion order).

#### Pagination — cursor-based (`nextPageToken`)

The `/rest/api/3/search/jql` endpoint uses cursor-based pagination, not
offset-based. Do **not** use `startAt` for this endpoint — it is deprecated
for the `/search/jql` variant.

**Request**:

```
GET /rest/api/3/search/jql
  ?jql=<url-encoded JQL>
  &maxResults=50
  &fields=summary
  [&nextPageToken=<token from previous response>]
Authorization: Basic ...
Accept: application/json
```

Only `summary` is fetched at this stage — the full issue fields are not
needed for a key-only scan.

**Response** (relevant fields):

```json
{
  "issues": [
    { "key": "KAN-42", "fields": { "summary": "Fix login bug" } }
  ],
  "nextPageToken": "eyJhbGciOi...",
  "total": 137
}
```

**Termination conditions** (either stops pagination):
- `nextPageToken` is absent or null
- `issues` is empty
- `issues` count < `maxResults` (partial page = last page)

`maxResults` of 50 is used here. Values up to 100 are supported by the API.

---

### Phase 1 — Per-Issue Changelog Fetch

**Goal**: For every candidate key, retrieve the complete changelog so that
event extraction can be done in memory without further API calls per user.

Fetching changelogs once per issue (not once per user per issue) is the key
optimisation for multi-user reports: N users × M issues → M API calls instead
of N×M.

#### Changelog endpoint

```
GET /rest/api/3/issue/{key}/changelog
  ?startAt=<offset>
  &maxResults=100
Authorization: Basic ...
Accept: application/json
```

**Response**:

```json
{
  "values": [
    {
      "created": "2026-03-25T14:32:11.000+0000",
      "author": {
        "accountId": "5f3e...",
        "displayName": "Alice Smith"
      },
      "items": [
        {
          "field": "status",
          "fieldtype": "jira",
          "from": "10000",
          "fromString": "To Do",
          "to": "10001",
          "toString": "In Progress"
        }
      ]
    }
  ],
  "isLast": false,
  "startAt": 0,
  "maxResults": 100,
  "total": 214
}
```

**Pagination**: This endpoint uses offset-based pagination (`startAt`).
Increment `startAt` by the number of values returned until `isLast` is `true`
or the returned `values` array is empty.

**Per-item fields used**:

| Field | Description |
|---|---|
| `field` | Internal field name (lowercase), e.g. `"status"`, `"assignee"`, `"timespent"` |
| `fromString` | Human-readable previous value |
| `toString` | Human-readable new value |
| `from` | Internal ID of previous value (used rarely) |
| `to` | Internal ID of new value (used rarely) |

The `resolution` field is filtered out — it always appears paired with a
`status` change and adds noise.

#### Comments endpoint

```
GET /rest/api/3/issue/{key}/comment
  ?startAt=<offset>
  &maxResults=100
  &orderBy=created
Authorization: Basic ...
Accept: application/json
```

**Response**:

```json
{
  "comments": [
    {
      "id": "10010",
      "created": "2026-03-25T09:15:00.000+0000",
      "updated": "2026-03-25T09:15:00.000+0000",
      "author": {
        "accountId": "5f3e...",
        "displayName": "Alice Smith"
      },
      "body": {
        "type": "doc",
        "version": 1,
        "content": [ ... ]
      }
    }
  ],
  "total": 12,
  "startAt": 0,
  "maxResults": 100
}
```

**Pagination**: Offset-based. Continue while
`startAt + comments.length < total` and `comments.length == maxResults`.

The `body` field is Atlassian Document Format (ADF) — see
[Section 7](#7-adf-text-extraction) for extraction.

**Error handling**: Errors for individual issues (404, network, etc.) are
silently swallowed — the issue is treated as having an empty history. This
prevents one inaccessible issue from aborting the entire report.

---

### Phase 1b — In-Memory Event Extraction

No API calls. For each issue key, the pre-fetched `(changelog, comments)` pair
is scanned for events matching `accountId` within `[from_date, to_date)`.

**Changelog matching**:

```python
for history in changelog:
    if history["author"]["accountId"] != account_id:
        continue
    ts = normalize_timestamp(history["created"])
    if not row_in_window(ts, from_date, to_date):
        continue
    items = [i for i in history["items"] if i["field"].lower() != "resolution"]
    if not items:
        continue
    emit row(
        timestamp  = ts,
        user       = display_name,
        issue_key  = key,
        action_type = classify_action(items),
        details    = format_items(items),
    )
```

**Comment matching**:

```python
for comment in comments:
    if comment["author"]["accountId"] != account_id:
        continue
    ts = normalize_timestamp(comment["created"])
    if not row_in_window(ts, from_date, to_date):
        continue
    body_text = extract_adf_text(comment["body"])
    preview   = body_text[:150] + ("…" if len(body_text) > 150 else "")
    emit row(
        timestamp   = ts,
        action_type = "commented",
        details     = f"Comment: {preview}",
    )
```

---

### Phase 2 — Created-Issues Scan

**Goal**: Capture issues created by each user in the window. Issue creation
is not always present in the changelog (it is for older API versions but is
unreliable in Jira Cloud's changelog endpoint). A dedicated JQL query is more
reliable.

**JQL template**:

```
reporter = "<accountId>"
AND created >= "<jql_from>"
AND created <= "<jql_to>"
[AND project in ("KAN", "OPS")]
ORDER BY created DESC
```

`reporter` is the field that records who created the issue. This is
user-specific so it runs once per selected user.

**Fields fetched**: `summary`, `created`

**Endpoint and pagination**: Same as Phase 0 — `GET /rest/api/3/search/jql`
with cursor-based `nextPageToken` pagination.

**Window check**: The `created` timestamp is checked with `_row_in_window`
after fetch (same as Phase 1b) — JQL padding may include issues just outside
the true window.

---

## 4. Timezone and Date Window Handling

### The problem

Jira stores all timestamps in UTC. JQL date predicates like `updated >=
"2026-03-25"` are evaluated in the **Jira site's configured timezone**, not
the caller's timezone. If your site is set to UTC-5 and you're in UTC-8, a
`>= "2026-03-25"` JQL clause may miss events you expect to be included.

The `updatedBy()` function has the same day-granular, site-timezone limitation.

### The solution — two layers of filtering

**Layer 1 — JQL padding (coarse)**

The JQL date strings passed to `updatedBy()` and `reporter =` queries are
padded by **±1 day**:

```python
jql_from = (from_date - timedelta(days=1)).strftime("%Y-%m-%d")
jql_to   = (to_date   + timedelta(days=1)).strftime("%Y-%m-%d")
```

This ensures events at day boundaries are never missed regardless of how Jira
interprets the dates relative to its site timezone.

**Layer 2 — Python timestamp filter (exact)**

Every candidate row's exact UTC timestamp is checked against the precise
window before it is included in results:

```python
def row_in_window(timestamp_str, from_date, to_date):
    dt = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
    return from_date <= dt < to_date   # inclusive start, exclusive end
```

This is the authoritative filter. JQL is only the coarse pre-scan.

### Local-day to UTC conversion

The frontend sends `tz_offset_minutes` — the value of
`new Date().getTimezoneOffset()` in the browser. This is positive for
UTC-behind zones (e.g. EDT = UTC-4 → +240) and negative for UTC-ahead
zones (e.g. IST = UTC+5:30 → -330).

```python
def local_midnight_utc(date_str, tz_offset_minutes):
    naive_midnight = datetime.fromisoformat(date_str)   # "2026-03-25" → 00:00 naive
    utc_midnight   = naive_midnight + timedelta(minutes=tz_offset_minutes)
    return utc_midnight.replace(tzinfo=timezone.utc)
```

Examples:

| Zone | Offset | Local 2026-03-25 00:00 → UTC |
|---|---|---|
| EDT (UTC-4) | +240 | 2026-03-25T04:00:00Z |
| PST (UTC-8) | +480 | 2026-03-25T08:00:00Z |
| IST (UTC+5:30) | -330 | 2026-03-24T18:30:00Z |
| UTC | 0 | 2026-03-25T00:00:00Z |

The window used for filtering is:
- `from_date` = UTC midnight of the start date in the user's timezone
- `to_date` = UTC midnight of the day *after* the end date (exclusive upper bound)

This means an event at 23:59 local on the last selected day is correctly
included.

---

## 5. Action Classification

Each changelog history entry contains one or more `items`. The following
precedence table maps field names to action types:

| Priority | Field name(s) | Action type |
|---|---|---|
| 1 (highest) | `status` | `status_change` |
| 2 | `assignee` | `assigned` |
| 3 | `attachment` | `attachment` |
| 4 | `link`, `issuelinks` | `linked` |
| 5 | `timespent`, `worklogid`, `timeestimate`, `remainingestimate`, `timeoriginalestimate` | `logged_work` |
| 6 (default) | anything else | `updated` |

Worklog fields are checked lazily — the loop continues past them in case a
higher-priority field appears later in the same items list. All other
classifications return immediately on first match.

Comments are always classified as `commented` — they do not go through this
classification logic.

Issue creation (Phase 2) is always classified as `created`.

---

## 6. Details Formatting

The `details` string shown in the table is built from `changelog.items`.
The `resolution` field is always suppressed — it is always paired with a
`status` transition and adds noise. Field-specific formatting:

| Field | Format |
|---|---|
| `status` | `Status: {fromString} → {toString}` |
| `assignee` | `Assignee → {toString}` (or `Unassigned`) |
| `attachment` | `Attachment: {filename}` |
| `link` / `issuelinks` | `Linked: {toString}` |
| `duedate` | `Due Date → {YYYY-MM-DD}` (first 10 chars of toString) |
| `description` | `Description updated` (body not shown) |
| `summary` | `Summary → {first 80 chars}` |
| `priority` | `Priority → {toString}` |
| `story points` / `customfield_10016` | `Story Points → {toString}` |
| `labels` | `Labels → {toString}` |
| `sprint` | `Sprint → {toString}` |
| `issueparentassociation` | `Parent → {toString}` |
| worklog fields | `{Field Title} → {toString}` |
| anything else | `{Field Title} → {first 80 chars}` or `{Field Title} cleared` |

Multiple items in a single changelog entry are joined with `, `.

---

## 7. ADF Text Extraction

Jira Cloud returns comment bodies (and some other rich-text fields) as
**Atlassian Document Format (ADF)** — a JSON tree, not a plain string.

The recursive extraction algorithm:

```python
def extract_adf_text(node):
    if not isinstance(node, dict):
        return ""
    if node["type"] == "text":
        return node.get("text", "")
    children = node.get("content") or []
    parts = [extract_adf_text(c) for c in children]
    sep = " " if node["type"] in {
        "paragraph", "heading", "blockquote", "codeBlock",
        "bulletList", "orderedList", "listItem", "panel",
    } else ""
    return sep.join(p for p in parts if p)
```

Block-level nodes (paragraphs, headings, list items, etc.) are joined with a
space so the result reads as a single flat string. Inline nodes (marks, links,
etc.) are concatenated without a separator.

The extracted text is truncated to 150 characters for the Details column
preview.

**ADF node structure** (simplified):

```json
{
  "type": "doc",
  "version": 1,
  "content": [
    {
      "type": "paragraph",
      "content": [
        { "type": "text", "text": "This is " },
        { "type": "text", "text": "bold", "marks": [{ "type": "strong" }] }
      ]
    }
  ]
}
```

Marks (bold, italic, code, link, etc.) are ignored by the extraction — only
`type: "text"` leaf nodes contribute their `text` value.

---

## 8. Timestamp Normalisation

Jira returns timestamps in ISO-8601 format with timezone offsets. Python's
`datetime.fromisoformat()` in versions ≤3.10 does not accept offsets without
a colon separator (e.g. `-0400` instead of `-04:00`). Normalisation:

```python
import re

def normalize_ts(iso_str):
    # Insert colon in timezone offset if missing: "-0400" → "-04:00"
    fixed = re.sub(r'([+-])(\d{2})(\d{2})$', r'\1\2:\3', iso_str)
    dt    = datetime.fromisoformat(fixed)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
```

All stored timestamps are normalised to UTC `Z` format. The frontend converts
them to local time using `new Date(iso).toLocaleString()`.

---

## 9. Deduplication and Sort

Before returning results, all rows from all phases are deduped and sorted:

**Dedupe key**:
```
(timestamp, user, issue_key, action_type, details)
```

Including `details` ensures two distinct field changes at the same second on
the same issue (common in bulk edits) are kept as separate rows rather than
collapsed.

**Sort**: Newest first by UTC timestamp. Invalid timestamps sort to the end.

---

## 10. Why Not the Activity Stream

The Jira Activity Stream (`/activity`) was evaluated and rejected:

| Problem | Detail |
|---|---|
| Incomplete by design | Server-side entry cap — cannot return all activity for a window |
| Unreliable pagination | `endDate` cursor is silently ignored; causes truncated results or infinite loops |
| No field-level data | Reports "user updated KAN-42"; no `Status: To Do → In Progress` detail |
| User filter unsupported | `account-id+IS+{id}` filter is undocumented under API token auth in Jira Cloud |

The REST changelog + JQL approach gives complete, authoritative, field-level
history for every action type the app audits.

---

## Reproducing the Pipeline in Another Language

The minimum set of operations needed:

1. **Auth**: `base64(email + ":" + api_token)` → `Authorization: Basic <value>`
2. **User search**: `GET /rest/api/3/user/search?query=<q>&maxResults=20` → array of `{accountId, displayName, avatarUrls}`
3. **Candidate scan**: For each user, paginate `GET /rest/api/3/search/jql?jql=issuekey+in+updatedBy(...)&fields=summary` using `nextPageToken`; union keys
4. **Changelog**: For each candidate key, paginate `GET /rest/api/3/issue/{key}/changelog?startAt=N&maxResults=100` using `isLast` + `startAt` offset
5. **Comments**: For each candidate key, paginate `GET /rest/api/3/issue/{key}/comment?startAt=N&maxResults=100&orderBy=created` using `total` + `startAt`
6. **Created scan**: For each user, paginate `GET /rest/api/3/search/jql?jql=reporter="<id>"+AND+created>=...` using `nextPageToken`
7. **Filter**: Check each event's exact UTC timestamp against `[from_date, to_date)` — JQL dates are padded ±1 day as a coarse pre-filter only
8. **Classify**: Apply the precedence table in [Section 5](#5-action-classification)
9. **Dedupe and sort**: Dedupe on `(timestamp, user, key, action_type, details)`; sort newest-first
