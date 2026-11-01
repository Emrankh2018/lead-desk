"""Research a lead's website, store evidence, and (re)score.

A run fetches at most: robots.txt, the homepage, up to two same-site contact/about pages, and for Shopify
stores the public /products.json catalogue. New evidence replaces the previous run's evidence as "current";
older evidence is kept for history. Contact details found are suggested on the lead only when its field is empty.
"""
import logging
import threading
from collections.abc import Callable

from sqlalchemy import select, update
from sqlalchemy.orm import Session, object_session

from leadgen import normalize
from leadgen.models import Business, Evidence, ResearchRun, utcnow
from leadgen.research import extract, scoring
from leadgen.research.fetcher import Fetcher
from leadgen.services import audit
from leadgen.services.dedupe import exact_matches

log = logging.getLogger("leadgen.research")
MAX_EXTRA_PAGES = 2


def research_lead(session: Session, biz: Business, *, actor: str, fetcher: Fetcher | None = None) -> ResearchRun:
    run = ResearchRun(business_id=biz.id)
    session.add(run)
    session.flush()
    issues: list[str] = []
    signals: list[tuple[extract.Signal, str, str | None]] = []  # (signal, source url, content hash)
    own_fetcher = fetcher is None
    fetcher = fetcher or Fetcher()
    try:
        if not biz.website:
            run.status = "skipped"
            issues.append("No website on record; only Instagram/LinkedIn/manual information can be used. "
                          "Website research needs a website URL.")
        else:
            home = fetcher.get(biz.website)
            if not home.ok:
                issues.append(f"Homepage {biz.website}: {home.issue}")
            elif "html" not in home.content_type and home.content_type:
                issues.append(f"Homepage returned {home.content_type}, not a web page")
            else:
                run.pages_fetched += 1
                if r := extract.redirect_signal(biz.website, home.final_url or biz.website):
                    signals.append((r, home.final_url, None))
                page_signals, extra = extract.extract_page(home.text, home.final_url, site_domain=biz.domain)
                signals += [(s, home.final_url, home.sha256) for s in page_signals]
                for url in extra[:MAX_EXTRA_PAGES]:
                    page = fetcher.get(url)
                    if not page.ok:
                        issues.append(f"{url}: {page.issue}")
                        continue
                    run.pages_fetched += 1
                    more, _ = extract.extract_page(page.text, page.final_url, site_domain=biz.domain)
                    # From secondary pages keep only contact and social facts; the homepage describes the business.
                    signals += [(s, page.final_url, page.sha256) for s in more
                                if s.kind in ("published_email", "published_phone", "social_link", "qatar_mentions")]
                if any(s.kind == "platform" and "Shopify" in s.value.get("platforms", []) for s, _, _ in signals):
                    base = (home.final_url or biz.website).split("/", 3)[:3]
                    catalog_url = "/".join(base) + "/products.json?limit=250"
                    cat = fetcher.get(catalog_url)
                    count = extract.shopify_product_count(cat.text) if cat.ok else None
                    if count is not None:
                        run.pages_fetched += 1
                        label = f"{count}{'+' if count == 250 else ''}"
                        signals.append((extract.Signal("product_count", f"Public Shopify catalogue lists {label} products",
                                                       {"count": count}), catalog_url, cat.sha256))
                    elif not cat.ok:
                        issues.append(f"Product catalogue: {cat.issue}")
            if run.pages_fetched == 0:
                run.status = "failed"
            else:
                run.status = "partial" if issues else "ok"
    except Exception as exc:  # never leave a run "running"; record what happened
        log.exception("research failed for business %s", biz.id)
        run.status = "failed"
        issues.append(f"Unexpected error: {type(exc).__name__}")
    finally:
        if own_fetcher:
            fetcher.close()

    if run.pages_fetched:
        session.execute(update(Evidence).where(Evidence.business_id == biz.id, Evidence.created_by == "research",
                                               Evidence.is_current.is_(True)).values(is_current=False))
        seen = set()
        for sig, url, digest in _dedupe(signals):
            key = (sig.kind, sig.text)
            if key in seen:
                continue
            seen.add(key)
            session.add(Evidence(business_id=biz.id, run_id=run.id, kind=sig.kind, value=sig.value, text=sig.text,
                                 verified=sig.verified, source_url=url, snippet=sig.snippet, content_hash=digest))
        for issue in issues:
            session.add(Evidence(business_id=biz.id, run_id=run.id, kind="fetch_issue", value={}, text=issue, verified=False))
        biz.researched_at = utcnow()
        _suggest_contacts(biz, signals)
    run.issues = issues
    run.finished_at = utcnow()
    session.flush()
    session.expire(biz, ["evidence"])
    rescore(session, biz)
    audit.record(session, actor, "research.run", "business", biz.id, run_id=run.id, status=run.status,
                 pages=run.pages_fetched, requests=fetcher.requests_made, issues=len(issues))
    return run


