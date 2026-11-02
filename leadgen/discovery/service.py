"""Turn discovered places into reviewable lead candidates and add the ones the user selects.

Branches of one business (same name, website or brand) become a single candidate; the branch count is recorded as
evidence so chains can be ranked lower. Nothing is added until the user confirms the preview.
"""
from dataclasses import asdict, dataclass, field

from sqlalchemy.orm import Session

from leadgen import normalize
from leadgen.discovery.osm import Place, label_of
from leadgen.models import DiscoveryRun, Evidence
from leadgen.research.service import rescore
from leadgen.services import audit
from leadgen.services.dedupe import exact_matches, fuzzy_matches
from leadgen.services.leads import DuplicateLead, LeadError, LeadInput, create_lead

MAX_LISTED_URLS = 10
# Service businesses are sometimes tagged as shops (live Qatar run: a massage centre and a spa under "cosmetics").
SERVICE_WORDS = ("salon", "spa", "massage", "clinic", "barber", "nails", "nail ", "laser", "saloon", "صالون", "مركز تجميل")


@dataclass
class Candidate:
    key: str
    name: str
    shop_types: list[str]
    osm_urls: list[str]
    branches: int
    brand: str | None = None
    website: str | None = None
    instagram: str | None = None
    phone: str | None = None
    email: str | None = None
    city: str | None = None
    status: str = "new"  # new | in_list | possible_duplicate | invalid
    message: str = ""
    selected: bool = True
    notes: list[str] = field(default_factory=list)


def _norm_brand(value: str | None) -> str | None:
    return normalize.name_key(value) if value else None


