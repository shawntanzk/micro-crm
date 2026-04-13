"""
Research Partnership CRM
A lightweight CRM for tracking research partnership leads.
Multi-user, admin-controlled, SQLite-backed.
"""

import streamlit as st
import json
import os
import sqlite3
import threading
import secrets
import uuid
from datetime import datetime
import pandas as pd
import bcrypt
import plotly.express as px

# ─── Page Config ──────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Research Partnership CRM",
    page_icon="🔬",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ─── WebSocket Keepalive ──────────────────────────────────────────────────────
# Pings the Streamlit health endpoint every 30 s to prevent OpenResty from
# closing the idle WebSocket connection (proxy idle timeout is typically 60 s).
st.components.v1.html(
    """
    <script>
    (function () {
        const INTERVAL_MS = 30000;
        function ping() {
            fetch("/_stcore/health").catch(() => {});
        }
        setInterval(ping, INTERVAL_MS);
    })();
    </script>
    """,
    height=0,
)

# ─── Data Directory ───────────────────────────────────────────────────────────

# On Domino, point DATA_DIR at your persisted dataset mount, e.g.:
#   CRM_DATA_DIR = "/domino/datasets/local/crm_data"
# For local development the sibling `data/` folder is used.
DATA_DIR = os.environ.get(
    "CRM_DATA_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"),
)
os.makedirs(DATA_DIR, exist_ok=True)

DB_PATH = os.path.join(DATA_DIR, "crm.db")

# ─── Constants ────────────────────────────────────────────────────────────────

PARTNERSHIP_STAGES = [
    "Prospect",
    "Initial Contact",
    "Proposal Sent",
    "In Negotiation",
    "Active Partnership",
    "On Hold",
    "Inactive",
]

MEETING_TYPES = [
    "In-Person",
    "Video Call",
    "Phone Call",
    "Email",
    "Conference / Event",
    "Other",
]

DATASET_STATUSES = [
    "Requested",
    "In Transfer",
    "Received",
    "Processing",
    "Integrated",
    "Archived",
]

PROJECT_STATUSES = [
    "Planning",
    "Active",
    "On Hold",
    "Completed",
    "Cancelled",
]

STAGE_COLORS = {
    "Prospect": "#94a3b8",
    "Initial Contact": "#60a5fa",
    "Proposal Sent": "#f59e0b",
    "In Negotiation": "#f97316",
    "Active Partnership": "#22c55e",
    "On Hold": "#a78bfa",
    "Inactive": "#f87171",
}


# ─── Database Helpers ─────────────────────────────────────────────────────────

# One threading lock guards all writes so concurrent Streamlit sessions
# never corrupt the database.  SQLite WAL mode allows concurrent reads.
# A second lock ensures the connection is only initialised once.
_db_write_lock = threading.Lock()
_db_init_lock = threading.Lock()
_db_conn: sqlite3.Connection | None = None


def _get_conn() -> sqlite3.Connection:
    """
    Return the shared SQLite connection, creating and initialising it on the
    first call.  Plain module-level singleton — no Streamlit cache machinery
    required, so it works reliably across all Domino/Streamlit configurations.
    """
    global _db_conn
    if _db_conn is not None:
        return _db_conn
    with _db_init_lock:
        if _db_conn is not None:   # another thread may have set it while we waited
            return _db_conn
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except Exception:
            pass  # WAL unsupported on NFS (Domino datasets); default DELETE mode is fine
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id            TEXT PRIMARY KEY,
            username      TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role          TEXT NOT NULL DEFAULT 'viewer',
            is_active     INTEGER NOT NULL DEFAULT 1,
            created_at    TEXT,
            created_by    TEXT
        );

        CREATE TABLE IF NOT EXISTS contacts (
            id                TEXT PRIMARY KEY,
            name              TEXT NOT NULL,
            institution       TEXT,
            role              TEXT,
            email             TEXT,
            phone             TEXT,
            country           TEXT,
            website           TEXT,
            partnership_stage TEXT,
            notes             TEXT,
            tags              TEXT DEFAULT '[]',
            last_contacted    TEXT,
            created_at        TEXT,
            created_by        TEXT,
            updated_at        TEXT,
            updated_by        TEXT
        );

        CREATE TABLE IF NOT EXISTS meetings (
            id              TEXT PRIMARY KEY,
            contact_ids     TEXT DEFAULT '[]',
            contact_display TEXT,
            date            TEXT,
            meeting_type    TEXT,
            summary         TEXT,
            attendees       TEXT DEFAULT '[]',
            notes           TEXT,
            action_items    TEXT,
            created_at      TEXT,
            created_by      TEXT,
            updated_at      TEXT,
            updated_by      TEXT
        );

        CREATE TABLE IF NOT EXISTS datasets (
            id            TEXT PRIMARY KEY,
            contact_id    TEXT,
            name          TEXT NOT NULL,
            format        TEXT,
            status        TEXT,
            acquired_date TEXT,
            size          TEXT,
            description   TEXT,
            notes         TEXT,
            tags          TEXT DEFAULT '[]',
            created_at    TEXT,
            created_by    TEXT,
            updated_at    TEXT,
            updated_by    TEXT
        );

        CREATE TABLE IF NOT EXISTS stage_history (
            id         TEXT PRIMARY KEY,
            contact_id TEXT NOT NULL,
            old_stage  TEXT,
            new_stage  TEXT,
            changed_at TEXT,
            changed_by TEXT
        );

        CREATE TABLE IF NOT EXISTS project_stage_history (
            id         TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            old_stage  TEXT,
            new_stage  TEXT,
            changed_at TEXT,
            changed_by TEXT
        );

        CREATE TABLE IF NOT EXISTS dataset_status_history (
            id         TEXT PRIMARY KEY,
            dataset_id TEXT NOT NULL,
            old_status TEXT,
            new_status TEXT,
            changed_at TEXT,
            changed_by TEXT
        );

        CREATE TABLE IF NOT EXISTS projects (
            id              TEXT PRIMARY KEY,
            name            TEXT NOT NULL,
            description     TEXT,
            status          TEXT,
            main_contact_id TEXT NOT NULL,
            dataset_ids     TEXT DEFAULT '[]',
            nda_signed      INTEGER DEFAULT 0,
            notes           TEXT,
            tags            TEXT DEFAULT '[]',
            created_at      TEXT,
            created_by      TEXT,
            updated_at      TEXT,
            updated_by      TEXT
        );
        """
        )
        conn.commit()
        # Migration: add recovery_code_hash if this is an existing DB without it
        try:
            conn.execute("ALTER TABLE users ADD COLUMN recovery_code_hash TEXT")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # column already exists
        # Migration: add tags to datasets
        try:
            conn.execute("ALTER TABLE datasets ADD COLUMN tags TEXT DEFAULT '[]'")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # column already exists
        # Migration: add tags to meetings
        try:
            conn.execute("ALTER TABLE meetings ADD COLUMN tags TEXT DEFAULT '[]'")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # column already exists
        # Migration: add project_id to meetings
        try:
            conn.execute("ALTER TABLE meetings ADD COLUMN project_id TEXT")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # column already exists
        # Migration: add dataset_ids to projects
        try:
            conn.execute("ALTER TABLE projects ADD COLUMN dataset_ids TEXT DEFAULT '[]'")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # column already exists
        # Migration: add last_pinged to contacts
        try:
            conn.execute("ALTER TABLE contacts ADD COLUMN last_pinged TEXT")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # column already exists
        # Migration: add attendee_usernames to meetings
        try:
            conn.execute("ALTER TABLE meetings ADD COLUMN attendee_usernames TEXT DEFAULT '[]'")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # column already exists
        _db_conn = conn
    return _db_conn


def _db_write(sql: str, params: tuple = ()) -> None:
    """Execute a single write statement inside the global write lock."""
    with _db_write_lock:
        conn = _get_conn()
        conn.execute(sql, params)
        conn.commit()


def _db_query(sql: str, params: tuple = ()) -> list:
    """Run a SELECT and return a list of plain dicts."""
    conn = _get_conn()
    cur = conn.execute(sql, params)
    return [dict(row) for row in cur.fetchall()]


def _db_query_one(sql: str, params: tuple = ()) -> dict | None:
    """Run a SELECT and return a single plain dict, or None."""
    conn = _get_conn()
    cur = conn.execute(sql, params)
    row = cur.fetchone()
    return dict(row) if row else None


# ── Row parsers: deserialise JSON-stored fields ───────────────────────────────

def _parse_user(row: dict) -> dict:
    row["is_active"] = bool(row.get("is_active", 1))
    return row


def _parse_contact(row: dict) -> dict:
    raw = row.get("tags")
    try:
        row["tags"] = json.loads(raw) if raw else []
    except (ValueError, TypeError):
        row["tags"] = []
    return row


def _parse_meeting(row: dict) -> dict:
    for field in ("contact_ids", "attendees", "attendee_usernames", "tags"):
        raw = row.get(field)
        try:
            row[field] = json.loads(raw) if raw else []
        except (ValueError, TypeError):
            row[field] = []
    return row


def _parse_dataset(row: dict) -> dict:
    raw = row.get("tags")
    try:
        row["tags"] = json.loads(raw) if raw else []
    except (ValueError, TypeError):
        row["tags"] = []
    return row


def _parse_project(row: dict) -> dict:
    for field in ("tags", "dataset_ids"):
        raw = row.get(field)
        try:
            row[field] = json.loads(raw) if raw else []
        except (ValueError, TypeError):
            row[field] = []
    row["nda_signed"] = bool(row.get("nda_signed", 0))
    return row


# ─── Auth Helpers ─────────────────────────────────────────────────────────────


def get_users() -> list:
    rows = _db_query("SELECT * FROM users ORDER BY created_at")
    return [_parse_user(r) for r in rows]


def find_user(username: str) -> dict | None:
    row = _db_query_one(
        "SELECT * FROM users WHERE lower(username) = ?",
        (username.strip().lower(),),
    )
    return _parse_user(row) if row else None


def authenticate(username: str, password: str) -> dict | None:
    u = find_user(username)
    if u and u.get("is_active", True):
        try:
            if bcrypt.checkpw(password.encode(), u["password_hash"].encode()):
                return u
        except Exception:
            pass
    return None


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def get_role() -> str:
    """
    Return the current user's role: "admin", "editor", or "viewer".
    Handles records written before the role field was introduced (is_admin=True → "admin").
    """
    user = st.session_state.get("user", {})
    if "role" in user:
        return user["role"]
    # backward-compat: old records only have is_admin bool
    return "admin" if user.get("is_admin", False) else "viewer"


def is_admin() -> bool:
    return get_role() == "admin"


def can_write() -> bool:
    """Admins and Editors can create new records."""
    return get_role() in ("admin", "editor")


def can_edit_record(record: dict) -> bool:
    """
    Admins can edit anything.
    Editors can only edit records they created themselves.
    Viewers cannot edit.
    """
    if is_admin():
        return True
    if get_role() == "editor" and record.get("created_by") == current_username():
        return True
    return False


def current_username() -> str:
    return st.session_state.get("user", {}).get("username", "unknown")


# ─── CRUD: Users ──────────────────────────────────────────────────────────────


ROLES = ["viewer", "editor", "admin"]
ROLE_DESCRIPTIONS = {
    "admin": "Full access: create, edit all records, manage users",
    "editor": "Can create records and edit their own entries only",
    "viewer": "Read-only access to all data",
}


def create_user(username: str, password: str, role: str, created_by: str) -> dict:
    uid = str(uuid.uuid4())
    now = datetime.now().isoformat()
    _db_write(
        """
        INSERT INTO users (id, username, password_hash, role, is_active, created_at, created_by)
        VALUES (?, ?, ?, ?, 1, ?, ?)
        """,
        (uid, username, hash_password(password), role, now, created_by),
    )
    return find_user(username)


def update_user_field(uid: str, field: str, value) -> None:
    _ALLOWED = {"role", "is_active"}
    if field not in _ALLOWED:
        raise ValueError(f"Field '{field}' cannot be updated via update_user_field")
    _db_write(f"UPDATE users SET {field} = ? WHERE id = ?", (value, uid))


def reset_user_password(uid: str, new_password: str) -> None:
    _db_write(
        "UPDATE users SET password_hash = ? WHERE id = ?",
        (hash_password(new_password), uid),
    )


# ─── Password Recovery ────────────────────────────────────────────────────────

_RECOVERY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # omits 0/O/1/I for readability


def _make_recovery_code() -> str:
    """Generate a readable 8-character recovery code."""
    return "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(8))


def generate_and_store_recovery_code(uid: str) -> str:
    """Create a new recovery code, persist its bcrypt hash, return the plaintext."""
    code = _make_recovery_code()
    code_hash = bcrypt.hashpw(code.encode(), bcrypt.gensalt()).decode()
    _db_write(
        "UPDATE users SET recovery_code_hash = ? WHERE id = ?",
        (code_hash, uid),
    )
    return code


def verify_recovery_code(uid: str, code: str) -> bool:
    """Return True if the submitted code matches the stored hash."""
    row = _db_query_one(
        "SELECT recovery_code_hash FROM users WHERE id = ?", (uid,)
    )
    if not row or not row.get("recovery_code_hash"):
        return False
    try:
        return bcrypt.checkpw(
            code.strip().upper().encode(), row["recovery_code_hash"].encode()
        )
    except Exception:
        return False


# ─── CRUD: Contacts ───────────────────────────────────────────────────────────

_CONTACT_COLUMNS = frozenset(
    {
        "name", "institution", "role", "email", "phone", "country",
        "website", "partnership_stage", "notes", "tags", "last_contacted",
    }
)


# ─── CRUD: Stage History ──────────────────────────────────────────────────────

def _log_stage_change(contact_id: str, old_stage: str, new_stage: str, changed_at: str, changed_by: str) -> None:
    _db_write(
        "INSERT INTO stage_history (id, contact_id, old_stage, new_stage, changed_at, changed_by) VALUES (?,?,?,?,?,?)",
        (str(uuid.uuid4()), contact_id, old_stage, new_stage, changed_at, changed_by),
    )


def get_stage_history(contact_id: str) -> list:
    return _db_query(
        "SELECT * FROM stage_history WHERE contact_id = ? ORDER BY changed_at ASC",
        (contact_id,),
    )


def _log_project_stage_change(project_id: str, old_stage: str, new_stage: str, changed_at: str, changed_by: str) -> None:
    _db_write(
        "INSERT INTO project_stage_history (id, project_id, old_stage, new_stage, changed_at, changed_by) VALUES (?,?,?,?,?,?)",
        (str(uuid.uuid4()), project_id, old_stage, new_stage, changed_at, changed_by),
    )


def get_project_stage_history(project_id: str) -> list:
    return _db_query(
        "SELECT * FROM project_stage_history WHERE project_id = ? ORDER BY changed_at ASC",
        (project_id,),
    )


def _log_dataset_status_change(dataset_id: str, old_status: str, new_status: str, changed_at: str, changed_by: str) -> None:
    _db_write(
        "INSERT INTO dataset_status_history (id, dataset_id, old_status, new_status, changed_at, changed_by) VALUES (?,?,?,?,?,?)",
        (str(uuid.uuid4()), dataset_id, old_status, new_status, changed_at, changed_by),
    )


def get_dataset_status_history(dataset_id: str) -> list:
    return _db_query(
        "SELECT * FROM dataset_status_history WHERE dataset_id = ? ORDER BY changed_at ASC",
        (dataset_id,),
    )


def get_contacts() -> list:
    rows = _db_query("SELECT * FROM contacts ORDER BY name")
    return [_parse_contact(r) for r in rows]


def get_contact(cid: str) -> dict | None:
    row = _db_query_one("SELECT * FROM contacts WHERE id = ?", (cid,))
    return _parse_contact(row) if row else None


def create_contact(fields: dict, username: str) -> dict:
    now = datetime.now().isoformat()
    cid = str(uuid.uuid4())
    _db_write(
        """
        INSERT INTO contacts
            (id, name, institution, role, email, phone, country, website,
             partnership_stage, notes, tags, last_contacted,
             created_at, created_by, updated_at, updated_by)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            cid,
            fields.get("name", ""),
            fields.get("institution", ""),
            fields.get("role", ""),
            fields.get("email", ""),
            fields.get("phone", ""),
            fields.get("country", ""),
            fields.get("website", ""),
            fields.get("partnership_stage", ""),
            fields.get("notes", ""),
            json.dumps(fields.get("tags", [])),
            fields.get("last_contacted"),
            now, username, now, username,
        ),
    )
    return get_contact(cid)


