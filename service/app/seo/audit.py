"""Audit orchestration.

Fetches run concurrently because they are independent and PSI is slow; a
sequential audit spends most of its wall clock waiting. Each leg degrades on
its own, so a site with no robots.txt still gets a full on-page report.
"""

import asyncio

import httpx

from ..config import Settings
from ..models import CoreWebVitals, SEOAudit, SEOAuditRequest, SEOFinding
from . import fetch, psi, rules
from .onpage import parse_onpage


async def run_audit(
    client: httpx.AsyncClient, request: SEOAuditRequest, settings: Settings
) -> SEOAudit:
    page_task = fetch.fetch_page(client, request.url, settings)
    robots_task = fetch.fetch_robots(client, request.url, settings)

    if request.include_psi:
        vitals_task = psi.fetch_vitals(client, request.url, settings, request.strategy)
    else:

        async def _skipped() -> CoreWebVitals:
            return CoreWebVitals(
                available=False, reason="PageSpeed check disabled by request"
            )

        vitals_task = _skipped()

    (html, status, page_error), robots, vitals = await asyncio.gather(
        page_task, robots_task, vitals_task
    )

    # Sitemap depends on robots, so it cannot join the gather above.
    sitemap = await fetch.fetch_sitemap(
        client, request.url, settings, declared=robots.sitemaps
    )

    onpage = None
    if html is not None and status is not None:
        onpage = parse_onpage(request.url, status, html)

    findings, score = rules.evaluate(onpage, robots, sitemap, vitals)

    if page_error:
        findings.insert(
            0,
            SEOFinding(
                code="page.fetch_failed",
                severity="critical",
                message="The page could not be read.",
                evidence=page_error,
            ),
        )

    degraded = onpage is None or not vitals.available
    return SEOAudit(
        url=request.url,
        score=score,
        onpage=onpage,
        robots=robots,
        sitemap=sitemap,
        vitals=vitals,
        findings=findings,
        degraded=degraded,
    )


def findings_html(audit: SEOAudit, limit: int = 12) -> str:
    """Render findings for a CRM note body."""
    if not audit.findings:
        return "<p>No findings.</p>"
    items = "".join(
        f"<li><b>{f.severity.upper()}</b> &mdash; {f.message}</li>"
        for f in audit.findings[:limit]
    )
    more = ""
    if len(audit.findings) > limit:
        more = f"<p>and {len(audit.findings) - limit} further findings.</p>"
    return f"<ul>{items}</ul>{more}"
