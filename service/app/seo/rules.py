"""The findings engine.

Pure. Every rule is a function of the parsed evidence, so the audit is
reproducible and a disagreement about a finding is a disagreement about a
threshold rather than about what the crawler saw.

Thresholds are the widely published Google guidance figures, and the source
of each is named in the rule. Where guidance is a range rather than a limit,
the finding is `warning`, not `critical` -- an audit that shouts about
everything gets ignored.
"""

from ..models import CoreWebVitals, OnPage, RobotsReport, SEOFinding, SitemapReport

TITLE_MIN, TITLE_MAX = 30, 60
DESC_MIN, DESC_MAX = 70, 160
THIN_CONTENT_WORDS = 300

# Core Web Vitals "good" thresholds.
LCP_GOOD_MS = 2500.0
CLS_GOOD = 0.1
INP_GOOD_MS = 200.0

SEVERITY_COST = {"critical": 15, "warning": 7, "info": 0}


def _title_findings(page: OnPage) -> list[SEOFinding]:
    if not page.title:
        return [
            SEOFinding(
                code="title.missing",
                severity="critical",
                message="Page has no <title>. Search results will fall back to "
                "whatever text Google chooses.",
            )
        ]
    out: list[SEOFinding] = []
    if page.title_length < TITLE_MIN:
        out.append(
            SEOFinding(
                code="title.short",
                severity="warning",
                message=f"Title is {page.title_length} characters; under "
                f"{TITLE_MIN} usually leaves ranking intent unstated.",
                evidence=page.title,
            )
        )
    elif page.title_length > TITLE_MAX:
        out.append(
            SEOFinding(
                code="title.long",
                severity="warning",
                message=f"Title is {page.title_length} characters and will be "
                f"truncated in most result layouts beyond ~{TITLE_MAX}.",
                evidence=page.title,
            )
        )
    return out


def _description_findings(page: OnPage) -> list[SEOFinding]:
    if not page.meta_description:
        return [
            SEOFinding(
                code="description.missing",
                severity="warning",
                message="No meta description. Google will synthesise a snippet, "
                "which forfeits control of the click-through copy.",
            )
        ]
    length = page.meta_description_length
    if length < DESC_MIN:
        return [
            SEOFinding(
                code="description.short",
                severity="info",
                message=f"Meta description is {length} characters; under "
                f"{DESC_MIN} wastes available snippet space.",
                evidence=page.meta_description,
            )
        ]
    if length > DESC_MAX:
        return [
            SEOFinding(
                code="description.long",
                severity="info",
                message=f"Meta description is {length} characters and will be "
                f"truncated beyond ~{DESC_MAX}.",
                evidence=page.meta_description,
            )
        ]
    return []


def _indexability_findings(page: OnPage, robots: RobotsReport) -> list[SEOFinding]:
    out: list[SEOFinding] = []
    directive = (page.robots_meta or "").casefold()
    if "noindex" in directive:
        out.append(
            SEOFinding(
                code="indexability.noindex",
                severity="critical",
                message="Page carries a noindex robots directive and will not "
                "appear in search results.",
                evidence=page.robots_meta,
            )
        )
    if "nofollow" in directive:
        out.append(
            SEOFinding(
                code="indexability.nofollow",
                severity="warning",
                message="Page carries a nofollow directive; outbound link equity "
                "will not pass.",
                evidence=page.robots_meta,
            )
        )
    if robots.blocks_all:
        out.append(
            SEOFinding(
                code="robots.disallow_all",
                severity="critical",
                message="robots.txt disallows all crawling for user-agent *.",
            )
        )
    if not page.canonical:
        out.append(
            SEOFinding(
                code="canonical.missing",
                severity="warning",
                message="No canonical link. Parameter and duplicate URLs may "
                "compete with each other.",
            )
        )
    return out


