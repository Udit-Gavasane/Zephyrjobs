"""
db.py — Supabase REST API helper
================================
Talks to Supabase directly via HTTP — no official client needed.
All agent code imports from here to read/write the database.
"""

import os
import httpx
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', '.env'))

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SECRET_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("SUPABASE_URL and SUPABASE_SECRET_KEY must be set in .env")

HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation",
}


# ── INTERNAL ──────────────────────────────────────────────────────────────────

def _url(table: str) -> str:
    return f"{SUPABASE_URL}/rest/v1/{table}"


def _storage_url(path: str) -> str:
    return f"{SUPABASE_URL}/storage/v1/object/resumes/{path}"


def _raise(resp: httpx.Response, context: str):
    if resp.status_code >= 400:
        raise RuntimeError(f"[DB] {context} failed ({resp.status_code}): {resp.text}")


# ── JOBS ──────────────────────────────────────────────────────────────────────

def insert_job(job: dict) -> dict:
    """Insert a job or return existing if external_id already exists."""
    resp = httpx.post(
        _url("jobs"),
        headers={**HEADERS, "Prefer": "resolution=ignore-duplicates,return=representation"},
        json=job,
    )
    if resp.status_code == 409 or (resp.status_code == 201 and not resp.json()):
        # Already exists — fetch it
        existing = httpx.get(
            _url("jobs"),
            headers=HEADERS,
            params={"external_id": f"eq.{job['external_id']}", "limit": "1"},
        )
        data = existing.json()
        return data[0] if data else {}
    _raise(resp, "insert_job")
    data = resp.json()
    return data[0] if data else {}


def get_job(job_id: str) -> dict:
    resp = httpx.get(
        _url("jobs"),
        headers=HEADERS,
        params={"id": f"eq.{job_id}", "limit": "1"},
    )
    _raise(resp, "get_job")
    data = resp.json()
    return data[0] if data else {}


def update_job_status(job_id: str, status: str):
    resp = httpx.patch(
        _url("jobs"),
        headers=HEADERS,
        params={"id": f"eq.{job_id}"},
        json={"status": status},
    )
    _raise(resp, "update_job_status")


def get_jobs_by_status(status: str) -> list:
    resp = httpx.get(
        _url("jobs"),
        headers=HEADERS,
        params={"status": f"eq.{status}", "order": "scraped_at.desc"},
    )
    _raise(resp, "get_jobs_by_status")
    return resp.json()


# ── APPLICATIONS ──────────────────────────────────────────────────────────────

def insert_application(app: dict) -> dict:
    resp = httpx.post(
        _url("applications"),
        headers=HEADERS,
        json=app,
    )
    _raise(resp, "insert_application")
    data = resp.json()
    return data[0] if data else {}


def update_application(app_id: str, fields: dict):
    resp = httpx.patch(
        _url("applications"),
        headers=HEADERS,
        params={"id": f"eq.{app_id}"},
        json=fields,
    )
    _raise(resp, "update_application")


def get_application(app_id: str) -> dict:
    resp = httpx.get(
        _url("applications"),
        headers=HEADERS,
        params={"id": f"eq.{app_id}", "limit": "1"},
    )
    _raise(resp, "get_application")
    data = resp.json()
    return data[0] if data else {}


# ── ISSUES ────────────────────────────────────────────────────────────────────

def insert_issue(issue: dict) -> dict:
    """Flag a form field the agent couldn't answer."""
    resp = httpx.post(
        _url("issues"),
        headers=HEADERS,
        json=issue,
    )
    _raise(resp, "insert_issue")
    data = resp.json()
    return data[0] if data else {}


def get_open_issues(application_id: str) -> list:
    resp = httpx.get(
        _url("issues"),
        headers=HEADERS,
        params={
            "application_id": f"eq.{application_id}",
            "status": "eq.open",
            "order": "created_at.asc",
        },
    )
    _raise(resp, "get_open_issues")
    return resp.json()


def resolve_issue(issue_id: str, answer: str):
    resp = httpx.patch(
        _url("issues"),
        headers=HEADERS,
        params={"id": f"eq.{issue_id}"},
        json={
            "status": "resolved",
            "your_answer": answer,
            "resolved_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    _raise(resp, "resolve_issue")


# ── AGENT LOGS ────────────────────────────────────────────────────────────────

def log(level: str, action: str, detail: str = "", job_id: str = None):
    """Write a log entry. level: info | warn | error | success"""
    print(f"[{level.upper()}] {action}: {detail}")
    payload = {"level": level, "action": action, "detail": detail}
    if job_id:
        payload["job_id"] = job_id
    try:
        httpx.post(_url("agent_logs"), headers=HEADERS, json=payload)
    except Exception as e:
        print(f"[WARN] Log write failed: {e}")


# ── STORAGE ───────────────────────────────────────────────────────────────────

def upload_resume_pdf(pdf_bytes: bytes, filename: str) -> str:
    """
    Upload a compiled resume PDF to Supabase Storage.
    Returns the storage path on success.
    """
    path = f"applications/{filename}"
    resp = httpx.post(
        _storage_url(path),
        headers={
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/pdf",
        },
        content=pdf_bytes,
    )
    _raise(resp, "upload_resume_pdf")
    return path


def get_resume_url(path: str) -> str:
    """Get a signed URL to download a resume PDF (valid 1 hour)."""
    resp = httpx.post(
        f"{SUPABASE_URL}/storage/v1/object/sign/resumes/{path}",
        headers=HEADERS,
        json={"expiresIn": 3600},
    )
    _raise(resp, "get_resume_url")
    return resp.json().get("signedURL", "")


# ── CONFIG ────────────────────────────────────────────────────────────────────

def get_config() -> dict:
    """Load all agent config key/value pairs into a dict."""
    resp = httpx.get(_url("agent_config"), headers=HEADERS)
    _raise(resp, "get_config")
    return {row["key"]: row["value"] for row in resp.json()}
