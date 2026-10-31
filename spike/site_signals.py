"""M0 spike: can plain, robots-respecting HTTP fetching extract useful, verifiable signals from real target sites?

For each site: check robots.txt, fetch the homepage once, extract deterministic signals, save raw HTML
(gitignored) and a JSON summary to spike/out/site_signals.json.
"""
import json
import re
import sys
import time
from pathlib import Path
from urllib.robotparser import RobotFileParser

import httpx
from selectolax.lexbor import LexborHTMLParser as HTMLParser

OUT = Path(__file__).parent / "out"
RAW = OUT / "raw"
UA = "leadgen-spike/0.0.1 (personal research)"
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
SOCIAL_HOSTS = {
    "instagram.com": "instagram", "tiktok.com": "tiktok", "facebook.com": "facebook",
    "snapchat.com": "snapchat", "linkedin.com": "linkedin", "x.com": "x", "twitter.com": "x",
    "youtube.com": "youtube", "wa.me": "whatsapp", "api.whatsapp.com": "whatsapp",
}
PLATFORM_MARKERS = {
    "shopify": ["cdn.shopify.com", "Shopify.theme"],
    "woocommerce": ["woocommerce", "wc-block"],
    "salla": ["salla.sa", "cdn.salla", "salla.network"],
    "zid": ["zid.store", "zidcdn", "zid.sa"],
    "magento": ["Magento_", "mage/cookies"],
    "wordpress": ["wp-content/"],
}


def robots_allows(client: httpx.Client, base: str) -> tuple[bool | None, str]:
    rp = RobotFileParser()
    try:
        r = client.get(base + "/robots.txt")
    except httpx.HTTPError as exc:
        return None, f"robots fetch error: {type(exc).__name__}"
    if r.status_code >= 400:
        return True, f"no robots.txt ({r.status_code})"
    rp.parse(r.text.splitlines())
    return rp.can_fetch(UA, base + "/"), "robots.txt parsed"


def extract(html: str, final_url: str) -> dict:
    tree = HTMLParser(html)
    lowered = html.lower()
    links = [a.attributes.get("href") or "" for a in tree.css("a")]
    socials: dict[str, str] = {}
    for href in links:
        host = httpx.URL(href).host.removeprefix("www.") if href.startswith("http") else ""
        if host in SOCIAL_HOSTS and SOCIAL_HOSTS[host] not in socials:
            socials[SOCIAL_HOSTS[host]] = href
    mailto = {h[7:].split("?")[0] for h in links if h.lower().startswith("mailto:")}
    text = tree.body.text(separator=" ") if tree.body else ""
    visible_emails = {e for e in EMAIL_RE.findall(text) if not e.lower().endswith((".png", ".jpg", ".webp"))}
    meta_desc = tree.css_first('meta[name="description"]')
    title = tree.css_first("title")
    return {
        "final_url": final_url,
        "title": title.text(strip=True) if title else None,
        "meta_description": meta_desc.attributes.get("content") if meta_desc else None,
        "lang": (tree.css_first("html").attributes.get("lang") if tree.css_first("html") else None),
        "platforms": [p for p, marks in PLATFORM_MARKERS.items() if any(m.lower() in lowered for m in marks)],
        "video_tags": len(tree.css("video")),
        "video_embeds": sum(1 for f in tree.css("iframe") if any(v in (f.attributes.get("src") or "") for v in ("youtube", "vimeo", "tiktok"))),
        "mp4_refs": lowered.count(".mp4"),
        "img_count": len(tree.css("img")),
        "socials": socials,
        "published_emails": sorted(mailto | visible_emails),
        "tel_links": sorted({h[4:] for h in links if h.lower().startswith("tel:")}),
        "text_chars": len(text),
    }


def main(domains: list[str]) -> None:
    RAW.mkdir(parents=True, exist_ok=True)
    results = []
    with httpx.Client(headers={"User-Agent": UA}, follow_redirects=True, timeout=20) as client:
        for domain in domains:
            base = f"https://{domain}"
            row: dict = {"domain": domain}
            allowed, note = robots_allows(client, base)
            row["robots"] = note
            if allowed is False:
                row["status"] = "skipped: disallowed by robots.txt"
            else:
                t0 = time.perf_counter()
                try:
                    r = client.get(base + "/")
                    row["http_status"] = r.status_code
                    row["fetch_ms"] = round((time.perf_counter() - t0) * 1000)
                    if r.status_code == 200 and "html" in r.headers.get("content-type", ""):
                        (RAW / f"{domain}.html").write_text(r.text, encoding="utf-8")
                        row.update(extract(r.text, str(r.url)))
                        row["status"] = "ok"
                    else:
                        row["status"] = f"unusable response ({r.status_code}, {r.headers.get('content-type')})"
                except httpx.HTTPError as exc:
                    row["status"] = f"fetch error: {type(exc).__name__}"
            results.append(row)
            print(json.dumps(row, ensure_ascii=False))
            time.sleep(2)  # polite pacing between sites
    (OUT / "site_signals.json").write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main(sys.argv[1:])
