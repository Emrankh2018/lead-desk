"""Explainable, deterministic lead scoring.

Each criterion returns a fraction (0-1) with an explanation and the evidence ids it relied on, or None when
there is no evidence either way. score = weighted average over criteria with evidence; coverage = share of the
total weight that had evidence. A high score with low coverage means "promising but under-researched".
The score measures fit with the ideal customer profile and visible opportunity, not purchase intent.

Criteria tied to a service (website, WhatsApp bot, ...) only count for profiles that sell that service. A criterion
can also answer NA ("does not apply", e.g. a website chatbot for a business with no website), which removes its
weight instead of counting as missing evidence.
"""
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import date

from leadgen.normalize import listing_host
from leadgen.offerings import services_of

RUBRIC_VERSION = "rubric-v3"  # v3 adds per-service criteria: websites, WhatsApp reply bots and website chatbots
NA = "na"


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
    services: tuple[str, ...] = ()  # empty: applies to every profile
    gap_for: str | None = None  # the service whose opportunity this criterion measures (used to pick a pitch)


def _of(evidence: list[Ev], kind: str) -> list[Ev]:
    return [e for e in evidence if e.kind == kind]


# British/American and common variant spellings, so "Jewellery" (OpenStreetMap label) matches a "jewelry" profile.
_SPELLING = {"jewellery": "jewelry", "jewelery": "jewelry", "jewellers": "jewelers", "jeweller": "jeweler",
             "perfumery": "perfume", "perfumes": "perfume", "fragrances": "fragrance", "colour": "color",
             "accessories": "accessory", "cosmetic": "cosmetics"}


def _norm(text: str) -> str:
    text = text.lower()
    for variant, canonical in _SPELLING.items():
        text = text.replace(variant, canonical)
    return text


def _industry_fit(biz, evidence, icp):
    terms = [_norm(t) for t in (icp.industries + icp.keywords)] if icp else []
    if not terms:
        return None, "Profile lists no industries or keywords", []
    sources = [(None, _norm(biz.industry or ""))] + [(e.id, _norm(e.text)) for e in evidence
                                                       if e.kind in ("page_title", "meta_description", "directory_listing")]
    hits = sorted({t for _, text in sources for t in terms if t and t in text})
    ids = [i for i, text in sources if i and any(t in text for t in hits)]
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


def _independent(biz, evidence, icp):
    listings = _of(evidence, "directory_listing")
    if not listings:
        return None, "Size unknown (no directory listing with location count)", []
    best = max(listings, key=lambda e: e.value.get("branches", 1))
    n = best.value.get("branches", 1)
    if best.value.get("wikidata_brand") or n >= 3:
        return 0.2, f"Chain or recognised brand ({n} locations listed); likely has in-house or agency marketing", [best.id]
    if n == 2:
        return 0.6, "Two locations listed: small multi-branch business", [best.id]
    return 1.0, "Single location listed: independent business, the core pilot customer", [best.id]


def _website_gap(biz, evidence, icp):
    listing = [e.id for e in _of(evidence, "directory_listing")]
    if not biz.website:
        if biz.instagram_handle:
            return 1.0, f"No website on record, only Instagram @{biz.instagram_handle} (check they have no site before pitching)", listing
        if listing:
            return 0.9, "No website on the directory listing or on record (not proof they have none; check before pitching)", listing
        return 0.8, "No website on record (not verified)", []
    if host := listing_host(biz.website):
        return 1.0, f"The only 'website' on record is a page on {host}, not a site of their own", listing
    if down := _of(evidence, "website_down"):
        return 0.9, "The website on record did not load when checked (could be temporary)", [down[0].id]
    viewport, https = _of(evidence, "mobile_viewport"), _of(evidence, "https")
    if not viewport and not https:
        return None, "Website not checked yet", []
    problems, ids, fraction = [], [], 0.15
    if viewport and not viewport[0].value.get("present"):
        problems.append("not set up for phones")
        ids.append(viewport[0].id)
        fraction += 0.35
    if https and not https[0].value.get("secure"):
        problems.append("no secure https")
        ids.append(https[0].id)
        fraction += 0.25
    years = _of(evidence, "copyright_year")
    if years and years[0].value.get("year", 9999) <= date.today().year - 3:
        problems.append(f"footer says © {years[0].value['year']}")
        ids.append(years[0].id)
        fraction += 0.25
    if not problems:
        return 0.15, "Website looks current (phone-ready, secure); a rebuild is a hard sell", [e.id for e in viewport + https]
    return min(fraction, 1.0), f"Website shows its age: {', '.join(problems)}", ids


