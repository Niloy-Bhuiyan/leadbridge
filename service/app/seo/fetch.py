"""Crawler-facing fetches: the page, robots.txt, and the XML sitemap.

Two constraints shape this module.

The audited site is untrusted infrastructure. It may be slow, may redirect
in a loop, may serve 50MB of HTML, and may not exist. Every fetch is bounded
by a timeout and a response-size cap, and a failure returns a report saying
so rather than raising -- an audit that dies on a missing robots.txt is
useless to the workflow calling it.

The audit identifies itself. A real User-Agent naming the tool is how a site
owner can tell automated traffic apart from an attack in their logs.
"""

import re
import xml.etree.ElementTree as ET
from urllib.parse import urljoin, urlparse

import httpx

from ..config import Settings
from ..models import RobotsReport, SitemapReport

MAX_BYTES = 5 * 1024 * 1024
SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}


def origin_of(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


async def fetch_page(
    client: httpx.AsyncClient, url: str, settings: Settings
) -> tuple[str | None, int | None, str | None]:
    """Return (html, status_code, error). Never raises."""
    try:
        response = await client.get(
            url,
            timeout=settings.seo_fetch_timeout,
            follow_redirects=True,
            headers={"User-Agent": settings.seo_user_agent},
        )
    except httpx.HTTPError as exc:
        return None, None, f"{type(exc).__name__}: {exc}"

    content_type = response.headers.get("content-type", "")
    if "html" not in content_type.casefold():
        return (
            None,
            response.status_code,
            f"content-type is {content_type or 'unset'}, not HTML",
        )
    body = response.content[:MAX_BYTES]
    return body.decode(response.encoding or "utf-8", errors="replace"), (
        response.status_code
    ), None


async def fetch_robots(
    client: httpx.AsyncClient, base_url: str, settings: Settings
) -> RobotsReport:
    url = urljoin(origin_of(base_url) + "/", "robots.txt")
    try:
        response = await client.get(
            url,
            timeout=settings.seo_fetch_timeout,
            follow_redirects=True,
            headers={"User-Agent": settings.seo_user_agent},
        )
    except httpx.HTTPError:
        return RobotsReport(found=False)

    if response.status_code != 200:
        return RobotsReport(found=False, status_code=response.status_code)

    text = response.text[:MAX_BYTES]
    sitemaps = [
        m.group(1).strip()
        for m in re.finditer(r"(?im)^\s*sitemap:\s*(\S+)", text)
    ]

    # Only the `*` group matters here: a Disallow aimed at one named bot is
    # normal configuration, not a site-wide block.
    blocks_all = False
    current_agents: list[str] = []
    for line in text.splitlines():
        stripped = line.split("#", 1)[0].strip()
        if not stripped:
            continue
        if ":" not in stripped:
            continue
        field, _, value = stripped.partition(":")
        field = field.strip().casefold()
        value = value.strip()
        if field == "user-agent":
            current_agents.append(value)
        elif field == "disallow":
            if "*" in current_agents and value == "/":
                blocks_all = True
        elif field == "allow":
            continue
        else:
            current_agents = []

    return RobotsReport(
        found=True,
        status_code=response.status_code,
        sitemaps=sitemaps,
        blocks_all=blocks_all,
    )


async def fetch_sitemap(
    client: httpx.AsyncClient,
    base_url: str,
    settings: Settings,
    declared: list[str] | None = None,
) -> SitemapReport:
    """Try what robots.txt declared, then the conventional location."""
    candidates = list(declared or [])
    fallback = urljoin(origin_of(base_url) + "/", "sitemap.xml")
    if fallback not in candidates:
        candidates.append(fallback)

    for candidate in candidates[:3]:
        try:
            response = await client.get(
                candidate,
                timeout=settings.seo_fetch_timeout,
                follow_redirects=True,
                headers={"User-Agent": settings.seo_user_agent},
            )
        except httpx.HTTPError:
            continue
        if response.status_code != 200:
            continue

        try:
            root = ET.fromstring(response.content[:MAX_BYTES])
        except ET.ParseError:
            return SitemapReport(
                found=True,
                url=candidate,
                status_code=response.status_code,
                url_count=0,
            )

        is_index = root.tag.endswith("sitemapindex")
        locs = root.findall(".//sm:loc", SITEMAP_NS) or root.findall(".//loc")
        return SitemapReport(
            found=True,
            url=candidate,
            status_code=response.status_code,
            url_count=min(len(locs), settings.seo_max_sitemap_urls),
            is_index=is_index,
        )

    return SitemapReport(found=False)
