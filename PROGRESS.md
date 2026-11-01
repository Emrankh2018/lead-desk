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
- M4 committed as d3a9e73.

## 2026-11-01 — M5 workflow, analytics, re-targeting, benchmark (done, local)
- Done, uncommitted: Today view (`/today`, now the home page), Analytics (funnel + by source + effort/cost log,
  migration `0005`), benchmark runner (`cli benchmark`, `leadgen/benchmark.py`), Dockerfile (not built: needs owner OK to
  pull base image), `docs/RUNBOOK.md`, Overpass backoff on 502/503/504. Tests: 117 passed.
- Browser-verified: Today, Analytics, effort log, second profile "UAE jewellery & watches" → live Dubai search
  (192 listings → 160 businesses, 25 with website; preview only, nothing added); all pages fit 375 px.
- **Benchmark result** (`docs/benchmark/2026-11-01-1343-qa.md`, isolated DB, cached OSM data): **8 of the 20-prospect
  target (NOT MET)**; 6 are independent brands. Funnel: 160 listings → 112 businesses → 4 services excluded → 108
  relevant → 99 name-only → 9 contactable → 1 excluded (website redirects to a UAE store) → 8. Blocker: free OSM data
  for Qatar rarely includes contact details. Closing the gap needs the paste-a-list import (Instagram brands, owner's
  own research) or a paid source (Google Places, banked until there is budget).
- M5 complete (resumed after the usage pause).

## 2026-11-01 — Full audit of the app (single-operator self-review, not an independent review)
Method: list every route and boundary, write a failing test for each suspected defect, fix at the source, re-run.
Regression tests: `tests/test_audit.py`.

Confirmed and fixed:
1. Non-web link schemes (e.g. `ftp://`, and any scheme slipping through) accepted as websites; LinkedIn URL and
   observation source stored raw and rendered as clickable links (stored-XSS risk). Now only http(s) is stored, LinkedIn
   must be linkedin.com, and templates render any non-http(s) stored link as an inert `#` (defence in depth).
2. Open redirect after login via `next=/\evil.example` (browsers treat `\` as `/`). Backslashes, encoded backslashes and
   control characters are now rejected.
3. Activity "When" time entered in Doha time was stored as UTC, so it showed 3 hours late and skewed "no reply" days.
4. `openai_compatible` provider without a base URL crashed every page that shows AI status; now degrades to
   "AI misconfigured" with templates still available.
5. A new HTTP client was created (and never closed) on every AI status check/job; now one provider per app.
6. Lead/Outreach pages auto-refreshed with `<meta refresh>` while AI jobs ran, discarding text being typed in a draft;
   now a script refreshes only when nothing has been typed or focused.
7. Merging duplicates silently deleted the removed record's outreach drafts (incl. approved) and unlinked effort
   entries; both now move to the kept record.
8. Today's "best not-yet-contacted" ranked under-researched leads first (94 at 59% coverage above 90 at 100%); now
   ordered by score discounted for missing evidence.
9. Abandoned import/discovery previews (copies of contact data) piled up in `data/imports`; now deleted after 24 h.
10. Benchmark/score wording ("a AE storefront").

Checked and found sound: every non-login route requires a session; same-origin check on all POSTs plus SameSite=Strict
cookie; Jinja autoescaping (no `|safe`); export formula-injection guard; import token path validation; login throttle;
deleting a lead removes all related rows (evidence, runs, ideas, drafts, history, duplicate pairs); all 13 pages have
labelled form fields, no empty buttons/links, no duplicate ids, a title and an h1; no page scrolls sideways at 375 px.

Known, not fixed (low risk for a local single-user app; fix before any server deployment): the fetcher checks that a
host resolves to a public address and then connects separately, so a hostile domain could switch addresses in between
(DNS rebinding). Login throttling is per process and resets on restart.

- Tests: `uv run pytest -q` → 139 passed. Quick AI eval after the provider change: 5/5 valid, 0 injection leaks, thin
  evidence honest; one draft wrote "QAR 0450", which the price guard flagged as intended (left as a warning).
- Browser re-check after fixes: all 13 pages load; real links intact; unsafe link schemes refused via the forms;
  Today ranks well-researched leads first.