def _structure_findings(page: OnPage) -> list[SEOFinding]:
    out: list[SEOFinding] = []
    if not page.h1:
        out.append(
            SEOFinding(
                code="h1.missing",
                severity="warning",
                message="Page has no H1 heading.",
            )
        )
    elif len(page.h1) > 1:
        out.append(
            SEOFinding(
                code="h1.multiple",
                severity="info",
                message=f"Page has {len(page.h1)} H1 headings; one primary "
                "heading states the topic more clearly.",
                evidence=" | ".join(page.h1[:3]),
            )
        )
    if not page.jsonld_types:
        out.append(
            SEOFinding(
                code="structured_data.missing",
                severity="warning",
                message="No parseable JSON-LD structured data, so the page is "
                "not eligible for rich results.",
            )
        )
    if not page.html_lang:
        out.append(
            SEOFinding(
                code="lang.missing",
                severity="info",
                message="<html> has no lang attribute.",
            )
        )
    if page.images_total and page.images_missing_alt:
        share = page.images_missing_alt / page.images_total
        out.append(
            SEOFinding(
                code="images.missing_alt",
                severity="warning" if share > 0.5 else "info",
                message=f"{page.images_missing_alt} of {page.images_total} images "
                "have no alt text.",
            )
        )
    if page.word_count < THIN_CONTENT_WORDS:
        out.append(
            SEOFinding(
                code="content.thin",
                severity="warning",
                message=f"Only {page.word_count} words of body copy; under "
                f"{THIN_CONTENT_WORDS} commonly reads as a thin page.",
            )
        )
    if not page.og_tags:
        out.append(
            SEOFinding(
                code="opengraph.missing",
                severity="info",
                message="No Open Graph tags, so shared links render without a "
                "controlled title, description or image.",
            )
        )
    return out


def _discovery_findings(
    robots: RobotsReport, sitemap: SitemapReport
) -> list[SEOFinding]:
    out: list[SEOFinding] = []
    if not robots.found:
        out.append(
            SEOFinding(
                code="robots.missing",
                severity="info",
                message="No robots.txt was served.",
            )
        )
    elif not robots.sitemaps:
        out.append(
            SEOFinding(
                code="robots.no_sitemap_directive",
                severity="info",
                message="robots.txt does not declare a Sitemap: directive.",
            )
        )
    if not sitemap.found:
        out.append(
            SEOFinding(
                code="sitemap.missing",
                severity="warning",
                message="No XML sitemap was found at the declared location or "
                "at /sitemap.xml.",
            )
        )
    elif sitemap.url_count == 0:
        out.append(
            SEOFinding(
                code="sitemap.empty",
                severity="warning",
                message="Sitemap was served but contains no URLs.",
                evidence=sitemap.url,
            )
        )
    return out


def _vitals_findings(vitals: CoreWebVitals) -> list[SEOFinding]:
    if not vitals.available:
        return [
            SEOFinding(
                code="vitals.unavailable",
                severity="info",
                message="Core Web Vitals were not measured.",
                evidence=vitals.reason,
            )
        ]
    out: list[SEOFinding] = []
    if vitals.lcp_ms is not None and vitals.lcp_ms > LCP_GOOD_MS:
        out.append(
            SEOFinding(
                code="vitals.lcp",
                severity="warning",
                message=f"Largest Contentful Paint is {vitals.lcp_ms:.0f}ms, "
                f"above the {LCP_GOOD_MS:.0f}ms good threshold.",
            )
        )
    if vitals.cls is not None and vitals.cls > CLS_GOOD:
        out.append(
            SEOFinding(
                code="vitals.cls",
                severity="warning",
                message=f"Cumulative Layout Shift is {vitals.cls:.3f}, above the "
                f"{CLS_GOOD} good threshold.",
            )
        )
    if vitals.inp_ms is not None and vitals.inp_ms > INP_GOOD_MS:
        out.append(
            SEOFinding(
                code="vitals.inp",
                severity="warning",
                message=f"Interaction to Next Paint is {vitals.inp_ms:.0f}ms, "
                f"above the {INP_GOOD_MS:.0f}ms good threshold.",
            )
        )
    return out


def evaluate(
    page: OnPage | None,
    robots: RobotsReport,
    sitemap: SitemapReport,
    vitals: CoreWebVitals,
) -> tuple[list[SEOFinding], int]:
    """Return (findings, score).

    Score is a deduction model, not a model of search ranking. It says how
    many known problems this page has, which is the only thing an audit can
    honestly claim. The README repeats that so nobody quotes it as a
    ranking prediction.
    """
    findings: list[SEOFinding] = []

    if page is None:
        findings.append(
            SEOFinding(
                code="page.unreachable",
                severity="critical",
                message="The page could not be fetched, so on-page checks did "
                "not run.",
            )
        )
    else:
        if page.status_code >= 400:
            findings.append(
                SEOFinding(
                    code="page.error_status",
                    severity="critical",
                    message=f"Page returned HTTP {page.status_code}.",
                )
            )
        findings.extend(_title_findings(page))
        findings.extend(_description_findings(page))
        findings.extend(_indexability_findings(page, robots))
        findings.extend(_structure_findings(page))

    findings.extend(_discovery_findings(robots, sitemap))
    findings.extend(_vitals_findings(vitals))

    deductions = sum(SEVERITY_COST[f.severity] for f in findings)
    score = max(0, min(100, 100 - deductions))

    order = {"critical": 0, "warning": 1, "info": 2}
    findings.sort(key=lambda f: (order[f.severity], f.code))
    return findings, score
