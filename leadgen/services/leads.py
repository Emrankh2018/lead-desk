"""Lead (business) lifecycle: create, update, stage changes, history, merge and delete."""
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

from sqlalchemy import Select, or_, select, update
from sqlalchemy.orm import Session

from leadgen import normalize
from leadgen.models import Activity, Business, DuplicateCandidate, EffortLog, OutreachDraft, PipelineStage, utcnow
from leadgen.services import audit
from leadgen.services.dedupe import Match, exact_matches, fuzzy_matches

CHANNELS = ["email", "instagram", "linkedin", "whatsapp", "phone", "in_person", "other"]
ACTIVITY_KINDS = ["note", "outreach", "reply", "meeting", "proposal"]
# Fields copied from the removed record into the kept one when the kept one is empty.
MERGE_FIELDS = ["website", "domain", "instagram_handle", "linkedin_url", "phone_e164", "email_public",
                "email_source_url", "city", "country", "industry", "icp_id", "deal_value", "follow_up_on"]


class LeadError(ValueError):
    pass


class DuplicateLead(LeadError):
    def __init__(self, matches: list[Match]):
        self.matches = matches
        super().__init__("; ".join(f"{m.reason} as #{m.business.id} {m.business.name}" for m in matches))


@dataclass
class LeadInput:
    """Raw user/import input. normalise() turns it into storable fields or raises LeadError."""
    name: str = ""
    website: str = ""
    instagram: str = ""
    linkedin_url: str = ""
    phone: str = ""
    email: str = ""
    email_source: str = ""
    city: str = ""
    country: str = ""
    industry: str = ""
    icp_id: int | None = None
    deal_value: str = ""
    follow_up_on: str = ""
    notes: str = ""
    errors: list[str] = field(default_factory=list)

    def normalise(self, default_region: str) -> dict:
        errors = []
        website = self.website.strip()
        handle = normalize.instagram_handle(self.instagram)
        # A social URL pasted into the website field is a common mistake: keep it as the right identifier.
        if website and normalize.is_social_url(website):
            if "instagram.com" in website.lower() and not handle:
                handle = normalize.instagram_handle(website)
            elif "linkedin.com" in website.lower() and not self.linkedin_url:
                self.linkedin_url = website
            website = ""
        site = normalize.website_url(website)
        if website and not site:
            errors.append(f"website '{website}' is not a valid URL")
        linkedin = normalize.linkedin_url(self.linkedin_url) if self.linkedin_url.strip() else None
        if self.linkedin_url.strip() and not linkedin:
            errors.append(f"LinkedIn '{self.linkedin_url}' must be a linkedin.com address")
        if self.instagram.strip() and not handle:
            errors.append(f"Instagram '{self.instagram}' is not a valid handle")
        country = (self.country.strip().upper() or default_region)[:2]
        phone = normalize.phone_e164(self.phone, country)
        if self.phone.strip() and not phone:
            errors.append(f"phone '{self.phone}' is not a valid number")
        mail = normalize.email(self.email)
        if self.email.strip() and not mail:
            errors.append(f"email '{self.email}' is not a valid address")
        name = self.name.strip()
        if not name:
            # Never invent a name; derive a visible placeholder from a real identifier only.
            name = normalize.domain_of(site) or (f"@{handle}" if handle else "")
        if not name:
            errors.append("a business name, website or Instagram handle is required")
        deal = None
        if self.deal_value.strip():
            try:
                deal = Decimal(self.deal_value.replace(",", "").strip())
            except InvalidOperation:
                errors.append(f"deal value '{self.deal_value}' is not a number")
        follow = None
        if self.follow_up_on.strip():
            try:
                follow = date.fromisoformat(self.follow_up_on.strip())
            except ValueError:
                errors.append(f"follow-up date '{self.follow_up_on}' must be YYYY-MM-DD")
        if errors:
            raise LeadError("; ".join(errors))
        return {
            "name": name[:200], "name_key": normalize.name_key(name), "website": site, "domain": normalize.domain_of(site),
            "instagram_handle": handle, "linkedin_url": linkedin, "phone_e164": phone,
            "email_public": mail, "email_source_url": (self.email_source.strip() or None) if mail else None,
            "city": self.city.strip() or None, "country": country or None, "industry": self.industry.strip() or None,
            "icp_id": self.icp_id, "deal_value": deal, "follow_up_on": follow,
        }


