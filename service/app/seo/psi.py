"""PageSpeed Insights.

Field data (CrUX) is preferred over lab data when the API returns it,
because lab numbers from a single synthetic run are not what the page's
visitors experience. The record says which it used via `strategy`, and when
neither is present it returns available=False with the reason rather than
inventing a number.

The API answers unauthenticated at a low rate limit, so PAGESPEED_API_KEY
is genuinely optional.
"""

from typing import Any

import httpx

from ..config import Settings
from ..models import CoreWebVitals


def _metric(crux: dict[str, Any], key: str) -> float | None:
    entry = crux.get("metrics", {}).get(key)
    if not isinstance(entry, dict):
        return None
    value = entry.get("percentile")
    return float(value) if isinstance(value, (int, float)) else None


def _lab_ms(audits: dict[str, Any], key: str) -> float | None:
    entry = audits.get(key)
    if not isinstance(entry, dict):
        return None
    value = entry.get("numericValue")
    return float(value) if isinstance(value, (int, float)) else None


def parse_psi(body: dict[str, Any], strategy: str) -> CoreWebVitals:
    lighthouse = body.get("lighthouseResult") or {}
    audits = lighthouse.get("audits") or {}
    categories = lighthouse.get("categories") or {}

    performance = categories.get("performance") or {}
    raw_score = performance.get("score")
    score = float(raw_score) * 100 if isinstance(raw_score, (int, float)) else None

    crux = body.get("loadingExperience") or {}
    lcp = _metric(crux, "LARGEST_CONTENTFUL_PAINT_MS")
    inp = _metric(crux, "INTERACTION_TO_NEXT_PAINT")
    cls_raw = _metric(crux, "CUMULATIVE_LAYOUT_SHIFT_SCORE")
    # CrUX reports CLS multiplied by 100 as an integer percentile.
    cls = cls_raw / 100 if cls_raw is not None else None

    source = "field"
    if lcp is None:
        lcp = _lab_ms(audits, "largest-contentful-paint")
        source = "lab"
    if cls is None:
        entry = audits.get("cumulative-layout-shift")
        if isinstance(entry, dict) and isinstance(
            entry.get("numericValue"), (int, float)
        ):
            cls = float(entry["numericValue"])
        source = "lab"

    if lcp is None and cls is None and score is None:
        return CoreWebVitals(
            available=False, reason="PageSpeed response contained no usable metrics"
        )

    return CoreWebVitals(
        available=True,
        performance_score=score,
        lcp_ms=lcp,
        cls=cls,
        inp_ms=inp,
        strategy=f"{strategy}/{source}",
    )


async def fetch_vitals(
    client: httpx.AsyncClient, url: str, settings: Settings, strategy: str = "mobile"
) -> CoreWebVitals:
    params: dict[str, str] = {"url": url, "strategy": strategy}
    if settings.pagespeed_api_key:
        params["key"] = settings.pagespeed_api_key

    try:
        # PSI runs a real Lighthouse pass server-side and is genuinely slow;
        # the shared 10s budget is too tight for it specifically.
        response = await client.get(
            settings.pagespeed_base_url, params=params, timeout=60.0
        )
    except httpx.HTTPError as exc:
        return CoreWebVitals(
            available=False, reason=f"PageSpeed request failed: {type(exc).__name__}"
        )

    if response.status_code == 429:
        return CoreWebVitals(
            available=False,
            reason="PageSpeed rate limit reached (set PAGESPEED_API_KEY to raise it)",
        )
    if response.status_code != 200:
        return CoreWebVitals(
            available=False,
            reason=f"PageSpeed returned HTTP {response.status_code}",
        )

    try:
        body = response.json()
    except ValueError:
        return CoreWebVitals(
            available=False, reason="PageSpeed response was not valid JSON"
        )
    return parse_psi(body, strategy)