def update_contact(cid: str, fields: dict, username: str) -> None:
    now = datetime.now().isoformat()
    safe = {}
    for k, v in fields.items():
        if k in _CONTACT_COLUMNS:
            safe[k] = json.dumps(v) if k == "tags" else v
    if not safe:
        return
    # Detect and log stage changes before writing
    if "partnership_stage" in safe:
        current = _db_query_one("SELECT partnership_stage FROM contacts WHERE id = ?", (cid,))
        if current:
            old_stage = current.get("partnership_stage", "") or ""
            new_stage = safe["partnership_stage"] or ""
            if old_stage != new_stage:
                _log_stage_change(cid, old_stage, new_stage, now, username)
    set_clause = ", ".join(f"{k} = ?" for k in safe)
    params = tuple(safe.values()) + (now, username, cid)
    _db_write(
        f"UPDATE contacts SET {set_clause}, updated_at = ?, updated_by = ? WHERE id = ?",
        params,
    )


def delete_contact(cid: str) -> None:
    _db_write("DELETE FROM contacts WHERE id = ?", (cid,))


def log_ping(cid: str, date_str: str, username: str) -> None:
    """Record the date a contact was pinged. Does not affect last_contacted."""
    now = datetime.now().isoformat()
    _db_write(
        "UPDATE contacts SET last_pinged = ?, updated_at = ?, updated_by = ? WHERE id = ?",
        (date_str, now, username, cid),
    )


# ─── CRUD: Meetings ───────────────────────────────────────────────────────────

_MEETING_COLUMNS = frozenset(
    {
        "contact_ids", "contact_display", "date", "meeting_type",
        "summary", "attendees", "attendee_usernames", "notes", "action_items", "tags", "project_id",
    }
)
_MEETING_JSON_FIELDS = frozenset({"contact_ids", "attendees", "attendee_usernames", "tags"})


def get_meetings() -> list:
    rows = _db_query("SELECT * FROM meetings ORDER BY date DESC")
    return [_parse_meeting(r) for r in rows]


def _meeting_contact_ids(meeting: dict) -> list:
    """
    Return the list of linked contact IDs for a meeting.
    Handles legacy records that stored a single contact_id string.
    """
    ids = meeting.get("contact_ids", [])
    if isinstance(ids, list) and ids:
        return ids
    legacy = meeting.get("contact_id")
    return [legacy] if legacy else []


def get_meetings_for_contact(cid: str) -> list:
    return [m for m in get_meetings() if cid in _meeting_contact_ids(m)]


def _recalculate_last_contacted(cid: str, username: str) -> None:
    """Set last_contacted to the most recent meeting date across all meetings for this contact."""
    all_meetings = get_meetings_for_contact(cid)
    dates = [m.get("date", "") for m in all_meetings if m.get("date")]
    latest = max(dates) if dates else None
    update_contact(cid, {"last_contacted": latest}, username)


def create_meeting(fields: dict, username: str) -> dict:
    mid = str(uuid.uuid4())
    now = datetime.now().isoformat()
    _db_write(
        """
        INSERT INTO meetings
            (id, contact_ids, contact_display, date, meeting_type,
             summary, attendees, notes, action_items, tags, project_id, created_at, created_by)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            mid,
            json.dumps(fields.get("contact_ids", [])),
            fields.get("contact_display", ""),
            fields.get("date", ""),
            fields.get("meeting_type", ""),
            fields.get("summary", ""),
            json.dumps(fields.get("attendees", [])),
            fields.get("notes", ""),
            fields.get("action_items", ""),
            json.dumps(fields.get("tags", [])),
            fields.get("project_id") or None,
            now, username,
        ),
    )
    record = _db_query_one("SELECT * FROM meetings WHERE id = ?", (mid,))
    record = _parse_meeting(record)
    for cid in _meeting_contact_ids(record):
        _recalculate_last_contacted(cid, username)
    return record


def meeting_contact_label(meeting: dict, contact_map: dict) -> str:
    """Comma-joined display name of all linked contacts plus any free-text name."""
    parts = [contact_map[cid]["name"] for cid in _meeting_contact_ids(meeting) if cid in contact_map]
    freetext = meeting.get("contact_display", "").strip()
    if freetext:
        parts.append(freetext)
    return ", ".join(parts) if parts else "—"


def meeting_all_attendees(meeting: dict, contact_map: dict) -> list[str]:
    """
    Full ordered attendee list for display inside a meeting expander:
      1. Linked system contacts (by name)
      2. Free-text names not in system
      3. Additional attendees field
    Duplicates are removed while preserving order.
    """
    seen: set[str] = set()
    result: list[str] = []
    for cid in _meeting_contact_ids(meeting):
        name = contact_map.get(cid, {}).get("name", "")
        if name and name not in seen:
            seen.add(name)
            result.append(name)
    for ft in [n.strip() for n in meeting.get("contact_display", "").split(",") if n.strip()]:
        if ft not in seen:
            seen.add(ft)
            result.append(ft)
    for a in meeting.get("attendees", []):
        if a and a not in seen:
            seen.add(a)
            result.append(a)
    for uname in meeting.get("attendee_usernames", []):
        if uname and uname not in seen:
            seen.add(uname)
            result.append(uname)
    return result


def update_meeting(mid: str, fields: dict, username: str) -> None:
    now = datetime.now().isoformat()
    safe = {}
    for k, v in fields.items():
        if k in _MEETING_COLUMNS:
            safe[k] = json.dumps(v) if k in _MEETING_JSON_FIELDS else v
    if not safe:
        return
    set_clause = ", ".join(f"{k} = ?" for k in safe)
    params = tuple(safe.values()) + (now, username, mid)
    _db_write(
        f"UPDATE meetings SET {set_clause}, updated_at = ?, updated_by = ? WHERE id = ?",
        params,
    )
    meeting = _db_query_one("SELECT * FROM meetings WHERE id = ?", (mid,))
    if meeting:
        meeting = _parse_meeting(meeting)
        for cid in _meeting_contact_ids(meeting):
            _recalculate_last_contacted(cid, username)


def delete_meeting(mid: str, username: str = "system") -> None:
    meeting = _db_query_one("SELECT * FROM meetings WHERE id = ?", (mid,))
    linked_cids = _meeting_contact_ids(_parse_meeting(meeting)) if meeting else []
    _db_write("DELETE FROM meetings WHERE id = ?", (mid,))
    for cid in linked_cids:
        _recalculate_last_contacted(cid, username)


# ─── CRUD: Datasets ───────────────────────────────────────────────────────────

_DATASET_COLUMNS = frozenset(
    {
        "contact_id", "name", "format", "status",
        "acquired_date", "size", "description", "notes", "tags",
    }
)
_DATASET_JSON_FIELDS = frozenset({"tags"})


def get_datasets() -> list:
    rows = _db_query("SELECT * FROM datasets ORDER BY acquired_date DESC")
    return [_parse_dataset(r) for r in rows]


def get_datasets_for_contact(cid: str) -> list:
    rows = _db_query("SELECT * FROM datasets WHERE contact_id = ?", (cid,))
    return [_parse_dataset(r) for r in rows]


def create_dataset(fields: dict, username: str) -> dict:
    now = datetime.now().isoformat()
    did = str(uuid.uuid4())
    _db_write(
        """
        INSERT INTO datasets
            (id, contact_id, name, format, status, acquired_date, size,
             description, notes, tags, created_at, created_by, updated_at, updated_by)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            did,
            fields.get("contact_id", ""),
            fields.get("name", ""),
            fields.get("format", ""),
            fields.get("status", ""),
            fields.get("acquired_date", ""),
            fields.get("size", ""),
            fields.get("description", ""),
            fields.get("notes", ""),
            json.dumps(fields.get("tags", [])),
            now, username, now, username,
        ),
    )
    row = _db_query_one("SELECT * FROM datasets WHERE id = ?", (did,))
    return _parse_dataset(row)


def update_dataset(did: str, fields: dict, username: str) -> None:
    now = datetime.now().isoformat()
    safe = {}
    for k, v in fields.items():
        if k in _DATASET_COLUMNS:
            safe[k] = json.dumps(v) if k in _DATASET_JSON_FIELDS else v
    if not safe:
        return
    # Detect and log status changes before writing
    if "status" in safe:
        current = _db_query_one("SELECT status FROM datasets WHERE id = ?", (did,))
        if current:
            old_status = current.get("status", "") or ""
            new_status = safe["status"] or ""
            if old_status != new_status:
                _log_dataset_status_change(did, old_status, new_status, now, username)
    set_clause = ", ".join(f"{k} = ?" for k in safe)
    params = tuple(safe.values()) + (now, username, did)
    _db_write(
        f"UPDATE datasets SET {set_clause}, updated_at = ?, updated_by = ? WHERE id = ?",
        params,
    )


def delete_dataset(did: str) -> None:
    _db_write("DELETE FROM datasets WHERE id = ?", (did,))


# ─── CRUD: Projects ───────────────────────────────────────────────────────────

_PROJECT_COLUMNS = frozenset(
    {
        "name", "description", "status",
        "main_contact_id", "dataset_ids", "nda_signed", "notes", "tags",
    }
)
_PROJECT_JSON_FIELDS = frozenset({"tags", "dataset_ids"})


def get_projects() -> list:
    rows = _db_query("SELECT * FROM projects ORDER BY created_at DESC")
    return [_parse_project(r) for r in rows]


def get_project(pid: str) -> dict | None:
    row = _db_query_one("SELECT * FROM projects WHERE id = ?", (pid,))
    return _parse_project(row) if row else None


def get_projects_for_contact(cid: str) -> list:
    rows = _db_query("SELECT * FROM projects WHERE main_contact_id = ?", (cid,))
    return [_parse_project(r) for r in rows]


