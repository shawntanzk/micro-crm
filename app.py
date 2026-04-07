"""
Research Partnership CRM
A lightweight CRM for tracking research partnership leads.
Multi-user, admin-controlled, file-backed.
"""

import streamlit as st
import json
import os
import uuid
from datetime import datetime
import pandas as pd
from filelock import FileLock
import bcrypt
import plotly.express as px

# ─── Page Config ──────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Research Partnership CRM",
    page_icon="🔬",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ─── Data Directory ───────────────────────────────────────────────────────────

# On Domino, point DATA_DIR at your persisted dataset mount, e.g.:
#   DATA_DIR = "/domino/datasets/local/crm_data"
# For local development the sibling `data/` folder is used.
DATA_DIR = os.environ.get(
    "CRM_DATA_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"),
)
os.makedirs(DATA_DIR, exist_ok=True)

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

STAGE_COLORS = {
    "Prospect": "#94a3b8",
    "Initial Contact": "#60a5fa",
    "Proposal Sent": "#f59e0b",
    "In Negotiation": "#f97316",
    "Active Partnership": "#22c55e",
    "On Hold": "#a78bfa",
    "Inactive": "#f87171",
}

SESSION_TIMEOUT_MINUTES = 30

# ─── Low-Level File Helpers ───────────────────────────────────────────────────


def _path(filename: str) -> str:
    return os.path.join(DATA_DIR, filename)


def _load(filename: str) -> list:
    """Read JSON; return [] on missing/corrupt file."""
    p = _path(filename)
    if not os.path.exists(p):
        return []
    with open(p, "r") as fh:
        try:
            return json.load(fh)
        except (json.JSONDecodeError, ValueError):
            return []


def _atomic_update(filename: str, fn):
    """
    Hold a file lock, read current data, apply fn(data) -> new_data, write back.
    Returns the new data list.  Safe for concurrent Streamlit sessions.
    """
    lock = FileLock(_path(filename) + ".lock", timeout=15)
    with lock:
        data = _load(filename)
        result = fn(data)
        if result is None:
            result = data
        with open(_path(filename), "w") as fh:
            json.dump(result, fh, indent=2, default=str)
        return result


def check_session_timeout():
    """Log out the user after SESSION_TIMEOUT_MINUTES of inactivity."""
    if "user" not in st.session_state:
        return
    last = st.session_state.get("last_activity")
    if last:
        elapsed = (datetime.now() - datetime.fromisoformat(last)).total_seconds() / 60
        if elapsed > SESSION_TIMEOUT_MINUTES:
            st.session_state.clear()
            st.warning(f"You were logged out after {SESSION_TIMEOUT_MINUTES} minutes of inactivity.")
            st.stop()
    st.session_state["last_activity"] = datetime.now().isoformat()


# ─── Auth Helpers ─────────────────────────────────────────────────────────────


def get_users() -> list:
    return _load("users.json")


def find_user(username: str) -> dict | None:
    for u in get_users():
        if u["username"].lower() == username.strip().lower():
            return u
    return None


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
    new_user = {
        "id": str(uuid.uuid4()),
        "username": username,
        "password_hash": hash_password(password),
        "role": role,
        "is_active": True,
        "created_at": datetime.now().isoformat(),
        "created_by": created_by,
    }

    def _add(data):
        data.append(new_user)
        return data

    _atomic_update("users.json", _add)
    return new_user


def update_user_field(uid: str, field: str, value) -> None:
    def _upd(data):
        for u in data:
            if u["id"] == uid:
                u[field] = value
        return data

    _atomic_update("users.json", _upd)


def reset_user_password(uid: str, new_password: str) -> None:
    def _upd(data):
        for u in data:
            if u["id"] == uid:
                u["password_hash"] = hash_password(new_password)
        return data

    _atomic_update("users.json", _upd)


# ─── CRUD: Contacts ───────────────────────────────────────────────────────────


def get_contacts() -> list:
    return _load("contacts.json")


def get_contact(cid: str) -> dict | None:
    for c in get_contacts():
        if c["id"] == cid:
            return c
    return None


def create_contact(fields: dict, username: str) -> dict:
    now = datetime.now().isoformat()
    record = {
        "id": str(uuid.uuid4()),
        "created_at": now,
        "created_by": username,
        "updated_at": now,
        "updated_by": username,
        "last_contacted": None,
        **fields,
    }

    def _add(data):
        data.append(record)
        return data

    _atomic_update("contacts.json", _add)
    return record


