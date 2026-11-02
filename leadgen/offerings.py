"""The services the owner sells. A customer profile picks which of them it looks for; scoring, offers and drafts
follow from that choice. Wording here is shown to the AI model and used in template drafts, so it stays factual."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Service:
    key: str
    label: str
    what: str  # one factual sentence describing the service, used in prompts and templates
    subject: str  # default email subject; {name} is the business name
    ask: str  # closing question for template drafts


SERVICES = {
    "website": Service(
        "website", "Website",
        "I build mobile-friendly business websites in English and Arabic, with WhatsApp contact, maps and booking or enquiry forms.",
        "A website idea for {name}", "Would a short call this week to show you an example be useful?"),
    "whatsapp_bot": Service(
        "whatsapp_bot", "WhatsApp reply bot",
        "I set up WhatsApp reply bots that answer common customer questions, share prices and opening hours, and take "
        "bookings or orders automatically, handing over to staff when needed.",
        "Faster WhatsApp replies for {name}", "Would you like to see how it would answer your most common questions?"),
    "ai_chatbot": Service(
        "ai_chatbot", "Website AI chatbot",
        "I add an AI assistant to business websites that answers visitors' questions from the business's own information "
        "and collects enquiries, day and night.",
        "An AI assistant for the {name} website", "Would you like a quick demo trained on your own website?"),
    "product_ads": Service(
        "product_ads", "AI product ads",
        "I make short cinematic product videos and marketing images using AI-assisted production, without a full video shoot.",
        "A product video idea for {name}", "Would you like to see a short example made in a style that fits your products?"),
}
DIGITAL_SERVICES = ["website", "whatsapp_bot", "ai_chatbot"]
LEGACY_SERVICES = ["product_ads"]  # profiles created before services existed were built for product ads


def services_of(icp) -> list[str]:
    chosen = [s for s in (getattr(icp, "services", None) or []) if s in SERVICES]
    return chosen or list(LEGACY_SERVICES)


def label(key: str | None) -> str:
    return SERVICES[key].label if key in SERVICES else "General"
