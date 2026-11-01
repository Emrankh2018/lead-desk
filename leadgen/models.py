"""Persistent data model.

Facts about a business live on Business (contact fields) and, from M2, on Evidence rows with source URLs.
Activity is append-only history; AuditLog records every consequential action.
"""
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import JSON, Boolean, Date, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from leadgen.db import Base


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class IcpProfile(Base):
    """Ideal customer profile: who to look for. Scoring weights are added in M2."""
    __tablename__ = "icp_profiles"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    countries: Mapped[list[str]] = mapped_column(JSON, default=list)
    cities: Mapped[list[str]] = mapped_column(JSON, default=list)
    industries: Mapped[list[str]] = mapped_column(JSON, default=list)
    keywords: Mapped[list[str]] = mapped_column(JSON, default=list)
    size_notes: Mapped[str] = mapped_column(Text, default="")
    # {criterion_id: weight}; criteria are defined in leadgen.research.scoring. Empty = defaults.
    scoring_weights: Mapped[dict] = mapped_column(JSON, default=dict)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Offer(Base):
    __tablename__ = "offers"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String(3), default="QAR")
    description: Mapped[str] = mapped_column(Text, default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class PipelineStage(Base):
    __tablename__ = "pipeline_stages"
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(40), unique=True)
    name: Mapped[str] = mapped_column(String(80))
    position: Mapped[int] = mapped_column(Integer, default=0)
    # "won" / "lost" mark terminal stages; analytics count deals by these, never by stage name.
    outcome: Mapped[str | None] = mapped_column(String(10))


class Business(Base):
    __tablename__ = "businesses"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    name_key: Mapped[str] = mapped_column(String(200), index=True)  # normalised for fuzzy dedupe
    website: Mapped[str | None] = mapped_column(String(500))
    domain: Mapped[str | None] = mapped_column(String(253), unique=True)
    instagram_handle: Mapped[str | None] = mapped_column(String(64), unique=True)
    linkedin_url: Mapped[str | None] = mapped_column(String(500))
    phone_e164: Mapped[str | None] = mapped_column(String(20), unique=True)
    email_public: Mapped[str | None] = mapped_column(String(254))  # only an address the business itself published
    email_source_url: Mapped[str | None] = mapped_column(String(500))
    city: Mapped[str | None] = mapped_column(String(120))
    country: Mapped[str | None] = mapped_column(String(2))
    industry: Mapped[str | None] = mapped_column(String(120))
    source: Mapped[str] = mapped_column(String(40))  # manual | csv | osm | places ...
    source_ref: Mapped[str | None] = mapped_column(String(500))
    icp_id: Mapped[int | None] = mapped_column(ForeignKey("icp_profiles.id", ondelete="SET NULL"))
    stage_id: Mapped[int] = mapped_column(ForeignKey("pipeline_stages.id"))
    score: Mapped[int | None] = mapped_column(Integer)  # 0-100 over criteria with known evidence
    score_coverage: Mapped[int | None] = mapped_column(Integer)  # % of rubric weight backed by evidence
    score_breakdown: Mapped[list | None] = mapped_column(JSON)
    scored_at: Mapped[datetime | None] = mapped_column(DateTime)
    researched_at: Mapped[datetime | None] = mapped_column(DateTime)
    deal_value: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    deal_currency: Mapped[str] = mapped_column(String(3), default="QAR")
    do_not_contact: Mapped[bool] = mapped_column(Boolean, default=False)
    follow_up_on: Mapped[date | None] = mapped_column(Date, index=True)
    last_contacted_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    stage: Mapped[PipelineStage] = relationship()
    icp: Mapped[IcpProfile | None] = relationship()
    activities: Mapped[list["Activity"]] = relationship(
        back_populates="business", cascade="all, delete-orphan", order_by="Activity.occurred_at.desc()"
    )
    # Current evidence only; rows are removed by the database's ON DELETE CASCADE when the business is deleted.
    evidence: Mapped[list["Evidence"]] = relationship(
        order_by="Evidence.id", primaryjoin="and_(Business.id == Evidence.business_id, Evidence.is_current)", viewonly=True,
    )


