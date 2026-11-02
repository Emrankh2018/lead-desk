"""Deterministic checks applied to every model output before the user sees it.

Insights: items without valid evidence ids, or with numbers that do not appear in the cited evidence, are dropped.
Drafts: problems are flagged as warnings for the user to fix (the user edits and approves every message).
"""
import re

NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
URL_RE = re.compile(r"(?:https?://|www\.)[^\s)>\]]+|\b[a-z0-9-]+\.(?:com|qa|net|org|co|io|shop|store|ae)(?:/[^\s)]*)?\b", re.I)
PHONE_RE = re.compile(r"\+?\d[\d\s-]{7,}\d")
PRICE_RE = re.compile(r"(?:QAR|QR|ر\.ق)\s*[-–]?\s*([\d,]+(?:\.\d+)?)|([\d,]+(?:\.\d+)?)\s*(?:QAR|QR|ر\.ق)", re.I)
GARBLED_PRICE_RE = re.compile(r"(?:QAR|QR)\s*[-–]\s*\d", re.I)
PLACEHOLDER_RE = re.compile(r"\[[A-Za-z _]{2,30}\]|\{[A-Za-z_ ]{2,30}\}|<[A-Za-z _]{2,30}>")
OVERCLAIMS = ["guarantee", "guaranteed", "increase your sales", "double your sales", "boost your sales by",
              "% more sales", "#1 ", "number one", "best in qatar", "100% results", "viral"]
CHANNEL_LIMITS = {"whatsapp": 1000, "instagram": 1000, "linkedin": 1300, "email": 1800, "followup": 900}
LINKEDIN_NOTE_LIMIT = 300
# Claims about money or buying intent are exactly what injected page text tries to plant; they are kept only when
# the software-written evidence itself contains the word.
INTENT_CLAIMS = ["budget", "urgent", "urgently", "ready to buy", "looking for an agency", "looking for a video",
                 "wants to hire", "needs video ads"]
MAX_HYPOTHESES = 5
MAX_ITEM_CHARS = 320


def _numbers(text: str) -> set[str]:
    return {n.replace(",", "").rstrip(".") for n in NUMBER_RE.findall(text)}


def validate_insight(data: dict, evidence: dict[int, str]) -> tuple[dict, list[dict]]:
    """Return (clean content, dropped items with reasons). `evidence` maps id -> text the model was shown."""
    kept, dropped = [], []
    for item in (data.get("hypotheses") or [])[:MAX_HYPOTHESES * 2]:
        text = str(item.get("text", "")).strip()[:MAX_ITEM_CHARS]
        ids = [i for i in item.get("evidence_ids") or [] if isinstance(i, int)]
        if not text:
            continue
        if not ids or any(i not in evidence for i in ids):
            dropped.append({"text": text, "reason": "cites no evidence or evidence that does not exist"})
            continue
        cited = " ".join(evidence[i] for i in ids)
        stray = _numbers(text) - _numbers(cited)
        if stray:
            dropped.append({"text": text, "reason": f"number(s) {', '.join(sorted(stray))} not found in the cited evidence"})
            continue
        claims = [c for c in INTENT_CLAIMS if c in text.lower() and c not in cited.lower()]
        if claims:
            dropped.append({"text": text, "reason": f"claims budget or buying intent (“{claims[0]}”) not shown by the evidence"})
            continue
        if len(kept) < MAX_HYPOTHESES:
            kept.append({"text": text, "evidence_ids": sorted(set(ids))})
    angle = str(data.get("angle", "")).strip()[:MAX_ITEM_CHARS]
    if angle.lower().replace(" ", "_") in ("insufficient_evidence", "none", "n/a"):
        angle = ""  # the model sometimes writes the flag into the angle field
    all_text = " ".join(evidence.values()).lower()
    if angle and _numbers(angle) - _numbers(all_text):
        dropped.append({"text": angle, "reason": "suggested angle contains numbers not in the evidence"})
        angle = ""
    elif angle and any(c in angle.lower() and c not in all_text for c in INTENT_CLAIMS):
        dropped.append({"text": angle, "reason": "suggested angle claims budget or buying intent not shown by the evidence"})
        angle = ""
    # The model's own flag is unreliable (eval: flagged insufficient while giving 5 grounded ideas). Trust what
    # survived the checks: none or one grounded idea is "thin"; two or more is enough to show.
    insufficient = not kept or (bool(data.get("insufficient_evidence")) and len(kept) < 2)
    return {"hypotheses": kept, "angle": angle, "insufficient": insufficient}, dropped


def check_draft(channel: str, subject: str, body: str, *, business_name: str, allowed_contacts: set[str],
                allowed_prices: set[str]) -> list[str]:
    """Return warnings. `allowed_contacts` holds lower-case emails/URLs/phones the message may contain."""
    warnings = []
    text = f"{subject}\n{body}"
    low = text.lower()
    limit = CHANNEL_LIMITS.get(channel, 1500)
    if len(body) > limit:
        warnings.append(f"Message is {len(body)} characters; keep {channel} messages under {limit}.")
    if channel == "linkedin" and len(body) > LINKEDIN_NOTE_LIMIT:
        warnings.append(f"Longer than {LINKEDIN_NOTE_LIMIT} characters: too long for a LinkedIn connection-request note "
                        "(fine as a message to an existing connection).")
    if not body.strip():
        warnings.append("Message is empty.")
    normalised_allowed = {a.lower().rstrip("/").removeprefix("https://").removeprefix("http://").removeprefix("www.")
                          for a in allowed_contacts}
    for found in EMAIL_RE.findall(text) + URL_RE.findall(EMAIL_RE.sub(" ", text)):
        f = found.lower().rstrip("/.,").removeprefix("https://").removeprefix("http://").removeprefix("www.")
        if "@" in f and f.split("@")[-1] in {"example.com"}:
            warnings.append(f"Contains a placeholder address: {found}")
        elif not any(f == a or f.startswith(a) or a.startswith(f) for a in normalised_allowed):
            warnings.append(f"Contains a contact detail or link that is not in your profile or the lead's record: {found}")
    allowed_digits = {re.sub(r"\D", "", a) for a in allowed_contacts if re.sub(r"\D", "", a)}
    for phone in PHONE_RE.findall(text):
        if re.sub(r"\D", "", phone) not in allowed_digits:
            warnings.append(f"Contains a phone number not in your profile: {phone.strip()}")
    for m in PRICE_RE.finditer(text):
        price = (m.group(1) or m.group(2) or "").replace(",", "").rstrip(".")
        if price and price not in allowed_prices and price.split(".")[0] not in allowed_prices:
            warnings.append(f"Mentions price {m.group(0).strip()}, which is not one of your offer prices.")
    if GARBLED_PRICE_RE.search(text):
        warnings.append(f"Price looks garbled: “{GARBLED_PRICE_RE.search(text).group(0)}…”")
    for claim in OVERCLAIMS:
        if claim in low:
            warnings.append(f"Avoid promising results (“{claim.strip()}”).")
    if PLACEHOLDER_RE.search(text):
        warnings.append(f"Fill in the placeholder: {PLACEHOLDER_RE.search(text).group(0)}")
    first_word = business_name.split()[0].lower() if business_name.split() else ""
    if first_word and first_word not in low and not business_name.startswith("@"):
        warnings.append("Does not mention the business by name; it may read as generic.")
    return warnings
