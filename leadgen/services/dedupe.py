"""Duplicate detection.

Exact identifiers (domain, Instagram handle, phone) are hard duplicates and block creation.
A similar name in the same (or unknown) city is only a *possible* duplicate: it is recorded for review, never merged silently.
"""
from dataclasses import dataclass

from rapidfuzz import fuzz
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from leadgen.models import Business

FUZZY_THRESHOLD = 90


@dataclass
class Match:
    business: Business
    reason: str
    similarity: int = 100


def exact_matches(session: Session, *, domain: str | None, handle: str | None, phone: str | None,
                  exclude_id: int | None = None) -> list[Match]:
    conds = []
    if domain:
        conds.append(Business.domain == domain)
    if handle:
        conds.append(Business.instagram_handle == handle)
    if phone:
        conds.append(Business.phone_e164 == phone)
    if not conds:
        return []
    stmt = select(Business).where(or_(*conds))
    if exclude_id:
        stmt = stmt.where(Business.id != exclude_id)
    out = []
    for b in session.scalars(stmt):
        reasons = [label for label, mine, theirs in (("website domain", domain, b.domain),
                                                     ("Instagram handle", handle, b.instagram_handle),
                                                     ("phone", phone, b.phone_e164)) if mine and mine == theirs]
        out.append(Match(b, "same " + " and ".join(reasons)))
    return out


MIN_COMPACT_LEN = 8  # shorter names ("gold", "oud") are too generic for containment matching


def _compact(text: str | None) -> str:
    return "".join(ch for ch in (text or "").lower() if ch.isalnum())


def _stem(domain: str | None) -> str:
    return _compact(domain.split(".")[0]) if domain else ""


def fuzzy_matches(session: Session, *, name_key: str, city: str | None, exclude_id: int | None = None,
                  domain: str | None = None, handle: str | None = None) -> list[Match]:
    """Possible duplicates for review: similar names (also with spacing differences, "Al Sugha" vs "Alsugha"),
    or a website name that equals another lead's Instagram handle."""
    city_key = (city or "").strip().lower()
    compact = _compact(name_key)
    out = []
    for b in session.scalars(select(Business).where(Business.id != (exclude_id or 0))):
        other_city = (b.city or "").strip().lower()
        stems = {(_stem(domain), _compact(b.instagram_handle)), (_compact(handle), _stem(b.domain))}
        if any(a and len(a) >= MIN_COMPACT_LEN and a == c for a, c in stems):
            out.append(Match(b, "website name matches the other lead's Instagram handle", 95))
            continue
        if city_key and other_city and city_key != other_city:
            continue
        sim = round(fuzz.token_sort_ratio(name_key, b.name_key))
        other = _compact(b.name_key)
        if sim < FUZZY_THRESHOLD and min(len(compact), len(other)) >= MIN_COMPACT_LEN and (compact in other or other in compact):
            sim = FUZZY_THRESHOLD  # one name contains the other once spaces are ignored
        if sim >= FUZZY_THRESHOLD:
            out.append(Match(b, f"similar name ({sim}%)" + (" in same city" if city_key and other_city else ""), sim))
    return sorted(out, key=lambda m: -m.similarity)
