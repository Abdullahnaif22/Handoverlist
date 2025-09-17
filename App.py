# ENT Handover App — Streamlit (MVP, secrets‑safe)
# -------------------------------------------------------------
# Purpose: A simple shared handover app for ENT at Glan Clwyd.
# This version avoids Streamlit secrets crashes and persists locally if
# no Google Sheets credentials are present. It also supports environment
# variables as an alternative to secrets.
# -------------------------------------------------------------

import os
import json
import uuid
from pathlib import Path
from datetime import datetime
from typing import Optional

import streamlit as st
import pandas as pd

# ----------------------------
# Page config
# ----------------------------
st.set_page_config(page_title="ENT Handover — Glan Clwyd", page_icon="🩺", layout="wide")

# ----------------------------
# Constants & Columns
# ----------------------------
PATIENT_COLUMNS = [
    "uid",  # hidden internal id
    "Patient Name",
    "Hospital Number",
    "NHS Number",
    "Date of Birth",
    "Ward/Bed",
    "Reason for Admission",
    "PMH/PSH/DH",
    "Progress",
    "Jobs",
    "Priority",  # Low / Medium / High
    "Assigned To",
    "Status",  # Active / Discharged
    "Last Updated",
]

AUDIT_COLUMNS = [
    "timestamp",
    "user_role",
    "user_initials",
    "action",
    "uid",
    "patient_name",
    "details",
]

DEFAULT_PRIORITY = "Medium"
DEFAULT_STATUS = "Active"

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
LOCAL_PATIENTS_CSV = DATA_DIR / "patients.csv"
LOCAL_AUDIT_CSV = DATA_DIR / "audit_log.csv"

# ----------------------------
# Secrets/Env helpers (won't crash without secrets)
# ----------------------------

def safe_get_secret(key: str, default: Optional[str] = None):
    """Get a secret from st.secrets if available; else env var; else default.
    This never raises StreamlitSecretNotFoundError.
    """
    # Try Streamlit secrets
    try:
        # Accessing st.secrets may raise if no secrets file; guard with try
        if key in st.secrets:  # type: ignore[operator]
            return st.secrets.get(key, default)  # type: ignore[attr-defined]
    except Exception:
        pass
    # Env var fallback (upper case)
    env_val = os.getenv(key) or os.getenv(key.upper())
    if env_val is not None:
        return env_val
    return default


def load_service_account_from_sources() -> Optional[dict]:
    # 1) Try secrets key containing JSON object
    try:
        if "gcp_service_account" in st.secrets:  # type: ignore[operator]
            val = st.secrets.get("gcp_service_account", None)
            if isinstance(val, dict):
                return val
            if isinstance(val, str) and val.strip():
                return json.loads(val)
    except Exception:
        pass
    # 2) Try env var GCP_SERVICE_ACCOUNT_JSON (stringified JSON)
    env_json = os.getenv("GCP_SERVICE_ACCOUNT_JSON")
    if env_json:
        try:
            return json.loads(env_json)
        except Exception:
            pass
    return None

# ----------------------------
# Backend: Google Sheets (with CSV fallback)
# ----------------------------
@st.cache_resource(show_spinner=False)
def _init_backend():
    """Return backend dict describing whether Google Sheets is configured."""
    # Optional deps
    try:
        import gspread  # type: ignore
        from google.oauth2.service_account import Credentials  # type: ignore
    except Exception:
        gspread = None
        Credentials = None

    creds_info = load_service_account_from_sources()
    sheet_url = safe_get_secret("SPREADSHEET_URL", default=None)

    if gspread and Credentials and creds_info and sheet_url:
        try:
            scopes = [
                "https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive",
            ]
            creds = Credentials.from_service_account_info(creds_info, scopes=scopes)
            client = gspread.authorize(creds)
            sh = client.open_by_url(sheet_url)
            # Ensure worksheets exist
            def get_or_create_ws(title, header):
                try:
                    ws = sh.worksheet(title)
                except Exception:
                    ws = sh.add_worksheet(title=title, rows=2000, cols=len(header) + 2)
                    ws.append_row(header)
                # Ensure header first row
                try:
                    first_row = ws.row_values(1)
                    if first_row != header:
                        ws.delete_rows(1)
                        ws.insert_row(header, 1)
                except Exception:
                    pass
                return ws

            p_ws = get_or_create_ws("patients", PATIENT_COLUMNS)
            a_ws = get_or_create_ws("audit_log", AUDIT_COLUMNS)
            return {"type": "sheets", "p_ws": p_ws, "a_ws": a_ws}
        except Exception as e:
            st.warning(f"Google Sheets backend not available: {e}. Falling back to local CSV.")

    # Fallback: local CSV files
    return {"type": "csv", "patients_path": LOCAL_PATIENTS_CSV, "audit_path": LOCAL_AUDIT_CSV}