class Activity(Base):
    """Append-only contact history and notes. Never edited in place."""
    __tablename__ = "activities"
    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(30))  # note | outreach | reply | meeting | proposal | stage_change | created | merged
    channel: Mapped[str | None] = mapped_column(String(30))  # email | instagram | linkedin | phone | whatsapp | in_person
    summary: Mapped[str] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    business: Mapped[Business] = relationship(back_populates="activities")


class DuplicateCandidate(Base):
    """A possible duplicate pair found by fuzzy matching. Merging always needs the user's confirmation."""
    __tablename__ = "duplicate_candidates"
    __table_args__ = (UniqueConstraint("business_id", "other_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"))
    other_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"))
    reason: Mapped[str] = mapped_column(String(200))
    similarity: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(12), default="open")  # open | merged | dismissed
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    business: Mapped[Business] = relationship(foreign_keys=[business_id])
    other: Mapped[Business] = relationship(foreign_keys=[other_id])


class ResearchRun(Base):
    __tablename__ = "research_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(12), default="running")  # running | ok | partial | failed | skipped
    pages_fetched: Mapped[int] = mapped_column(Integer, default=0)
    issues: Mapped[list] = mapped_column(JSON, default=list)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)

    business: Mapped[Business] = relationship()


class Evidence(Base):
    """One verifiable observation with its source. Produced by code (or entered by the user), never by an LLM."""
    __tablename__ = "evidence"
    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("research_runs.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(40))
    value: Mapped[dict] = mapped_column(JSON, default=dict)
    text: Mapped[str] = mapped_column(Text)  # human-readable statement of exactly what was observed
    verified: Mapped[bool] = mapped_column(Boolean, default=True)  # False = absence/limitation, not proof
    source_url: Mapped[str | None] = mapped_column(String(1000))
    snippet: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str | None] = mapped_column(String(64))
    observed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_by: Mapped[str] = mapped_column(String(20), default="research")  # research | user


class AppSetting(Base):
    """Small key/value store for user preferences such as the sender profile used in drafts."""
    __tablename__ = "app_settings"
    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)


class AiInsight(Base):
    """AI-written opportunity hypotheses for a lead. Every item cites evidence ids; uncited items are dropped."""
    __tablename__ = "ai_insights"
    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(12), default="pending")  # pending | ready | failed
    content: Mapped[dict] = mapped_column(JSON, default=dict)  # {hypotheses: [{text, evidence_ids}], angle, insufficient}
    dropped: Mapped[list] = mapped_column(JSON, default=list)  # items removed by validation, with the reason
    error: Mapped[str | None] = mapped_column(Text)
    provider: Mapped[str | None] = mapped_column(String(40))
    model: Mapped[str | None] = mapped_column(String(120))
    prompt_version: Mapped[str | None] = mapped_column(String(20))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class OutreachDraft(Base):
    """A message prepared for the user's review. The app never sends it; the user sends it and marks it sent."""
    __tablename__ = "outreach_drafts"
    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    channel: Mapped[str] = mapped_column(String(20))  # email | linkedin | instagram | followup
    status: Mapped[str] = mapped_column(String(12), default="pending")  # pending | draft | approved | sent | discarded | failed
    subject: Mapped[str | None] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text, default="")
    warnings: Mapped[list] = mapped_column(JSON, default=list)
    evidence_ids: Mapped[list] = mapped_column(JSON, default=list)
    source: Mapped[str] = mapped_column(String(12), default="ai")  # ai | template
    error: Mapped[str | None] = mapped_column(Text)
    provider: Mapped[str | None] = mapped_column(String(40))
    model: Mapped[str | None] = mapped_column(String(120))
    prompt_version: Mapped[str | None] = mapped_column(String(20))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)

    business: Mapped[Business] = relationship()


class AuditLog(Base):
    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_entity", "entity", "entity_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(60))
    entity: Mapped[str] = mapped_column(String(40))
    entity_id: Mapped[int | None] = mapped_column(Integer)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
