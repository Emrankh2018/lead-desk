# Lead Desk — AI lead generation assistant

Local-first, single-user app for finding, researching, qualifying and tracking B2B prospects. The first use is
an AI product-advertising service in Qatar; target profiles, stages and offers are configurable for other markets.

**Status:** M1 (CRM core) and M2 (website research, evidence, explainable scoring) done. AI drafts (M3), discovery
connectors (M4) and analytics (M5) are next. See `PROGRESS.md` and `docs/M0-feasibility-report.md`.

## Principles
- Facts come from code and stored sources; AI output (from M3) is labelled hypothesis/draft and never auto-sent.
- No guessed contact details: only emails/phones the business published, with where they came from.
- No scraping behind logins, CAPTCHAs or rate limits; LinkedIn/Instagram data only from your own input or official APIs.
- Every consequential action is in the audit log. Deleting a lead needs the name typed to confirm.

## Run (Windows, PowerShell)
```powershell
uv sync                                         # Python 3.12 env + dependencies
uv run python -m leadgen.cli set-password owner # create your login (prompts, min 10 chars)
uv run python -m leadgen.cli serve              # http://127.0.0.1:8765
```
Other commands: `backup` (timestamped SQLite copy in `data/backups/`), `dev-user` (local testing login with a random
password in `data/dev-credentials.txt`).

Data lives in `data/` (git-ignored): `leadgen.sqlite`, `secret.key` (session signing), import previews.
Configuration via environment or `.env` (git-ignored), prefix `LEADGEN_`: `DATA_DIR`, `DATABASE_URL`, `SECRET_KEY`,
`HTTPS_ONLY_COOKIES` (set `true` behind HTTPS), `DEFAULT_COUNTRY` (phone parsing, default `QA`), `UI_DIR` (`rtl` for Arabic later).

## Features (M1)
- Leads list with search and filters (stage, profile, city, industry, follow-up due, do-not-contact), sorting, CSV/Excel export
  (formula-injection safe, UTF-8 BOM for Arabic).
- Add lead by name / website / Instagram handle or URL / phone; social URLs pasted as a website are moved to the right field.
- Duplicate protection: same domain, Instagram handle or phone is blocked; similar names in the same city are queued
  on the Duplicates page for merge or dismiss (never merged automatically).
- CSV import with preview (new / already in list / repeated in file / invalid) before anything is saved.
- Lead page: editable details, stage, append-only history (notes, outreach, replies, meetings, proposals), follow-up date,
  do-not-contact (blocks outreach logging), delete with confirmation.
- Pipeline board with configurable stages (rename, reorder, add, delete empty, mark won/lost).
- Settings: customer profiles (ICP) and offers; audit log page.

## Research and scoring (M2)
- **Research website** on a lead (or a background batch of up to 25 on the Research page) reads robots.txt, the homepage,
  up to two same-site contact/about pages and, for Shopify, the public product catalogue. Requests are 2 s apart, sites
  that block or rate-limit are not retried, and leads tried in the last 14 days are left out of batches.
- Every finding is stored as evidence with its source URL and time. Absences ("no social links found") are listed under
  *Not verified*. Empty email/phone/Instagram fields are filled only from details the site itself published.
- **Score (0–100)** = weighted average of six criteria that have evidence: industry fit, location fit, video opportunity,
  sells online, visual social presence, reachable. **Coverage %** shows how much of the rubric had evidence; low coverage
  means under-researched, not a bad lead. Weights are editable per profile (Settings → Scoring weights).
- The score measures fit and visible opportunity, not intent to buy, and does not judge company size.
- Add your own observations (e.g. from Instagram) with the page you checked; they appear as evidence.

## Security notes
Binds to 127.0.0.1 only. scrypt password hashes, login throttling (5 failures → 60 s), signed HttpOnly SameSite=Strict
session cookie, same-origin check on every POST, Jinja autoescaping, no API docs endpoints exposed.
Before any VPS deployment: HTTPS, `LEADGEN_HTTPS_ONLY_COOKIES=true`, backups, and a deliberate decision.

## Development
```powershell
uv run pytest -q
uv run alembic revision --autogenerate -m "describe change"   # after model changes
```
`spike/` holds the M0 feasibility scripts (OSM query, site signal extraction, local LLM bake-off).
Local AI default is `qwen3:4b-instruct` via Ollama (CPU); larger local models do not fit this laptop.
