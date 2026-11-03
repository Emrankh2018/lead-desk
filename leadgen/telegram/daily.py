"""The morning job: find new reachable businesses on OpenStreetMap, research the ones with a website, send the list.

Started by /cron/daily (a free scheduler opens it each morning). Runs in a background thread; one run at a time.
"""
import logging
import threading
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from leadgen import normalize
from leadgen.discovery import osm
from leadgen.discovery import service as discovery
from leadgen.models import Business, DiscoveryRun, IcpProfile
from leadgen.offerings import services_of
from leadgen.research.service import research_lead
from leadgen.services import audit

log = logging.getLogger("leadgen.daily")
ACTOR = "daily-job"
NEW_LEADS_PER_DAY = 15
RESEARCH_PER_DAY = 15


def pick_profile(session: Session) -> IcpProfile | None:
    """The active profile to search for: one selling websites/AI assistants first, else the first active one."""
    icps = list(session.scalars(select(IcpProfile).where(IcpProfile.active.is_(True)).order_by(IcpProfile.id)))
    return next((i for i in icps if set(services_of(i)) - {"product_ads"}), icps[0] if icps else None)


def discover(session: Session, app_state: Any, today: date) -> int:
    settings = app_state.settings
    icp = pick_profile(session)
    if not icp:
        return 0
    country = icp.countries[0] if icp.countries else settings.default_country
    city = icp.cities[today.toordinal() % len(icp.cities)] if icp.cities else None  # a different city each day
    types = osm.categories_for(icp.industries, icp.keywords, services_of(icp))
    places, cached = app_state.osm_search(country, types, city, cache_dir=settings.data_dir / "cache")
    candidates, _ = discovery.group_places(places, city)
    discovery.classify(session, candidates, settings.default_country, services_of(icp))
    # Only new businesses that published a way to reach them; chains are weaker fits.
    # classify() sorted them best first, so the first ones are kept.
    wanted = [c.key for c in candidates if c.status == "new" and c.branches <= 2 and (c.phone or c.instagram or c.email)]
    keys = set(wanted[:NEW_LEADS_PER_DAY])
    params = {"country": country, "city": city or "", "types": types, "icp_id": icp.id, "automatic": True}
    run = DiscoveryRun(source="osm", params=params, places_found=len(places), candidates=len(candidates),
                       new_candidates=sum(1 for c in candidates if c.status == "new"), from_cache=cached)
    session.add(run)
    session.flush()
    result = discovery.add_candidates(session, candidates, keys, run=run, country=country, icp_id=icp.id, actor=ACTOR,
                                      default_region=settings.default_country)
    session.commit()
    return result["created"]


def research_new(app_state: Any) -> int:
    with app_state.db.sessionmaker() as s:
        ids = [b.id for b in s.scalars(select(Business).where(Business.researched_at.is_(None), Business.website.is_not(None),
                                                               Business.do_not_contact.is_(False)).order_by(Business.id))
               if not normalize.listing_host(b.website)][:RESEARCH_PER_DAY]
    fetcher = app_state.fetcher_factory()
    done = 0
    try:
        for lead_id in ids:
            with app_state.db.sessionmaker() as s:
                biz = s.get(Business, lead_id)
                try:
                    research_lead(s, biz, actor=ACTOR, fetcher=fetcher)
                    s.commit()
                    done += 1
                except Exception:
                    s.rollback()
                    log.exception("daily research failed for lead %s", lead_id)
    finally:
        fetcher.close()
    return done


class DailyJob:
    def __init__(self, app_state: Any):
        self._state = app_state
        self._lock = threading.Lock()

    def start(self, today: date) -> bool:
        if not self._lock.acquire(blocking=False):
            return False
        threading.Thread(target=self._run, args=(today,), daemon=True, name="daily-job").start()
        return True

    def run_now(self, today: date) -> dict:
        """Run in the caller's thread (tests and CLI)."""
        with self._lock:
            return self._work(today)

    def _run(self, today: date) -> None:
        try:
            self._work(today)
        except Exception:
            log.exception("daily job failed")
        finally:
            self._lock.release()

    def _work(self, today: date) -> dict:
        found, notes = 0, []
        try:
            with self._state.db.sessionmaker() as s:
                found = discover(s, self._state, today)
        except osm.OsmError as exc:
            notes.append(f"OpenStreetMap search failed today ({exc}).")
        researched = research_new(self._state)
        notes.insert(0, f"Found {found} new businesses, researched {researched} websites.")
        bot = self._state.telegram
        sent = bot.send_digest(today, " ".join(notes)) if bot else 0
        with self._state.db.sessionmaker() as s:
            audit.record(s, ACTOR, "daily.run", "bot", None, found=found, researched=researched, offered=sent)
            s.commit()
        return {"found": found, "researched": researched, "offered": sent}
