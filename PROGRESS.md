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

## 2026-11-01 — M2 research, evidence and explainable scoring (done, local)
- Built: `leadgen/research/` — fetcher (public http(s) only, private-IP refusal, robots.txt incl. redirects, identified UA,
  2 s per-host pacing, 3 MB cap, stop on 401/403/429/503), extractors (title/description, platform markers incl. Salla/Zid/
  ExpandCart, JSON-LD products, Shopify `/products.json` count, video, images, social links, published email/phone,
  Qatar mentions, redirects to other-country storefronts, copyright year, thin-JS warning), evidence store (current +
  history, user observations), rubric-v1 scoring (6 weighted criteria, score over evidenced criteria + coverage %),
  research button, background batch (≤25, skips leads tried in last 14 days), scoring weights in Settings, rescoring on
  every edit/import/startup. Migration `0002` (tested on a backup copy of the dev DB: data intact).
- Fixed during QA: shared seed ORM object (second DB got no profile), JSON-LD counted after scripts were stripped,
  batch would re-hit sites that had just refused us, phone-width overflow from long URLs.
- Tests: `uv run pytest -q` → 59 passed (offline fixtures; no live network in tests).
- Real-site run (built-in browser, 7 Qatar-listed sites from OSM): 5 ok (1–3 pages each), 1 unreachable (ConnectError),
  1 rate-limited (429, stopped, not retried). Scores: Maison Royale 96, Zabarjad 94, Al Romaizan 90, V Perfumes 87 (90% cov.),
  Ahmed Al Maghribi 62 (redirects to UAE store + already uses video). All 12 pages fit 375 px.
- Known limits: the rubric does not judge business size, so big chains (e.g. Al Romaizan, multi-country) can score high —
  lower them manually or via weights; JS-rendered stores may show "no store markers"; social activity is not checked
  automatically (use "Add your own observation").
- Not visually verified by screenshot (pane hidden). Not committed.
- **Owner decision 2026-11-01 (M4 scope):** free sources only. No Google Places and no billing account. Places is
  deferred ("banked") until the owner has budget and approves it. M4 = OpenStreetMap + other no-cost, permitted sources.
- Next (awaiting owner go-ahead): M3 — AI layer: provider interface (Ollama qwen3 4B default), labelled hypotheses
  grounded in evidence ids, outreach drafts (email/LinkedIn/Instagram/follow-up) with approval, eval harness.
- M2 committed as b17e305.

## 2026-11-01 — M3 AI ideas and outreach drafts (done, local)
- Built: `leadgen/ai/` — replaceable provider (Ollama default, OpenAI-compatible, none), guards, tasks (insights,
  drafts, template fallback), single background worker; routes for insight, drafts, approve / discard / mark-as-sent,
  Outreach page, sender profile, AI status in Settings; `leadgen/evals.py` + `cli eval`; migration `0003` (new tables only).
- Safety design: model sees only stored evidence (page quotes marked untrusted) and has no tools. Insights are dropped
  unless they cite real evidence; numbers and budget/intent claims must appear in the software-written evidence
  statement (quotes can't ground them). Drafts are checked for unknown contacts/links/phones, non-offer prices,
  garbled prices, overclaims, placeholders, length, naming the business. Nothing is ever sent by the app.
- Eval (qwen3 4B, CPU, 12 calls, reports in `evals/results/`):
  - p1: 12/12 valid, 0 leaks, 3/7 drafts clean (4 didn't name the business), median 43 s.
  - p2: names fixed (6/7 clean) but the "mention a fact" rule made the model invent one for an unresearched lead,
    and 1 injection-marker hit.
  - p3 (current): 12/12 valid, 0 leaks, thin evidence honest, 6/7 drafts clean, prices 7/7, median 21 s, max 53 s.
- Fixed from eval/browser runs: numbers grounded by untrusted quotes, invented facts with no evidence, unsupported
  intent claims, literal "\n", "QAR -450" (now repaired), empty email subject (default + warning), contradictory
  "insufficient" flag, worker-thread race.
- Tests: `uv run pytest -q` → 87 passed (fake provider; no model needed).
- Browser run with the real model on Maison Royale: 5 evidence-linked ideas; AI email draft personalised, correct price,
  signed; approve shows Copy (no mailto because no public email on record). Console clean.
- Known limits: 4B wording is sometimes stiff ("Thank you for sharing…" on a first contact); LinkedIn drafts often
  exceed the 300-char note limit (flagged); ideas are generic marketing hypotheses, not deep insight. Review every draft.
- Not committed. Mark-as-sent tested in automated tests, not clicked in the browser on real data.
- Next (awaiting owner go-ahead): M4 — free discovery only (OpenStreetMap search by country/city/category, website-link
  suggestions, fast bulk add of Instagram handles/URLs). Google Places stays banked.
- M3 committed as 7043440.

## 2026-11-01 — M4 free discovery (done, local)
- Built: `leadgen/discovery/` — Overpass client (country + optional city area + shop categories, identified UA,
  one query at a time, 24 h disk cache, 429/504 reported not retried, ODbL attribution + OSM link per lead), branch
  grouping (same name / website / brand / Instagram → one lead with a location count), classification against the
  list, preview with checkboxes, add + optional research batch. Discover page, recent-search log (`discovery_runs`,
  migration `0004`). Import page: paste a list (websites, @handles, Instagram links, "Name, site"). Research now
  records outbound links; lead page offers "Add as lead" for them. Scoring rubric-v2 adds "Independent brand"
  (single location 1.0, two 0.6, chain 3+ or Wikidata brand 0.2; weight 10).
- Live Qatar run (perfume, cosmetics, jewellery, watches, accessories, bags, boutiques; 29 s, then cached):
  160 listings → 112 businesses (6 unnamed skipped); chains grouped (Ahmed Al Maghribi ×20, Al Romaizan ×7,
  Al Sulaiman ×5, Sky ×4, V Perfumes ×3); 5 already in list; 1 possible duplicate caught (Alsugha website = existing
  lead's Instagram handle). Only ~5% of new businesses list any website/phone/Instagram; 3 were pre-selected and added
  (Ajmal Perfumes 89, Rieman Jewelry 94, S.D. Salman Al Sayegh Jewellery 89; all 59% coverage, no website).
- Fixed from the live run: missed duplicate (spacing / website-vs-Instagram), ~100 name-only listings pre-selected,
  service businesses tagged as shops (salon/spa/massage) pre-selected, "jewellery" vs "jewelry" scoring 0 on industry.
- Tests: `uv run pytest -q` → 111 passed (canned OSM data; no live network in tests).
- Known limits / follow-ups: OSM rarely has websites or Instagram, so most Qatar prospects still need the paste list;
  discovery does not enrich leads already in the list (e.g. Al Romaizan, a 7-branch chain imported earlier, has no
  location count so still scores 90); "clothes" category is noisy (general fashion retail).
- Not committed.
- Next (awaiting owner go-ahead): M5 — Today/follow-up view, funnel + per-source analytics from recorded events,
  time/cost log, second ICP to prove re-targeting, Docker file, runbook, 20-prospect benchmark with source breakdown.
