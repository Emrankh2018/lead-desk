"""Default configuration for a fresh database. Everything here is editable in Settings afterwards."""
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from leadgen.models import IcpProfile, Offer, PipelineStage

DEFAULT_STAGES = [
    ("discovered", "Discovered", None), ("qualified", "Qualified", None), ("ready", "Ready for outreach", None),
    ("contacted", "Contacted", None), ("responded", "Responded", None), ("meeting", "Meeting", None),
    ("proposal", "Proposal", None), ("won", "Won", "won"), ("lost", "Lost", "lost"),
]

DEFAULT_OFFERS = [
    ("Paid pilot", Decimal("450"), "One 10-15 s 9:16 promotional reel + 3 product visuals, 1 revision round."),
    ("Monthly creative package", Decimal("1400"), "4 short promotional reels + 8 marketing images + captions per month."),
    ("Custom campaign", None, "Individually quoted multi-product or special-format campaigns."),
]

DEFAULT_ICP = IcpProfile(
    name="Qatar product brands (fragrance, beauty, jewelry)",
    description="Product-based businesses in Qatar selling visually appealing products online or on social media, "
                "who could benefit from AI-produced promo videos and product imagery.",
    countries=["QA"], cities=["Doha", "Al Wakrah", "Lusail", "Al Rayyan", "Al Khor"],
    industries=["perfume", "fragrance", "skincare", "cosmetics", "jewelry", "accessories", "e-commerce"],
    keywords=["oud", "perfume", "abaya", "skincare", "gold", "online store"],
    size_notes="Small and medium brands; large chains with in-house agencies are lower priority.",
)


def seed_defaults(session: Session) -> None:
    if not session.scalars(select(PipelineStage)).first():
        for pos, (key, name, outcome) in enumerate(DEFAULT_STAGES):
            session.add(PipelineStage(key=key, name=name, position=pos, outcome=outcome))
    if not session.scalars(select(Offer)).first():
        for name, price, desc in DEFAULT_OFFERS:
            session.add(Offer(name=name, price=price, currency="QAR", description=desc))
    if not session.scalars(select(IcpProfile)).first():
        session.add(DEFAULT_ICP)
    session.commit()
