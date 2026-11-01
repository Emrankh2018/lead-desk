"""OpenStreetMap discovery through the public Overpass API (free, ODbL-licensed data).

Usage-policy safeguards: one query at a time, identified User-Agent, server-side timeout, results cached on disk
for 24 hours so repeating a search does not hit the server, at most two backed-off retries when the server says it is
at capacity (502/503/504), and an immediate stop on 429 (rate-limited).
Data © OpenStreetMap contributors (ODbL); each lead keeps a link to its OSM object for attribution.
"""
import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from leadgen import normalize

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
USER_AGENT = "LeadDesk/0.4 (personal B2B research; contact via project owner)"
CACHE_TTL_SECONDS = 24 * 3600
BUSY_STATUSES = (502, 503, 504)
BUSY_BACKOFF_SECONDS = (5, 15)  # two polite retries when the server reports it is at capacity
sleep = time.sleep  # tests replace this
_LOCK = threading.Lock()  # Overpass asks clients not to run parallel queries

# Shop categories offered in the UI: OSM tag value -> label. "beauty" is mostly salons (services), so it is opt-in.
SHOP_TYPES = {
    "perfumery": "Perfume shops", "cosmetics": "Cosmetics & skincare shops", "jewelry": "Jewellery", "watches": "Watches",
    "fashion_accessories": "Fashion accessories", "bag": "Bags", "clothes": "Clothing & abayas", "gift": "Gift shops",
    "chocolate": "Chocolates & sweets", "confectionery": "Confectionery", "beauty": "Beauty salons (services)",
    "boutique": "Boutiques", "shoes": "Shoes",
}
# ICP industry/keyword words -> shop types, used to pre-select categories from the customer profile.
INDUSTRY_HINTS = {
    "perfume": ["perfumery"], "fragrance": ["perfumery"], "oud": ["perfumery"], "skincare": ["cosmetics"],
    "cosmetics": ["cosmetics"], "beauty": ["cosmetics"], "jewel": ["jewelry"], "gold": ["jewelry"], "watch": ["watches"],
    "accessor": ["fashion_accessories", "bag"], "abaya": ["clothes", "boutique"], "fashion": ["clothes", "boutique"],
    "chocolate": ["chocolate", "confectionery"], "gift": ["gift"],
}
# Letters (any script), spaces, apostrophes and hyphens: all literal in Overpass regex and safe in its quoted strings.
_PLACE_RE = re.compile(r"^[^\W\d_][^\W\d_ '\-]*(?:[ '\-][^\W\d_]+)*$")


class OsmError(RuntimeError):
    pass


@dataclass
class Place:
    osm_type: str
    osm_id: int
    name: str
    shop: str
    tags: dict = field(default_factory=dict)

    @property
    def url(self) -> str:
        return f"https://www.openstreetmap.org/{self.osm_type}/{self.osm_id}"

    @property
    def website(self) -> str | None:
        raw = self.tags.get("website") or self.tags.get("contact:website") or self.tags.get("url")
        return normalize.website_url(raw) if raw else None

    @property
    def instagram(self) -> str | None:
        return normalize.instagram_handle(self.tags.get("contact:instagram") or self.tags.get("instagram"))

    @property
    def phone(self) -> str | None:
        raw = (self.tags.get("phone") or self.tags.get("contact:phone") or "").split(";")[0]
        return raw.strip() or None

    @property
    def email(self) -> str | None:
        return normalize.email(self.tags.get("email") or self.tags.get("contact:email"))

    @property
    def brand(self) -> str | None:
        return self.tags.get("brand") or self.tags.get("operator")


def categories_for(industries: list[str], keywords: list[str]) -> list[str]:
    words = " ".join(industries + keywords).lower()
    picked = {t for hint, types in INDUSTRY_HINTS.items() if hint in words for t in types}
    return [t for t in SHOP_TYPES if t in picked]


def build_query(country: str, shop_types: list[str], city: str | None) -> str:
    if not re.fullmatch(r"[A-Z]{2}", country):
        raise OsmError("country must be a two-letter ISO code, e.g. QA")
    types = [t for t in shop_types if t in SHOP_TYPES]
    if not types:
        raise OsmError("choose at least one category")
    lines = ["[out:json][timeout:90];", f'area["ISO3166-1"="{country}"][admin_level=2]->.country;']
    scope = "(area.country)"
    if city:
        safe = city.strip()
        if len(safe) > 60 or not _PLACE_RE.match(safe):
            raise OsmError("city may contain letters, spaces, apostrophes and hyphens only")
        lines.append(f'area["boundary"="administrative"]["name:en"~"^{safe}$",i](area.country)->.city;')
        lines.append(f'area["boundary"="administrative"]["name"~"^{safe}$",i](area.country)->.city2;')
        lines.append("(.city; .city2;)->.place;")
        scope = "(area.place)"
    lines.append(f'nwr["shop"~"^({"|".join(types)})$"]{scope};')
    lines.append("out center tags;")
    return "\n".join(lines)


def parse(elements: list[dict]) -> list[Place]:
    places = []
    for e in elements:
        tags = e.get("tags") or {}
        name = (tags.get("name:en") or tags.get("name") or tags.get("brand") or "").strip()
        places.append(Place(e.get("type", "node"), int(e.get("id", 0)), name, tags.get("shop", ""), tags))
    return places


def search(country: str, shop_types: list[str], city: str | None, *, cache_dir: Path,
           client: httpx.Client | None = None) -> tuple[list[Place], bool]:
    """Return (places, served_from_cache)."""
    query = build_query(country, shop_types, city)
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / f"osm-{hashlib.sha256(query.encode()).hexdigest()[:24]}.json"
    if cache_file.exists() and time.time() - cache_file.stat().st_mtime < CACHE_TTL_SECONDS:
        return parse(json.loads(cache_file.read_text(encoding="utf-8"))), True
    own = client is None
    client = client or httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=120)
    try:
        with _LOCK:
            for attempt, wait in enumerate(BUSY_BACKOFF_SECONDS + (None,)):
                resp = client.post(OVERPASS_URL, data={"data": query})
                # 502/503/504 mean "at capacity"; Overpass asks clients to back off and retry. 429 means we
                # are being rate-limited, so we stop at once.
                if resp.status_code not in BUSY_STATUSES or wait is None:
                    break
                sleep(wait)
    except httpx.TimeoutException as exc:
        raise OsmError("OpenStreetMap did not answer in time; try again later or narrow the search") from exc
    except httpx.HTTPError as exc:
        raise OsmError(f"could not reach OpenStreetMap ({type(exc).__name__})") from exc
    finally:
        if own:
            client.close()
    if resp.status_code == 429 or resp.status_code in BUSY_STATUSES:
        raise OsmError("OpenStreetMap's free server is busy or rate-limiting (HTTP %d); wait a few minutes" % resp.status_code)
    if resp.status_code != 200:
        raise OsmError(f"OpenStreetMap returned HTTP {resp.status_code}")
    try:
        elements = resp.json().get("elements", [])
    except ValueError as exc:
        raise OsmError("OpenStreetMap returned an unreadable answer") from exc
    cache_file.write_text(json.dumps(elements, ensure_ascii=False), encoding="utf-8")
    return parse(elements), False
