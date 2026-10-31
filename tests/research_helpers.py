"""Offline HTTP for research tests: a URL → (status, headers, body) map served through httpx.MockTransport."""
from pathlib import Path

import httpx

from leadgen.research.fetcher import USER_AGENT, Fetcher

SITES = Path(__file__).parent / "fixtures" / "sites"
HTML = {"content-type": "text/html; charset=utf-8"}


def site(name: str) -> str:
    return (SITES / name).read_text(encoding="utf-8")


def oudnoir_routes() -> dict:
    return {
        "https://oudnoir.qa/robots.txt": (200, {}, "User-agent: *\nDisallow: /checkout\nDisallow: /pages/about-us\n"),
        "https://oudnoir.qa/": (200, HTML, site("oudnoir_home.html")),
        "https://oudnoir.qa/pages/contact": (200, HTML, site("oudnoir_contact.html")),
        "https://oudnoir.qa/products.json?limit=250": (200, {"content-type": "application/json"},
                                                       '{"products": [{"id": 1}, {"id": 2}, {"id": 3}, {"id": 4}]}'),
    }


def make_fetcher(routes: dict, log: list | None = None) -> Fetcher:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.url.raw_path in (b"", b"/") and not url.endswith("/"):
            url += "/"  # a server treats https://host and https://host/ the same
        if log is not None:
            log.append(url)
        assert request.headers["user-agent"] == USER_AGENT
        status, headers, body = routes.get(url, (404, {}, "not found"))
        return httpx.Response(status, headers=headers, text=body)

    client = httpx.Client(transport=httpx.MockTransport(handler), headers={"User-Agent": USER_AGENT}, follow_redirects=False)
    return Fetcher(client=client, delay_seconds=0, resolve_host=False)