backend = _init_backend()


def _load_csv(path: Path, columns: list[str]) -> pd.DataFrame:
    if path.exists() and path.stat().st_size > 0:
        try:
            df = pd.read_csv(path)
            # Ensure all columns exist
            for c in columns:
                if c not in df.columns:
                    df[c] = ""
            return df[columns]
        except Exception:
            pass
    return pd.DataFrame(columns=columns)


def load_data():
    if backend["type"] == "sheets":
        patients = backend["p_ws"].get_all_records()
        audit = backend["a_ws"].get_all_records()
        p_df = pd.DataFrame(patients, columns=PATIENT_COLUMNS)
        a_df = pd.DataFrame(audit, columns=AUDIT_COLUMNS)
    else:
        p_df = _load_csv(backend["patients_path"], PATIENT_COLUMNS)
        a_df = _load_csv(backend["audit_path"], AUDIT_COLUMNS)
    if not p_df.empty:
        p_df["Last Updated"] = pd.to_datetime(p_df["Last Updated"], errors="coerce")
    return p_df, a_df


def _write_patients(df: pd.DataFrame):
    if backend["type"] == "sheets":
        ws = backend["p_ws"]
        ws.resize(rows=1)  # keep header only
        if not df.empty:
            ws.update(
                "A2",
                [df[col].astype(str).fillna("").tolist() for col in df.columns],
                raw=False,
                major_dimension="COLUMNS",
            )
    else:
        df.to_csv(backend["patients_path"], index=False)


def _append_audit(row: dict):
    if backend["type"] == "sheets":
        backend["a_ws"].append_row([row.get(c, "") for c in AUDIT_COLUMNS])
    else:
        df = _load_csv(backend["audit_path"], AUDIT_COLUMNS)
        df.loc[len(df)] = [row.get(c, "") for c in AUDIT_COLUMNS]
        df.to_csv(backend["audit_path"], index=False)


# ----------------------------
# Auth (very lightweight MVP)
# ----------------------------
with st.sidebar:
    st.markdown("### 👤 Role & Access")
    role = st.selectbox("Select your role", ["Doctor", "Nurse", "Admin"], index=0)

    doctor_key = safe_get_secret("ENT_DOCTOR_KEY", "")
    nurse_key = safe_get_secret("ENT_NURSE_KEY", "")
    admin_key = safe_get_secret("ENT_ADMIN_KEY", "")

    required_key = {"Doctor": doctor_key, "Nurse": nurse_key, "Admin": admin_key}.get(role, "")

    pass_ok = True
    if required_key:
        entered = st.text_input("Department passcode", type="password")
        pass_ok = (entered == required_key)
        if not pass_ok:
            st.info("Enter the department passcode to enable editing.")

    initials = st.text_input("Your initials (for audit)", max_chars=6).upper()

p_df, a_df = load_data()

# ----------------------------
# Helper functions
# ----------------------------

def _now_iso():
    return datetime.now().isoformat(timespec="seconds")


def _new_uid():
    return str(uuid.uuid4())