def create_project(fields: dict, username: str) -> dict:
    pid = str(uuid.uuid4())
    now = datetime.now().isoformat()
    _db_write(
        """
        INSERT INTO projects
            (id, name, description, status,
             main_contact_id, dataset_ids, nda_signed, notes, tags,
             created_at, created_by, updated_at, updated_by)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            pid,
            fields.get("name", ""),
            fields.get("description", ""),
            fields.get("status", ""),
            fields.get("main_contact_id", ""),
            json.dumps(fields.get("dataset_ids", [])),
            1 if fields.get("nda_signed") else 0,
            fields.get("notes", ""),
            json.dumps(fields.get("tags", [])),
            now, username, now, username,
        ),
    )
    row = _db_query_one("SELECT * FROM projects WHERE id = ?", (pid,))
    return _parse_project(row)


def update_project(pid: str, fields: dict, username: str) -> None:
    now = datetime.now().isoformat()
    safe = {}
    for k, v in fields.items():
        if k in _PROJECT_COLUMNS:
            if k in _PROJECT_JSON_FIELDS:
                safe[k] = json.dumps(v)
            elif k == "nda_signed":
                safe[k] = 1 if v else 0
            else:
                safe[k] = v
    if not safe:
        return
    # Detect and log stage changes before writing
    if "status" in safe:
        current = _db_query_one("SELECT status FROM projects WHERE id = ?", (pid,))
        if current:
            old_stage = current.get("status", "") or ""
            new_stage = safe["status"] or ""
            if old_stage != new_stage:
                _log_project_stage_change(pid, old_stage, new_stage, now, username)
    set_clause = ", ".join(f"{k} = ?" for k in safe)
    params = tuple(safe.values()) + (now, username, pid)
    _db_write(
        f"UPDATE projects SET {set_clause}, updated_at = ?, updated_by = ? WHERE id = ?",
        params,
    )


def delete_project(pid: str) -> None:
    _db_write("DELETE FROM projects WHERE id = ?", (pid,))


# ─── UI: Login / First-Run Setup ──────────────────────────────────────────────


def page_login():
    st.markdown(
        "<h1 style='text-align:center'>🔬 Research Partnership CRM</h1>",
        unsafe_allow_html=True,
    )
    st.markdown("")

    # ── Show recovery code if one was just generated for a new account ──
    if "_recovery_code_to_show" in st.session_state:
        rc_username = st.session_state.pop("_rc_username", "")
        rc_code = st.session_state.pop("_recovery_code_to_show", "")
        col_l, col_c, col_r = st.columns([1, 1.2, 1])
        with col_c:
            st.success(f"Account **{rc_username}** created! Please log in.")
            st.warning(
                f"**Save your recovery code:** `{rc_code}`\n\n"
                "Store it somewhere safe — this is the only time it will be shown. "
                "You will need it to reset your password if you forget it."
            )
            if st.button("I've saved my code — Go to Login", type="primary", use_container_width=True):
                st.rerun()
        return

    users = get_users()

    # ── First-run: no users exist ──
    if not users:
        st.info("No accounts found. Create the first admin account to get started.")
        col_l, col_c, col_r = st.columns([1, 1.2, 1])
        with col_c:
            with st.form("first_setup"):
                username = st.text_input("Choose a username")
                password = st.text_input("Choose a password", type="password")
                confirm = st.text_input("Confirm password", type="password")
                submitted = st.form_submit_button("Create Admin Account", type="primary", use_container_width=True)
            if submitted:
                if not username or not password:
                    st.error("Username and password are required.")
                elif password != confirm:
                    st.error("Passwords do not match.")
                elif len(password) < 6:
                    st.error("Password must be at least 6 characters.")
                else:
                    new_user = create_user(username, password, role="admin", created_by="system")
                    code = generate_and_store_recovery_code(new_user["id"])
                    st.session_state["_rc_username"] = username
                    st.session_state["_recovery_code_to_show"] = code
                    st.rerun()
        return

    # ── Normal login ──
    col_left, col_center, col_right = st.columns([1, 1.2, 1])
    with col_center:
        with st.form("login"):
            st.subheader("Sign In")
            username = st.text_input("Username")
            password = st.text_input("Password", type="password")
            submitted = st.form_submit_button("Log In", type="primary", use_container_width=True)
        if submitted:
            user = authenticate(username, password)
            if user:
                st.session_state["user"] = user
                st.rerun()
            else:
                st.error("Invalid credentials or account is inactive.")
        st.markdown("")
        if st.button("Forgot your password?", use_container_width=True):
            st.session_state["_recovery_mode"] = True
            st.rerun()


# ─── UI: Dashboard ────────────────────────────────────────────────────────────


def page_dashboard():  # noqa: C901
    st.title("Dashboard")

    contacts  = get_contacts()
    meetings  = get_meetings()
    datasets  = get_datasets()
    projects  = get_projects()
    contact_map = {c["id"]: c for c in contacts}
    project_map = {p["id"]: p for p in projects}
    today = datetime.today().date()

    # ── Pre-compute commonly needed values ────────────────────────────────────
    active_contacts    = [c for c in contacts if c.get("partnership_stage") not in ("Inactive", "On Hold")]
    active_partnerships = sum(1 for c in contacts if c.get("partnership_stage") == "Active Partnership")
    in_negotiation     = sum(1 for c in contacts if c.get("partnership_stage") == "In Negotiation")
    active_projects    = sum(1 for p in projects if p.get("status") == "Active")
    nda_count          = sum(1 for p in projects if p.get("nda_signed"))

    # Meetings this month
    this_month = today.strftime("%Y-%m")
    meetings_this_month = sum(
        1 for m in meetings if (m.get("date") or "").startswith(this_month)
    )

    # ── KPI row ───────────────────────────────────────────────────────────────
    k1, k2, k3, k4, k5, k6 = st.columns(6)
    k1.metric("Active Partnerships", active_partnerships)
    k2.metric("In Negotiation", in_negotiation)
    k3.metric("Active Projects", active_projects,
              delta=f"{nda_count} NDA signed" if projects else None)
    k4.metric("Meetings This Month", meetings_this_month)
    k5.metric("Datasets", len(datasets))
    k6.metric("Total Contacts", len(contacts))

    st.divider()

    # ── Row 1: Partnership pipeline ───────────────────────────────────────────
    st.subheader("Partnership Pipeline")
    if contacts:
        pipeline_stages = [s for s in PARTNERSHIP_STAGES if s not in ("On Hold", "Inactive")]
        pipeline_counts = [sum(1 for c in contacts if c.get("partnership_stage") == s) for s in pipeline_stages]
        fig_pipeline = px.bar(
            x=pipeline_counts,
            y=pipeline_stages,
            orientation="h",
            color=pipeline_stages,
            color_discrete_map=STAGE_COLORS,
            labels={"x": "Contacts", "y": ""},
            text=pipeline_counts,
        )
        fig_pipeline.update_traces(textposition="outside")
        fig_pipeline.update_layout(
            showlegend=False,
            yaxis={"categoryorder": "array", "categoryarray": list(reversed(pipeline_stages))},
            margin=dict(t=10, b=10, l=10, r=40),
            height=260,
        )
        st.plotly_chart(fig_pipeline, use_container_width=True)
        paused = sum(1 for c in contacts if c.get("partnership_stage") in ("On Hold", "Inactive"))
        if paused:
            st.caption(f"{paused} contact(s) On Hold or Inactive — not shown above.")
    else:
        st.info("No contacts yet.")

    # ── Row 2: Dataset pipeline ───────────────────────────────────────────────
    st.subheader("Dataset Pipeline")
    if datasets:
        ds_counts = {s: 0 for s in DATASET_STATUSES}
        for d in datasets:
            s = d.get("status") or "Unknown"
            ds_counts[s] = ds_counts.get(s, 0) + 1
        fig_ds = px.funnel(
            y=DATASET_STATUSES,
            x=[ds_counts.get(s, 0) for s in DATASET_STATUSES],
            labels={"x": "Datasets", "y": ""},
        )
        fig_ds.update_layout(margin=dict(t=10, b=10), height=260)
        st.plotly_chart(fig_ds, use_container_width=True)
    else:
        st.info("No datasets recorded yet.")

    st.divider()

    # ── Row 3: Follow-up urgency ───────────────────────────────────────────────
    st.subheader("Follow-up Urgency")
    followup_days = st.slider(
        "Show contacts not reached in more than N days",
        min_value=14, max_value=180, value=60, step=7,
        key="dash_followup",
    )
    overdue = []
    for c in active_contacts:
        lc = c.get("last_contacted")
        lp = c.get("last_pinged")
        ping_display = lp[:10] if lp else "Never"
        if lc:
            try:
                days = (today - datetime.fromisoformat(lc).date()).days
                if days >= followup_days:
                    overdue.append({
                        "_id": c["id"],
                        "Name": c["name"],
                        "Institution": c.get("institution", ""),
                        "Stage": c.get("partnership_stage", ""),
                        "Last Contacted": lc[:10],
                        "Last Pinged": ping_display,
                        "Days Ago": days,
                    })
            except (ValueError, TypeError):
                pass
        else:
            overdue.append({
                "_id": c["id"],
                "Name": c["name"],
                "Institution": c.get("institution", ""),
                "Stage": c.get("partnership_stage", ""),
                "Last Contacted": "Never",
                "Last Pinged": ping_display,
                "Days Ago": 9999,
            })

    overdue_sorted = sorted(overdue, key=lambda r: r["Days Ago"], reverse=True)
    for r in overdue_sorted:
        if r["Days Ago"] == 9999:
            r["Days Ago"] = "Never"

    if overdue_sorted:
        df_overdue = pd.DataFrame(overdue_sorted)
        event = st.dataframe(
            df_overdue.drop(columns=["_id"]),
            use_container_width=True,
            hide_index=True,
            on_select="rerun",
            selection_mode="single-row",
        )
        sel = event.selection.get("rows", [])
        if sel:
            st.session_state["selected_contact_id"] = df_overdue.iloc[sel[0]]["_id"]
            st.info(
                f"Selected **{df_overdue.iloc[sel[0]]['Name']}** — go to **Contacts → Contact Detail** to view."
            )
    else:
        st.success(f"All active contacts reached within {followup_days} days.")

    st.divider()

    # ── Row 4: Relationship network ───────────────────────────────────────────
    st.subheader("Relationship Network")
    st.caption("Shows how contacts, projects, and datasets are linked.")

    if contacts or projects or datasets:
        from pyvis.network import Network
        import streamlit.components.v1 as components

        NEO4J_NODE = {
            "contact": {"fill": "#4C8EDA", "border": "#2E6DB4", "highlight_fill": "#6aaae8", "highlight_border": "#4C8EDA", "text": "#ffffff", "badge": "Person"},
            "project": {"fill": "#57C4A7", "border": "#2E9E85", "highlight_fill": "#79d4ba", "highlight_border": "#57C4A7", "text": "#1a1a1a", "badge": "Project"},
            "dataset": {"fill": "#F0A847", "border": "#C87D1E", "highlight_fill": "#f5c070", "highlight_border": "#F0A847", "text": "#1a1a1a", "badge": "Dataset"},
        }
        EDGE_LABELS = {
            "project-contact": "WORKS_WITH",
            "project-dataset": "HAS_DATASET",
            "dataset-contact": "OWNED_BY",
        }

        # ── Entity filter controls ───────────────────────────────────────────
        # Selecting any entity auto-includes all directly associated entities.
        # Leaving all three empty shows the full graph.
        contact_by_id = {c["id"]: c for c in contacts}
        project_by_id = {p["id"]: p for p in projects}
        dataset_by_id = {d["id"]: d for d in datasets}

        nc1, nc2, nc3 = st.columns(3)
        with nc1:
            sel_contacts = st.multiselect(
                "Contacts",
                options=[c["id"] for c in contacts],
                format_func=lambda i: contact_by_id[i]["name"],
                key="net_sel_contacts",
            )
        with nc2:
            sel_projects = st.multiselect(
                "Projects",
                options=[p["id"] for p in projects],
                format_func=lambda i: project_by_id[i]["name"],
                key="net_sel_projects",
            )
        with nc3:
            sel_datasets = st.multiselect(
                "Datasets",
                options=[d["id"] for d in datasets],
                format_func=lambda i: dataset_by_id[i]["name"],
                key="net_sel_datasets",
            )

        # ── Expand associations ───────────────────────────────────────────────
        if sel_contacts or sel_projects or sel_datasets:
            inc_contacts: set = set(sel_contacts)
            inc_projects: set = set(sel_projects)
            inc_datasets: set = set(sel_datasets)

            # Selected contact → its projects and their datasets, plus direct datasets
            for cid in list(sel_contacts):
                for p in projects:
                    if p.get("main_contact_id") == cid:
                        inc_projects.add(p["id"])
                        inc_datasets.update(p.get("dataset_ids", []))
                for d in datasets:
                    if d.get("contact_id") == cid:
                        inc_datasets.add(d["id"])

            # Selected project → its contact and datasets
            for pid in list(sel_projects):
                p = project_by_id.get(pid, {})
                if p.get("main_contact_id"):
                    inc_contacts.add(p["main_contact_id"])
                inc_datasets.update(p.get("dataset_ids", []))

            # Selected dataset → its contact and any project that references it
            for did in list(sel_datasets):
                d = dataset_by_id.get(did, {})
                if d.get("contact_id"):
                    inc_contacts.add(d["contact_id"])
                for p in projects:
                    if did in p.get("dataset_ids", []):
                        inc_projects.add(p["id"])
                        if p.get("main_contact_id"):
                            inc_contacts.add(p["main_contact_id"])

            filtered_contacts = [c for c in contacts if c["id"] in inc_contacts]
            filtered_projects = [p for p in projects if p["id"] in inc_projects]
            filtered_datasets  = [d for d in datasets  if d["id"] in inc_datasets]
        else:
            filtered_contacts = contacts
            filtered_projects = projects
            filtered_datasets  = datasets

        contact_ids_in_graph = {c["id"] for c in filtered_contacts}
        dataset_ids_in_graph  = {d["id"] for d in filtered_datasets}

        # Pre-compute degree so node size scales with connections
        degree: dict[str, int] = {}
        for p in filtered_projects:
            mid = p.get("main_contact_id", "")
            if mid in contact_ids_in_graph:
                degree[p["id"]] = degree.get(p["id"], 0) + 1
                degree[mid]     = degree.get(mid, 0) + 1
            for did in p.get("dataset_ids", []):
                if did in dataset_ids_in_graph:
                    degree[p["id"]] = degree.get(p["id"], 0) + 1
                    degree[did]     = degree.get(did, 0) + 1
        for d in filtered_datasets:
            cid = d.get("contact_id", "")
            if cid in contact_ids_in_graph:
                degree[d["id"]] = degree.get(d["id"], 0) + 1
                degree[cid]     = degree.get(cid, 0) + 1

        total_nodes = len(filtered_contacts) + len(filtered_projects) + len(filtered_datasets)

        if total_nodes == 0:
            st.info("No nodes to display with current filters.")
        else:
            # ── Build pyvis network ───────────────────────────────────────────
            net = Network(
                height="660px",
                bgcolor="#ffffff",
                font_color="#111827",
                directed=True,
                notebook=False,
            )

            net.set_options("""
{
  "physics": {
    "enabled": true,
    "barnesHut": {
      "gravitationalConstant": -8000,
      "centralGravity": 0.25,
      "springLength": 220,
      "springConstant": 0.04,
      "damping": 0.12,
      "avoidOverlap": 0.4
    },
    "stabilization": { "iterations": 150 }
  },
  "edges": {
    "arrows": { "to": { "enabled": true, "scaleFactor": 0.7 } },
    "color": { "color": "#9ca3af", "highlight": "#6b7280", "hover": "#6b7280" },
    "smooth": { "type": "curvedCW", "roundness": 0.15 },
    "font": { "size": 9, "color": "#6b7280", "align": "middle", "strokeWidth": 2, "strokeColor": "#ffffff" },
    "width": 1.5,
    "selectionWidth": 2.5,
    "hoverWidth": 2.5
  },
  "nodes": {
    "shape": "dot",
    "borderWidth": 0,
    "borderWidthSelected": 0,
    "shadow": { "enabled": false },
    "font": { "size": 12, "face": "Inter, sans-serif", "bold": { "size": 12 } }
  },
  "interaction": {
    "hover": true,
    "tooltipDelay": 80,
    "navigationButtons": true,
    "keyboard": { "enabled": true },
    "multiselect": true,
    "zoomView": true
  }
}
""")

            def _node_size(nid: str, base: int) -> int:
                return base + degree.get(nid, 0) * 6

            for c in filtered_contacts:
                col = NEO4J_NODE["contact"]
                stage = c.get("partnership_stage", "")
                tooltip = "\n".join(filter(None, [
                    c["name"],
                    f"Institution: {c.get('institution') or '—'}",
                    f"Stage: {stage or '—'}",
                    f"Country: {c.get('country') or '—'}",
                ]))
                net.add_node(
                    c["id"],
                    label=c["name"],
                    title=tooltip,
                    size=_node_size(c["id"], 22),
                    color={
                        "background": col["fill"],
                        "border": col["fill"],
                        "highlight": {"background": col["highlight_fill"], "border": col["highlight_fill"]},
                        "hover":     {"background": col["highlight_fill"], "border": col["highlight_fill"]},
                    },
                    font={"color": col["text"], "size": 12},
                    group="contact",
                )

            for p in filtered_projects:
                col = NEO4J_NODE["project"]
                status = p.get("status", "")
                fill = "#22c55e" if status == "Active" else col["fill"]
                tooltip = "\n".join(filter(None, [
                    p["name"],
                    f"Status: {status or '—'}",
                    f"NDA signed: {'Yes' if p.get('nda_signed') else 'No'}",
                ]))
                net.add_node(
                    p["id"],
                    label=p["name"],
                    title=tooltip,
                    size=_node_size(p["id"], 24),
                    color={
                        "background": fill,
                        "border": fill,
                        "highlight": {"background": col["highlight_fill"], "border": col["highlight_fill"]},
                        "hover":     {"background": col["highlight_fill"], "border": col["highlight_fill"]},
                    },
                    font={"color": col["text"], "size": 12},
                    group="project",
                )

            for d in filtered_datasets:
                col = NEO4J_NODE["dataset"]
                tooltip = "\n".join(filter(None, [
                    d["name"],
                    f"Status: {d.get('status') or '—'}",
                    f"Format: {d.get('format') or '—'}",
                ]))
                net.add_node(
                    d["id"],
                    label=d["name"],
                    title=tooltip,
                    size=_node_size(d["id"], 20),
                    color={
                        "background": col["fill"],
                        "border": col["fill"],
                        "highlight": {"background": col["highlight_fill"], "border": col["highlight_fill"]},
                        "hover":     {"background": col["highlight_fill"], "border": col["highlight_fill"]},
                    },
                    font={"color": col["text"], "size": 12},
                    group="dataset",
                )

            for p in filtered_projects:
                mid = p.get("main_contact_id", "")
                if mid in contact_ids_in_graph:
                    net.add_edge(p["id"], mid, label=EDGE_LABELS["project-contact"], title="WORKS_WITH")
                for did in p.get("dataset_ids", []):
                    if did in dataset_ids_in_graph:
                        net.add_edge(p["id"], did, label=EDGE_LABELS["project-dataset"], title="HAS_DATASET")
            for d in filtered_datasets:
                cid = d.get("contact_id", "")
                if cid in contact_ids_in_graph:
                    net.add_edge(d["id"], cid, label=EDGE_LABELS["dataset-contact"], title="OWNED_BY")

            html_str = net.generate_html()
            components.html(html_str, height=680, scrolling=False)

            # ── Node detail table ────────────────────────────────────────────
            with st.expander("Node details", expanded=False):
                node_rows = []
                for c in filtered_contacts:
                    node_rows.append({"Type": "Person", "Name": c["name"],
                                      "Stage / Status": c.get("partnership_stage", ""),
                                      "Institution / Format": c.get("institution", ""),
                                      "Connections": degree.get(c["id"], 0)})
                for p in filtered_projects:
                    node_rows.append({"Type": "Project", "Name": p["name"],
                                      "Stage / Status": p.get("status", ""),
                                      "Institution / Format": "",
                                      "Connections": degree.get(p["id"], 0)})
                for d in filtered_datasets:
                    node_rows.append({"Type": "Dataset", "Name": d["name"],
                                      "Stage / Status": d.get("status", ""),
                                      "Institution / Format": d.get("format", ""),
                                      "Connections": degree.get(d["id"], 0)})
                df_nodes = pd.DataFrame(node_rows).sort_values(["Type", "Name"])
                st.dataframe(df_nodes, use_container_width=True, hide_index=True)
    else:
        st.info("No data to display yet.")


# ─── UI: Contacts ─────────────────────────────────────────────────────────────


def page_contacts():
    st.title("Contacts")
    contacts = get_contacts()

    tabs = ["All Contacts", "Contact Detail"]
    if can_write():
        tabs.insert(1, "Add Contact")
    tab_objects = st.tabs(tabs)

    # ── Tab: All contacts ──
    with tab_objects[0]:
        if not contacts:
            st.info("No contacts yet. An admin can create the first one.")
        else:
            col1, col2, col3 = st.columns([2, 1, 1])
            with col1:
                search = st.text_input("Search name or institution", placeholder="Type to filter…")
            with col2:
                stage_filter = st.selectbox("Filter by stage", ["All"] + PARTNERSHIP_STAGES)
            with col3:
                all_tags = sorted({t for c in contacts for t in c.get("tags", []) if t})
                tag_filter = st.multiselect("Filter by tag", all_tags)

            filtered = contacts
            if search:
                sl = search.lower()
                filtered = [
                    c
                    for c in filtered
                    if sl in c.get("name", "").lower() or sl in c.get("institution", "").lower()
                ]
            if stage_filter != "All":
                filtered = [c for c in filtered if c.get("partnership_stage") == stage_filter]
            if tag_filter:
                filtered = [c for c in filtered if any(t in c.get("tags", []) for t in tag_filter)]

            if not filtered:
                st.warning("No contacts match the current filters.")
            else:
                rows = []
                for c in filtered:
                    rows.append(
                        {
                            "_id": c["id"],
                            "Name": c.get("name", ""),
                            "Institution": c.get("institution", ""),
                            "Role": c.get("role", ""),
                            "Email": c.get("email", ""),
                            "Country": c.get("country", ""),
                            "Stage": c.get("partnership_stage", ""),
                            "Last Contacted": c.get("last_contacted") or "—",
                            "Updated By": c.get("updated_by", ""),
                        }
                    )
                df = pd.DataFrame(rows)
                event = st.dataframe(
                    df.drop(columns=["_id"]),
                    use_container_width=True,
                    hide_index=True,
                    on_select="rerun",
                    selection_mode="single-row",
                )
                sel = event.selection.get("rows", [])
                if sel:
                    selected_id = df.iloc[sel[0]]["_id"]
                    st.session_state["selected_contact_id"] = selected_id
                    st.info(
                        f"Selected **{df.iloc[sel[0]]['Name']}** — open the 'Contact Detail' tab to view."
                    )

                # CSV export
                csv = df.drop(columns=["_id"]).to_csv(index=False).encode()
                st.download_button(
                    "Export to CSV",
                    data=csv,
                    file_name="contacts_export.csv",
                    mime="text/csv",
                )

    # ── Tab: Add contact (admin + editor) ──
    if can_write():
        with tab_objects[1]:
            st.subheader("New Contact")
            with st.form("new_contact"):
                col1, col2 = st.columns(2)
                with col1:
                    name = st.text_input("Full Name *")
                    institution = st.text_input("Institution / Organisation *")
                    role = st.text_input("Job Title / Role")
                    email = st.text_input("Email")
                    phone = st.text_input("Phone")
                with col2:
                    country = st.text_input("Country")
                    website = st.text_input("Website / LinkedIn")
                    stage = st.selectbox("Partnership Stage", PARTNERSHIP_STAGES)
                    tags = st.text_input("Tags (comma-separated)")
                notes = st.text_area("Notes")
                submitted = st.form_submit_button("Create Contact", type="primary")
            if submitted:
                if not name or not institution:
                    st.error("Name and Institution are required.")
                else:
                    # Deduplication check
                    nl = name.strip().lower()
                    il = institution.strip().lower()
                    similar = [
                        c for c in get_contacts()
                        if nl in c.get("name", "").lower() or c.get("name", "").lower() in nl
                    ]
                    warn_key = f"{nl}|{il}"
                    already_warned = st.session_state.get("dedup_warn_key") == warn_key

                    if similar and not already_warned:
                        st.session_state["dedup_warn_key"] = warn_key
                        names_str = ", ".join(
                            f"**{c['name']}** ({c.get('institution', '')})" for c in similar[:3]
                        )
                        st.warning(
                            f"Similar contacts already exist: {names_str}\n\n"
                            "Click **Create Contact** again to save anyway."
                        )
                    else:
                        create_contact(
                            {
                                "name": name,
                                "institution": institution,
                                "role": role,
                                "email": email,
                                "phone": phone,
                                "country": country,
                                "website": website,
                                "partnership_stage": stage,
                                "notes": notes,
                                "tags": [t.strip() for t in tags.split(",") if t.strip()],
                            },
                            current_username(),
                        )
                        st.session_state.pop("dedup_warn_key", None)
                        st.success(f"Contact '{name}' created.")
                        st.rerun()

    # ── Tab: Contact detail ──
    detail_tab = tab_objects[-1]
    with detail_tab:
        cid = st.session_state.get("selected_contact_id")
        if not cid:
            st.info("Select a contact from the 'All Contacts' tab to view their detail.")
            return

        contact = get_contact(cid)
        if not contact:
            st.error("Contact not found.")
            st.session_state.pop("selected_contact_id", None)
            return

        # Header
        col_hdr, col_actions = st.columns([3, 1])
        with col_hdr:
            stage_color = STAGE_COLORS.get(contact.get("partnership_stage", ""), "#94a3b8")
            st.markdown(
                f"## {contact['name']}  "
                f"<span style='background:{stage_color};color:white;padding:3px 10px;"
                f"border-radius:12px;font-size:0.8rem'>{contact.get('partnership_stage','')}</span>",
                unsafe_allow_html=True,
            )
            lp = contact.get("last_pinged")
            ping_str = f" · Last pinged: **{lp[:10] if lp else 'Never'}**"
            st.caption(
                f"{contact.get('role','—')} @ {contact.get('institution','—')} · "
                f"Last contacted: **{contact.get('last_contacted') or 'Never'}**"
                + ping_str
            )

        with col_actions:
            if can_write():
                ping_date = st.date_input(
                    "Ping date",
                    value=datetime.today().date(),
                    key=f"ping_date_{cid}",
                    label_visibility="collapsed",
                )
                if st.button("Log Ping", use_container_width=True, key=f"log_ping_{cid}"):
                    log_ping(cid, ping_date.isoformat(), current_username())
                    st.success(f"Ping logged for {ping_date}.")
                    st.rerun()
            if can_edit_record(contact):
                if st.button("🗑 Delete Contact", type="secondary", use_container_width=True):
                    st.session_state["confirm_delete_contact"] = cid

        # Delete confirmation
        if st.session_state.get("confirm_delete_contact") == cid:
            linked_meeting_count = len(get_meetings_for_contact(cid))
            linked_dataset_count = len(get_datasets_for_contact(cid))
            warn_lines = ["**Permanently delete this contact?**"]
            if linked_meeting_count:
                warn_lines.append(
                    f"⚠️ {linked_meeting_count} linked meeting(s) will become unlinked."
                )
            if linked_dataset_count:
                warn_lines.append(
                    f"⚠️ {linked_dataset_count} linked dataset(s) will lose their main contact point."
                )
            st.warning("\n\n".join(warn_lines))
            c1, c2, _ = st.columns([1, 1, 3])
            with c1:
                if st.button("Confirm Delete", type="primary"):
                    delete_contact(cid)
                    st.session_state.pop("selected_contact_id", None)
                    st.session_state.pop("confirm_delete_contact", None)
                    st.success("Contact deleted.")
                    st.rerun()
            with c2:
                if st.button("Cancel"):
                    st.session_state.pop("confirm_delete_contact", None)
                    st.rerun()

        st.divider()

        # Info section
        if can_edit_record(contact):
            with st.expander("Edit Contact Information", expanded=True):
                with st.form("edit_contact"):
                    col1, col2 = st.columns(2)
                    with col1:
                        e_name = st.text_input("Full Name", value=contact.get("name", ""))
                        e_inst = st.text_input(
                            "Institution", value=contact.get("institution", "")
                        )
                        e_role = st.text_input("Role", value=contact.get("role", ""))
                        e_email = st.text_input("Email", value=contact.get("email", ""))
                        e_phone = st.text_input("Phone", value=contact.get("phone", ""))
                    with col2:
                        e_country = st.text_input("Country", value=contact.get("country", ""))
                        e_website = st.text_input("Website", value=contact.get("website", ""))
                        current_stage = contact.get("partnership_stage", PARTNERSHIP_STAGES[0])
                        stage_idx = (
                            PARTNERSHIP_STAGES.index(current_stage)
                            if current_stage in PARTNERSHIP_STAGES
                            else 0
                        )
                        e_stage = st.selectbox("Stage", PARTNERSHIP_STAGES, index=stage_idx)
                        e_tags = st.text_input(
                            "Tags", value=", ".join(contact.get("tags", []))
                        )
                    e_notes = st.text_area("Notes", value=contact.get("notes", ""))
                    if st.form_submit_button("Save Changes", type="primary"):
                        update_contact(
                            cid,
                            {
                                "name": e_name,
                                "institution": e_inst,
                                "role": e_role,
                                "email": e_email,
                                "phone": e_phone,
                                "country": e_country,
                                "website": e_website,
                                "partnership_stage": e_stage,
                                "notes": e_notes,
                                "tags": [t.strip() for t in e_tags.split(",") if t.strip()],
                            },
                            current_username(),
                        )
                        st.success("Contact updated.")
                        st.rerun()
        else:
            col1, col2 = st.columns(2)
            with col1:
                st.markdown(f"**Institution:** {contact.get('institution','—')}")
                st.markdown(f"**Role:** {contact.get('role','—')}")
                st.markdown(f"**Email:** {contact.get('email','—')}")
                st.markdown(f"**Phone:** {contact.get('phone','—')}")
            with col2:
                st.markdown(f"**Country:** {contact.get('country','—')}")
                st.markdown(f"**Website:** {contact.get('website','—')}")
                st.markdown(f"**Last Contacted:** {contact.get('last_contacted','—')}")
                st.markdown(f"**Last Pinged:** {contact.get('last_pinged','—') or '—'}")
                if contact.get("tags"):
                    st.markdown("**Tags:** " + " · ".join(f"`{t}`" for t in contact["tags"]))
            if contact.get("notes"):
                st.markdown(f"**Notes:** {contact['notes']}")

        # Audit trail
        st.caption(
            f"Created by **{contact.get('created_by')}** on {contact.get('created_at','')[:10]}  |  "
            f"Last edited by **{contact.get('updated_by')}** on {contact.get('updated_at','')[:10]}"
        )

        # Stage history
        stage_history = get_stage_history(cid)
        if stage_history:
            with st.expander("Stage Change History", expanded=False):
                for entry in stage_history:
                    old = entry.get("old_stage") or "—"
                    new = entry.get("new_stage") or "—"
                    when = entry.get("changed_at", "")[:10]
                    who = entry.get("changed_by", "")
                    old_color = STAGE_COLORS.get(old, "#94a3b8")
                    new_color = STAGE_COLORS.get(new, "#94a3b8")
                    st.markdown(
                        f"**{when}** by {who} — "
                        f"<span style='background:{old_color};color:white;padding:2px 8px;border-radius:10px;font-size:0.75rem'>{old}</span>"
                        f" → "
                        f"<span style='background:{new_color};color:white;padding:2px 8px;border-radius:10px;font-size:0.75rem'>{new}</span>",
                        unsafe_allow_html=True,
                    )

        st.divider()

        # Sub-tabs: Meetings & Datasets
        meetings_tab, datasets_tab = st.tabs(["Meetings", "Datasets"])

        # ── Meetings ──
        with meetings_tab:
            if can_write():
                st.info("To log a new meeting, go to the **Meetings** page — you can link it to this contact there.")

            meetings = get_meetings_for_contact(cid)
            if not meetings:
                st.info("No meetings linked to this contact yet.")
            else:
                all_contacts_cd = get_contacts()
                contact_map_cd = {c["id"]: c for c in all_contacts_cd}
                all_projects_cd = get_projects()
                project_map_cd = {p["id"]: p for p in all_projects_cd}
                cd_proj_name_to_id = {p["name"]: p["id"] for p in all_projects_cd}
                cd_proj_names_list = ["(none)"] + [p["name"] for p in sorted(all_projects_cd, key=lambda p: p["name"])]
                cd_active_usernames = sorted(u["username"] for u in get_users() if u.get("is_active"))
                for m in sorted(meetings, key=lambda x: x.get("date", ""), reverse=True):
                    label = (
                        f"📅 {m.get('date')}  ·  {m.get('meeting_type')}  ·  {m.get('summary','')}"
                    )
                    with st.expander(label):
                        all_att_cd = meeting_all_attendees(m, contact_map_cd)
                        if all_att_cd:
                            st.markdown(f"**Attendees:** {', '.join(all_att_cd)}")
                        if m.get("project_id") and m["project_id"] in project_map_cd:
                            st.markdown(f"**Project:** {project_map_cd[m['project_id']]['name']}")
                        st.markdown(f"**Notes:**\n\n{m.get('notes') or '—'}")
                        if m.get("action_items"):
                            st.markdown(f"**Action Items:**\n\n{m['action_items']}")
                        if m.get("tags"):
                            st.markdown("**Tags:** " + " · ".join(f"`{t}`" for t in m["tags"]))
                        updated = m.get("updated_by")
                        if updated:
                            st.caption(
                                f"Logged by **{m.get('created_by')}** on {m.get('created_at','')[:10]}"
                                f"  ·  Last edited by **{updated}** on {m.get('updated_at','')[:10]}"
                            )
                        else:
                            st.caption(
                                f"Logged by **{m.get('created_by')}** on {m.get('created_at','')[:10]}"
                            )
                        if can_edit_record(m):
                            st.divider()
                            with st.expander("Edit this meeting", expanded=False):
                                ecd_opts = [c["name"] for c in sorted(all_contacts_cd, key=lambda c: c["name"])]
                                ecd_name_to_id = {c["name"]: c["id"] for c in all_contacts_cd}
                                ecd_id_to_name = {c["id"]: c["name"] for c in all_contacts_cd}
                                ecd_default = [ecd_id_to_name[i] for i in _meeting_contact_ids(m) if i in ecd_id_to_name]
                                ecd_proj_default_idx = next(
                                    (i for i, n in enumerate(cd_proj_names_list) if cd_proj_name_to_id.get(n) == m.get("project_id")), 0
                                )
                                with st.form(f"edit_m_cd_{m['id']}"):
                                    col1, col2 = st.columns(2)
                                    with col1:
                                        try:
                                            e_date = datetime.fromisoformat(m.get("date", "")).date()
                                        except (ValueError, TypeError):
                                            e_date = datetime.today().date()
                                        e_date = st.date_input("Date", value=e_date, key=f"ecd_date_{m['id']}")
                                        e_type = st.selectbox(
                                            "Meeting Type", MEETING_TYPES,
                                            index=MEETING_TYPES.index(m["meeting_type"]) if m.get("meeting_type") in MEETING_TYPES else 0,
                                            key=f"ecd_type_{m['id']}",
                                        )
                                        e_summary = st.text_input("Summary / Topic", value=m.get("summary", ""), key=f"ecd_sum_{m['id']}")
                                        e_project_cd = st.selectbox(
                                            "Associated Project",
                                            cd_proj_names_list,
                                            index=ecd_proj_default_idx,
                                            key=f"ecd_proj_{m['id']}",
                                        )
                                    with col2:
                                        e_contact_sel = st.multiselect(
                                            "Contacts (from system)",
                                            options=ecd_opts,
                                            default=ecd_default,
                                            key=f"ecd_csel_{m['id']}",
                                        )
                                        e_contact_ft = st.text_input(
                                            "Other names not in system (comma-separated)",
                                            value=m.get("contact_display", ""),
                                            key=f"ecd_cft_{m['id']}",
                                        )
                                        e_attendee_users_cd = st.multiselect(
                                            "CRM users attending",
                                            options=cd_active_usernames,
                                            default=[u for u in m.get("attendee_usernames", []) if u in cd_active_usernames],
                                            key=f"ecd_uatt_{m['id']}",
                                        )
                                        e_attendees = st.text_input(
                                            "Other attendees (not in CRM, comma-separated)", value=", ".join(m.get("attendees", [])), key=f"ecd_att_{m['id']}"
                                        )
                                    e_notes = st.text_area("Notes", value=m.get("notes", ""), key=f"ecd_notes_{m['id']}")
                                    e_actions = st.text_area("Action Items", value=m.get("action_items", ""), key=f"ecd_act_{m['id']}")
                                    e_tags_cd = st.text_input("Tags", value=", ".join(m.get("tags", [])), key=f"ecd_tags_{m['id']}")
                                    if st.form_submit_button("Save Changes", type="primary"):
                                        if not e_summary:
                                            st.error("Summary is required.")
                                        else:
                                            update_meeting(
                                                m["id"],
                                                {
                                                    "contact_ids": [ecd_name_to_id[n] for n in e_contact_sel if n in ecd_name_to_id],
                                                    "contact_display": e_contact_ft.strip(),
                                                    "date": e_date.isoformat(),
                                                    "meeting_type": e_type,
                                                    "summary": e_summary,
                                                    "attendee_usernames": e_attendee_users_cd,
                                                    "attendees": [a.strip() for a in e_attendees.split(",") if a.strip()],
                                                    "notes": e_notes,
                                                    "action_items": e_actions,
                                                    "tags": [t.strip() for t in e_tags_cd.split(",") if t.strip()],
                                                    "project_id": cd_proj_name_to_id.get(e_project_cd) if e_project_cd != "(none)" else None,
                                                },
                                                current_username(),
                                            )
                                            st.success("Meeting updated.")
                                            st.rerun()
                            mid_cd = m["id"]
                            if st.session_state.get("confirm_delete_meeting") == mid_cd:
                                st.warning("Are you sure you want to delete this meeting?")
                                ca, cb = st.columns(2)
                                with ca:
                                    if st.button("Yes, delete", key=f"conf_del_m_cd_{mid_cd}", type="primary"):
                                        delete_meeting(mid_cd, current_username())
                                        st.session_state.pop("confirm_delete_meeting", None)
                                        st.rerun()
                                with cb:
                                    if st.button("Cancel", key=f"canc_del_m_cd_{mid_cd}"):
                                        st.session_state.pop("confirm_delete_meeting", None)
                                        st.rerun()
                            else:
                                if st.button("Delete this meeting", key=f"del_m_{mid_cd}"):
                                    st.session_state["confirm_delete_meeting"] = mid_cd
                                    st.rerun()

        # ── Datasets ──
        with datasets_tab:
            if can_write():
                st.info("To add a new dataset, go to the **Datasets** page — you can set this contact as the main contact point there.")

            datasets = get_datasets_for_contact(cid)
            if not datasets:
                st.info("No datasets recorded for this contact yet.")
            else:
                for d in sorted(datasets, key=lambda x: x.get("acquired_date", ""), reverse=True):
                    label = f"💾 {d.get('name')}  ·  {d.get('status')}  ·  {d.get('format','')}"
                    with st.expander(label):
                        col1, col2 = st.columns(2)
                        with col1:
                            st.markdown(f"**Format:** {d.get('format','—')}")
                            st.markdown(f"**Size:** {d.get('size','—')}")
                            st.markdown(f"**Acquired/Expected:** {d.get('acquired_date','—')}")
                        with col2:
                            st.markdown(f"**Status:** {d.get('status')}")
                            st.markdown(f"**Added by:** {d.get('created_by')}")
                            st.markdown(f"**Last updated by:** {d.get('updated_by')}")
                        if d.get("description"):
                            st.markdown(f"**Description:** {d['description']}")
                        if d.get("notes"):
                            st.markdown(f"**Notes:** {d['notes']}")
                        st.caption("To update or delete this dataset, go to the Datasets page.")


# ─── UI: Meetings ────────────────────────────────────────────────────────────


def page_meetings():
    st.title("Meetings")

    contacts = get_contacts()
    meetings = get_meetings()
    projects = get_projects()
    users = get_users()
    contact_map = {c["id"]: c for c in contacts}
    project_map = {p["id"]: p for p in projects}
    active_usernames = sorted(u["username"] for u in users if u.get("is_active"))

    tabs = ["All Meetings"]
    if can_write():
        tabs.append("Log Meeting")
    tab_objects = st.tabs(tabs)

    # ── Tab: Log Meeting ──
    proj_name_to_id = {p["name"]: p["id"] for p in projects}
    proj_names_list = ["(none)"] + [p["name"] for p in sorted(projects, key=lambda p: p["name"])]

    if can_write():
        with tab_objects[1]:
            st.subheader("Log a New Meeting")
            sorted_contacts = sorted(contacts, key=lambda c: c["name"])
            contact_name_to_id = {c["name"]: c["id"] for c in sorted_contacts}
            contact_names_list = list(contact_name_to_id.keys())

            with st.form("new_meeting_global", clear_on_submit=True):
                col1, col2 = st.columns(2)
                with col1:
                    m_date = st.date_input("Date", value=datetime.today())
                    m_type = st.selectbox("Meeting Type", MEETING_TYPES)
                    m_summary = st.text_input("Summary / Topic *")
                    m_project = st.selectbox("Associated Project (optional)", proj_names_list)
                with col2:
                    m_contact_sel = st.multiselect(
                        "Contacts (from system)",
                        options=contact_names_list,
                        help="Optional — select one or more existing contacts.",
                    )
                    m_contact_freetext = st.text_input(
                        "Other names not in system (comma-separated)",
                        help="Optional — for people not in your contacts list.",
                    )
                    m_attendee_users = st.multiselect(
                        "CRM users attending",
                        options=active_usernames,
                        help="Select other CRM users who attended.",
                    )
                    m_attendees = st.text_input("Other attendees (not in CRM, comma-separated)")
                m_notes = st.text_area("Meeting Notes")
                m_actions = st.text_area("Action Items")
                m_tags = st.text_input("Tags (comma-separated)")

                if st.form_submit_button("Log Meeting", type="primary"):
                    if not m_summary:
                        st.error("Please add a summary.")
                    else:
                        linked_ids = [contact_name_to_id[n] for n in m_contact_sel if n in contact_name_to_id]
                        freetext = m_contact_freetext.strip()
                        create_meeting(
                            {
                                "contact_ids": linked_ids,
                                "contact_display": freetext,
                                "date": m_date.isoformat(),
                                "meeting_type": m_type,
                                "summary": m_summary,
                                "attendee_usernames": m_attendee_users,
                                "attendees": [a.strip() for a in m_attendees.split(",") if a.strip()],
                                "notes": m_notes,
                                "action_items": m_actions,
                                "tags": [t.strip() for t in m_tags.split(",") if t.strip()],
                                "project_id": proj_name_to_id.get(m_project) if m_project != "(none)" else None,
                            },
                            current_username(),
                        )
                        label_parts = list(m_contact_sel) + ([freetext] if freetext else [])
                        label_str = ", ".join(label_parts) if label_parts else "no linked contacts"
                        suffix = " Last Contacted updated." if linked_ids else ""
                        st.success(f"Meeting logged ({label_str}).{suffix}")
                        st.rerun()

    # ── Tab: All Meetings ──
    with tab_objects[0]:
        if not meetings:
            st.info("No meetings logged yet.")
        else:
            col1, col2, col6 = st.columns(3)
            with col1:
                search = st.text_input("Search notes or summary")
            with col2:
                type_filter = st.selectbox("Meeting Type", ["All"] + MEETING_TYPES)
            with col6:
                all_mtg_tags = sorted({t for m in meetings for t in m.get("tags", []) if t})
                mtg_tag_filter = st.multiselect("Filter by tag", all_mtg_tags)
            col3, col4, col5 = st.columns(3)
            with col3:
                # Build per-name filter options from all individual contact names in meetings
                individual_names: set[str] = set()
                for m in meetings:
                    for cid in _meeting_contact_ids(m):
                        if cid in contact_map:
                            individual_names.add(contact_map[cid]["name"])
                    ft = m.get("contact_display", "").strip()
                    if ft:
                        individual_names.add(ft)
                contact_filter = st.selectbox("Contact / Name", ["All"] + sorted(individual_names))
            with col4:
                date_from = st.date_input("From date", value=None, key="mtg_date_from")
            with col5:
                date_to = st.date_input("To date", value=None, key="mtg_date_to")

            filtered = meetings
            if search:
                sl = search.lower()
                filtered = [
                    m
                    for m in filtered
                    if sl in m.get("notes", "").lower() or sl in m.get("summary", "").lower()
                ]
            if type_filter != "All":
                filtered = [m for m in filtered if m.get("meeting_type") == type_filter]
            if contact_filter != "All":
                def _meeting_matches_filter(m):
                    for cid in _meeting_contact_ids(m):
                        if contact_map.get(cid, {}).get("name") == contact_filter:
                            return True
                    return contact_filter in m.get("contact_display", "")
                filtered = [m for m in filtered if _meeting_matches_filter(m)]
            if date_from:
                filtered = [m for m in filtered if m.get("date", "") >= date_from.isoformat()]
            if date_to:
                filtered = [m for m in filtered if m.get("date", "") <= date_to.isoformat()]
            if mtg_tag_filter:
                filtered = [m for m in filtered if any(t in m.get("tags", []) for t in mtg_tag_filter)]

            filtered = sorted(filtered, key=lambda m: m.get("date", ""), reverse=True)
            st.markdown(f"**{len(filtered)} meeting(s) found**")

            rows = []
            for m in filtered:
                rows.append(
                    {
                        "Date": m.get("date"),
                        "Contact(s)": meeting_contact_label(m, contact_map),
                        "In System": "Yes" if _meeting_contact_ids(m) else "No",
                        "Type": m.get("meeting_type"),
                        "Summary": m.get("summary", ""),
                        "Attendees": ", ".join(m.get("attendees", [])),
                        "Action Items": m.get("action_items", ""),
                        "Logged By": m.get("created_by"),
                    }
                )

            df = pd.DataFrame(rows)
            st.dataframe(df, use_container_width=True, hide_index=True)

            csv = df.to_csv(index=False).encode()
            st.download_button(
                "Export to CSV", data=csv, file_name="meetings_export.csv", mime="text/csv"
            )

            st.divider()
            st.subheader("Full Meeting Notes")
            for m in filtered:
                label = (
                    f"📅 {m.get('date')}  ·  {meeting_contact_label(m, contact_map)}"
                    f"  ·  {m.get('summary','')}"
                )
                with st.expander(label):
                    all_att = meeting_all_attendees(m, contact_map)
                    if all_att:
                        st.markdown(f"**Attendees:** {', '.join(all_att)}")
                    if m.get("project_id") and m["project_id"] in project_map:
                        st.markdown(f"**Project:** {project_map[m['project_id']]['name']}")
                    st.markdown(f"**Notes:**\n\n{m.get('notes') or '—'}")
                    if m.get("action_items"):
                        st.markdown(f"**Action Items:**\n\n{m['action_items']}")
                    if m.get("tags"):
                        st.markdown("**Tags:** " + " · ".join(f"`{t}`" for t in m["tags"]))
                    updated = m.get("updated_by")
                    if updated:
                        st.caption(
                            f"Logged by **{m.get('created_by')}** on {m.get('created_at','')[:10]}"
                            f"  ·  Last edited by **{updated}** on {m.get('updated_at','')[:10]}"
                        )
                    else:
                        st.caption(
                            f"Logged by **{m.get('created_by')}** on {m.get('created_at','')[:10]}"
                        )
                    if can_edit_record(m):
                        st.divider()
                        with st.expander("Edit this meeting", expanded=False):
                            em_contact_opts = [c["name"] for c in sorted(contacts, key=lambda c: c["name"])]
                            em_name_to_id = {c["name"]: c["id"] for c in contacts}
                            em_id_to_name = {c["id"]: c["name"] for c in contacts}
                            em_default = [em_id_to_name[cid] for cid in _meeting_contact_ids(m) if cid in em_id_to_name]
                            em_proj_default_idx = next(
                                (i for i, n in enumerate(proj_names_list) if proj_name_to_id.get(n) == m.get("project_id")), 0
                            )
                            with st.form(f"edit_m_{m['id']}"):
                                col1, col2 = st.columns(2)
                                with col1:
                                    try:
                                        e_date = datetime.fromisoformat(m.get("date", "")).date()
                                    except (ValueError, TypeError):
                                        e_date = datetime.today().date()
                                    e_date = st.date_input("Date", value=e_date, key=f"em_date_{m['id']}")
                                    e_type = st.selectbox(
                                        "Meeting Type", MEETING_TYPES,
                                        index=MEETING_TYPES.index(m["meeting_type"]) if m.get("meeting_type") in MEETING_TYPES else 0,
                                        key=f"em_type_{m['id']}",
                                    )
                                    e_summary = st.text_input("Summary / Topic", value=m.get("summary", ""), key=f"em_sum_{m['id']}")
                                    e_project = st.selectbox(
                                        "Associated Project",
                                        proj_names_list,
                                        index=em_proj_default_idx,
                                        key=f"em_proj_{m['id']}",
                                    )
                                with col2:
                                    e_contact_sel = st.multiselect(
                                        "Contacts (from system)",
                                        options=em_contact_opts,
                                        default=em_default,
                                        key=f"em_csel_{m['id']}",
                                    )
                                    e_contact_ft = st.text_input(
                                        "Other names not in system (comma-separated)",
                                        value=m.get("contact_display", ""),
                                        key=f"em_cft_{m['id']}",
                                    )
                                    e_attendee_users = st.multiselect(
                                        "CRM users attending",
                                        options=active_usernames,
                                        default=[u for u in m.get("attendee_usernames", []) if u in active_usernames],
                                        key=f"em_uatt_{m['id']}",
                                    )
                                    e_attendees = st.text_input(
                                        "Other attendees (not in CRM, comma-separated)", value=", ".join(m.get("attendees", [])), key=f"em_att_{m['id']}"
                                    )
                                e_notes = st.text_area("Notes", value=m.get("notes", ""), key=f"em_notes_{m['id']}")
                                e_actions = st.text_area("Action Items", value=m.get("action_items", ""), key=f"em_act_{m['id']}")
                                e_tags_m = st.text_input("Tags", value=", ".join(m.get("tags", [])), key=f"em_tags_{m['id']}")
                                if st.form_submit_button("Save Changes", type="primary"):
                                    if not e_summary:
                                        st.error("Summary is required.")
                                    else:
                                        update_meeting(
                                            m["id"],
                                            {
                                                "contact_ids": [em_name_to_id[n] for n in e_contact_sel if n in em_name_to_id],
                                                "contact_display": e_contact_ft.strip(),
                                                "date": e_date.isoformat(),
                                                "meeting_type": e_type,
                                                "summary": e_summary,
                                                "attendee_usernames": e_attendee_users,
                                                "attendees": [a.strip() for a in e_attendees.split(",") if a.strip()],
                                                "notes": e_notes,
                                                "action_items": e_actions,
                                                "tags": [t.strip() for t in e_tags_m.split(",") if t.strip()],
                                                "project_id": proj_name_to_id.get(e_project) if e_project != "(none)" else None,
                                            },
                                            current_username(),
                                        )
                                        st.success("Meeting updated.")
                                        st.rerun()
                        mid = m["id"]
                        if st.session_state.get("confirm_delete_meeting") == mid:
                            st.warning("Are you sure you want to delete this meeting?")
                            ca, cb = st.columns(2)
                            with ca:
                                if st.button("Yes, delete", key=f"conf_del_m_pg_{mid}", type="primary"):
                                    delete_meeting(mid, current_username())
                                    st.session_state.pop("confirm_delete_meeting", None)
                                    st.rerun()
                            with cb:
                                if st.button("Cancel", key=f"canc_del_m_pg_{mid}"):
                                    st.session_state.pop("confirm_delete_meeting", None)
                                    st.rerun()
                        else:
                            if st.button("Delete this meeting", key=f"del_m_pg_{mid}"):
                                st.session_state["confirm_delete_meeting"] = mid
                                st.rerun()


# ─── UI: Datasets ────────────────────────────────────────────────────────────


def page_datasets():
    st.title("Datasets")

    contacts = get_contacts()
    datasets = get_datasets()
    contact_map = {c["id"]: c for c in contacts}

    tabs = ["All Datasets"]
    if can_write():
        tabs.append("Add Dataset")
    tab_objects = st.tabs(tabs)

    # ── Tab: Add Dataset ──
    if can_write():
        with tab_objects[1]:
            st.subheader("Add a New Dataset")
            if not contacts:
                st.warning(
                    "No contacts exist yet. Create a contact first — "
                    "every dataset requires a main contact point."
                )
            else:
                contact_options = sorted(contacts, key=lambda c: c["name"])
                contact_display_options = [
                    f"{c['name']}  ·  {c.get('institution','')}" for c in contact_options
                ]

                with st.form("new_dataset_global", clear_on_submit=True):
                    st.markdown("**Main Contact Point** *(required — must be an existing contact)*")
                    ds_contact_idx = st.selectbox(
                        "Select contact",
                        range(len(contact_options)),
                        format_func=lambda i: contact_display_options[i],
                    )
                    st.divider()
                    col1, col2 = st.columns(2)
                    with col1:
                        ds_name = st.text_input("Dataset Name *")
                        ds_format = st.text_input("Format (e.g. CSV, DICOM, FHIR, HL7)")
                        ds_status = st.selectbox("Status", DATASET_STATUSES)
                    with col2:
                        ds_acquired = st.date_input(
                            "Acquired / Expected Date", value=datetime.today()
                        )
                        ds_size = st.text_input("Size / Volume (e.g. 50 GB, 10 k records)")
                        ds_tags = st.text_input("Tags (comma-separated)")
                    ds_desc = st.text_area("Description")
                    ds_notes = st.text_area("Notes / Caveats")

                    if st.form_submit_button("Add Dataset", type="primary"):
                        if not ds_name:
                            st.error("Dataset name is required.")
                        else:
                            chosen_contact = contact_options[ds_contact_idx]
                            create_dataset(
                                {
                                    "contact_id": chosen_contact["id"],
                                    "name": ds_name,
                                    "format": ds_format,
                                    "status": ds_status,
                                    "acquired_date": ds_acquired.isoformat(),
                                    "size": ds_size,
                                    "description": ds_desc,
                                    "notes": ds_notes,
                                    "tags": [t.strip() for t in ds_tags.split(",") if t.strip()],
                                },
                                current_username(),
                            )
                            st.success(
                                f"Dataset **{ds_name}** added with main contact **{chosen_contact['name']}**."
                            )
                            st.rerun()

    # ── Tab: All Datasets ──
    with tab_objects[0]:
        if not datasets:
            st.info("No datasets recorded yet.")
        else:
            # Status pipeline metrics
            status_counts: dict[str, int] = {s: 0 for s in DATASET_STATUSES}
            for d in datasets:
                s = d.get("status", "Unknown")
                status_counts[s] = status_counts.get(s, 0) + 1
            metric_cols = st.columns(len(DATASET_STATUSES))
            for i, s in enumerate(DATASET_STATUSES):
                metric_cols[i].metric(s, status_counts.get(s, 0))

            st.divider()

            col1, col2, col3 = st.columns(3)
            with col1:
                status_filter = st.selectbox("Filter by Status", ["All"] + DATASET_STATUSES)
            with col2:
                search = st.text_input("Search name or description")
            with col3:
                all_ds_tags = sorted({t for d in datasets for t in d.get("tags", []) if t})
                ds_tag_filter = st.multiselect("Filter by tag", all_ds_tags)

            filtered = datasets
            if status_filter != "All":
                filtered = [d for d in filtered if d.get("status") == status_filter]
            if search:
                sl = search.lower()
                filtered = [
                    d
                    for d in filtered
                    if sl in d.get("name", "").lower()
                    or sl in d.get("description", "").lower()
                ]
            if ds_tag_filter:
                filtered = [d for d in filtered if any(t in d.get("tags", []) for t in ds_tag_filter)]

            rows = []
            for d in filtered:
                c = contact_map.get(d.get("contact_id", ""), {})
                rows.append(
                    {
                        "Dataset": d.get("name"),
                        "Main Contact": c.get("name", "Unknown"),
                        "Institution": c.get("institution", ""),
                        "Format": d.get("format", ""),
                        "Status": d.get("status"),
                        "Size": d.get("size", ""),
                        "Acquired / Expected": d.get("acquired_date", ""),
                        "Added By": d.get("created_by"),
                        "Updated By": d.get("updated_by"),
                    }
                )

            df = pd.DataFrame(rows)
            st.dataframe(df, use_container_width=True, hide_index=True)

            csv = df.to_csv(index=False).encode()
            st.download_button(
                "Export to CSV", data=csv, file_name="datasets_export.csv", mime="text/csv"
            )

            # Detailed expandable view with edit controls
            st.divider()
            st.subheader("Dataset Details")
            for d in sorted(filtered, key=lambda x: x.get("acquired_date", ""), reverse=True):
                c = contact_map.get(d.get("contact_id", ""), {})
                label = f"💾 {d.get('name')}  ·  {d.get('status')}  ·  {c.get('name','Unknown')}"
                with st.expander(label):
                    col1, col2 = st.columns(2)
                    with col1:
                        st.markdown(f"**Main Contact:** {c.get('name','—')}")
                        st.markdown(f"**Institution:** {c.get('institution','—')}")
                        st.markdown(f"**Format:** {d.get('format','—')}")
                        st.markdown(f"**Size:** {d.get('size','—')}")
                    with col2:
                        st.markdown(f"**Status:** {d.get('status')}")
                        st.markdown(f"**Acquired/Expected:** {d.get('acquired_date','—')}")
                        st.markdown(f"**Added by:** {d.get('created_by')}")
                        st.markdown(f"**Last updated by:** {d.get('updated_by')}")
                    if d.get("description"):
                        st.markdown(f"**Description:** {d['description']}")
                    if d.get("notes"):
                        st.markdown(f"**Notes:** {d['notes']}")
                    if d.get("tags"):
                        st.markdown("**Tags:** " + " · ".join(f"`{t}`" for t in d["tags"]))
                    updated_ds = d.get("updated_by")
                    if updated_ds:
                        st.caption(
                            f"Added by **{d.get('created_by')}** on {d.get('created_at','')[:10]}"
                            f"  ·  Last edited by **{updated_ds}** on {d.get('updated_at','')[:10]}"
                        )
                    else:
                        st.caption(f"Added by **{d.get('created_by')}** on {d.get('created_at','')[:10]}")
                    ds_status_hist = get_dataset_status_history(d["id"])
                    if ds_status_hist:
                        with st.expander("Status Change History", expanded=False):
                            for entry in ds_status_hist:
                                st.markdown(
                                    f"**{entry.get('changed_at','')[:10]}** by {entry.get('changed_by','')} — "
                                    f"**{entry.get('old_status') or '—'}** → **{entry.get('new_status') or '—'}**"
                                )
                    if can_edit_record(d):
                        st.divider()
                        with st.expander("Edit this dataset", expanded=False):
                            ds_contact_opts = sorted(contacts, key=lambda c: c["name"])
                            ds_contact_disp = [
                                f"{c['name']}  ·  {c.get('institution','')}" for c in ds_contact_opts
                            ]
                            current_cid = d.get("contact_id", "")
                            current_c_idx = next(
                                (i for i, c in enumerate(ds_contact_opts) if c["id"] == current_cid), 0
                            )
                            try:
                                current_acq = datetime.fromisoformat(d.get("acquired_date", "")).date()
                            except (ValueError, TypeError):
                                current_acq = datetime.today().date()
                            current_status_ds = d.get("status", DATASET_STATUSES[0])
                            status_idx_ds = (
                                DATASET_STATUSES.index(current_status_ds)
                                if current_status_ds in DATASET_STATUSES
                                else 0
                            )
                            with st.form(f"edit_ds_{d['id']}"):
                                st.markdown("**Main Contact Point**")
                                e_contact_idx = st.selectbox(
                                    "Contact",
                                    range(len(ds_contact_opts)),
                                    index=current_c_idx,
                                    format_func=lambda i: ds_contact_disp[i],
                                    key=f"eds_c_{d['id']}",
                                )
                                st.divider()
                                col1, col2 = st.columns(2)
                                with col1:
                                    e_name = st.text_input("Dataset Name", value=d.get("name", ""), key=f"eds_name_{d['id']}")
                                    e_format = st.text_input("Format", value=d.get("format", ""), key=f"eds_fmt_{d['id']}")
                                    e_status = st.selectbox(
                                        "Status", DATASET_STATUSES, index=status_idx_ds, key=f"eds_st_{d['id']}"
                                    )
                                with col2:
                                    e_acq = st.date_input("Acquired / Expected Date", value=current_acq, key=f"eds_acq_{d['id']}")
                                    e_size = st.text_input("Size / Volume", value=d.get("size", ""), key=f"eds_sz_{d['id']}")
                                    e_tags_ds = st.text_input("Tags", value=", ".join(d.get("tags", [])), key=f"eds_tags_{d['id']}")
                                e_desc = st.text_area("Description", value=d.get("description", ""), key=f"eds_desc_{d['id']}")
                                e_notes = st.text_area("Notes / Caveats", value=d.get("notes", ""), key=f"eds_notes_{d['id']}")
                                if st.form_submit_button("Save Changes", type="primary"):
                                    if not e_name:
                                        st.error("Dataset name is required.")
                                    else:
                                        chosen = ds_contact_opts[e_contact_idx]
                                        update_dataset(
                                            d["id"],
                                            {
                                                "contact_id": chosen["id"],
                                                "name": e_name,
                                                "format": e_format,
                                                "status": e_status,
                                                "acquired_date": e_acq.isoformat(),
                                                "size": e_size,
                                                "description": e_desc,
                                                "notes": e_notes,
                                                "tags": [t.strip() for t in e_tags_ds.split(",") if t.strip()],
                                            },
                                            current_username(),
                                        )
                                        st.success("Dataset updated.")
                                        st.rerun()
                        did = d["id"]
                        if st.session_state.get("confirm_delete_dataset") == did:
                            st.warning("Are you sure you want to delete this dataset?")
                            da, db = st.columns(2)
                            with da:
                                if st.button("Yes, delete", key=f"conf_del_ds_{did}", type="primary"):
                                    delete_dataset(did)
                                    st.session_state.pop("confirm_delete_dataset", None)
                                    st.rerun()
                            with db:
                                if st.button("Cancel", key=f"canc_del_ds_{did}"):
                                    st.session_state.pop("confirm_delete_dataset", None)
                                    st.rerun()
                        else:
                            if st.button("Delete", key=f"ds_del_pg_{did}"):
                                st.session_state["confirm_delete_dataset"] = did
                                st.rerun()


# ─── UI: Password Recovery (pre-login) ───────────────────────────────────────


def page_password_recovery():
    st.markdown(
        "<h1 style='text-align:center'>🔬 Research Partnership CRM</h1>",
        unsafe_allow_html=True,
    )
    st.markdown("")

    col_l, col_c, col_r = st.columns([1, 1.2, 1])
    with col_c:
        st.subheader("Reset Password")
        st.caption(
            "Enter your username and the recovery code you saved when your account was created. "
            "A new recovery code will be issued after a successful reset."
        )
        st.markdown("")

        with st.form("recovery_form"):
            rec_username = st.text_input("Username")
            rec_code = st.text_input(
                "Recovery Code",
                placeholder="e.g. AB3X7QM2",
                help="The 8-character code shown when your account was created or last reset.",
            )
            new_pw1 = st.text_input("New Password", type="password")
            new_pw2 = st.text_input("Confirm New Password", type="password")
            submitted = st.form_submit_button(
                "Reset Password", type="primary", use_container_width=True
            )

        if submitted:
            u = find_user(rec_username)
            if not u:
                st.error("Username not found.")
            elif not u.get("is_active", True):
                st.error("This account is inactive. Contact an administrator.")
            elif not verify_recovery_code(u["id"], rec_code):
                st.error("Invalid recovery code.")
            elif len(new_pw1) < 6:
                st.error("Password must be at least 6 characters.")
            elif new_pw1 != new_pw2:
                st.error("Passwords do not match.")
            else:
                reset_user_password(u["id"], new_pw1)
                new_code = generate_and_store_recovery_code(u["id"])
                st.success("Password reset successfully! You can now log in.")
                st.warning(
                    f"**New recovery code: `{new_code}`**\n\n"
                    "Your old code is no longer valid. Save this new code somewhere safe."
                )

        st.markdown("---")
        if st.button("Back to Login", use_container_width=True):
            st.session_state.pop("_recovery_mode", None)
            st.rerun()


# ─── UI: Change Password (all roles) ────────────────────────────────────────


def page_change_password():
    st.title("Change Password")
    st.caption("Your password is stored as a one-way hash — nobody, including admins, can read it.")

    col_left, col_center, col_right = st.columns([1, 1.2, 1])
    with col_center:
        with st.form("change_pw_self"):
            old_pw = st.text_input("Current password", type="password")
            new_pw1 = st.text_input("New password", type="password")
            new_pw2 = st.text_input("Confirm new password", type="password")
            if st.form_submit_button("Update Password", type="primary", use_container_width=True):
                me = find_user(current_username())
                if not me:
                    st.error("User not found.")
                elif not bcrypt.checkpw(old_pw.encode(), me["password_hash"].encode()):
                    st.error("Current password is incorrect.")
                elif new_pw1 != new_pw2:
                    st.error("New passwords do not match.")
                elif len(new_pw1) < 6:
                    st.error("Password must be at least 6 characters.")
                else:
                    reset_user_password(me["id"], new_pw1)
                    st.success("Password updated successfully.")

    st.divider()
    st.subheader("Recovery Code")
    st.caption(
        "Your recovery code lets you reset your password from the login screen if you ever "
        "forget it. Generating a new code immediately invalidates the old one."
    )
    col_left, col_center, col_right = st.columns([1, 1.2, 1])
    with col_center:
        with st.form("regen_recovery"):
            confirm_pw = st.text_input(
                "Confirm current password to generate a new code", type="password"
            )
            if st.form_submit_button(
                "Generate New Recovery Code", use_container_width=True
            ):
                me = find_user(current_username())
                if not me:
                    st.error("User not found.")
                elif not bcrypt.checkpw(confirm_pw.encode(), me["password_hash"].encode()):
                    st.error("Current password is incorrect.")
                else:
                    new_code = generate_and_store_recovery_code(me["id"])
                    st.success("New recovery code generated!")
                    st.warning(
                        f"**Your recovery code: `{new_code}`**\n\n"
                        "Save this somewhere safe — it won't be shown again. "
                        "Your previous code is no longer valid."
                    )


# ─── UI: Projects ────────────────────────────────────────────────────────────


def page_projects():
    st.title("Projects")

    contacts = get_contacts()
    projects = get_projects()
    datasets = get_datasets()
    contact_map = {c["id"]: c for c in contacts}
    dataset_map = {d["id"]: d for d in datasets}

    tabs = ["All Projects"]
    if can_write():
        tabs.insert(1, "Add Project")
    tabs.append("Project Detail")
    tab_objects = st.tabs(tabs)

    # ── Tab: All Projects ──
    with tab_objects[0]:
        if not projects:
            st.info("No projects yet.")
        else:
            col1, col2, col3 = st.columns(3)
            with col1:
                search = st.text_input("Search name or description", key="proj_search")
            with col2:
                status_filter = st.selectbox("Filter by status", ["All"] + PROJECT_STATUSES, key="proj_sf")
            with col3:
                all_proj_tags = sorted({t for p in projects for t in p.get("tags", []) if t})
                proj_tag_filter = st.multiselect("Filter by tag", all_proj_tags, key="proj_tf")

            filtered = projects
            if search:
                sl = search.lower()
                filtered = [p for p in filtered if sl in p.get("name", "").lower() or sl in p.get("description", "").lower()]
            if status_filter != "All":
                filtered = [p for p in filtered if p.get("status") == status_filter]
            if proj_tag_filter:
                filtered = [p for p in filtered if any(t in p.get("tags", []) for t in proj_tag_filter)]

            if not filtered:
                st.warning("No projects match the current filters.")
            else:
                rows = []
                for p in filtered:
                    c = contact_map.get(p.get("main_contact_id", ""), {})
                    ds_names = ", ".join(dataset_map[did]["name"] for did in p.get("dataset_ids", []) if did in dataset_map)
                    rows.append({
                        "_id": p["id"],
                        "Name": p.get("name", ""),
                        "Status": p.get("status", ""),
                        "Main Contact": c.get("name", "—"),
                        "Institution": c.get("institution", ""),
                        "NDA/CDA Signed": "Yes" if p.get("nda_signed") else "No",
                        "Datasets": ds_names or "—",
                        "Tags": ", ".join(p.get("tags", [])),
                    })
                df = pd.DataFrame(rows)
                event = st.dataframe(
                    df.drop(columns=["_id"]),
                    use_container_width=True,
                    hide_index=True,
                    on_select="rerun",
                    selection_mode="single-row",
                )
                sel = event.selection.get("rows", [])
                if sel:
                    st.session_state["selected_project_id"] = df.iloc[sel[0]]["_id"]
                    st.info(f"Selected **{df.iloc[sel[0]]['Name']}** — open 'Project Detail' tab to view.")

                csv = df.drop(columns=["_id"]).to_csv(index=False).encode()
                st.download_button("Export to CSV", data=csv, file_name="projects_export.csv", mime="text/csv")

    # ── Tab: Add Project ──
    if can_write():
        with tab_objects[1]:
            st.subheader("New Project")
            if not contacts:
                st.warning("No contacts exist yet. Create a contact first — every project requires a main contact.")
            else:
                contact_options = sorted(contacts, key=lambda c: c["name"])
                contact_display_options = [f"{c['name']}  ·  {c.get('institution','')}" for c in contact_options]
                dataset_options = sorted(datasets, key=lambda d: d["name"])
                dataset_name_to_id = {d["name"]: d["id"] for d in dataset_options}
                dataset_names_list = list(dataset_name_to_id.keys())

                with st.form("new_project", clear_on_submit=True):
                    col1, col2 = st.columns(2)
                    with col1:
                        p_name = st.text_input("Project Name *")
                        p_status = st.selectbox("Status", PROJECT_STATUSES)
                        p_nda = st.checkbox("NDA/CDA Signed")
                        p_tags = st.text_input("Tags (comma-separated)")
                    with col2:
                        st.markdown("**Main Contact** *(required)*")
                        p_contact_idx = st.selectbox(
                            "Select contact",
                            range(len(contact_options)),
                            format_func=lambda i: contact_display_options[i],
                            key="new_proj_contact",
                        )
                        p_datasets = st.multiselect(
                            "Associated Datasets",
                            options=dataset_names_list,
                            help="Optionally link one or more datasets to this project.",
                        )
                    p_desc = st.text_area("Description")
                    p_notes = st.text_area("Notes")
                    if st.form_submit_button("Create Project", type="primary"):
                        if not p_name:
                            st.error("Project name is required.")
                        else:
                            chosen = contact_options[p_contact_idx]
                            create_project(
                                {
                                    "name": p_name,
                                    "status": p_status,
                                    "main_contact_id": chosen["id"],
                                    "dataset_ids": [dataset_name_to_id[n] for n in p_datasets if n in dataset_name_to_id],
                                    "nda_signed": p_nda,
                                    "tags": [t.strip() for t in p_tags.split(",") if t.strip()],
                                    "description": p_desc,
                                    "notes": p_notes,
                                },
                                current_username(),
                            )
                            st.success(f"Project **{p_name}** created.")
                            st.rerun()

    # ── Tab: Project Detail ──
    detail_tab = tab_objects[-1]
    with detail_tab:
        pid = st.session_state.get("selected_project_id")
        if not pid:
            st.info("Select a project from 'All Projects' to view its detail.")
            return

        project = get_project(pid)
        if not project:
            st.error("Project not found.")
            st.session_state.pop("selected_project_id", None)
            return

        main_contact = contact_map.get(project.get("main_contact_id", ""), {})

        col_hdr, col_actions = st.columns([3, 1])
        with col_hdr:
            st.markdown(f"## {project['name']}")
            st.caption(
                f"Status: **{project.get('status', '—')}**  ·  "
                f"Main Contact: **{main_contact.get('name', '—')}** ({main_contact.get('institution', '—')})"
            )
        with col_actions:
            if can_edit_record(project):
                if st.button("🗑 Delete Project", type="secondary", use_container_width=True):
                    st.session_state["confirm_delete_project"] = pid

        if st.session_state.get("confirm_delete_project") == pid:
            st.warning("**Permanently delete this project?**")
            c1, c2, _ = st.columns([1, 1, 3])
            with c1:
                if st.button("Confirm Delete", type="primary", key="conf_del_proj"):
                    delete_project(pid)
                    st.session_state.pop("selected_project_id", None)
                    st.session_state.pop("confirm_delete_project", None)
                    st.success("Project deleted.")
                    st.rerun()
            with c2:
                if st.button("Cancel", key="canc_del_proj"):
                    st.session_state.pop("confirm_delete_project", None)
                    st.rerun()

        st.divider()

        if can_edit_record(project):
            with st.expander("Edit Project", expanded=True):
                contact_options_d = sorted(contacts, key=lambda c: c["name"])
                contact_display_d = [f"{c['name']}  ·  {c.get('institution','')}" for c in contact_options_d]
                current_c_idx = next(
                    (i for i, c in enumerate(contact_options_d) if c["id"] == project.get("main_contact_id")), 0
                )
                current_status_idx = (
                    PROJECT_STATUSES.index(project.get("status", PROJECT_STATUSES[0]))
                    if project.get("status") in PROJECT_STATUSES else 0
                )
                ds_opts_d = sorted(datasets, key=lambda d: d["name"])
                ds_name_to_id_d = {d["name"]: d["id"] for d in ds_opts_d}
                ds_id_to_name_d = {d["id"]: d["name"] for d in ds_opts_d}
                ds_names_list_d = list(ds_name_to_id_d.keys())
                ds_default_d = [ds_id_to_name_d[did] for did in project.get("dataset_ids", []) if did in ds_id_to_name_d]

                with st.form("edit_project"):
                    col1, col2 = st.columns(2)
                    with col1:
                        ep_name = st.text_input("Project Name", value=project.get("name", ""))
                        ep_status = st.selectbox("Status", PROJECT_STATUSES, index=current_status_idx)
                        ep_nda = st.checkbox("NDA/CDA Signed", value=bool(project.get("nda_signed")))
                        ep_tags = st.text_input("Tags", value=", ".join(project.get("tags", [])))
                    with col2:
                        st.markdown("**Main Contact** *(required)*")
                        ep_contact_idx = st.selectbox(
                            "Contact",
                            range(len(contact_options_d)),
                            index=current_c_idx,
                            format_func=lambda i: contact_display_d[i],
                            key="ep_contact",
                        )
                        ep_datasets = st.multiselect(
                            "Associated Datasets",
                            options=ds_names_list_d,
                            default=ds_default_d,
                            key="ep_datasets",
                        )
                    ep_desc = st.text_area("Description", value=project.get("description", ""))
                    ep_notes = st.text_area("Notes", value=project.get("notes", ""))
                    if st.form_submit_button("Save Changes", type="primary"):
                        if not ep_name:
                            st.error("Project name is required.")
                        else:
                            chosen = contact_options_d[ep_contact_idx]
                            update_project(
                                pid,
                                {
                                    "name": ep_name,
                                    "status": ep_status,
                                    "main_contact_id": chosen["id"],
                                    "dataset_ids": [ds_name_to_id_d[n] for n in ep_datasets if n in ds_name_to_id_d],
                                    "nda_signed": ep_nda,
                                    "tags": [t.strip() for t in ep_tags.split(",") if t.strip()],
                                    "description": ep_desc,
                                    "notes": ep_notes,
                                },
                                current_username(),
                            )
                            st.success("Project updated.")
                            st.rerun()
        else:
            col1, col2 = st.columns(2)
            with col1:
                st.markdown(f"**Status:** {project.get('status', '—')}")
                st.markdown(f"**Main Contact:** {main_contact.get('name', '—')}")
                st.markdown(f"**Institution:** {main_contact.get('institution', '—')}")
                st.markdown(f"**NDA/CDA Signed:** {'Yes' if project.get('nda_signed') else 'No'}")
            with col2:
                linked_ds = [dataset_map[did] for did in project.get("dataset_ids", []) if did in dataset_map]
                if linked_ds:
                    st.markdown("**Datasets:** " + ", ".join(d["name"] for d in linked_ds))
                if project.get("tags"):
                    st.markdown("**Tags:** " + " · ".join(f"`{t}`" for t in project["tags"]))
            if project.get("description"):
                st.markdown(f"**Description:** {project['description']}")
            if project.get("notes"):
                st.markdown(f"**Notes:** {project['notes']}")

        proj_stage_history = get_project_stage_history(pid)
        if proj_stage_history:
            with st.expander("Status Change History", expanded=False):
                for entry in proj_stage_history:
                    old = entry.get("old_stage") or "—"
                    new = entry.get("new_stage") or "—"
                    when = entry.get("changed_at", "")[:10]
                    who = entry.get("changed_by", "")
                    st.markdown(f"**{when}** by {who} — **{old}** → **{new}**")

        st.caption(
            f"Created by **{project.get('created_by')}** on {project.get('created_at','')[:10]}  |  "
            f"Last edited by **{project.get('updated_by')}** on {project.get('updated_at','')[:10]}"
        )


# ─── UI: Admin Panel ─────────────────────────────────────────────────────────


def page_admin():
    if not is_admin():
        st.error("Admin access required.")
        return

    st.title("Admin Panel")

    tab_users, tab_activity = st.tabs(["User Management", "Activity Log"])

    # ── User Management ──
    with tab_users:
        st.subheader("Create New User")
        for role_key, role_desc in ROLE_DESCRIPTIONS.items():
            st.caption(f"**{role_key.capitalize()}** — {role_desc}")
        with st.form("new_user", clear_on_submit=True):
            col1, col2 = st.columns(2)
            with col1:
                nu_username = st.text_input("Username")
                nu_password = st.text_input("Password", type="password")
            with col2:
                nu_role = st.selectbox(
                    "Role",
                    ROLES,
                    index=0,
                    format_func=lambda r: r.capitalize(),
                )
            if st.form_submit_button("Create User", type="primary"):
                if not nu_username or not nu_password:
                    st.error("Username and password are required.")
                elif len(nu_password) < 6:
                    st.error("Password must be at least 6 characters.")
                elif find_user(nu_username):
                    st.error(f"Username '{nu_username}' already exists.")
                else:
                    new_u = create_user(nu_username, nu_password, nu_role, current_username())
                    rc = generate_and_store_recovery_code(new_u["id"])
                    st.session_state["_last_created_user"] = (nu_username, nu_role, rc)
                    st.rerun()

        # Show recovery code for the user that was just created
        if "_last_created_user" in st.session_state:
            lc_name, lc_role, lc_code = st.session_state.pop("_last_created_user")
            st.success(f"User **{lc_name}** created with role **{lc_role}**.")
            st.warning(
                f"**Recovery code for '{lc_name}': `{lc_code}`**\n\n"
                "Share this code with the user and ask them to store it safely. "
                "It will not be shown again. They can use it to reset their password from the login screen."
            )

        st.divider()
        st.subheader("Existing Users")

        for u in get_users():
            # Resolve role — handle legacy records that only stored is_admin
            u_role = u.get("role") or ("admin" if u.get("is_admin") else "viewer")
            role_icon = {"admin": "👑", "editor": "✏️", "viewer": "👁"}.get(u_role, "👤")
            status_suffix = "" if u.get("is_active", True) else " (inactive)"
            with st.expander(f"{role_icon} {u['username']}  ·  {u_role.capitalize()}{status_suffix}"):
                col1, col2, col3 = st.columns(3)
                with col1:
                    st.markdown(f"**Role:** {u_role.capitalize()}")
                    st.markdown(f"**Active:** {'Yes' if u.get('is_active', True) else 'No'}")
                with col2:
                    st.markdown(f"**Created:** {u.get('created_at','')[:10]}")
                    st.markdown(f"**Created by:** {u.get('created_by','')}")

                # Cannot modify your own account here
                if u["username"] == current_username():
                    with col3:
                        st.caption("This is your account. Use the 'Change My Password' tab.")
                else:
                    with col3:
                        # Activate / Deactivate
                        active_label = "Deactivate" if u.get("is_active", True) else "Activate"
                        if st.button(active_label, key=f"active_{u['id']}"):
                            update_user_field(u["id"], "is_active", not u.get("is_active", True))
                            st.rerun()

                        # Role change
                        new_role = st.selectbox(
                            "Change role",
                            ROLES,
                            index=ROLES.index(u_role) if u_role in ROLES else 0,
                            format_func=lambda r: r.capitalize(),
                            key=f"role_sel_{u['id']}",
                        )
                        if st.button("Update Role", key=f"role_upd_{u['id']}"):
                            update_user_field(u["id"], "role", new_role)
                            st.rerun()

                    # Password reset inline
                    with st.form(f"reset_{u['id']}"):
                        new_pw = st.text_input(
                            "Set New Password", type="password", key=f"pw_{u['id']}"
                        )
                        if st.form_submit_button("Reset Password"):
                            if not new_pw:
                                st.error("Password cannot be empty.")
                            else:
                                reset_user_password(u["id"], new_pw)
                                st.success(f"Password for '{u['username']}' reset.")

    # ── Activity Log ──
    with tab_activity:
        st.subheader("Full Activity Log")
        contacts = get_contacts()
        meetings = get_meetings()
        datasets = get_datasets()
        contact_map = {c["id"]: c for c in contacts}
        contact_name_map = {c["id"]: c["name"] for c in contacts}

        rows = []
        for c in contacts:
            rows.append(
                {
                    "Timestamp": c.get("created_at", ""),
                    "User": c.get("created_by", ""),
                    "Action": "Created Contact",
                    "Detail": c.get("name", ""),
                }
            )
            if c.get("updated_at") != c.get("created_at"):
                rows.append(
                    {
                        "Timestamp": c.get("updated_at", ""),
                        "User": c.get("updated_by", ""),
                        "Action": "Updated Contact",
                        "Detail": c.get("name", ""),
                    }
                )
        for m in meetings:
            rows.append(
                {
                    "Timestamp": m.get("created_at", ""),
                    "User": m.get("created_by", ""),
                    "Action": "Logged Meeting",
                    "Detail": f"{meeting_contact_label(m, contact_map)} — {m.get('summary','')}",
                }
            )
        for d in datasets:
            rows.append(
                {
                    "Timestamp": d.get("created_at", ""),
                    "User": d.get("created_by", ""),
                    "Action": "Added Dataset",
                    "Detail": f"{d.get('name','')} ({contact_name_map.get(d.get('contact_id',''),'Unknown')})",
                }
            )
            if d.get("updated_at") != d.get("created_at"):
                rows.append(
                    {
                        "Timestamp": d.get("updated_at", ""),
                        "User": d.get("updated_by", ""),
                        "Action": "Updated Dataset",
                        "Detail": f"{d.get('name','')} → {d.get('status','')}",
                    }
                )

        if rows:
            df = pd.DataFrame(sorted(rows, key=lambda x: x["Timestamp"], reverse=True))
            st.dataframe(df, use_container_width=True, hide_index=True)
            csv = df.to_csv(index=False).encode()
            st.download_button(
                "Export Activity Log",
                data=csv,
                file_name="activity_log.csv",
                mime="text/csv",
            )
        else:
            st.info("No activity recorded yet.")


# ─── Main ─────────────────────────────────────────────────────────────────────


def main():

    # Password recovery flow (accessible without being logged in)
    if st.session_state.get("_recovery_mode"):
        page_password_recovery()
        return

    # Gate: must be logged in
    if "user" not in st.session_state:
        page_login()
        return

    user = st.session_state["user"]

    with st.sidebar:
        st.markdown("## 🔬 Research CRM")
        st.divider()
        st.markdown(f"**{user['username']}**")
        role_label = get_role().capitalize()
        st.caption(f"Role: {role_label}")
        st.divider()

        pages = ["Dashboard", "Contacts", "Meetings", "Datasets", "Projects"]
        if is_admin():
            pages.append("Admin Panel")
        pages.append("Change Password")

        page = st.radio(
            "Navigate",
            pages,
            label_visibility="collapsed",
            key="current_page",
        )

        st.divider()
        if st.button("Log Out", use_container_width=True):
            st.session_state.clear()
            st.rerun()

    if page == "Dashboard":
        page_dashboard()
    elif page == "Contacts":
        page_contacts()
    elif page == "Meetings":
        page_meetings()
    elif page == "Datasets":
        page_datasets()
    elif page == "Projects":
        page_projects()
    elif page == "Admin Panel":
        page_admin()
    elif page == "Change Password":
        page_change_password()


if __name__ == "__main__":
    main()