def first_stage(session: Session) -> PipelineStage:
    stage = session.scalars(select(PipelineStage).order_by(PipelineStage.position)).first()
    if not stage:
        raise LeadError("no pipeline stages configured")
    return stage


def create_lead(session: Session, data: LeadInput, *, actor: str, source: str, source_ref: str | None,
                default_region: str) -> tuple[Business, list[Match]]:
    """Create a lead. Raises DuplicateLead on an exact identifier match; returns fuzzy candidates for review."""
    fields = data.normalise(default_region)
    exact = exact_matches(session, domain=fields["domain"], handle=fields["instagram_handle"], phone=fields["phone_e164"])
    if exact:
        raise DuplicateLead(exact)
    fuzzy = fuzzy_matches(session, name_key=fields["name_key"], city=fields["city"], domain=fields["domain"],
                          handle=fields["instagram_handle"])
    biz = Business(**fields, source=source, source_ref=source_ref, stage=first_stage(session))
    session.add(biz)
    session.flush()
    session.add(Activity(business_id=biz.id, kind="created", summary=f"Added from {source}" + (f" ({source_ref})" if source_ref else "")))
    if data.notes.strip():
        session.add(Activity(business_id=biz.id, kind="note", summary=data.notes.strip()))
    for m in fuzzy:
        session.add(DuplicateCandidate(business_id=biz.id, other_id=m.business.id, reason=m.reason, similarity=m.similarity))
    audit.record(session, actor, "lead.create", "business", biz.id, source=source, source_ref=source_ref,
                 possible_duplicates=[m.business.id for m in fuzzy])
    return biz, fuzzy


def update_lead(session: Session, biz: Business, data: LeadInput, *, actor: str, default_region: str) -> list[str]:
    fields = data.normalise(default_region)
    exact = exact_matches(session, domain=fields["domain"], handle=fields["instagram_handle"],
                          phone=fields["phone_e164"], exclude_id=biz.id)
    if exact:
        raise DuplicateLead(exact)
    changed = []
    for key, value in fields.items():
        if getattr(biz, key) != value:
            changed.append(key)
            setattr(biz, key, value)
    if changed:
        audit.record(session, actor, "lead.update", "business", biz.id, fields=changed)
    return changed


def change_stage(session: Session, biz: Business, stage: PipelineStage, *, actor: str) -> None:
    if biz.stage_id == stage.id:
        return
    old = biz.stage.name
    biz.stage = stage
    session.add(Activity(business_id=biz.id, kind="stage_change", summary=f"Stage: {old} → {stage.name}"))
    audit.record(session, actor, "lead.stage", "business", biz.id, old=old, new=stage.name)


def add_activity(session: Session, biz: Business, *, kind: str, channel: str | None, summary: str,
                 occurred_at=None, follow_up_on: date | None = None, actor: str) -> Activity:
    if kind not in ACTIVITY_KINDS:
        raise LeadError(f"unknown activity type '{kind}'")
    if channel and channel not in CHANNELS:
        raise LeadError(f"unknown channel '{channel}'")
    if not summary.strip():
        raise LeadError("summary is required")
    if kind == "outreach" and biz.do_not_contact:
        raise LeadError("this business is marked do-not-contact")
    act = Activity(business_id=biz.id, kind=kind, channel=channel or None, summary=summary.strip(),
                   occurred_at=occurred_at or utcnow())
    session.add(act)
    if kind == "outreach":
        biz.last_contacted_at = act.occurred_at
    if follow_up_on is not None:
        biz.follow_up_on = follow_up_on
    audit.record(session, actor, f"activity.{kind}", "business", biz.id, channel=channel)
    return act


def set_do_not_contact(session: Session, biz: Business, value: bool, *, actor: str) -> None:
    if biz.do_not_contact == value:
        return
    biz.do_not_contact = value
    session.add(Activity(business_id=biz.id, kind="note", summary="Marked do-not-contact" if value else "Do-not-contact removed"))
    audit.record(session, actor, "lead.do_not_contact", "business", biz.id, value=value)


