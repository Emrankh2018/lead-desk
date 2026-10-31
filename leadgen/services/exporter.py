import csv
import io

from openpyxl import Workbook

from leadgen.models import Business

COLUMNS = [
    ("id", lambda b: b.id), ("name", lambda b: b.name), ("stage", lambda b: b.stage.name),
    ("website", lambda b: b.website), ("instagram", lambda b: f"@{b.instagram_handle}" if b.instagram_handle else ""),
    ("linkedin", lambda b: b.linkedin_url), ("phone", lambda b: b.phone_e164), ("public_email", lambda b: b.email_public),
    ("email_source", lambda b: b.email_source_url), ("city", lambda b: b.city), ("country", lambda b: b.country),
    ("industry", lambda b: b.industry), ("icp", lambda b: b.icp.name if b.icp else ""), ("score", lambda b: b.score),
    ("deal_value", lambda b: b.deal_value), ("deal_currency", lambda b: b.deal_currency),
    ("do_not_contact", lambda b: "yes" if b.do_not_contact else "no"), ("follow_up_on", lambda b: b.follow_up_on),
    ("last_contacted_at", lambda b: b.last_contacted_at), ("source", lambda b: b.source),
    ("source_ref", lambda b: b.source_ref), ("created_at", lambda b: b.created_at),
]


def _safe(value) -> str:
    """Stringify and neutralise spreadsheet formula injection from scraped or imported text."""
    if value is None:
        return ""
    text = str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def to_csv(leads: list[Business]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([c for c, _ in COLUMNS])
    for b in leads:
        writer.writerow([_safe(get(b)) for _, get in COLUMNS])
    return buf.getvalue().encode("utf-8-sig")  # BOM so Excel reads Arabic text correctly


def to_xlsx(leads: list[Business]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Leads"
    ws.append([c for c, _ in COLUMNS])
    for b in leads:
        ws.append([_safe(get(b)) for _, get in COLUMNS])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
