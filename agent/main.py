"""
main.py — ZephyrJobs Agent Orchestrator
========================================
Entry point for the agent. For now targets one specific job URL.
Run: py main.py

Later this will:
  - Scrape multiple portals for new jobs
  - Loop through jobs and apply to each
  - Run on a schedule overnight
"""

import asyncio
import hashlib
import os
import sys
from pathlib import Path
from datetime import datetime, timezone

from dotenv import load_dotenv
load_dotenv(dotenv_path=Path(__file__).parent.parent / '.env')

import db
from portals.workday import WorkdayAgent

# ── TARGET JOB ────────────────────────────────────────────────────────────────
# Hardcoded for now — one job, one application, prove the system works end to end

TARGET_JOB = {
    "url":         "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/US-CA-Santa-Clara/GPU-Architecture-Engineer---New-College-Grad-2026_JR2014794-1",
    "title":       "GPU Architecture Engineer - New College Grad 2026",
    "company":     "NVIDIA",
    "portal":      "workday",
    "location":    "US-CA-Santa-Clara",
    "description": "GPU Architecture Engineer role at NVIDIA Santa Clara",
}


# ── HELPERS ───────────────────────────────────────────────────────────────────

def make_external_id(url: str, title: str, company: str) -> str:
    raw = f"{url}|{title}|{company}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def get_or_create_job() -> dict:
    """Insert the target job into Supabase if it doesn't exist, return it."""
    job_data = {
        **TARGET_JOB,
        "external_id": make_external_id(
            TARGET_JOB["url"],
            TARGET_JOB["title"],
            TARGET_JOB["company"]
        ),
        "status": "new",
    }

    result = db.insert_job(job_data)

    if result:
        db.log("info", "MAIN", f"Job inserted: {result['id']}")
        return result

    # Already exists — fetch it
    resp = __import__("httpx").get(
        f"{os.getenv('SUPABASE_URL')}/rest/v1/jobs",
        headers={
            "apikey":        os.getenv("SUPABASE_SECRET_KEY"),
            "Authorization": f"Bearer {os.getenv('SUPABASE_SECRET_KEY')}",
        },
        params={
            "external_id": f"eq.{job_data['external_id']}",
            "limit":       "1",
        }
    )
    data = resp.json()
    if data:
        db.log("info", "MAIN", f"Job already exists: {data[0]['id']}")
        return data[0]

    raise RuntimeError("Could not insert or fetch job")


def create_application(job_id: str) -> dict:
    """Create a pending application record for the job."""
    app = db.insert_application({
        "job_id": job_id,
        "status": "pending",
    })
    db.log("info", "MAIN", f"Application created: {app['id']}")
    return app


# ── MAIN ──────────────────────────────────────────────────────────────────────

async def run():
    db.log("info", "MAIN", "ZephyrJobs agent starting")

    # Step 1: Get or create job in Supabase
    db.log("info", "MAIN", f"Target: {TARGET_JOB['title']} @ {TARGET_JOB['company']}")
    job = get_or_create_job()

    # Step 2: Check if already applied
    if job.get("status") in ("applied", "applying"):
        db.log("info", "MAIN", f"Already applied to this job (status={job['status']}), skipping")
        return

    # Step 3: Create application record
    db.update_job_status(job["id"], "applying")
    app = create_application(job["id"])

    # Step 4: Run Workday agent
    # resume_pdf_path: set to None for now — agent will skip resume upload
    # Once LaTeX tailoring is built, this will point to the compiled PDF
    agent = WorkdayAgent(
        job=job,
        application_id=app["id"],
        resume_pdf_path=r"D:\Jet Brains\IntelliJ\Zephyrjobs\agent\resume.pdf",
    )

    db.log("info", "MAIN", "Launching Workday agent...")

    try:
        success = await agent.run()

        if success:
            db.log("success", "MAIN",
                f"Application submitted successfully for {job['company']}!")
            print("\n✓ Application submitted!")
            print(f"  Job:     {job['title']} @ {job['company']}")
            print(f"  App ID:  {app['id']}")
            print(f"  Status:  applied")

        else:
            # Check if flagged (has open issues)
            issues = db.get_open_issues(app["id"])
            if issues:
                db.log("warn", "MAIN",
                    f"Application flagged — {len(issues)} issues need resolution")
                print(f"\n⚠ Application paused — {len(issues)} issue(s) need your input:")
                for i, issue in enumerate(issues, 1):
                    print(f"  {i}. Page {issue['page_number']}: {issue['field_label']}")
                    if issue.get("options"):
                        print(f"     Options: {issue['options']}")
                print("\nResolve these in the dashboard, then run:")
                print(f"  py main.py --replay {app['id']}")
            else:
                db.log("error", "MAIN", "Application failed — check agent_logs in Supabase")
                print("\n✗ Application failed. Check Supabase agent_logs for details.")

    except Exception as e:
        db.log("error", "MAIN", f"Agent crashed: {e}")
        db.update_job_status(job["id"], "failed")
        db.update_application(app["id"], {"status": "failed", "notes": str(e)})
        raise


async def replay(application_id: str):
    """Replay a flagged application after issues have been resolved."""
    from portals.workday import replay_application

    db.log("info", "MAIN", f"Replaying application {application_id}")
    success = await replay_application(application_id)

    if success:
        print(f"\n✓ Application submitted on replay!")
    else:
        print(f"\n✗ Replay failed — check agent_logs in Supabase")


# ── ENTRY POINT ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--replay":
        asyncio.run(replay(sys.argv[2]))
    else:
        asyncio.run(run())