def merge(session: Session, keep: Business, remove: Business, *, actor: str) -> None:
    """Merge `remove` into `keep`: empty fields are filled, history is moved, `remove` is deleted."""
    if keep.id == remove.id:
        raise LeadError("cannot merge a record into itself")
    copied = {f: getattr(remove, f) for f in MERGE_FIELDS if getattr(keep, f) in (None, "") and getattr(remove, f) not in (None, "")}
    removed_snapshot = {"id": remove.id, "name": remove.name, "domain": remove.domain, "instagram": remove.instagram_handle}
    for f in ("domain", "instagram_handle", "phone_e164"):  # free unique values before copying them across
        setattr(remove, f, None)
    session.flush()
    for f, v in copied.items():
        setattr(keep, f, v)
    for act in list(remove.activities):
        act.business_id = keep.id
    # The user's own work moves too; only evidence and AI ideas (tied to the removed record's research) are discarded.
    session.execute(update(OutreachDraft).where(OutreachDraft.business_id == remove.id).values(business_id=keep.id))
    session.execute(update(EffortLog).where(EffortLog.business_id == remove.id).values(business_id=keep.id))
    for cand in session.scalars(select(DuplicateCandidate).where(
            or_(DuplicateCandidate.business_id == remove.id, DuplicateCandidate.other_id == remove.id))):
        session.delete(cand)
    session.flush()
    session.expire(remove, ["activities"])
    session.add(Activity(business_id=keep.id, kind="merged", summary=f"Merged duplicate #{remove.id} {remove.name}"
                         + (" (its research evidence was discarded; re-run research)" if remove.researched_at else "")))
    session.delete(remove)
    audit.record(session, actor, "lead.merge", "business", keep.id, removed=removed_snapshot, copied_fields=sorted(copied))


def delete_lead(session: Session, biz: Business, *, actor: str) -> None:
    audit.record(session, actor, "lead.delete", "business", biz.id, name=biz.name, domain=biz.domain,
                 instagram=biz.instagram_handle, activities=len(biz.activities))
    session.delete(biz)


@dataclass
class LeadFilter:
    q: str = ""
    stage_id: int | None = None
    icp_id: int | None = None
    city: str = ""
    industry: str = ""
    follow_up_due: bool = False
    dnc: str = "exclude"  # exclude | include | only
    min_score: int | None = None
    sort: str = "updated"  # updated | name | score | follow_up | created

    def apply(self, stmt: Select, today: date) -> Select:
        if self.q.strip():
            like = f"%{self.q.strip()}%"
            stmt = stmt.where(or_(Business.name.ilike(like), Business.domain.ilike(like), Business.instagram_handle.ilike(like)))
        if self.stage_id:
            stmt = stmt.where(Business.stage_id == self.stage_id)
        if self.icp_id:
            stmt = stmt.where(Business.icp_id == self.icp_id)
        if self.city.strip():
            stmt = stmt.where(Business.city.ilike(self.city.strip()))
        if self.industry.strip():
            stmt = stmt.where(Business.industry.ilike(f"%{self.industry.strip()}%"))
        if self.follow_up_due:
            stmt = stmt.where(Business.follow_up_on.is_not(None), Business.follow_up_on <= today)
        if self.dnc == "exclude":
            stmt = stmt.where(Business.do_not_contact.is_(False))
        elif self.dnc == "only":
            stmt = stmt.where(Business.do_not_contact.is_(True))
        if self.min_score is not None:
            stmt = stmt.where(Business.score >= self.min_score)
        order = {
            "name": [Business.name_key],
            "score": [Business.score.desc().nulls_last(), Business.name_key],
            "follow_up": [Business.follow_up_on.asc().nulls_last(), Business.name_key],
            "created": [Business.created_at.desc()],
        }.get(self.sort, [Business.updated_at.desc()])
        return stmt.order_by(*order)


def query_leads(session: Session, flt: LeadFilter, today: date, limit: int | None = None) -> list[Business]:
    stmt = flt.apply(select(Business), today)
    if limit:
        stmt = stmt.limit(limit)
    return list(session.scalars(stmt))
