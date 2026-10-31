"""M0 spike: how many target-category businesses does OpenStreetMap list in Qatar, and how many have a website/social tag?

One read-only Overpass query, identified User-Agent, results saved to spike/out/osm_qatar.json.
"""
import json
from collections import Counter
from pathlib import Path

import httpx

OUT = Path(__file__).parent / "out"
SHOP_TYPES = ["perfumery", "cosmetics", "jewelry", "watches", "beauty", "fashion_accessories"]
QUERY = f"""
[out:json][timeout:90];
area["ISO3166-1"="QA"][admin_level=2]->.qa;
nwr["shop"~"^({'|'.join(SHOP_TYPES)})$"](area.qa);
out center tags;
"""
HEADERS = {"User-Agent": "leadgen-spike/0.0.1 (personal research; contact: project owner)"}


def main() -> None:
    OUT.mkdir(exist_ok=True)
    resp = httpx.post("https://overpass-api.de/api/interpreter", data={"data": QUERY}, headers=HEADERS, timeout=120)
    resp.raise_for_status()
    elements = resp.json()["elements"]
    (OUT / "osm_qatar.json").write_text(json.dumps(elements, ensure_ascii=False, indent=1), encoding="utf-8")

    by_type = Counter(e["tags"].get("shop") for e in elements)
    named = [e for e in elements if e["tags"].get("name") or e["tags"].get("name:en")]
    web = [e for e in elements if any(k in e["tags"] for k in ("website", "contact:website", "url"))]
    social = [e for e in elements if any(k in e["tags"] for k in ("contact:instagram", "instagram", "contact:facebook"))]
    phone = [e for e in elements if any(k in e["tags"] for k in ("phone", "contact:phone"))]
    print(f"total={len(elements)} named={len(named)} with_website={len(web)} with_social={len(social)} with_phone={len(phone)}")
    print("by shop type:", dict(by_type))
    domains = Counter()
    for e in web:
        url = e["tags"].get("website") or e["tags"].get("contact:website") or e["tags"].get("url")
        host = httpx.URL(url if "://" in url else "https://" + url).host.removeprefix("www.")
        domains[host] += 1
    print(f"distinct website domains={len(domains)}")
    for host, n in domains.most_common(60):
        print(f"  {n:3d}  {host}")


if __name__ == "__main__":
    main()