def upsert_patient(record: dict, *, editor_role: str, editor_initials: str):
    global p_df
    rec = record.copy()
    if not rec.get("uid"):
        rec["uid"] = _new_uid()
    rec["Last Updated"] = _now_iso()
    for c in PATIENT_COLUMNS:
        if c not in rec:
            rec[c] = ""

    if not p_df.empty and rec["uid"] in p_df["uid"].values:
        idx = p_df.index[p_df["uid"] == rec["uid"]][0]
        p_df.loc[idx, PATIENT_COLUMNS] = [rec[c] for c in PATIENT_COLUMNS]
        action = "update"
    else:
        new_row = pd.DataFrame([rec], columns=PATIENT_COLUMNS)
        p_df = pd.concat([p_df, new_row], ignore_index=True)
        action = "create"

    _write_patients(p_df)
    _append_audit({
        "timestamp": _now_iso(),
        "user_role": editor_role,
        "user_initials": editor_initials,
        "action": action,
        "uid": rec["uid"],
        "patient_name": rec.get("Patient Name", ""),
        "details": f"{action} by {editor_initials}",
    })
    return rec["uid"]


def discharge_patient(uid: str, *, editor_role: str, editor_initials: str):
    global p_df
    if p_df.empty or uid not in p_df["uid"].values:
        return False
    idx = p_df.index[p_df["uid"] == uid][0]
    p_df.loc[idx, "Status"] = "Discharged"
    p_df.loc[idx, "Last Updated"] = _now_iso()
    _write_patients(p_df)
    _append_audit({
        "timestamp": _now_iso(),
        "user_role": editor_role,
        "user_initials": editor_initials,
        "action": "discharge",
        "uid": uid,
        "patient_name": p_df.loc[idx, "Patient Name"],
        "details": f"discharged by {editor_initials}",
    })
    return True


# ----------------------------
# Sidebar: Filters & Quick Add
# ----------------------------
with st.sidebar:
    st.markdown("### 🔎 Filters")
    q = st.text_input("Search name / NHS / Hosp No / Ward")
    only_active = st.toggle("Show Active only", value=True)
    priority_filter = st.multiselect("Priority", ["High", "Medium", "Low"], default=["High", "Medium", "Low"])
    ward_filter = st.text_input("Ward contains")

    st.markdown("---")
    st.markdown("### ➕ Quick Add (MVP)")
    with st.form("quick_add"):
        qa_name = st.text_input("Patient Name")
        qa_hosp = st.text_input("Hospital Number")
        qa_nhs = st.text_input("NHS Number")
        qa_dob = st.date_input("Date of Birth", format="DD/MM/YYYY")
        qa_reason = st.text_area("Reason for Admission", height=80)
        submit_qa = st.form_submit_button("Add Patient", use_container_width=True, disabled=not pass_ok)
        if submit_qa:
            if not qa_name.strip():
                st.error("Name is required.")
            else:
                rec = {
                    "uid": _new_uid(),
                    "Patient Name": qa_name.strip(),
                    "Hospital Number": qa_hosp.strip(),
                    "NHS Number": qa_nhs.strip(),
                    "Date of Birth": qa_dob.strftime("%d/%m/%Y"),
                    "Ward/Bed": "",
                    "Reason for Admission": qa_reason.strip(),
                    "PMH/PSH/DH": "",
                    "Progress": "",
                    "Jobs": "",
                    "Priority": DEFAULT_PRIORITY,
                    "Assigned To": "",
                    "Status": DEFAULT_STATUS,
                    "Last Updated": _now_iso(),
                }
                upsert_patient(rec, editor_role=role, editor_initials=initials or role[:2])
                st.success("Patient added.")

# ----------------------------
# Main: Navigation
# ----------------------------
st.title("🩺 ENT Handover — Glan Clwyd (MVP)")

TABS = ["Board", "Add/Update", "Jobs & Views", "Audit Log", "Export/Print", "About"]
page = st.tabs(TABS)

# Filtered dataframe for display
view_df = p_df.copy()
if q:
    ql = q.lower()
    view_df = view_df[view_df.apply(lambda r: any(str(r[c]).lower().find(ql) >= 0 for c in [
        "Patient Name", "NHS Number", "Hospital Number", "Ward/Bed", "Reason for Admission", "Assigned To"
    ]), axis=1)]
