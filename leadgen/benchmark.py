"""Prospect benchmark: `uv run python -m leadgen.cli benchmark [--country QA] [--no-research]`.

Question answered: from free, permitted sources only, how many genuinely relevant, non-duplicate, evidence-backed
prospects can the app produce? Runs in a fresh database (data/benchmark/<timestamp>/) so earlier test data cannot
inflate the count, and writes a report to docs/benchmark/.

Criteria (all must hold to count as a benchmark prospect):
  1. Relevant category: OpenStreetMap lists it as a product shop in the target categories, and it is not a
     service business (salon, spa, clinic...).
  2. In the target country: found inside the country's boundary on OpenStreetMap; if its website was researched and
     redirects to another country's storefront, it is excluded.
  3. Non-duplicate: one entry per business after branch grouping and the app's duplicate checks.
  4. Evidence-backed: at least one stored evidence item with a source URL (the OSM listing; plus website evidence
     when researched).
  5. Contactable: a public website, phone, email or Instagram handle is listed by the source. A name alone does not
     count, because the owner cannot act on it.
Independent (single/two-location) brands are reported separately as the core pilot audience.
"""
import json
from datetime import datetime
from pathlib import Path

from sqlalchemy import select

from leadgen.config import PROJECT_ROOT, Settings
from leadgen.db import Database
from leadgen.discovery import osm
from leadgen.discovery import service as discovery
from leadgen.migrate import upgrade
from leadgen.models import Business, DiscoveryRun, Evidence, IcpProfile, ResearchRun
from leadgen.research import service as research
from leadgen.research.fetcher import Fetcher
from leadgen.services.seed import seed_defaults

TARGET = 20
PRODUCT_TYPES = ["perfumery", "cosmetics", "jewelry", "watches", "fashion_accessories", "bag", "boutique"]
MAX_RESEARCH = 25


def run(settings: Settings, *, country: str = "QA", do_research: bool = True, search=osm.search,
        fetcher_factory=Fetcher, out_dir: Path | None = None) -> dict:
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M")
    bench = Settings(data_dir=settings.data_dir / "benchmark" / stamp, _env_file=None)
    bench.data_dir.mkdir(parents=True, exist_ok=True)
    upgrade(bench.db_url)
    db = Database(bench.db_url)
    s = db.sessionmaker()
    try:
        seed_defaults(s)
        icp = s.scalars(select(IcpProfile)).first()
        places, cached = search(country, PRODUCT_TYPES, None, cache_dir=settings.data_dir / "cache")
        candidates, unnamed = discovery.group_places(places, None)
        discovery.classify(s, candidates, country)
        services = [c for c in candidates if any("service business" in n for n in c.notes)]
        relevant = [c for c in candidates if c not in services]
        contactable = [c for c in relevant if c.website or c.instagram or c.phone or c.email]
        run_row = DiscoveryRun(source="osm", params={"country": country, "types": PRODUCT_TYPES, "benchmark": True},
                               places_found=len(places), candidates=len(candidates), from_cache=cached)
        s.add(run_row)
        s.flush()
        result = discovery.add_candidates(s, contactable, {c.key for c in contactable}, run=run_row, country=country,
                                          icp_id=icp.id, actor="benchmark", default_region=country)
        s.commit()
        leads = list(s.scalars(select(Business).where(Business.id.in_(result["ids"]))))
        researched = []
        if do_research:
            fetcher = fetcher_factory()
            try:
                for b in [b for b in leads if b.website][:MAX_RESEARCH]:
                    researched.append(research.research_lead(s, b, actor="benchmark", fetcher=fetcher))
                    s.commit()
            finally:
                fetcher.close()
        rows, excluded = [], []
        for b in leads:
            ev = list(s.scalars(select(Evidence).where(Evidence.business_id == b.id, Evidence.is_current.is_(True))))
            redirect_away = [e for e in ev if e.kind == "redirect" and e.value.get("country_hint") not in (None, country)]
            last_run = s.scalars(select(ResearchRun).where(ResearchRun.business_id == b.id).order_by(ResearchRun.id.desc())).first()
            listing = next((e for e in ev if e.kind == "directory_listing"), None)
            row = {"id": b.id, "name": b.name, "industry": b.industry, "branches": listing.value.get("branches") if listing else None,
                   "independent": listing is not None and listing.value.get("branches", 1) <= 2 and not listing.value.get("wikidata_brand"),
                   "website": b.website, "instagram": b.instagram_handle, "phone": b.phone_e164, "email": b.email_public,
                   "research": last_run.status if last_run else "not run (no website)",
                   "score": b.score, "coverage": b.score_coverage, "evidence_items": len(ev),
                   "sources": sorted({e.source_url for e in ev if e.source_url})[:3]}
            if redirect_away:
                excluded.append(row | {"reason": f"website redirects to a storefront for {redirect_away[0].value['country_hint']}"})
            elif not any(e.source_url for e in ev):
                excluded.append(row | {"reason": "no evidence with a source URL"})
            else:
                rows.append(row)
        rows.sort(key=lambda r: (-(r["score"] or 0), r["name"]))
        summary = {
            "country": country, "categories": PRODUCT_TYPES, "osm_listings": len(places), "unnamed_listings": unnamed,
            "businesses_after_grouping": len(candidates), "service_businesses_excluded": len(services),
            "relevant_businesses": len(relevant), "name_only": len(relevant) - len(contactable),
            "contactable": len(contactable), "added": result["created"], "excluded_after_research": len(excluded),
            "benchmark_prospects": len(rows), "independent_prospects": sum(1 for r in rows if r["independent"]),
            "with_website": sum(1 for r in rows if r["website"]), "with_instagram": sum(1 for r in rows if r["instagram"]),
            "researched_ok": sum(1 for r in rows if r["research"] in ("ok", "partial")),
            "target": TARGET, "target_met": len(rows) >= TARGET, "from_cache": cached, "database": str(bench.db_url),
        }
    finally:
        s.close()
        db.engine.dispose()
    report = {"run_at": datetime.now().isoformat(timespec="seconds"), "summary": summary, "prospects": rows, "excluded": excluded}
    out_dir = out_dir or PROJECT_ROOT / "docs" / "benchmark"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{stamp}-{country.lower()}.json").write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    path = out_dir / f"{stamp}-{country.lower()}.md"
    path.write_text(_markdown(report), encoding="utf-8")
    report["path"] = str(path)
    return report


