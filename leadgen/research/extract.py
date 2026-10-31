"""Deterministic signal extraction from fetched HTML.

Each Signal states only what the page shows. Absence signals are marked verified=False because a homepage
not showing something (often due to JavaScript rendering) is not proof the business lacks it.
"""
import json
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

from selectolax.lexbor import LexborHTMLParser

from leadgen import normalize

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
ASSET_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".css", ".js")
SOCIAL_HOSTS = {
    "instagram.com": "instagram", "tiktok.com": "tiktok", "facebook.com": "facebook", "snapchat.com": "snapchat",
    "linkedin.com": "linkedin", "x.com": "x", "twitter.com": "x", "youtube.com": "youtube", "pinterest.com": "pinterest",
    "wa.me": "whatsapp", "api.whatsapp.com": "whatsapp",
}
PLATFORM_MARKERS = {
    "Shopify": ["cdn.shopify.com", "shopify.theme", "myshopify.com"],
    "WooCommerce": ["woocommerce", "wc-block", "wp-content/plugins/woocommerce"],
    "Salla": ["salla.sa", "cdn.salla", "salla.network", "s-salla"],
    "Zid": ["zid.store", "zidcdn", "zid.sa/"],
    "ExpandCart": ["expandcart"],
    "Magento": ["mage/cookies", "magento_"],
    "Wix": ["wixstatic.com", "wix.com/"],
    "Squarespace": ["squarespace.com", "static1.squarespace"],
    "BigCommerce": ["bigcommerce.com"],
    "OpenCart": ["route=product/", "catalog/view/theme"],
}
STORE_TEXT_MARKERS = ["add to cart", "add to bag", "add to basket", "buy now", "checkout", "أضف إلى السلة", "اضف للسلة", "اشتر الآن"]
QATAR_MARKERS = [("QAR", r"\bQAR\b"), ("ر.ق", r"ر\.ق"), ("Doha", r"\bDoha\b"), ("Qatar", r"\bQatar\b"),
                 ("الدوحة", r"الدوحة"), ("قطر", r"قطر"), ("+974", r"\+974")]
CONTACT_LINK_WORDS = ["contact", "about", "تواصل", "اتصل", "من نحن"]
SNIPPET_LEN = 240


@dataclass
class Signal:
    kind: str
    text: str
    value: dict = field(default_factory=dict)
    verified: bool = True
    snippet: str | None = None


def _visible_text(tree: LexborHTMLParser) -> str:
    for node in tree.css("script, style, noscript, template, svg"):
        node.decompose()
    body = tree.body
    return re.sub(r"\s+", " ", body.text(separator=" ")).strip() if body else ""