def _messaging_gap(biz, evidence, icp):
    if auto := [e for e in _of(evidence, "whatsapp_automation") if e.value.get("tools")]:
        return 0.15, f"Already uses WhatsApp automation ({', '.join(auto[0].value['tools'])})", [auto[0].id]
    wa = [e for e in _of(evidence, "social_link") if e.value.get("network") == "whatsapp"]
    wa += [e for e in _of(evidence, "whatsapp_automation") if e.value.get("button")]
    if wa:
        return 1.0, "Invites customers to message on WhatsApp; replies are most likely typed by hand", [e.id for e in wa]
    phones = _of(evidence, "published_phone")
    if phones or biz.phone_e164:
        return 0.8, "Customers reach them by phone; a WhatsApp bot could answer common questions and bookings", [e.id for e in phones]
    if biz.instagram_handle:
        return 0.5, "Only Instagram on record: customers message by DM; WhatsApp use unknown", []
    return None, "No phone or WhatsApp on record", []


def _chat_gap(biz, evidence, icp):
    if not biz.website or listing_host(biz.website) or _of(evidence, "website_down"):
        return NA, "No working website: a site chatbot comes after a website", []
    chat = _of(evidence, "chat_widget")
    if not chat:
        return None, "Website not checked yet", []
    if chat[0].value.get("tools"):
        return 0.2, f"Site already has a chat tool ({', '.join(chat[0].value['tools'])})", [chat[0].id]
    return 0.9, "No chat assistant found on the site; visitors' questions wait for someone to reply", [chat[0].id]


# OpenStreetMap tags (and industry words) for businesses whose customers ask, book and order a lot.
_HIGH_CONTACT = ("restaurant", "cafe", "fast_food", "fast food", "bakery", "pastry", "clinic", "dentist", "doctor",
                 "pharmacy", "veterinary", "optician", "beauty", "hairdresser", "massage", "spa", "fitness", "gym",
                 "car_repair", "car repair", "car_wash", "car wash", "tyres", "hotel", "guest_house", "language_school",
                 "driving_school", "music_school", "training", "estate_agent", "real estate", "travel_agency", "travel",
                 "salon", "barber", "laundry", "dry_cleaning", "nails")
_RETAIL = ("shop", "store", "boutique", "jewel", "perfum", "cosmetic", "clothes", "furniture", "electronics",
           "mobile_phone", "florist", "gift", "shoes", "toys")


def _customer_questions(biz, evidence, icp):
    listing = _of(evidence, "directory_listing")
    words = " ".join([biz.industry or ""] + [" ".join(e.value.get("shop_types") or []) for e in listing]).lower()
    ids = [e.id for e in listing]
    if not words.strip():
        return None, "Business type unknown", []
    hit = next((w for w in _HIGH_CONTACT if re.search(rf"(?<![a-z]){re.escape(w)}s?(?![a-z])", words)), None)
    if hit:
        booking = [e for e in _of(evidence, "online_booking") if e.value.get("tools")]
        if booking:
            return 0.7, (f"Customer-facing business ({hit.replace('_', ' ')}) that already books online "
                         f"({', '.join(booking[0].value['tools'])}); a bot would mainly answer questions"), ids + [booking[0].id]
        return 1.0, f"Customer-facing business ({hit.replace('_', ' ')}): many booking, price and opening-hour questions", ids
    if any(w in words for w in _RETAIL):
        return 0.6, "Shop: customers ask about stock, prices and delivery", ids
    return 0.4, "Office or other business: fewer routine customer questions", ids


