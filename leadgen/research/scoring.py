"""Explainable, deterministic lead scoring.

Each criterion returns a fraction (0-1) with an explanation and the evidence ids it relied on, or None when
there is no evidence either way. score = weighted average over criteria with evidence; coverage = share of the
total weight that had evidence. A high score with low coverage means "promising but under-researched".
The score measures fit with the ideal customer profile and visible opportunity, not purchase intent.
"""
from collections.abc import Callable
from dataclasses import asdict, dataclass

RUBRIC_VERSION = "rubric-v1"


@dataclass
class Ev:
    id: int
    kind: str
    value: dict
    verified: bool
    text: str


@dataclass
class CriterionResult:
    id: str
    label: str
    weight: int
    fraction: float | None
    explanation: str
    evidence_ids: list[int]

    @property
    def points(self) -> float | None:
        return None if self.fraction is None else round(self.weight * self.fraction, 1)


@dataclass
class Criterion:
    id: str
    label: str
    default_weight: int
    help: str
    fn: Callable


def _of(evidence: list[Ev], kind: str) -> list[Ev]:
    return [e for e in evidence if e.kind == kind]


def _industry_fit(biz, evidence, icp):
    terms = [t.lower() for t in (icp.industries + icp.keywords)] if icp else []
    if not terms:
        return None, "Profile lists no industries or keywords", []
    sources = [(None, biz.industry or "")] + [(e.id, e.text) for e in evidence if e.kind in ("page_title", "meta_description")]
    hits = sorted({t for _, text in sources for t in terms if t and t in text.lower()})
    ids = [i for i, text in sources if i and any(t in text.lower() for t in hits)]
    if hits:
        return 1.0, f"Matches profile terms: {', '.join(hits[:5])}", ids
    if any(text for _, text in sources):
        return 0.0, "Industry, title and description do not mention any profile industry or keyword", [i for i, _ in sources if i]
    return None, "No industry recorded and no website text yet", []


def _location_fit(biz, evidence, icp):
    countries = [c.upper() for c in icp.countries] if icp and icp.countries else []
    if not countries:
        return None, "Profile has no target country", []
    redirects = [e for e in _of(evidence, "redirect") if e.value.get("country_hint") and e.value["country_hint"] not in countries]
    mentions = _of(evidence, "qatar_mentions") if "QA" in countries else []
    if redirects:
        r = redirects[0]
        return 0.3, f"Website redirects to a storefront for {r.value['country_hint']}; may be run from outside the target country", [r.id]
    if mentions:
        return 1.0, f"Site mentions {', '.join(mentions[0].value.get('markers', [])[:3])}", [mentions[0].id]
    if biz.country and biz.country.upper() in countries:
        return 0.8, f"Recorded country is {biz.country} (from your input or import, not verified on the site)", []
    if biz.country:
        return 0.0, f"Recorded country {biz.country} is outside the profile ({', '.join(countries)})", []
    return None, "No location evidence", []


def _sells_online(biz, evidence, icp):
    platform, store = _of(evidence, "platform"), _of(evidence, "store_markers")
    products = _of(evidence, "product_count") + _of(evidence, "structured_products")
    if products and any((p.value.get("count") or 0) > 0 for p in products):
        best = max(products, key=lambda p: p.value.get("count") or 0)
        return 1.0, f"Online catalogue with {best.value['count']}+ products", [best.id]
    if platform or store:
        return 0.9, "Store platform or shopping wording found on the site", [e.id for e in platform + store]
    if _of(evidence, "page_title"):
        return 0.3, "Website found but no online-store markers detected (could be a brochure site or JavaScript-rendered store)", []
    if biz.instagram_handle:
        return None, "No website checked; may sell through Instagram (check manually)", []
    return None, "No website evidence", []


def _social_presence(biz, evidence, icp):
    links = _of(evidence, "social_link")
    visual = [e for e in links if e.value.get("network") in ("instagram", "tiktok", "snapchat", "youtube", "pinterest")]
    if visual:
        nets = sorted({e.value["network"] for e in visual})
        return 1.0, f"Active on visual platforms: {', '.join(nets)}", [e.id for e in visual]
    if biz.instagram_handle:
        return 1.0, f"Instagram account @{biz.instagram_handle} on record (activity not verified)", []
    if links:
        return 0.6, "Only non-visual social links found", [e.id for e in links]
    return None, "No social links found on the pages checked (they may load via JavaScript; not proof of absence)", []


def _video_gap(biz, evidence, icp):
    video = _of(evidence, "video_presence")
    if not video:
        return None, "Website video content not checked", []
    total = sum((v.value.get("video_tags", 0) + v.value.get("video_embeds", 0) + min(v.value.get("video_files", 0), 1)) for v in video)
    ids = [v.id for v in video]
    if total == 0:
        return 1.0, "No video on the pages checked: clear room for product video (homepage only; social media not checked)", ids
    return 0.3, "Site already uses video; opportunity is upgrading or adding formats, not introducing video", ids


def _reachable(biz, evidence, icp):
    if biz.do_not_contact:
        return 0.0, "Marked do-not-contact", []
    emails = _of(evidence, "published_email")
    phones = _of(evidence, "published_phone") + [e for e in _of(evidence, "social_link") if e.value.get("network") == "whatsapp"]
    if emails or biz.email_public:
        return 1.0, "Public email available", [e.id for e in emails]
    if phones or biz.phone_e164 or biz.instagram_handle:
        return 0.7, "Reachable by phone/WhatsApp or Instagram DM", [e.id for e in phones]
    if biz.linkedin_url:
        return 0.5, "Only a LinkedIn page on record", []
    return 0.0, "No public contact route on record", []


CRITERIA = [
    Criterion("industry_fit", "Industry fit", 25, "Business sells something in the profile's industries/keywords", _industry_fit),
    Criterion("location_fit", "Location fit", 20, "Operates in the profile's target country", _location_fit),
    Criterion("video_gap", "Video opportunity", 20, "No product video on the site yet", _video_gap),
    Criterion("sells_online", "Sells online", 15, "Has an online store or catalogue that benefits from better visuals", _sells_online),
    Criterion("social_presence", "Visual social presence", 10, "Uses Instagram/TikTok etc., where short ads are used", _social_presence),
    Criterion("reachable", "Reachable", 10, "Has a public, appropriate contact route", _reachable),
]
CRITERIA_BY_ID = {c.id: c for c in CRITERIA}


def weights_for(icp) -> dict[str, int]:
    custom = (icp.scoring_weights or {}) if icp else {}
    return {c.id: int(custom.get(c.id, c.default_weight)) for c in CRITERIA}


def score(biz, evidence: list[Ev], icp) -> tuple[int | None, int, list[dict]]:
    """Return (score 0-100 or None, coverage %, breakdown rows)."""
    weights = weights_for(icp)
    results = []
    for c in CRITERIA:
        w = weights[c.id]
        if w <= 0:
            continue
        fraction, explanation, ids = c.fn(biz, evidence, icp)
        results.append(CriterionResult(c.id, c.label, w, fraction, explanation, ids))
    total = sum(r.weight for r in results)
    known = [r for r in results if r.fraction is not None]
    known_weight = sum(r.weight for r in known)
    coverage = round(100 * known_weight / total) if total else 0
    value = round(100 * sum(r.weight * r.fraction for r in known) / known_weight) if known_weight else None
    rows = [asdict(r) | {"points": r.points} for r in results]
    return value, coverage, rows
