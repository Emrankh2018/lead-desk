# Lead Desk runbook

## Daily use (Windows, local)
1. Start Ollama (it normally starts with Windows) if you want AI ideas/drafts.
2. `uv run python -m leadgen.cli serve` and open http://127.0.0.1:8765. You land on **Today**.
3. Work the Today lists: replies first, then due follow-ups, approved drafts to send, leads with no reply.
4. Find new leads: **Discover** (OpenStreetMap) or **Import → Paste a list** (Instagram-only brands), then research.
5. On a lead: research → check evidence and score → generate ideas → draft → edit → approve → send it yourself →
   **Mark as sent** with a follow-up date. Log replies, meetings and proposals as they happen.
6. Log time and money spent on lead generation in **Analytics → Time and cost** (e.g. 45 min research).

## First-time setup
```powershell
uv sync
uv run python -m leadgen.cli set-password owner
```
Then Settings → Sender profile (name, studio, email, Instagram, portfolio link, signature) and check the offers.

## Backup and restore
- Backup: `uv run python -m leadgen.cli backup` → `data/backups/leadgen-YYYYMMDD-HHMMSS.sqlite` (safe while the app runs).
  Copy backups off the laptop regularly (e.g. to a USB drive); `data/` is not in git.
- Restore: stop the app, copy a backup to `data/leadgen.sqlite` (keep the old file aside), delete any
  `data/leadgen.sqlite-wal` / `-shm` files left from the old database, start the app.

## Updating the code
`git pull` (if a remote is set up later) or apply changes, then `uv sync`. Database migrations run automatically at
startup; take a backup first.

## Health checks
- `uv run pytest -q`: the whole automated suite (no network, no model needed).
- `uv run python -m leadgen.cli eval --quick`: checks the local AI model still produces valid, grounded output.
- Settings → AI model shows whether Ollama and the configured model are available.

## Retargeting to another market or service
Everything is data, not code: Settings → Customer profiles (countries, cities, industries, keywords), Scoring weights
per profile, Offers (name, price, currency) and Sender profile. Discover pre-selects categories from the profile.
Example kept in the database: "UAE jewellery & watches".

## Measuring prospect supply
`uv run python -m leadgen.cli benchmark --country QA` runs a fresh, isolated benchmark (see `leadgen/benchmark.py`)
and writes `docs/benchmark/<date>-qa.md`.

## Moving to a VPS later (not done; needs your decision and a server)
1. Build the image: `docker build -t leaddesk .` (Dockerfile in the repo; not yet built or tested).
2. Copy `data/leadgen.sqlite` (from a backup) into the `leaddesk-data` volume, create `.env` with `LEADGEN_SECRET_KEY`
   (random), `LEADGEN_HTTPS_ONLY_COOKIES=true`, and AI settings (a VPS without a GPU will be slow with local models).
3. Run behind HTTPS (e.g. Caddy) and keep the app port bound to 127.0.0.1 on the server.
4. Re-run `set-password`, the test suite and a backup/restore drill on the server before relying on it.

## Troubleshooting
- "AI unavailable: not enough memory": close the browser tabs/apps using RAM, or leave `LEADGEN_AI_NUM_GPU=0`.
- "OpenStreetMap's free server is busy": wait a few minutes; repeated searches within 24 h use the cache.
- A site "refused access" or "rate-limited us": the app will not retry it in batches for 14 days; that is intended.
