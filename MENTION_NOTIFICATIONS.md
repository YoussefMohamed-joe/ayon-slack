# Slack Addon – Mention Notifications (All-in-One Doc)

## 1. Overview

Automatically send Slack DMs when users are @mentioned in Ayon comments, with:
- **Zero UI freeze** (all work in background)
- **Clean messages** (no `Location: Task: Unknown`)
- **Clickable “Open in Ayon”** links
- **Per‑project enable/disable** and a **separate auth token** from publish

This document replaces all previous mention‑related docs (`CHANGELOG.md`, `CHANGES_SUMMARY.md`, `CODE_SIMPLIFICATIONS.md`, `PRODUCTION_VERIFICATION.md`).

---

## 2. Setup

### 2.1 Get Slack Bot Token
1. Go to `https://api.slack.com/apps`
2. Select your app → **OAuth & Permissions**
3. Copy **Bot User OAuth Token** (starts with `xoxb-`)

### 2.2 Configure in Ayon (per project)
1. Open **Project Settings → Slack → Mention notifications → Mention Notifications**
2. Fields:
   - **Enabled**: turn ON to activate for this project
   - **Auth Token**: paste your `xoxb-` token (can be same or different from publish token)
   - **Ayon Server URL**: e.g. `http://127.0.0.1:5003`
   - **User Mappings**:
     - `Ayon User`: `admin`
     - `Slack User ID`: `U0A67LHQXLP`

**Slack User ID:** in Slack, open user profile → **More** → **Copy member ID**.

### 2.3 Enable/Disable Behaviour
- If **Enabled = True** in project settings → mentions are processed.
- If **Enabled = False** in project settings → **no processing at all**:
  - No DB queries
  - No Slack calls
  - No background work  
  (Studio settings are **not** used when project has mention config and is disabled.)

---

## 3. How It Works (User View)

### 3.1 Creating a Mention
- Type directly: `@admin all good?`
- Or use Ayon mention UI, which creates `[Admin](user:admin)` in the comment body.

### 3.2 Slack Message Format
Sent via DM to the mentioned user:

```text
@yousef mohammed mentioned you in test

Comment:
@Ayon admin all is okay ?

[Open in Ayon]
```

Only:
- A title line (“mentioned you in *project*”)
- The comment text
- An **Open in Ayon** button  
No “Project:” column, no “Location: Task: Unknown”.

---

## 4. Architecture & Performance

### 4.1 High-level Flow
```text
Ayon comment → event handler → background task → Slack batch worker → Slack
                    ↓                 ↓
             returns instantly      sends later
```

### 4.2 Event Handlers (`server/events.py`)
- Handlers: `handle_notification_event`, `handle_activity_event`, special `inbox.message` path.
- They do **only**:
  - Basic checks (project name present, body contains `@` or `(user:`)
  - Capture a small `raw_event_data` dict
  - `asyncio.create_task(_process_in_background())`
- **No `await`** or slow work before the background task.

### 4.3 Background Tasks
Inside `_process_in_background()` (for notification, inbox, and activity):
- Acquire a global `asyncio.Semaphore(10)` so max 10 mentions process in parallel.
- **First step:** `get_enabled_settings(addon, project_name)`  
  - If project has mention config and `enabled=False` → returns `None` → task returns immediately.
- If enabled:
  - Use `get_mention_plugin_settings(settings)` to extract plugin dict.
  - For activity events: call `get_user_name()` and `get_entity_info()` via Postgres.
  - Call `process_mention_event(...)` in `mention_handler.py`.

### 4.4 Database Access
`server/events.py` uses the modern Postgres pattern:
```python
async with Postgres.acquire() as conn:
    row = await conn.fetchrow(query, entity_id)
```
Only inside background tasks; nothing blocks the main event loop.

### 4.5 Slack Batch System (`server/slack_operations.py`)
- Global in‑memory batch: `_message_batch: list[dict]`
- Background worker thread:
  - Wakes every 10 seconds
  - Takes up to `MAX_BATCH_SIZE` (500) messages
  - Sends them via `requests.post("https://slack.com/api/chat.postMessage", ...)`
  - Retries up to `MAX_RETRIES` (3) with timeout and rate‑limit handling
  - Circuit breaker after too many failures (pauses for `CIRCUIT_BREAKER_TIMEOUT`)
- `SlackOperations.send_message()`:
  - Starts worker if needed (double‑checked with lock)
  - Prepares headers + payload
  - Starts a **detached thread** that appends to `_message_batch`
  - Returns `True` **immediately** (no waiting for locks or HTTP).

