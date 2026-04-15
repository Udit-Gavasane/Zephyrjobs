-- ─────────────────────────────────────────
-- ZephyrJobs — Supabase Schema
-- Paste into SQL Editor and click Run
-- ─────────────────────────────────────────


-- ── JOBS ──────────────────────────────────
-- Every job the agent finds goes here first
CREATE TABLE IF NOT EXISTS jobs (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  external_id   TEXT UNIQUE NOT NULL,        -- hash of url+title+company
  title         TEXT NOT NULL,
  company       TEXT NOT NULL,
  portal        TEXT NOT NULL,               -- workday, greenhouse, lever, ashby
  url           TEXT NOT NULL,
  description   TEXT,
  location      TEXT,
  salary_range  TEXT,
  status        TEXT DEFAULT 'new',          -- new | applying | applied | failed | flagged | skipped
  scraped_at    TIMESTAMPTZ DEFAULT NOW(),
  posted_at     TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_jobs_status    ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_scraped   ON jobs(scraped_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_company   ON jobs(company);


-- ── APPLICATIONS ──────────────────────────
-- One record per submitted (or attempted) application
CREATE TABLE IF NOT EXISTS applications (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  job_id            UUID REFERENCES jobs(id) ON DELETE CASCADE,
  status            TEXT DEFAULT 'pending',  -- pending | applied | failed | flagged
  resume_pdf_path   TEXT,                    -- path in Supabase Storage
  resume_latex      TEXT,                    -- full tailored LaTeX source
  cover_letter      TEXT,                    -- generated cover letter text
  form_snapshot     JSONB,                   -- saved field values for replay
  applied_at        TIMESTAMPTZ,
  portal_response   TEXT,                    -- confirmation text from portal
  notes             TEXT
);

CREATE INDEX IF NOT EXISTS idx_apps_job_id   ON applications(job_id);
CREATE INDEX IF NOT EXISTS idx_apps_status   ON applications(status);
CREATE INDEX IF NOT EXISTS idx_apps_applied  ON applications(applied_at DESC);


-- ── ISSUES ────────────────────────────────
-- Agent flags anything it can't answer here
-- You resolve it in the dashboard, agent replays
CREATE TABLE IF NOT EXISTS issues (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  application_id  UUID REFERENCES applications(id) ON DELETE CASCADE,
  job_id          UUID REFERENCES jobs(id) ON DELETE CASCADE,
  status          TEXT DEFAULT 'open',       -- open | resolved | skipped
  page_number     INTEGER,                   -- which Workday page it got stuck on
  field_label     TEXT NOT NULL,             -- exact question text from the form
  field_type      TEXT,                      -- text | dropdown | radio | checkbox | file
  options         JSONB,                     -- available choices if dropdown/radio
  your_answer     TEXT,                      -- you fill this in via dashboard
  created_at      TIMESTAMPTZ DEFAULT NOW(),
  resolved_at     TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_issues_status ON issues(status);
CREATE INDEX IF NOT EXISTS idx_issues_app    ON issues(application_id);


-- ── AGENT LOGS ────────────────────────────
-- Full activity stream — every action the agent takes
CREATE TABLE IF NOT EXISTS agent_logs (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  level       TEXT DEFAULT 'info',           -- info | warn | error | success
  action      TEXT NOT NULL,
  detail      TEXT,
  job_id      UUID REFERENCES jobs(id) ON DELETE SET NULL,
  created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_logs_created  ON agent_logs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_logs_level    ON agent_logs(level);


-- ── AGENT CONFIG ──────────────────────────
-- Settings the agent reads before each run
CREATE TABLE IF NOT EXISTS agent_config (
  key   TEXT PRIMARY KEY,
  value JSONB NOT NULL
);

INSERT INTO agent_config (key, value) VALUES
  ('active_portals',     '["workday", "greenhouse", "lever", "ashby"]'),
  ('excluded_companies', '[]'),
  ('auto_apply',         'true'),
  ('max_applications',   '20'),
  ('apply_email',        '"udit.gavasane@nyu.edu"')
ON CONFLICT (key) DO NOTHING;


-- ── STORAGE BUCKET ────────────────────────
-- Run this separately if bucket doesn't exist yet
-- Supabase Storage → New bucket → name: resumes → private
-- Or via API:
INSERT INTO storage.buckets (id, name, public)
VALUES ('resumes', 'resumes', false)
ON CONFLICT (id) DO NOTHING;
