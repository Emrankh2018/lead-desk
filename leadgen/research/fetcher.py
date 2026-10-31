"""Polite, permission-respecting page fetcher.

Rules: public http(s) only (no private/loopback addresses), robots.txt honoured, identified User-Agent,
per-host pacing, size and redirect limits, and a host is abandoned on 401/403/429/503 instead of retried.
Every outcome is reported as data so the caller can tell the user exactly what could not be verified.
"""
import hashlib
import ipaddress
import socket
import time
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx

USER_AGENT = "LeadDesk/0.2 (personal B2B research; respects robots.txt)"
MAX_BYTES = 3 * 1024 * 1024
MAX_REDIRECTS = 5
TIMEOUT = httpx.Timeout(15.0, connect=10.0)
STOP_STATUSES = {401, 403, 429, 503}


@dataclass
class FetchResult:
    url: str
    ok: bool
    status: int | None = None
    final_url: str | None = None
    content_type: str = ""
    text: str = ""
    issue: str | None = None  # human-readable reason when not ok
    sha256: str | None = None


@dataclass
class Fetcher:
    """One Fetcher per research run; it remembers robots rules and blocked hosts for that run."""
    client: httpx.Client | None = None
    delay_seconds: float = 2.0
    resolve_host: bool = True  # tests disable DNS checks
    _robots: dict[str, RobotFileParser | None] = field(default_factory=dict)
    _last_hit: dict[str, float] = field(default_factory=dict)
    _blocked_hosts: dict[str, str] = field(default_factory=dict)
    requests_made: int = 0

    def __post_init__(self):
        if self.client is None:
            self.client = httpx.Client(headers={"User-Agent": USER_AGENT, "Accept-Language": "en,ar;q=0.8"},
                                       timeout=TIMEOUT, follow_redirects=False)

    def close(self) -> None:
        self.client.close()

    # --- safety checks -----------------------------------------------------------------------------------

    def _public_url_issue(self, url: str) -> str | None:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return "only public http(s) addresses can be researched"
        if not self.resolve_host:
            return None
        try:
            infos = socket.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80))
        except socket.gaierror:
            return "domain does not resolve (site may no longer exist)"
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                return "address points to a private or local network; refused"
        return None

    def _pace(self, host: str) -> None:
        last = self._last_hit.get(host)
        if last is not None:
            wait = self.delay_seconds - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
        self._last_hit[host] = time.monotonic()

    def _raw_get(self, url: str) -> tuple[httpx.Response, bytes, str]:
        """GET with a hard size cap. Returns (response, body bytes, decoded text)."""
        host = urlsplit(url).netloc
        self._pace(host)
        self.requests_made += 1
        with self.client.stream("GET", url) as resp:
            chunks, size = [], 0
            for chunk in resp.iter_bytes():
                size += len(chunk)
                if size > MAX_BYTES:
                    raise ValueError(f"page larger than {MAX_BYTES // 1024 // 1024} MB; skipped")
                chunks.append(chunk)
            body = b"".join(chunks)
            text = body.decode(resp.encoding or "utf-8", errors="replace")
        return resp, body, text

    # --- robots ------------------------------------------------------------------------------------------

    def _robots_for(self, url: str) -> RobotFileParser | None:
        parts = urlsplit(url)
        base = f"{parts.scheme}://{parts.netloc}"
        if base in self._robots:
            return self._robots[base]
        rp: RobotFileParser | None = RobotFileParser()
        try:
            target = base + "/robots.txt"
            for _ in range(MAX_REDIRECTS + 1):  # RFC 9309: follow up to five redirects
                if self._public_url_issue(target):
                    raise ValueError("robots.txt redirects to a non-public address")
                resp, _, text = self._raw_get(target)
                if not (resp.is_redirect and resp.headers.get("location")):
                    break
                target = urljoin(target, resp.headers["location"])
            if resp.status_code in (401, 403):
                rp.disallow_all = True  # per RFC 9309 treat as full disallow
            elif resp.status_code >= 300:
                rp.allow_all = True
            else:
                rp.parse(text.splitlines())
        except (httpx.HTTPError, ValueError):
            rp = None  # unreachable robots.txt: the page fetch will fail and be reported anyway
        self._robots[base] = rp
        return rp

    def allowed(self, url: str) -> bool:
        rp = self._robots_for(url)
        return True if rp is None else rp.can_fetch(USER_AGENT, url)

    # --- public API --------------------------------------------------------------------------------------

    def get(self, url: str) -> FetchResult:
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            host = urlsplit(current).netloc
            if host in self._blocked_hosts:
                return FetchResult(url, False, issue=self._blocked_hosts[host])
            if issue := self._public_url_issue(current):
                return FetchResult(url, False, issue=issue)
            if not self.allowed(current):
                return FetchResult(url, False, issue=f"robots.txt does not allow fetching {urlsplit(current).path or '/'}")
            try:
                resp, body, text = self._raw_get(current)
            except ValueError as exc:
                return FetchResult(url, False, issue=str(exc))
            except httpx.TimeoutException:
                return FetchResult(url, False, issue="site did not respond in time")
            except httpx.HTTPError as exc:
                return FetchResult(url, False, issue=f"could not connect ({type(exc).__name__})")
            if resp.is_redirect and resp.headers.get("location"):
                current = urljoin(current, resp.headers["location"])
                continue
            if resp.status_code in STOP_STATUSES:
                reason = {429: "site rate-limited us (HTTP 429)", 503: "site unavailable or bot-protected (HTTP 503)"}.get(
                    resp.status_code, f"site refused access (HTTP {resp.status_code})")
                self._blocked_hosts[host] = reason + "; not retried"
                return FetchResult(url, False, status=resp.status_code, final_url=current, issue=reason)
            ctype = resp.headers.get("content-type", "")
            if resp.status_code != 200:
                return FetchResult(url, False, status=resp.status_code, final_url=current, issue=f"HTTP {resp.status_code}")
            return FetchResult(url, True, resp.status_code, current, ctype, text, sha256=hashlib.sha256(body).hexdigest())
        return FetchResult(url, False, issue="too many redirects")