if only_active:
    view_df = view_df[view_df["Status"].fillna("").str.lower() == "active"]
if priority_filter:
    view_df = view_df[view_df["Priority"].isin(priority_filter)]
if ward_filter:
    view_df = view_df[view_df["Ward/Bed"].str.contains(ward_filter, case=False, na=False)]

# Sort: High priority first, then latest updated
priority_rank = {"High": 0, "Medium": 1, "Low": 2}
view_df["_prio"] = view_df["Priority"].map(lambda x: priority_rank.get(str(x), 99))
view_df = view_df.sort_values(["_prio", "Last Updated"], ascending=[True, False])

# ----------------------------
# Tab 1: Board
# ----------------------------
with page[0]:
    st.subheader("📋 Handover Board")
    board_cols = [c for c in PATIENT_COLUMNS if c not in ["uid"]]
    if view_df.empty:
        st.info("No patients to display with current filters.")
    else:
        st.dataframe(
            view_df[board_cols],
            use_container_width=True,
            hide_index=True,
        )

# ----------------------------
# Tab 2: Add/Update
# ----------------------------
with page[1]:
    st.subheader("✍️ Add or Update Patient")
    left, right = st.columns([1,1])

    with left:
        selection = None
        if not p_df.empty:
            options = p_df.sort_values("Patient Name")["Patient Name"].tolist()
            selection = st.selectbox("Select an existing patient to edit", ["— New —"] + options)
        else:
            selection = "— New —"

        if selection and selection != "— New —":
            rec = p_df[p_df["Patient Name"] == selection].iloc[0].to_dict()
        else:
            rec = {c: "" for c in PATIENT_COLUMNS}
            rec["uid"] = ""
            rec["Priority"] = DEFAULT_PRIORITY
            rec["Status"] = DEFAULT_STATUS

    with right:
        with st.form("edit_form"):
            c1, c2, c3 = st.columns(3)
            with c1:
                name = st.text_input("Patient Name", value=rec.get("Patient Name", ""))
                hosp = st.text_input("Hospital Number", value=rec.get("Hospital Number", ""))
                nhs = st.text_input("NHS Number", value=rec.get("NHS Number", ""))
            with c2:
                dob = st.text_input("Date of Birth (DD/MM/YYYY)", value=rec.get("Date of Birth", ""))
                ward = st.text_input("Ward/Bed", value=rec.get("Ward/Bed", ""))
                assigned = st.text_input("Assigned To", value=rec.get("Assigned To", ""))
            with c3:
                priority = st.selectbox("Priority", ["High", "Medium", "Low"], index=["High","Medium","Low"].index(rec.get("Priority", DEFAULT_PRIORITY)))
                status = st.selectbox("Status", ["Active", "Discharged"], index=["Active","Discharged"].index(rec.get("Status", DEFAULT_STATUS)))
                last_upd = rec.get("Last Updated", "")
                st.text_input("Last Updated", value=str(last_upd), disabled=True)

            pmhpshdh = st.text_area("PMH/PSH/DH", value=rec.get("PMH/PSH/DH", ""), height=120)
            reason = st.text_area("Reason for Admission", value=rec.get("Reason for Admission", ""), height=100)
            progress = st.text_area("Progress in Hospital", value=rec.get("Progress", ""), height=140)
            jobs = st.text_area("Jobs (use - [ ] task / - [x] done)", value=rec.get("Jobs", ""), height=160)

            c4, c5 = st.columns([1,1])
            with c4:
                submitted = st.form_submit_button("Save", use_container_width=True, disabled=not pass_ok)
            with c5:
                discharge = st.form_submit_button("Discharge", use_container_width=True, disabled=not pass_ok or not rec.get("uid"))

            if submitted:
                if not name.strip():
                    st.error("Patient name is required.")
                else:
                    new_rec = {
                        "uid": rec.get("uid", ""),
                        "Patient Name": name.strip(),
                        "Hospital Number": hosp.strip(),
                        "NHS Number": nhs.strip(),
                        "Date of Birth": dob.strip(),
                        "Ward/Bed": ward.strip(),
                        "Reason for Admission": reason.strip(),
                        "PMH/PSH/DH": pmhpshdh.strip(),
                        "Progress": progress.strip(),
                        "Jobs": jobs.strip(),
                        "Priority": priority,
                        "Assigned To": assigned.strip(),
                        "Status": status,
                        "Last Updated": _now_iso(),
                    }
                    uid = upsert_patient(new_rec, editor_role=role, editor_initials=initials or role[:2])
                    st.success("Saved.")

            if discharge and rec.get("uid"):
                ok = discharge_patient(rec["uid"], editor_role=role, editor_initials=initials or role[:2])
                if ok:
                    st.success("Patient discharged.")

