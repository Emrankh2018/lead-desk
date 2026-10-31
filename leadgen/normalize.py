"""Deterministic normalisation of business identifiers, used for storage and duplicate detection."""
import re
import unicodedata

import phonenumbers

SOCIAL_HOSTS = {"instagram.com", "facebook.com", "tiktok.com", "linkedin.com", "x.com", "twitter.com",
                "snapchat.com", "youtube.com", "wa.me", "linktr.ee"}
_HANDLE_RE = re.compile(r"^[a-z0-9._]{1,30}$")
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
# Legal suffixes that differ between listings of the same business. Product words are kept on purpose.
_LEGAL_SUFFIXES = {"llc", "wll", "w.l.l", "co", "company", "est", "establishment", "trading", "ltd", "inc", "qpsc", "spc"}


def _host_and_path(raw: str) -> tuple[str, str]:
    value = raw.strip()
    if "://" not in value:
        value = "https://" + value
    m = re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://([^/?#]+)([^?#]*)", value)
    if not m:
        return "", ""
    host = m.group(1).rsplit("@", 1)[-1].split(":")[0].lower().rstrip(".")
    return host.removeprefix("www.").removeprefix("m."), m.group(2)


def domain_of(url: str | None) -> str | None:
    """Return the registrable-ish host for a business website, or None for empty/social/invalid URLs."""
    if not url or not url.strip():
        return None
    host, _ = _host_and_path(url)
    if not host or "." not in host or host in SOCIAL_HOSTS:
        return None
    return host


def is_social_url(url: str | None) -> bool:
    if not url:
        return False
    host, _ = _host_and_path(url)
    return host in SOCIAL_HOSTS


def instagram_handle(value: str | None) -> str | None:
    """Accept '@name', 'name' or an instagram.com URL; return the lower-case handle or None."""
    if not value or not value.strip():
        return None
    v = value.strip()
    if "instagram.com" in v.lower():
        _, path = _host_and_path(v)
        parts = [p for p in path.split("/") if p]
        if not parts or parts[0] in {"p", "reel", "explore", "stories"}:
            return None
        v = parts[0]
    v = v.lstrip("@").lower()
    return v if _HANDLE_RE.match(v) else None


def phone_e164(value: str | None, default_region: str = "QA") -> str | None:
    if not value or not value.strip():
        return None
    try:
        num = phonenumbers.parse(value, default_region)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(num):
        return None
    return phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.E164)


def email(value: str | None) -> str | None:
    if not value or not value.strip():
        return None
    v = value.strip().removeprefix("mailto:").split("?")[0]
    return v.lower() if _EMAIL_RE.match(v) else None


def name_key(name: str) -> str:
    """Lower-case, accent-free, punctuation-free name with legal suffixes removed (Arabic letters kept)."""
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode() if name.isascii() else name
    # Dots are dropped (not spaced) so abbreviations like "W.L.L." collapse to "wll" before suffix removal.
    text = re.sub(r"[^\w\s]", " ", text.lower().replace(".", ""))
    words = [w for w in text.split() if w not in _LEGAL_SUFFIXES]
    return " ".join(words) or text.strip()


def website_url(value: str | None) -> str | None:
    """Canonical https URL for a website field, or None if it is empty, invalid or a social profile."""
    d = domain_of(value)
    if not d:
        return None
    v = value.strip()
    return v if "://" in v else "https://" + v
