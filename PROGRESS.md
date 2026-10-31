# Progress

## 2026-11-01 — M0 feasibility spike (done)
- Approved plan: architecture, MVP boundary, costs, milestones M0–M5 (see `docs/M0-feasibility-report.md` §4 for decisions carried forward).
- Done: git init, uv Python 3.12 env, `spike/osm_qatar.py`, `spike/site_signals.py`, `spike/llm_bakeoff.py`, report.
- Commands run: OSM query (exit 0); site signals on 8 domains (exit 0); bake-off 4B CPU (exit 0, 8/8 valid); bake-off 9B CPU (exit 0 script, 8/8 model-load failures).
- Not committed. No paid services, no outreach, no external writes.
- Owner removed `qwen3.5:9b` from Ollama (laptop can't run it). Local AI = `qwen3:4b-instruct` only.

## 2026-11-01 — M1 data core + CRM UI (done, local)
- Built: SQLAlchemy models + Alembic migration `0001`, seeded stages/offers/Qatar ICP, normalisation (domain, IG handle,
  E.164 phone, email, name key), dedupe (exact block + fuzzy review queue + confirmed merge), CSV import with preview,
  CSV/XLSX export, audit log, single-user login, FastAPI + Jinja UI (leads, lead detail, pipeline, import, duplicates,
  settings, audit), CLI (`serve`, `set-password`, `backup`, `dev-user`).
- Tests: `uv run pytest -q` → 40 passed.
- Browser journey (built-in browser, http://localhost:8766, local dev DB): login → CSV import preview (4 new, 1 repeated)
  → commit → manual add with similar name → duplicate flagged → merge (fields + history combined) → outreach logged with
  follow-up → stage moved → pipeline "follow-up due" filter → do-not-contact blocks outreach → invalid input 422 message
  → CSV/XLSX export → audit entries. No page overflow at 375 px. Console: only the intentional 422.
- Not verified: visual screenshots (the browser pane was hidden, so screenshots timed out).
- Dev DB `data/leadgen.sqlite` contains the test leads above; delete the file for a clean start.
- Deviation from plan: no HTMX; plain forms + 1 line of JS (auto-submit selects). Nothing needed it yet.
- Not committed.
- Next (awaiting owner go-ahead): M2 — robots-aware fetcher, extractors, evidence store, ICP scoring rubric with explanations.