def _dedupe(signals):
    """Keep one video/image count per page type: the homepage's (first) values win."""
    once = {"video_presence", "image_count", "page_title", "meta_description", "language", "platform"}
    seen_once = set()
    for sig, url, digest in signals:
        if sig.kind in once:
            if sig.kind in seen_once:
                continue
            seen_once.add(sig.kind)
        yield sig, url, digest


def _suggest_contacts(biz: Business, signals) -> None:
    """Fill empty contact fields with details the site itself published (never overwrite user data)."""
    if not biz.email_public:
        site_emails = [(s.value["email"], url) for s, url, _ in signals if s.kind == "published_email"]
        # Prefer an address on the business's own domain.
        site_emails.sort(key=lambda e: 0 if biz.domain and e[0].endswith("@" + biz.domain) else 1)
        if site_emails:
            biz.email_public, biz.email_source_url = site_emails[0]
    if not biz.phone_e164:
        raw_phones = [s.value["phone"] for s, _, _ in signals if s.kind == "published_phone"]
        raw_phones += [s.value["url"].rstrip("/").rsplit("/", 1)[-1] for s, _, _ in signals
                       if s.kind == "social_link" and s.value.get("network") == "whatsapp" and "wa.me/" in s.value["url"]]
        for raw in raw_phones:
            phone = normalize.phone_e164(raw, biz.country or "QA")
            if phone and not exact_matches(object_session(biz), domain=None, handle=None, phone=phone, exclude_id=biz.id):
                biz.phone_e164 = phone
                break
    if not biz.instagram_handle:
        handles = [s.value["handle"] for s, _, _ in signals if s.kind == "social_link" and s.value.get("handle")]
        # Skip a handle that already belongs to another lead (a likely duplicate the user should review).
        if handles and not exact_matches(object_session(biz), domain=None, handle=handles[0], phone=None, exclude_id=biz.id):
            biz.instagram_handle = handles[0]


def current_evidence(session: Session, biz: Business) -> list[scoring.Ev]:
    rows = session.scalars(select(Evidence).where(Evidence.business_id == biz.id, Evidence.is_current.is_(True)))
    return [scoring.Ev(e.id, e.kind, e.value or {}, e.verified, e.text) for e in rows]


def rescore(session: Session, biz: Business) -> None:
    value, coverage, rows = scoring.score(biz, current_evidence(session, biz), biz.icp)
    biz.score, biz.score_coverage, biz.score_breakdown, biz.scored_at = value, coverage, rows, utcnow()


def rescore_all(session: Session) -> int:
    n = 0
    for biz in session.scalars(select(Business)):
        rescore(session, biz)
        n += 1
    return n


def add_manual_evidence(session: Session, biz: Business, *, text: str, source_url: str, actor: str) -> Evidence:
    text = text.strip()
    if not text:
        raise ValueError("observation text is required")
    url = None
    if source_url.strip():
        url = normalize.http_url(source_url)  # rendered as a link, so only http(s) is allowed
        if not url:
            raise ValueError("source must be a web address (http or https)")
    ev = Evidence(business_id=biz.id, kind="manual_observation", value={}, text=text, verified=True,
                  source_url=url, created_by="user")
    session.add(ev)
    audit.record(session, actor, "evidence.manual", "business", biz.id)
    return ev


class BatchRunner:
    """Runs research for many leads one at a time in a background thread; only one batch at a time."""

    def __init__(self, sessionmaker: Callable[[], Session], fetcher_factory: Callable[[], Fetcher] = Fetcher):
        self._sessionmaker = sessionmaker
        self._fetcher_factory = fetcher_factory
        self._lock = threading.Lock()
        self.state = {"running": False, "done": 0, "total": 0, "current": None, "last_error": None}

    def start(self, lead_ids: list[int], actor: str) -> bool:
        if not self._lock.acquire(blocking=False):
            return False
        self.state = {"running": True, "done": 0, "total": len(lead_ids), "current": None, "last_error": None}
        threading.Thread(target=self._run, args=(lead_ids, actor), daemon=True).start()
        return True

    def _run(self, lead_ids: list[int], actor: str) -> None:
        fetcher = self._fetcher_factory()
        try:
            for lead_id in lead_ids:
                with self._sessionmaker() as s:
                    biz = s.get(Business, lead_id)
                    if biz is None:
                        continue
                    self.state["current"] = biz.name
                    try:
                        research_lead(s, biz, actor=actor, fetcher=fetcher)
                        s.commit()
                    except Exception as exc:
                        s.rollback()
                        log.exception("batch research failed for %s", lead_id)
                        self.state["last_error"] = f"{biz.name}: {type(exc).__name__}"
                self.state["done"] += 1
        finally:
            fetcher.close()
            self.state.update(running=False, current=None)
            self._lock.release()
