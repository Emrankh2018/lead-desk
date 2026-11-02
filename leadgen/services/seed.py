"""Default configuration for a fresh database. Everything here is editable in Settings afterwards."""
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from leadgen.models import AppSetting, IcpProfile, Offer, PipelineStage

DEFAULT_STAGES = [
    ("discovered", "Discovered", None), ("qualified", "Qualified", None), ("ready", "Ready for outreach", None),
    ("contacted", "Contacted", None), ("responded", "Responded", None), ("meeting", "Meeting", None),
    ("proposal", "Proposal", None), ("won", "Won", "won"), ("lost", "Lost", "lost"),
]

DEFAULT_OFFERS = [
    ("Paid pilot", Decimal("450"), "One 10-15 s 9:16 promotional reel + 3 product visuals, 1 revision round.", "product_ads"),
    ("Monthly creative package", Decimal("1400"), "4 short promotional reels + 8 marketing images + captions per month.", "product_ads"),
    ("Custom campaign", None, "Individually quoted multi-product or special-format campaigns.", "product_ads"),
]
# Added in seed version 2. Prices are left empty for the owner to fill in Settings; drafts then offer a quote.
SERVICE_OFFERS = [
    ("Business website", None, "Mobile-friendly website in English and Arabic with WhatsApp contact, map and enquiry form.", "website"),
    ("WhatsApp reply bot", None, "Automatic WhatsApp replies for common questions, prices, opening hours and bookings.", "whatsapp_bot"),
    ("Website AI chatbot", None, "AI assistant on the business's website that answers visitors' questions and collects enquiries.", "ai_chatbot"),
]

DEFAULT_ICP = dict(
    name="Qatar product brands (fragrance, beauty, jewelry)",
    description="Product-based businesses in Qatar selling visually appealing products online or on social media, "
                "who could benefit from AI-produced promo videos and product imagery.",
    countries=["QA"], cities=["Doha", "Al Wakrah", "Lusail", "Al Rayyan", "Al Khor"],
    industries=["perfume", "fragrance", "skincare", "cosmetics", "jewelry", "accessories", "e-commerce"],
    keywords=["oud", "perfume", "abaya", "skincare", "gold", "online store"],
    size_notes="Small and medium brands; large chains with in-house agencies are lower priority.",
    services=["product_ads"],
)
DIGITAL_ICP = dict(
    name="Qatar businesses needing websites & AI assistants",
    description="Any local business in Qatar (restaurants, clinics, salons, gyms, shops, car services, offices and more) "
                "that has no website, an outdated one, or answers customers by hand, and could use a website, "
                "a WhatsApp reply bot or a website AI chatbot.",
    countries=["QA"], cities=["Doha", "Al Wakrah", "Lusail", "Al Rayyan", "Al Khor"], industries=[], keywords=[],
    size_notes="Independent and small multi-branch businesses; large chains usually have agencies.",
    services=["website", "whatsapp_bot", "ai_chatbot"],
    # Reaching the owner matters more here: most of these businesses are found as a name and a phone number.
    scoring_weights={"reachable": 20, "location_fit": 15},
)
SEED_VERSION = 2


def seed_defaults(session: Session) -> None:
    if not session.scalars(select(PipelineStage)).first():
        for pos, (key, name, outcome) in enumerate(DEFAULT_STAGES):
            session.add(PipelineStage(key=key, name=name, position=pos, outcome=outcome))
    fresh = not session.scalars(select(IcpProfile)).first() and not session.scalars(select(Offer)).first()
    if fresh:
        for name, price, desc, service in DEFAULT_OFFERS:
            session.add(Offer(name=name, price=price, currency="QAR", description=desc, service=service))
        session.add(IcpProfile(**DEFAULT_ICP))  # a new instance per database; ORM objects can't be shared
    version = session.get(AppSetting, "seed_version")
    if not version or int(version.value.get("version", 1)) < 2:
        # Version 2: websites, WhatsApp bots and AI chatbots for any business. Added once, by name, so an
        # existing database gains them and a profile or offer the owner later deletes is not recreated.
        if not session.scalars(select(IcpProfile).where(IcpProfile.name == DIGITAL_ICP["name"])).first():
            session.add(IcpProfile(**DIGITAL_ICP))
        for name, price, desc, service in SERVICE_OFFERS:
            if not session.scalars(select(Offer).where(Offer.name == name)).first():
                session.add(Offer(name=name, price=price, currency="QAR", description=desc, service=service))
        session.merge(AppSetting(key="seed_version", value={"version": SEED_VERSION}))
    session.commit()