# ----------------------------
# Tab 3: Jobs & Views
# ----------------------------
with page[2]:
    st.subheader("🧾 Jobs & Quick Views")

    def parse_jobs(jtxt: str):
        items = []
        for line in (jtxt or "").splitlines():
            line = line.strip()
            if line.startswith("- [x]") or line.startswith("- [X]"):
                items.append((line[5:].strip(), True))
            elif line.startswith("- [ ]"):
                items.append((line[5:].strip(), False))
        return items

    pending = []
    for _, r in view_df.iterrows():
        for task, done in parse_jobs(r.get("Jobs", "")):
            if not done:
                pending.append({
                    "Patient": r.get("Patient Name", ""),
                    "Ward/Bed": r.get("Ward/Bed", ""),
                    "Priority": r.get("Priority", ""),
                    "Task": task,
                    "Assigned To": r.get("Assigned To", ""),
                    "Last Updated": r.get("Last Updated", ""),
                })

    if pending:
        st.markdown("**Open jobs (parsed from checklists):**")
        st.dataframe(pd.DataFrame(pending), use_container_width=True, hide_index=True)
    else:
        st.info("No open jobs detected from checklists with current filters.")

# ----------------------------
# Tab 4: Audit Log
# ----------------------------
with page[3]:
    st.subheader("📜 Audit Log (append-only)")
    if a_df.empty:
        st.info("No audit records yet.")
    else:
        st.dataframe(a_df.sort_values("timestamp", ascending=False), use_container_width=True, hide_index=True)

# ----------------------------
# Tab 5: Export / Print
# ----------------------------
with page[4]:
    st.subheader("⬇️ Export & 🖨️ Print")
    exp_cols = [c for c in PATIENT_COLUMNS if c not in ["uid"]]
    exp_df = view_df[exp_cols].copy()
    csv = exp_df.to_csv(index=False).encode("utf-8")
    st.download_button(
        label="Download current view as CSV",
        data=csv,
        file_name=f"ent_handover_export_{datetime.now().strftime('%Y%m%d_%H%M')}.csv",
        mime="text/csv",
        use_container_width=True,
    )
    st.caption("Tip: Use your browser's print function (Cmd/Ctrl+P) on the Board tab for a clean printout.")

# ----------------------------
# Tab 6: About & Setup Notes
# ----------------------------
with page[5]:
    st.subheader("ℹ️ About this MVP")
    st.markdown(
        """
        **What this provides**
        - A single, shared handover board accessible to both **Doctors** and **Nurses**.
        - Simple add/update workflow, with **priorities**, **assignments**, and an **audit trail**.
        - Jobs are managed via markdown checklists (e.g., `- [ ] book CT neck`). Pending jobs are auto‑listed.
        - Google Sheets backend (if configured) or local CSV files under `./data/`.

        **How to connect Google Sheets (optional)**
        - Either add Streamlit **Secrets** (`gcp_service_account`, `SPREADSHEET_URL`) or set environment variables:
          - `GCP_SERVICE_ACCOUNT_JSON` (stringified JSON)
          - `SPREADSHEET_URL` (share link)
        - Optional department keys: `ENT_DOCTOR_KEY`, `ENT_NURSE_KEY`, `ENT_ADMIN_KEY`.

        **Governance & IG**
        - For production, host within the Health Board network and use an approved database with SSO.
        - Add role‑based access control, encryption, and retention policy.
        """
    )