def _snippet(text: str, needle: str) -> str:
    i = text.lower().find(needle.lower())
    if i < 0:
        return text[:SNIPPET_LEN]
    start = max(0, i - SNIPPET_LEN // 2)
    return ("…" if start else "") + text[start:start + SNIPPET_LEN] + "…"


def extract_page(html: str, page_url: str, *, site_domain: str | None) -> tuple[list[Signal], list[str]]:
    """Return (signals, candidate contact/about page URLs on the same site)."""
    raw_lower = html.lower()
    tree = LexborHTMLParser(html)
    signals: list[Signal] = []
    products = _json_ld_products(tree)  # before _visible_text(), which strips <script> nodes

    title = tree.css_first("title")
    if title and title.text(strip=True):
        signals.append(Signal("page_title", f"Page title: “{title.text(strip=True)[:200]}”", {"title": title.text(strip=True)[:300]}))
    desc = tree.css_first('meta[name="description"]') or tree.css_first('meta[property="og:description"]')
    if desc and (desc.attributes.get("content") or "").strip():
        d = desc.attributes["content"].strip()[:400]
        signals.append(Signal("meta_description", f"Site description: “{d}”", {"description": d}))
    html_node = tree.css_first("html")
    lang = (html_node.attributes.get("lang") or "").strip() if html_node else ""
    if lang:
        signals.append(Signal("language", f"Page language attribute: {lang}", {"lang": lang}))

    platforms = [p for p, marks in PLATFORM_MARKERS.items() if any(m in raw_lower for m in marks)]
    if platforms:
        signals.append(Signal("platform", f"Site code contains markers of: {', '.join(platforms)}", {"platforms": platforms}))

    socials: dict[str, str] = {}
    contact_pages: list[str] = []
    mailtos, tels = set(), set()
    for a in tree.css("a[href]"):
        href = (a.attributes.get("href") or "").strip()
        low = href.lower()
        if low.startswith("mailto:"):
            if e := normalize.email(href):
                mailtos.add(e)
            continue
        if low.startswith("tel:"):
            tels.add(href[4:].strip())
            continue
        absolute = urljoin(page_url, href)
        host = (urlsplit(absolute).hostname or "").removeprefix("www.").removeprefix("m.")
        if host in SOCIAL_HOSTS:
            net = SOCIAL_HOSTS[host]
            if net not in socials and urlsplit(absolute).path.strip("/"):
                socials[net] = absolute
        elif site_domain and host.endswith(site_domain):
            label = (a.text(strip=True) + " " + low).lower()
            if any(w in label for w in CONTACT_LINK_WORDS) and absolute not in contact_pages:
                contact_pages.append(absolute.split("#")[0])
    for net, url in socials.items():
        value = {"network": net, "url": url}
        if net == "instagram" and (h := normalize.instagram_handle(url)):
            value["handle"] = h
        signals.append(Signal("social_link", f"Links to a {net} profile: {url}", value))

    text = _visible_text(tree)
    visible_emails = {e.lower() for e in EMAIL_RE.findall(text) if not e.lower().endswith(ASSET_SUFFIXES)}
    for e in sorted(mailtos | {v for v in visible_emails if normalize.email(v)}):
        signals.append(Signal("published_email", f"Email address published on the page: {e}", {"email": e}, snippet=_snippet(text, e)))
    for t in sorted(tels):
        signals.append(Signal("published_phone", f"Phone link on the page: {t}", {"phone": t}))

    video_tags = len(tree.css("video"))
    embeds = [f.attributes.get("src") or "" for f in tree.css("iframe")]
    video_embeds = sum(1 for s in embeds if any(v in s for v in ("youtube", "vimeo", "tiktok", "wistia")))
    mp4 = raw_lower.count(".mp4") + raw_lower.count(".webm") + raw_lower.count(".m3u8")
    signals.append(Signal("video_presence", f"Page has {video_tags} video element(s), {video_embeds} video embed(s), {mp4} video file reference(s)",
                          {"video_tags": video_tags, "video_embeds": video_embeds, "video_files": mp4}))
    imgs = len(tree.css("img")) + len(tree.css("picture source"))
    signals.append(Signal("image_count", f"Page has {imgs} image element(s)", {"images": imgs}))

    store_hits = [m for m in STORE_TEXT_MARKERS if m in text.lower()]
    if store_hits:
        signals.append(Signal("store_markers", f"Page shows shopping wording: {', '.join(store_hits[:3])}", {"markers": store_hits},
                              snippet=_snippet(text, store_hits[0])))
    if products:
        signals.append(Signal("structured_products", f"Page declares {products} product(s) in structured data (schema.org)", {"count": products}))

    qa_hits = [label for label, pattern in QATAR_MARKERS if re.search(pattern, text)]
    if qa_hits:
        signals.append(Signal("qatar_mentions", f"Page text mentions: {', '.join(qa_hits)}", {"markers": qa_hits},
                              snippet=_snippet(text, qa_hits[0].lstrip("+"))))
    years = [int(y) for y in re.findall(r"(?:©|&copy;|copyright)\s*(?:\d{4}\s*[-–]\s*)?(20\d{2})", html, flags=re.I)]
    if years:
        signals.append(Signal("copyright_year", f"Footer copyright year: {max(years)}", {"year": max(years)}))
    if len(text) < 200:
        signals.append(Signal("thin_content", "Very little readable text in the HTML (site may render with JavaScript); "
                              "some details could not be checked", {"chars": len(text)}, verified=False))
    return signals, contact_pages[:3]


def _json_ld_products(tree: LexborHTMLParser) -> int:
    count = 0
    for node in tree.css('script[type="application/ld+json"]'):
        try:
            data = json.loads(node.text() or "null")
        except json.JSONDecodeError:
            continue
        stack = [data]
        while stack:
            item = stack.pop()
            if isinstance(item, list):
                stack.extend(item)
            elif isinstance(item, dict):
                t = item.get("@type")
                if t == "Product" or (isinstance(t, list) and "Product" in t):
                    count += 1
                stack.extend(v for k, v in item.items() if k in ("@graph", "itemListElement", "item"))
    return count


def shopify_product_count(json_text: str) -> int | None:
    try:
        data = json.loads(json_text)
    except json.JSONDecodeError:
        return None
    products = data.get("products") if isinstance(data, dict) else None
    return len(products) if isinstance(products, list) else None


def redirect_signal(requested: str, final: str) -> Signal | None:
    """Flag a homepage that lands on another domain or a country-specific storefront."""
    a, b = urlsplit(requested), urlsplit(final)
    ha, hb = (a.hostname or "").removeprefix("www."), (b.hostname or "").removeprefix("www.")
    if ha == hb and not re.match(r"^/(ae|sa|kw|om|bh|uae|ksa)(/|$)", b.path, flags=re.I):
        return None
    return Signal("redirect", f"Website redirects to {final}", {"from": requested, "to": final,
                  "country_hint": _country_hint(hb, b.path)})


_COUNTRY_CODES = {"ae": "AE", "uae": "AE", "sa": "SA", "ksa": "SA", "kw": "KW", "om": "OM", "bh": "BH", "qa": "QA"}


def _country_hint(host: str, path: str) -> str | None:
    """Country implied by a subdomain (ae.brand.com), TLD (brand.ae) or first path segment (/ae/...)."""
    labels = host.split(".")
    first_segment = path.strip("/").split("/")[0].lower()
    for candidate in (labels[0], labels[-1], first_segment):
        if candidate in _COUNTRY_CODES:
            return _COUNTRY_CODES[candidate]
    return None