### 4.6 Non‑Blocking Timeline
```text
User posts comment       → Comment appears instantly
Background task starts   → DB/settings/Slack work
Slack batch worker       → Sends messages every ~10 seconds
```

---

## 5. Settings & Data Structures

### 5.1 Pydantic Models (`server/settings/main.py`)
Key parts:
```python
class UserMapping(BaseSettingsModel):
    ayon_user: str
    slack_user_id: str

class MentionNotificationsPlugin(BaseSettingsModel):
    enabled: bool
    token: str
    ayon_server_url: str
    user_mappings: list[UserMapping]
    notify_self_mentions: bool

class MentionNotificationsContainer(BaseSettingsModel):
    MentionNotifications: MentionNotificationsPlugin

class SlackSettings(BaseSettingsModel):
    enabled: bool
    token: str            # publish token
    publish: SlackPublishPlugins
    mention_notifications: MentionNotificationsContainer  # mention feature
```

### 5.2 Settings Helpers (`server/settings_utils.py`)
- **`get_mention_plugin_settings(settings)`**  
  Handles both Pydantic objects and plain dicts, returns plugin settings dict.
- **`get_enabled_settings(addon, project_name)`**
  - Project settings:
    - If they have mention config and `enabled=True` → returns project settings.
    - If they have mention config and `enabled=False` → returns `None` (**no studio fallback**).
  - Only if project has **no mention config at all** does it check studio settings.
- **`build_user_mappings(user_mappings_list)`**  
  Converts list of mapping dicts to `{lowercase_ayon_user: slack_user_id}`.

### 5.3 Example JSON Structure
```json
{
  "mention_notifications": {
    "MentionNotifications": {
      "enabled": true,
      "token": "xoxb-your-token",
      "ayon_server_url": "http://127.0.0.1:5003",
      "user_mappings": [
        { "ayon_user": "admin", "slack_user_id": "U0A67LHQXLP" }
      ],
      "notify_self_mentions": false
    }
  }
}
```

---

## 6. Message Content & URL Generation

### 6.1 Building the Ayon URL (`server/mention_handler.py`)
```python
def build_ayon_url(ayon_server_url, project_name, entity_type, entity_id, activity_id=None):
    base = ayon_server_url.rstrip("/")
    url = f"{base}/projects/{project_name}/overview" \
          f"?project={project_name}&type={entity_type}&id={entity_id}"
    if activity_id:
        url += f"&activity={activity_id}"
    return url
```

Matches Ayon’s real UI URLs, e.g.:
`http://127.0.0.1:5003/projects/test/overview?project=test&type=task&id=abc123`

### 6.2 Mention Extraction
```python
def extract_mentions(text: str) -> list[str]:
    # Supports:
    # - [Name](user:username)
    # - @username / @First Last
```

### 6.3 Final Slack Blocks
```python
blocks = [
    {
        "type": "section",
        "text": {"type": "mrkdwn",
                 "text": f"{mentioner_display} mentioned you in *{project_name}*"},
    },
    {
        "type": "section",
        "text": {"type": "mrkdwn",
                 "text": f"*Comment:*\n{clean_body[:500]}"},
    },
    {
        "type": "actions",
        "elements": [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Open in Ayon"},
                "url": ayon_url,
            }
        ],
    },
]
```

---

## 7. Production Readiness Summary

- **UI Blocking**: None (all heavy work in background tasks).
- **Concurrency**: Semaphore limits to 10 concurrent tasks; safe for 100+ users.
- **Slack Limits**: Batched sending every 10s with retry + rate‑limit handling.
- **Failures**:
  - Retries per message
  - Circuit breaker after many failures
  - Errors logged, but user experience remains smooth.
- **Memory Safety**: Batch capped at 500 messages; oldest dropped with warning.
- **Toggle Semantics**:
  - Project **Enabled = False** → absolutely no calculations and no messages.

---

## 8. Troubleshooting & FAQ

- **No DM**:
  - Check token (starts with `xoxb-`, correct workspace, `chat:write` scope).
  - Confirm user mapping exists for the mentioned user.
  - Look for server log entries about “No Slack user ID found”.
- **Still see “Location: Task: Unknown”**:
  - That comes from the **old publish Slack template**, not mention DMs.
  - Ensure the server is actually running **version 1.2.2** of this addon.
- **Disable per project**:
  - Turn OFF **Mention Notifications → Enabled** in project settings; no messages will be sent for that project.

For deeper internals, inspect `server/events.py`, `server/mention_handler.py`, `server/slack_operations.py`, and `server/settings_utils.py` – this document reflects their current behaviour.