def update_contact(cid: str, fields: dict, username: str) -> None:
    def _upd(data):
        for c in data:
            if c["id"] == cid:
                c.update(fields)
                c["updated_at"] = datetime.now().isoformat()
                c["updated_by"] = username
        return data

    _atomic_update("contacts.json", _upd)


def delete_contact(cid: str) -> None:
    _atomic_update("contacts.json", lambda d: [c for c in d if c["id"] != cid])


# ─── CRUD: Meetings ───────────────────────────────────────────────────────────


def get_meetings() -> list:
    return _load("meetings.json")


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
    record = {
        "id": str(uuid.uuid4()),
        "created_at": datetime.now().isoformat(),
        "created_by": username,
        **fields,
    }

    def _add(data):
        data.append(record)
        return data

    _atomic_update("meetings.json", _add)
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
    return result


def update_meeting(mid: str, fields: dict, username: str) -> None:
    def _upd(data):
        for m in data:
            if m["id"] == mid:
                m.update(fields)
                m["updated_at"] = datetime.now().isoformat()
                m["updated_by"] = username
        return data

    _atomic_update("meetings.json", _upd)
    meeting = next((m for m in get_meetings() if m["id"] == mid), None)
    if meeting:
        for cid in _meeting_contact_ids(meeting):
            _recalculate_last_contacted(cid, username)


def delete_meeting(mid: str, username: str = "system") -> None:
    meeting = next((m for m in get_meetings() if m["id"] == mid), None)
    linked_cids = _meeting_contact_ids(meeting) if meeting else []
    _atomic_update("meetings.json", lambda d: [m for m in d if m["id"] != mid])
    for cid in linked_cids:
        _recalculate_last_contacted(cid, username)


# ─── CRUD: Datasets ───────────────────────────────────────────────────────────


def get_datasets() -> list:
    return _load("datasets.json")


def get_datasets_for_contact(cid: str) -> list:
    return [d for d in get_datasets() if d["contact_id"] == cid]


def create_dataset(fields: dict, username: str) -> dict:
    now = datetime.now().isoformat()
    record = {
        "id": str(uuid.uuid4()),
        "created_at": now,
        "created_by": username,
        "updated_at": now,
        "updated_by": username,
        **fields,
    }

    def _add(data):
        data.append(record)
        return data

    _atomic_update("datasets.json", _add)
    return record


def update_dataset(did: str, fields: dict, username: str) -> None:
    def _upd(data):
        for d in data:
            if d["id"] == did:
                d.update(fields)
                d["updated_at"] = datetime.now().isoformat()
                d["updated_by"] = username
        return data

    _atomic_update("datasets.json", _upd)


def delete_dataset(did: str) -> None:
    _atomic_update("datasets.json", lambda d: [x for x in d if x["id"] != did])


# ─── UI: Login / First-Run Setup ──────────────────────────────────────────────


def page_login():
    st.markdown(
        "<h1 style='text-align:center'>🔬 Research Partnership CRM</h1>",
        unsafe_allow_html=True,
    )
    st.markdown("")

    users = get_users()

    # ── First-run: no users exist ──
    if not users:
        st.info("No accounts found. Create the first admin account to get started.")
        with st.form("first_setup"):
            username = st.text_input("Choose a username")
            password = st.text_input("Choose a password", type="password")
            confirm = st.text_input("Confirm password", type="password")
            submitted = st.form_submit_button("Create Admin Account", type="primary")
        if submitted:
            if not username or not password:
                st.error("Username and password are required.")
            elif password != confirm:
                st.error("Passwords do not match.")
            else:
                create_user(username, password, role="admin", created_by="system")
                st.success("Admin account created — please log in.")
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


# ─── UI: Dashboard ────────────────────────────────────────────────────────────