def group_places(places: list[Place], city: str | None) -> tuple[list[Candidate], int]:
    """Group branches. Returns (candidates, places skipped for having no name)."""
    named = [p for p in places if p.name]
    parent = list(range(len(named)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    seen: dict[tuple[str, str], int] = {}
    for i, p in enumerate(named):
        for key in (("name", normalize.name_key(p.name)), ("domain", normalize.domain_of(p.website) or ""),
                    ("brand", _norm_brand(p.brand) or ""), ("ig", p.instagram or "")):
            if not key[1]:
                continue
            if key in seen:
                parent[find(i)] = find(seen[key])
            else:
                seen[key] = i
    groups: dict[int, list[Place]] = {}
    for i, p in enumerate(named):
        groups.setdefault(find(i), []).append(p)

    candidates = []
    for members in groups.values():
        first = members[0]
        pick = lambda attr: next((getattr(m, attr) for m in members if getattr(m, attr)), None)  # noqa: E731
        brand = pick("brand")
        c = Candidate(
            key=f"{first.osm_type}/{first.osm_id}", name=(brand if brand and len(members) > 1 else first.name)[:200],
            shop_types=sorted({m.shop for m in members}), osm_urls=[m.url for m in members][:MAX_LISTED_URLS],
            branches=len(members), brand=brand, website=pick("website"), instagram=pick("instagram"),
            phone=pick("phone"), email=pick("email"),
            city=next((m.tags.get("addr:city") for m in members if m.tags.get("addr:city")), None) or city,
        )
        if len(members) > 1:
            c.notes.append(f"{len(members)} locations on OpenStreetMap")
        if brand and first.tags.get("brand:wikidata"):
            c.notes.append("recognised brand (Wikidata)")
        if set(c.shop_types) == {"beauty"} or any(w in f" {c.name.lower()} " for w in SERVICE_WORDS):
            c.notes.append("beauty salon or service business: sells services, usually not products")
        candidates.append(c)
    return sorted(candidates, key=lambda c: (c.branches > 2, c.website is None, c.name.lower())), len(places) - len(named)


def classify(session: Session, candidates: list[Candidate], default_region: str, services: list[str] | None = None) -> None:
    """services: what the chosen profile sells. Salons are poor product-ad prospects but good bot and website ones."""
    product_ads = not services or services == ["product_ads"]
    for c in candidates:
        if not product_ads:
            c.notes = [n for n in c.notes if not n.startswith("beauty salon")]
            if not c.website:
                c.notes.append("no website listed")
            elif host := normalize.listing_host(c.website):
                c.notes.append(f"'website' is a {host} page, not their own site")
        phone = normalize.phone_e164(c.phone, default_region) if c.phone else None
        exact = exact_matches(session, domain=normalize.domain_of(c.website), handle=c.instagram, phone=phone)
        if exact:
            c.status, c.selected = "in_list", False
            c.message = f"already in your list as #{exact[0].business.id} {exact[0].business.name} ({exact[0].reason})"
            continue
        fuzzy = fuzzy_matches(session, name_key=normalize.name_key(c.name), city=c.city,
                              domain=normalize.domain_of(c.website), handle=c.instagram)
        if fuzzy:
            c.status, c.selected = "possible_duplicate", False
            c.message = f"similar to #{fuzzy[0].business.id} {fuzzy[0].business.name} ({fuzzy[0].reason})"
        if "beauty salon" in " ".join(c.notes) or c.branches > 2:
            c.selected = False  # shown, but not pre-selected: services or chains are weaker fits
        if not (c.website or c.instagram or c.phone or c.email):
            # Live Qatar search: ~95% of listings were name-only. Pre-selecting them would flood the list.
            c.selected = False
            c.notes.append("name only on OpenStreetMap: find their website or Instagram first" if product_ads
                           else "name only on OpenStreetMap: no phone or social to reach them yet")
    if not product_ads:
        # Best first: reachable businesses with no site of their own, then reachable ones with a site; chains last.
        candidates.sort(key=lambda c: (c.branches > 2, not (c.phone or c.instagram or c.email),
                                       bool(c.website) and not normalize.listing_host(c.website), c.name.lower()))


def add_candidates(session: Session, candidates: list[Candidate], keys: set[str], *, run: DiscoveryRun, country: str,
                   icp_id: int | None, actor: str, default_region: str) -> dict:
    created, skipped, ids = 0, 0, []
    for c in candidates:
        if c.key not in keys or c.status == "in_list":
            continue
        industry = ", ".join(label_of(t).lower() for t in c.shop_types)[:120]
        data = LeadInput(name=c.name, website=c.website or "", instagram=c.instagram or "", phone=c.phone or "",
                         email=c.email or "", email_source=c.osm_urls[0] if c.email else "", city=c.city or "",
                         country=country, industry=industry, icp_id=icp_id,
                         notes="Found on OpenStreetMap" + (f" ({'; '.join(c.notes)})" if c.notes else ""))
        try:
            biz, _ = create_lead(session, data, actor=actor, source="osm", source_ref=c.osm_urls[0], default_region=default_region)
        except (DuplicateLead, LeadError):
            skipped += 1  # changed since preview, or OSM data too odd to store (e.g. invalid phone)
            continue
        session.add(Evidence(
            business_id=biz.id, kind="directory_listing", created_by="directory", source_url=c.osm_urls[0],
            text=f"OpenStreetMap lists {c.branches} location(s) for this business in {country}"
                 + (f" (brand tag: {c.brand})" if c.brand else "") + f"; category: {', '.join(c.shop_types)}",
            value={"branches": c.branches, "brand": c.brand, "shop_types": c.shop_types, "osm_urls": c.osm_urls,
                   "wikidata_brand": "recognised brand (Wikidata)" in c.notes}))
        session.flush()
        session.expire(biz, ["evidence"])
        rescore(session, biz)
        created += 1
        ids.append(biz.id)
    run.added += created
    audit.record(session, actor, "discovery.add", "discovery", run.id, source=run.source, added=created, skipped=skipped)
    return {"created": created, "skipped": skipped, "ids": ids}


def to_json(candidates: list[Candidate]) -> list[dict]:
    return [asdict(c) for c in candidates]


def from_json(items: list[dict]) -> list[Candidate]:
    return [Candidate(**i) for i in items]
