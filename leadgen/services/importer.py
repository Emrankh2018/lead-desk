"""CSV import with a preview step.

preview() classifies every row (new / duplicate of an existing lead / duplicate within the file / invalid)
without writing anything; commit() then creates only the rows still classified as new.
"""
import csv
import io
from dataclasses import asdict, dataclass, field

from sqlalchemy.orm import Session

from leadgen.services.dedupe import exact_matches, fuzzy_matches
from leadgen.services.leads import DuplicateLead, LeadError, LeadInput, create_lead

MAX_BYTES = 2 * 1024 * 1024
MAX_ROWS = 5000
COLUMN_ALIASES = {
    "name": ["name", "business", "business name", "company", "company name", "brand", "store"],
    "website": ["website", "url", "site", "web", "domain"],
    "instagram": ["instagram", "ig", "instagram handle", "instagram url", "insta"],
    "linkedin_url": ["linkedin", "linkedin url"],
    "phone": ["phone", "mobile", "whatsapp", "telephone", "tel", "phone number"],
    "email": ["email", "e-mail", "email address"],
    "city": ["city", "area", "town"],
    "country": ["country", "country code"],
    "industry": ["industry", "category", "niche", "type"],
    "notes": ["notes", "note", "comment", "comments"],
}


class ImportError_(ValueError):
    pass


@dataclass
class PreviewRow:
    line: int
    data: dict
    status: str  # new | duplicate | duplicate_in_file | invalid
    message: str = ""
    possible_duplicates: list[str] = field(default_factory=list)


def map_columns(headers: list[str]) -> dict[str, str]:
    """Return {field: original header} for recognised columns."""
    lookup = {alias: f for f, aliases in COLUMN_ALIASES.items() for alias in aliases}
    mapping: dict[str, str] = {}
    for h in headers:
        f = lookup.get(h.strip().lower())
        if f and f not in mapping:
            mapping[f] = h
    return mapping


def parse(content: bytes) -> tuple[dict[str, str], list[dict]]:
    if len(content) > MAX_BYTES:
        raise ImportError_(f"file is larger than {MAX_BYTES // 1024 // 1024} MB")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ImportError_("file must be UTF-8 encoded CSV (in Excel: Save As → CSV UTF-8)") from exc
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    if not reader.fieldnames:
        raise ImportError_("the file has no header row")
    mapping = map_columns(reader.fieldnames)
    if not ({"name", "website", "instagram"} & mapping.keys()):
        raise ImportError_("no name, website or Instagram column found; headers seen: " + ", ".join(reader.fieldnames))
    rows = []
    for i, raw in enumerate(reader):
        if i >= MAX_ROWS:
            raise ImportError_(f"more than {MAX_ROWS} rows; split the file")
        rows.append({f: (raw.get(h) or "").strip() for f, h in mapping.items()})
    return mapping, rows


def preview(session: Session, rows: list[dict], *, icp_id: int | None, default_region: str) -> list[PreviewRow]:
    out: list[PreviewRow] = []
    seen: dict[str, int] = {}
    for line, data in enumerate(rows, start=2):
        if not any(data.values()):
            continue
        lead = LeadInput(**data, icp_id=icp_id)
        try:
            fields = lead.normalise(default_region)
        except LeadError as exc:
            out.append(PreviewRow(line, data, "invalid", str(exc)))
            continue
        keys = [k for k in (fields["domain"], fields["instagram_handle"], fields["phone_e164"]) if k]
        dup_line = next((seen[k] for k in keys if k in seen), None)
        if dup_line:
            out.append(PreviewRow(line, data, "duplicate_in_file", f"same identifier as line {dup_line}"))
            continue
        for k in keys:
            seen[k] = line
        exact = exact_matches(session, domain=fields["domain"], handle=fields["instagram_handle"], phone=fields["phone_e164"])
        if exact:
            out.append(PreviewRow(line, data, "duplicate", "; ".join(f"{m.reason} as #{m.business.id} {m.business.name}" for m in exact)))
            continue
        fuzzy = fuzzy_matches(session, name_key=fields["name_key"], city=fields["city"])
        out.append(PreviewRow(line, data, "new", fields["name"], [f"#{m.business.id} {m.business.name} ({m.reason})" for m in fuzzy[:3]]))
    return out


def commit(session: Session, rows: list[PreviewRow], *, filename: str, icp_id: int | None, actor: str,
           default_region: str) -> dict:
    created, skipped = 0, 0
    for row in rows:
        if row.status != "new":
            skipped += 1
            continue
        data = dict(row.data)
        if data.get("email"):
            data["email_source"] = f"imported from {filename}"
        try:
            create_lead(session, LeadInput(**data, icp_id=icp_id), actor=actor, source="csv",
                        source_ref=f"{filename}:{row.line}", default_region=default_region)
            created += 1
        except (DuplicateLead, LeadError):
            skipped += 1  # changed since preview (e.g. added meanwhile); never overwrite
    return {"created": created, "skipped": skipped}


def rows_to_json(rows: list[PreviewRow]) -> list[dict]:
    return [asdict(r) for r in rows]


def rows_from_json(items: list[dict]) -> list[PreviewRow]:
    return [PreviewRow(**i) for i in items]
