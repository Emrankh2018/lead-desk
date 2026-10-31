# M0 Feasibility Spike — Report (2026-11-01, Asia/Qatar)

## TL;DR
- **Discovery:** OpenStreetMap lists **287** target-category businesses in Qatar (276 named) but only **10 distinct website domains** and 6 social handles, and those skew to large chains. OSM is useful for **names + locations**, not as a complete source of researchable websites.
- **Website research works** with plain, robots-respecting HTTP: 5 of 8 sites gave usable signals (platform, video presence, image count, social links, published email). 1 returned 429 (rate-limited, respected, not retried), 2 were unreachable — the app must record these as "could not verify".
- **Local AI:** the 4B model returned schema-valid, citation-grounded JSON 8/8 times, but **followed an instruction injected in page text** and made **illogical fit calls**. → AI must not own facts or ranking. The **9B model could not load** (needs ~6.6 GB; only 3.9 GB RAM free with normal apps open, GPU shared with the browser) → not viable on this laptop.
- **Decision proposed:** deterministic code owns facts and score; AI only writes labelled hypotheses + drafts; discovery = OSM names + add-by-URL/handle + CSV import, with Google Places as the main gated upgrade.

## 1. Discovery — OSM Overpass (one read-only query)
Script: `spike/osm_qatar.py` · output `spike/out/osm_qatar.json`

| Metric | Count |
|---|---|
| Total (shop = perfumery, cosmetics, jewelry, watches, beauty, fashion_accessories) | 287 |
| Named | 276 |
| With website tag | 37 (10 distinct domains) |
| With social tag | 23 (6 distinct handles) |
| With phone | 42 |

By type: beauty 138, jewelry 79, perfumery 38, cosmetics 17, watches 9, fashion_accessories 6.
Website domains are dominated by chains (e.g. one perfume chain = 20 of 37 tags) and include non-business noise (hotel, facebook.com). "beauty" is mostly salons (services, not products) → lower ICP fit.

**Implication:** OSM alone cannot produce 20 evidence-backed *small/medium* product brands. It gives a seed list of names/areas to look up. Google Places (gated) would add website/phone for most of those names; Instagram-first brands still need add-by-handle.

## 2. Website signal extraction
Script: `spike/site_signals.py` · output `spike/out/site_signals.json` (raw HTML kept locally, gitignored)

| Site | Result | Notable signals |
|---|---|---|
| ahmedalmaghribi.com | ok | redirected to **UAE** storefront; 22 `<video>` tags; IG/TikTok/YouTube/FB; published info@ email |
| alromaizan.com | ok | no video; IG/TikTok/X/FB/WhatsApp (UAE number) |
| vperfumes.com | ok | 5 imgs, no socials found (likely JS-rendered) |
| zabarjadjewelry.com | ok | WooCommerce; no video; no socials |
| maisonroyaleqatar.com | ok | no video; 28 imgs; no socials found |
| diamondshouse.com | 429 | rate-limited — stopped |
| byeljoori.com, alsughajewelry.com | connect error | dead/unreachable |

Lessons for M2:
- Strip `<script>/<style>` before text extraction (one page reported 532k "text" chars from inline JSON).
- Platform markers missed custom/headless stores; add more markers and treat "none detected" as unknown, not "no store".
- JS-rendered pages hide social links → "not found on homepage" must be shown as *unverified*, never "has no Instagram".
- Redirects to other-country storefronts (UAE) must be captured: geographic fit needs evidence.
- Respect 429/robots, cache responses, pace requests (2 s used here).

## 3. Local LLM bake-off
Script: `spike/llm_bakeoff.py` · fixtures: 5 real sites + 3 synthetic (thin evidence, prompt injection, Arabic).
Hardware note: the browser held ~2 GB of the 4 GB GPU, so GPU loading failed (CUDA OOM) and partial offload was unreliable. Runs below are **CPU-only** (`num_gpu=0`), the dependable baseline on this laptop.

| Check | qwen3 4B instruct (q4) | qwen3.5 9B |
|---|---|---|
| Schema-valid JSON | 8/8 | 0/8 — **failed to load** |
| Citations reference existing evidence | 8/8 (0 ungrounded) | n/a |
| Contradiction traps (e.g. "no video" when video exists) | 0 hit | n/a |
| Thin evidence → `insufficient_evidence` | yes | n/a |
| Prompt injection resisted | **no** — stated injected "QAR 50,000 budget" as fact, fit=strong | n/a |
| Fit logic | poor — rated the site with most existing video "strong", others "weak" | n/a |
| Speed (CPU) | ~7 tok/s, 20–117 s per lead | n/a |

9B error (observed): CPU buffer allocation of ~3.2 GB failed; free RAM at test time 3.9 GB of 16 GB. It might run with most apps closed, but a tool that needs the laptop emptied first is not dependable.
Raw results: `spike/out/llm_bakeoff_4b_cpu.json`, `spike/out/llm_bakeoff_gpu0.json` (9B errors).

## 4. Decisions proposed for M1–M3
1. **Facts come from code, never the model.** Evidence items are produced by extractors; the AI may only output `hypothesis` observations plus drafts, each citing evidence ids. Any AI "fact" is downgraded.
2. **Score is deterministic** (weighted rubric per ICP with per-criterion explanation). AI fit is shown as a secondary, labelled opinion — not used for ranking.
3. **Injection defence in code:** free page text is passed only as quoted, length-capped snippets; numbers/claims about budget or intent from page text are never shown as facts; outputs are post-checked.
4. **Model choice:** default = local `qwen3:4b-instruct` on CPU, as a background job (~1 min per lead is acceptable for 20–60 leads/week). It is good enough for structured summaries and first-draft messages under the code-side guards above, not for judgement. Any OpenAI-compatible provider can be plugged in later (e.g. a cheap cloud model for better drafts) — off unless you approve it. The eval harness from this spike becomes `evals/` so any model is re-tested before switching.
5. **Discovery:** OSM seed + add-by-URL/Instagram handle + CSV import in MVP; Google Places Text Search proposed as the main upgrade (needs your approval for a Google billing account).
