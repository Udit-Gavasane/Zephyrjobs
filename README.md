# ZephyrJobs

> End-to-end autonomous job application pipeline. Scrapes portals, tailors LaTeX resumes, navigates ATS systems, submits.

[![Python](https://img.shields.io/badge/Python-3.11+-3776AB?style=flat&logo=python&logoColor=white)](https://python.org)
[![Next.js](https://img.shields.io/badge/Next.js-15-000000?style=flat&logo=next.js&logoColor=white)](https://nextjs.org)
[![Playwright](https://img.shields.io/badge/Playwright-1.44-2EAD33?style=flat&logo=playwright&logoColor=white)](https://playwright.dev)
[![Supabase](https://img.shields.io/badge/Supabase-Database-3ECF8E?style=flat&logo=supabase&logoColor=white)](https://supabase.com)
[![Claude](https://img.shields.io/badge/Claude-Haiku-CC785C?style=flat&logo=anthropic&logoColor=white)](https://anthropic.com)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg?style=flat)](LICENSE)

---

## What Is This

ZephyrJobs is a fully autonomous job application agent. You set it up once. It finds roles that match your resume, tailors your LaTeX resume per job description, navigates Workday and other ATS portals, handles email verification, and submits — without you touching a thing.

Built for developers who are tired of spending hours on job applications that should take minutes.

---

## How It Works

```
Your Resume (LaTeX)
      │
      ▼
┌─────────────────┐
│  Portal Scanner  │  Scrapes Workday, Greenhouse, Lever, Ashby
│  (Playwright)    │  Filters by your profile
└────────┬────────┘
         │
┌────────▼────────┐
│  Resume Tailor  │  Claude Haiku rewrites bullets to match JD
│  (Claude API)   │  pdflatex compiles to PDF
└────────┬────────┘
         │
┌────────▼────────┐
│  ATS Navigator  │  Playwright fills every form field
│  (Playwright)   │  Google SSO, Gmail verification, multi-page
└────────┬────────┘
         │
┌────────▼────────┐
│  Issues Queue   │  Flags ambiguous questions to dashboard
│  (Supabase)     │  You resolve, agent replays and submits
└────────┬────────┘
         │
         ▼
   Application Submitted ✓
   PDF saved to Supabase Storage
   Status logged to dashboard
```

---

## Tech Stack

| Layer | Technology |
|---|---|
| Agent | Python 3.11, Playwright |
| AI | Claude Haiku (Anthropic API) |
| Resume | LaTeX + pdflatex |
| ATS Navigation | Playwright + Google OAuth |
| Email Verification | Gmail API (OAuth) |
| Database | Supabase (PostgreSQL) |
| File Storage | Supabase Storage |
| Dashboard | Next.js 15, TypeScript |
| Auth | Cloudflare Access |
| Deployment | Private (self-hosted) |

---

## Features

- **Workday automation** — Google SSO, multi-page forms, file upload
- **LaTeX resume tailoring** — per-job bullet rewriting, keyword injection, PDF compilation
- **Gmail verification handler** — OTP extraction, magic link navigation, session persistence
- **Issues queue** — agent flags blockers, you resolve in dashboard, agent replays
- **Application tracker** — every submission logged with PDF, status, timestamp
- **Portal scanner** — Workday, Greenhouse, Lever, Ashby support
- **Supabase Storage** — every tailored resume PDF saved and linked to its application

---

## Status

🚧 **Active development** — building toward first fully automated end-to-end Workday submission.
✅ **First successful automated submission complete** — Workday agent is fully operational.

- [x] Project architecture
- [x] Supabase schema (jobs, applications, issues, agent_logs)
- [x] Workday Playwright agent — Google SSO, multi-page forms, file upload
- [x] Session persistence — login once, runs headlessly forever
- [x] Issues queue — flags blockers to Supabase, supports replay
- [x] Resume upload — PDF attached to every application
- [x] First successful automated submission (NVIDIA, confirmed via email)
- [ ] LaTeX resume tailoring per job description
- [ ] Portal scanner (Greenhouse, Lever, Ashby)
- [ ] Gmail verification handler
- [ ] Dashboard UI
- [ ] Overnight scheduled runs

---

## Author

Built by **Udit Gavasane** — MS Computer Science, NYU.

[![GitHub](https://img.shields.io/badge/GitHub-UditGavasane-181717?style=flat&logo=github&logoColor=white)](https://github.com)
[![LinkedIn](https://img.shields.io/badge/LinkedIn-Connect-0A66C2?style=flat&logo=linkedin&logoColor=white)](https://linkedin.com)

---

## License

MIT
