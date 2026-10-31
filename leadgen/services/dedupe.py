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


def fuzzy_matches(session: Session, *, name_key: str, city: str | None, exclude_id: int | None = None) -> list[Match]:
    city_key = (city or "").strip().lower()
    out = []
    for b in session.scalars(select(Business).where(Business.id != (exclude_id or 0))):
        other_city = (b.city or "").strip().lower()
        if city_key and other_city and city_key != other_city:
            continue
        sim = round(fuzz.token_sort_ratio(name_key, b.name_key))
        if sim >= FUZZY_THRESHOLD:
            out.append(Match(b, f"similar name ({sim}%)" + (" in same city" if city_key and other_city else ""), sim))
    return sorted(out, key=lambda m: -m.similarity)