def page_dashboard():
    st.title("Dashboard")

    contacts = get_contacts()
    meetings = get_meetings()
    datasets = get_datasets()
    contact_map = {c["id"]: c for c in contacts}

    # ── Top KPIs ──
    k1, k2, k3, k4, k5 = st.columns(5)
    k1.metric("Total Contacts", len(contacts))
    k2.metric("Total Meetings", len(meetings))
    k3.metric("Total Datasets", len(datasets))
    active = sum(1 for c in contacts if c.get("partnership_stage") == "Active Partnership")
    k4.metric("Active Partnerships", active)
    prospects = sum(1 for c in contacts if c.get("partnership_stage") == "Prospect")
    k5.metric("Prospects", prospects)

    st.divider()

    # ── Charts ──
    col_left, col_right = st.columns(2)

    with col_left:
        if contacts:
            stage_counts: dict[str, int] = {}
            for c in contacts:
                s = c.get("partnership_stage", "Unknown")
                stage_counts[s] = stage_counts.get(s, 0) + 1
            colors = [STAGE_COLORS.get(s, "#94a3b8") for s in stage_counts.keys()]
            fig = px.pie(
                values=list(stage_counts.values()),
                names=list(stage_counts.keys()),
                title="Contacts by Partnership Stage",
                color_discrete_sequence=colors,
            )
            fig.update_traces(textposition="inside", textinfo="percent+label")
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("No contacts yet.")

    with col_right:
        if meetings:
            df_m = pd.DataFrame(meetings)
            df_m["date"] = pd.to_datetime(df_m["date"], errors="coerce")
            df_m["month"] = df_m["date"].dt.to_period("M").astype(str)
            monthly = df_m.groupby("month").size().reset_index(name="count")
            fig2 = px.bar(monthly, x="month", y="count", title="Meetings per Month")
            st.plotly_chart(fig2, use_container_width=True)
        else:
            st.info("No meetings logged yet.")

    # ── Dataset status breakdown ──
    if datasets:
        st.subheader("Dataset Pipeline")
        status_counts: dict[str, int] = {s: 0 for s in DATASET_STATUSES}
        for d in datasets:
            s = d.get("status", "Unknown")
            status_counts[s] = status_counts.get(s, 0) + 1
        cols = st.columns(len(DATASET_STATUSES))
        for i, s in enumerate(DATASET_STATUSES):
            cols[i].metric(s, status_counts.get(s, 0))

    st.divider()

    # ── Recent meetings ──
    st.subheader("Recent Meetings")
    if meetings:
        recent = sorted(meetings, key=lambda m: m.get("date", ""), reverse=True)[:8]
        rows = []
        for m in recent:
            c = contact_map.get(m.get("contact_id", ""), {})
            rows.append(
                {
                    "Date": m.get("date"),
                    "Contact": meeting_contact_label(m, contact_map),
                    "Institution": c.get("institution", ""),
                    "Type": m.get("meeting_type"),
                    "Summary": m.get("summary", ""),
                    "Logged By": m.get("created_by"),
                }
            )
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    else:
        st.info("No meetings logged yet.")

    # ── Contacts needing follow-up (no contact in 60+ days) ──
    if contacts:
        st.subheader("Follow-up Needed (60+ days since last contact)")
        today = datetime.today().date()
        overdue = []
        for c in contacts:
            if c.get("partnership_stage") in ("Inactive", "On Hold"):
                continue
            lc = c.get("last_contacted")
            if lc:
                try:
                    days = (today - datetime.fromisoformat(lc).date()).days
                    if days >= 60:
                        overdue.append(
                            {
                                "Name": c["name"],
                                "Institution": c.get("institution", ""),
                                "Stage": c.get("partnership_stage", ""),
                                "Last Contacted": lc,
                                "Days Ago": days,
                            }
                        )
                except ValueError:
                    pass
            else:
                overdue.append(
                    {
                        "Name": c["name"],
                        "Institution": c.get("institution", ""),
                        "Stage": c.get("partnership_stage", ""),
                        "Last Contacted": "Never",
                        "Days Ago": "—",
                    }
                )
        if overdue:
            st.dataframe(pd.DataFrame(overdue), use_container_width=True, hide_index=True)
        else:
            st.success("All active contacts have been reached in the last 60 days.")


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
            st.caption(
                f"{contact.get('role','—')} @ {contact.get('institution','—')} · "
                f"Last contacted: **{contact.get('last_contacted') or 'Never'}**"
            )

        with col_actions:
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
                if contact.get("tags"):
                    st.markdown("**Tags:** " + " · ".join(f"`{t}`" for t in contact["tags"]))
            if contact.get("notes"):
                st.markdown(f"**Notes:** {contact['notes']}")

        # Audit trail
        st.caption(
            f"Created by **{contact.get('created_by')}** on {contact.get('created_at','')[:10]}  |  "
            f"Last edited by **{contact.get('updated_by')}** on {contact.get('updated_at','')[:10]}"
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
                for m in sorted(meetings, key=lambda x: x.get("date", ""), reverse=True):
                    label = (
                        f"📅 {m.get('date')}  ·  {m.get('meeting_type')}  ·  {m.get('summary','')}"
                    )
                    with st.expander(label):
                        all_att_cd = meeting_all_attendees(m, contact_map_cd)
                        if all_att_cd:
                            st.markdown(f"**Attendees:** {', '.join(all_att_cd)}")
                        st.markdown(f"**Notes:**\n\n{m.get('notes') or '—'}")
                        if m.get("action_items"):
                            st.markdown(f"**Action Items:**\n\n{m['action_items']}")
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
                                        e_attendees = st.text_input(
                                            "Additional attendees", value=", ".join(m.get("attendees", [])), key=f"ecd_att_{m['id']}"
                                        )
                                    e_notes = st.text_area("Notes", value=m.get("notes", ""), key=f"ecd_notes_{m['id']}")
                                    e_actions = st.text_area("Action Items", value=m.get("action_items", ""), key=f"ecd_act_{m['id']}")
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
                                                    "attendees": [a.strip() for a in e_attendees.split(",") if a.strip()],
                                                    "notes": e_notes,
                                                    "action_items": e_actions,
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
    contact_map = {c["id"]: c for c in contacts}

    tabs = ["All Meetings"]
    if can_write():
        tabs.append("Log Meeting")
    tab_objects = st.tabs(tabs)

    # ── Tab: Log Meeting ──
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
                    m_attendees = st.text_input("Additional attendees (comma-separated)")
                m_notes = st.text_area("Meeting Notes")
                m_actions = st.text_area("Action Items")

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
                                "attendees": [a.strip() for a in m_attendees.split(",") if a.strip()],
                                "notes": m_notes,
                                "action_items": m_actions,
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
            col1, col2 = st.columns(2)
            with col1:
                search = st.text_input("Search notes or summary")
            with col2:
                type_filter = st.selectbox("Meeting Type", ["All"] + MEETING_TYPES)
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
            _OTHER = "— Other (not in system) —"
            for m in filtered:
                label = (
                    f"📅 {m.get('date')}  ·  {meeting_contact_label(m, contact_map)}"
                    f"  ·  {m.get('summary','')}"
                )
                with st.expander(label):
                    all_att = meeting_all_attendees(m, contact_map)
                    if all_att:
                        st.markdown(f"**Attendees:** {', '.join(all_att)}")
                    st.markdown(f"**Notes:**\n\n{m.get('notes') or '—'}")
                    if m.get("action_items"):
                        st.markdown(f"**Action Items:**\n\n{m['action_items']}")
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
                                    e_attendees = st.text_input(
                                        "Additional attendees", value=", ".join(m.get("attendees", [])), key=f"em_att_{m['id']}"
                                    )
                                e_notes = st.text_area("Notes", value=m.get("notes", ""), key=f"em_notes_{m['id']}")
                                e_actions = st.text_area("Action Items", value=m.get("action_items", ""), key=f"em_act_{m['id']}")
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
                                                "attendees": [a.strip() for a in e_attendees.split(",") if a.strip()],
                                                "notes": e_notes,
                                                "action_items": e_actions,
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

            col1, col2 = st.columns(2)
            with col1:
                status_filter = st.selectbox("Filter by Status", ["All"] + DATASET_STATUSES)
            with col2:
                search = st.text_input("Search name or description")

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
                    updated_ds = d.get("updated_by")
                    if updated_ds:
                        st.caption(
                            f"Added by **{d.get('created_by')}** on {d.get('created_at','')[:10]}"
                            f"  ·  Last edited by **{updated_ds}** on {d.get('updated_at','')[:10]}"
                        )
                    else:
                        st.caption(f"Added by **{d.get('created_by')}** on {d.get('created_at','')[:10]}")
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
                elif find_user(nu_username):
                    st.error(f"Username '{nu_username}' already exists.")
                else:
                    create_user(nu_username, nu_password, nu_role, current_username())
                    st.success(f"User '{nu_username}' created with role '{nu_role}'.")
                    st.rerun()

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
    # Check session timeout on every render
    check_session_timeout()

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

        pages = ["Dashboard", "Contacts", "Meetings", "Datasets"]
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
    elif page == "Admin Panel":
        page_admin()
    elif page == "Change Password":
        page_change_password()


if __name__ == "__main__":
    main()