CRITERIA = [
    Criterion("industry_fit", "Industry fit", 25, "Business sells something in the profile's industries/keywords", _industry_fit),
    Criterion("location_fit", "Location fit", 20, "Operates in the profile's target country", _location_fit),
    Criterion("video_gap", "Video opportunity", 20, "No product video on the site yet", _video_gap, ("product_ads",), "product_ads"),
    Criterion("sells_online", "Sells online", 15, "Has an online store or catalogue that benefits from better visuals",
              _sells_online, ("product_ads",)),
    Criterion("social_presence", "Visual social presence", 10, "Uses Instagram/TikTok etc., where short ads are used",
              _social_presence, ("product_ads",)),
    Criterion("website_gap", "Website opportunity", 25, "No website, a dead one, or one that is not phone-ready or secure",
              _website_gap, ("website",), "website"),
    Criterion("messaging_gap", "WhatsApp bot opportunity", 20, "Customers message or call them, with no WhatsApp automation yet",
              _messaging_gap, ("whatsapp_bot",), "whatsapp_bot"),
    Criterion("chat_gap", "Website chatbot opportunity", 10, "Has a website with no chat assistant", _chat_gap,
              ("ai_chatbot",), "ai_chatbot"),
    Criterion("customer_questions", "Customer questions", 15, "Type of business that gets many booking and price questions",
              _customer_questions, ("whatsapp_bot", "ai_chatbot")),
    Criterion("reachable", "Reachable", 10, "Has a public, appropriate contact route", _reachable),
    Criterion("independent", "Independent brand", 10, "Single-location or small brand rather than a large chain", _independent),
]
CRITERIA_BY_ID = {c.id: c for c in CRITERIA}


def applies(c: Criterion, icp) -> bool:
    if c.id == "industry_fit" and icp and not (icp.industries or icp.keywords):
        return False  # a profile open to any industry has nothing to match against
    return not c.services or bool(set(c.services) & set(services_of(icp)))


def weights_for(icp) -> dict[str, int]:
    """Weight per criterion; 0 for criteria that do not apply to this profile's services."""
    custom = (icp.scoring_weights or {}) if icp else {}
    return {c.id: int(custom.get(c.id, c.default_weight)) if applies(c, icp) else 0 for c in CRITERIA}


def service_fits(breakdown: list[dict] | None, icp) -> list[tuple[str, float, str]]:
    """(service, fraction, explanation) for each of the profile's services that has evidence, best first."""
    rows = {r["id"]: r for r in breakdown or []}
    fits = []
    for c in CRITERIA:
        r = rows.get(c.id)
        if c.gap_for in services_of(icp) and r and isinstance(r.get("fraction"), (int, float)):
            fits.append((c.gap_for, float(r["fraction"]), r["explanation"]))
    return sorted(fits, key=lambda f: -f[1])


def best_service(breakdown: list[dict] | None, icp) -> str | None:
    """The service to pitch: the strongest opportunity, if it is a real one."""
    fits = service_fits(breakdown, icp)
    return fits[0][0] if fits and fits[0][1] >= 0.6 else None


def score(biz, evidence: list[Ev], icp) -> tuple[int | None, int, list[dict]]:
    """Return (score 0-100 or None, coverage %, breakdown rows)."""
    weights = weights_for(icp)
    results = []
    for c in CRITERIA:
        w = weights[c.id]
        if w <= 0:
            continue
        fraction, explanation, ids = c.fn(biz, evidence, icp)
        if fraction == NA:
            continue  # does not apply to this business; neither evidence nor a gap
        results.append(CriterionResult(c.id, c.label, w, fraction, explanation, ids))
    total = sum(r.weight for r in results)
    known = [r for r in results if r.fraction is not None]
    known_weight = sum(r.weight for r in known)
    coverage = round(100 * known_weight / total) if total else 0
    value = round(100 * sum(r.weight * r.fraction for r in known) / known_weight) if known_weight else None
    rows = [asdict(r) | {"points": r.points} for r in results]
    return value, coverage, rows