def _markdown(report: dict) -> str:
    s = report["summary"]
    verdict = ("MET" if s["target_met"] else "NOT MET")
    lines = [
        f"# Prospect benchmark — {s['country']}, {report['run_at']}", "",
        f"**Target: {s['target']} relevant, non-duplicate, evidence-backed, contactable prospects from free sources → "
        f"{s['benchmark_prospects']} found ({verdict}).** Independent (1–2 location) brands among them: {s['independent_prospects']}.", "",
        "Source: OpenStreetMap via Overpass (free, ODbL)" + (" — served from the 24 h cache" if s["from_cache"] else "") +
        f". Categories: {', '.join(s['categories'])}. Criteria: see `leadgen/benchmark.py`.", "",
        "## Funnel", "",
        f"| Step | Count |", "|---|---|",
        f"| OSM listings in the country | {s['osm_listings']} |",
        f"| … without a name (skipped) | {s['unnamed_listings']} |",
        f"| Businesses after grouping branches | {s['businesses_after_grouping']} |",
        f"| Service businesses excluded (salon, spa…) | {s['service_businesses_excluded']} |",
        f"| Relevant product businesses | {s['relevant_businesses']} |",
        f"| … name only, no website/phone/email/Instagram | {s['name_only']} |",
        f"| Contactable, added to the benchmark database | {s['added']} |",
        f"| Excluded after website research | {s['excluded_after_research']} |",
        f"| **Benchmark prospects** | **{s['benchmark_prospects']}** |",
        f"| … with a website / with Instagram | {s['with_website']} / {s['with_instagram']} |",
        f"| … website researched successfully | {s['researched_ok']} |", "",
        "## Prospects", "",
        "| # | Business | Category | Locations | Contact route | Website research | Score (coverage) |", "|---|---|---|---|---|---|---|",
    ]
    for i, r in enumerate(report["prospects"], 1):
        contact = ", ".join(x for x in [r["website"] and "website", r["instagram"] and "Instagram", r["phone"] and "phone",
                                        r["email"] and "email"] if x)
        lines.append(f"| {i} | {r['name']} | {r['industry']} | {r['branches']}{'' if r['independent'] else ' (chain)'} | {contact} | "
                     f"{r['research']} | {r['score']} ({r['coverage']}%) |")
    if report["excluded"]:
        lines += ["", "## Excluded after research", ""] + [f"- {r['name']}: {r['reason']}" for r in report["excluded"]]
    lines += ["", "## Reading this honestly", "",
              "- A prospect here is a real, contactable product business in the target categories with a source link. It is "
              "not a claim that the business needs or will buy the service; that is what research, the score and outreach test.",
              "- Most OpenStreetMap shops in Qatar list only a name and location, so the free map alone yields few contactable "
              "prospects. The rest of the market (especially Instagram-only brands) has to come from the paste-a-list import, "
              "or from a paid source such as Google Places, which is deliberately not connected.",
              f"- The benchmark database is kept for inspection: `{s['database']}`."]
    return "\n".join(lines) + "\n"
