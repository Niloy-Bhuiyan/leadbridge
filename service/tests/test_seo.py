import httpx
import pytest

from app.config import Settings
from app.models import CoreWebVitals, RobotsReport, SitemapReport
from app.seo import fetch, psi, rules
from app.seo.onpage import parse_onpage

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


GOOD_HTML = """
<!doctype html>
<html lang="en">
<head>
  <title>AI Automation and CRM Integration Services for Agencies</title>
  <meta name="description" content="We build workflow automation, CRM
    integrations and technical SEO audits for growing agencies and their
    clients across three continents.">
  <link rel="canonical" href="/services">
  <meta property="og:title" content="AI Automation">
  <script type="application/ld+json">
    {"@context":"https://schema.org","@type":"Organization","name":"Octopi"}
  </script>
</head>
<body>
  <h1>AI automation that ships</h1>
  <img src="a.png" alt="A diagram">
  <p>WORDS</p>
</body>
</html>
"""

BAD_HTML = """
<!doctype html>
<html>
<head>
  <meta name="robots" content="noindex, nofollow">
  <script type="application/ld+json">{ this is not json }</script>
</head>
<body>
  <h1>One</h1><h1>Two</h1>
  <img src="a.png"><img src="b.png" alt="">
  <p>Thin.</p>
</body>
</html>
"""


class TestOnPageParsing:
    def test_extracts_the_good_page(self):
        page = parse_onpage("https://x.test/services", 200, GOOD_HTML)
        assert page.title.startswith("AI Automation")
        assert page.meta_description_length > 70
        assert page.canonical == "https://x.test/services"
        assert page.h1 == ["AI automation that ships"]
        assert page.jsonld_types == ["Organization"]
        assert page.og_tags["og:title"] == "AI Automation"
        assert page.html_lang == "en"
        assert page.images_total == 1
        assert page.images_missing_alt == 0

    def test_relative_canonical_is_resolved_against_the_page_url(self):
        page = parse_onpage("https://x.test/a/b", 200, GOOD_HTML)
        assert page.canonical == "https://x.test/services"

    def test_reports_the_bad_page_without_crashing(self):
        page = parse_onpage("https://x.test/", 200, BAD_HTML)
        assert page.title is None
        assert page.meta_description is None
        assert page.canonical is None
        assert page.robots_meta == "noindex, nofollow"
        assert len(page.h1) == 2
        assert page.html_lang is None
        assert page.images_total == 2
        assert page.images_missing_alt == 2

    def test_malformed_jsonld_is_skipped_not_fatal(self):
        page = parse_onpage("https://x.test/", 200, BAD_HTML)
        assert page.jsonld_types == []

    def test_script_and_style_are_excluded_from_the_word_count(self):
        html = (
            "<html><body><p>one two three</p>"
            "<script>var a = 'four five six seven eight';</script>"
            "<style>.x{content:'nine ten'}</style></body></html>"
        )
        assert parse_onpage("https://x.test/", 200, html).word_count == 3

    def test_nested_jsonld_graph_types_are_collected(self):
        html = (
            '<html><head><script type="application/ld+json">'
            '{"@graph":[{"@type":"WebSite"},{"@type":["Article","BlogPosting"]}]}'
            "</script></head><body></body></html>"
        )
        assert parse_onpage("https://x.test/", 200, html).jsonld_types == [
            "Article",
            "BlogPosting",
            "WebSite",
        ]


class TestRules:
    def _empty(self):
        return (
            RobotsReport(found=True, sitemaps=["https://x.test/sitemap.xml"]),
            SitemapReport(found=True, url="https://x.test/sitemap.xml", url_count=12),
            CoreWebVitals(available=True, lcp_ms=1200, cls=0.02, inp_ms=120),
        )

    def test_a_healthy_page_scores_high(self):
        robots, sitemap, vitals = self._empty()
        page = parse_onpage("https://x.test/services", 200, GOOD_HTML)
        findings, score = rules.evaluate(page, robots, sitemap, vitals)
        codes = {f.code for f in findings}
        assert "title.missing" not in codes
        assert "canonical.missing" not in codes
        assert score >= 80

    def test_noindex_is_critical(self):
        robots, sitemap, vitals = self._empty()
        page = parse_onpage("https://x.test/", 200, BAD_HTML)
        findings, score = rules.evaluate(page, robots, sitemap, vitals)
        noindex = next(f for f in findings if f.code == "indexability.noindex")
        assert noindex.severity == "critical"
        assert score < 60

    def test_findings_are_sorted_critical_first(self):
        robots, sitemap, vitals = self._empty()
        page = parse_onpage("https://x.test/", 200, BAD_HTML)
        findings, _ = rules.evaluate(page, robots, sitemap, vitals)
        severities = [f.severity for f in findings]
        assert severities == sorted(
            severities, key=lambda s: {"critical": 0, "warning": 1, "info": 2}[s]
        )

    def test_robots_disallow_all_is_critical(self):
        _, sitemap, vitals = self._empty()
        page = parse_onpage("https://x.test/services", 200, GOOD_HTML)
        findings, _ = rules.evaluate(
            page, RobotsReport(found=True, blocks_all=True), sitemap, vitals
        )
        assert any(
            f.code == "robots.disallow_all" and f.severity == "critical"
            for f in findings
        )

    def test_unreachable_page_is_reported_not_scored_as_perfect(self):
        robots, sitemap, vitals = self._empty()
        findings, score = rules.evaluate(None, robots, sitemap, vitals)
        assert any(f.code == "page.unreachable" for f in findings)
        assert score < 100

    def test_unavailable_vitals_are_info_not_invented(self):
        robots, sitemap, _ = self._empty()
        page = parse_onpage("https://x.test/services", 200, GOOD_HTML)
        findings, _ = rules.evaluate(
            page,
            robots,
            sitemap,
            CoreWebVitals(available=False, reason="rate limited"),
        )
        finding = next(f for f in findings if f.code == "vitals.unavailable")
        assert finding.severity == "info"
        assert finding.evidence == "rate limited"

    def test_slow_lcp_is_flagged(self):
        robots, sitemap, _ = self._empty()
        page = parse_onpage("https://x.test/services", 200, GOOD_HTML)
        findings, _ = rules.evaluate(
            page, robots, sitemap, CoreWebVitals(available=True, lcp_ms=5200)
        )
        assert any(f.code == "vitals.lcp" for f in findings)

    def test_score_is_clamped_to_zero(self):
        findings, score = rules.evaluate(
            None,
            RobotsReport(found=False),
            SitemapReport(found=False),
            CoreWebVitals(available=False, reason="x"),
        )
        assert 0 <= score <= 100


class TestRobotsParsing:
    def _client(self, text: str, status: int = 200):
        def handler(request):
            return httpx.Response(status, text=text,
                                  headers={"content-type": "text/plain"})

        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def test_collects_sitemap_directives(self):
        client = self._client(
            "User-agent: *\nDisallow: /admin\nSitemap: https://x.test/sitemap.xml\n"
        )
        report = await fetch.fetch_robots(client, "https://x.test/", Settings())
        assert report.found is True
        assert report.sitemaps == ["https://x.test/sitemap.xml"]
        assert report.blocks_all is False

    async def test_detects_a_site_wide_block(self):
        client = self._client("User-agent: *\nDisallow: /\n")
        report = await fetch.fetch_robots(client, "https://x.test/", Settings())
        assert report.blocks_all is True

    async def test_a_block_on_one_named_bot_is_not_site_wide(self):
        client = self._client("User-agent: BadBot\nDisallow: /\n")
        report = await fetch.fetch_robots(client, "https://x.test/", Settings())
        assert report.blocks_all is False

    async def test_missing_robots_is_reported_not_raised(self):
        client = self._client("", status=404)
        report = await fetch.fetch_robots(client, "https://x.test/", Settings())
        assert report.found is False
        assert report.status_code == 404


class TestPSIParsing:
    def test_prefers_field_data_and_says_so(self):
        body = {
            "loadingExperience": {
                "metrics": {
                    "LARGEST_CONTENTFUL_PAINT_MS": {"percentile": 2100},
                    "CUMULATIVE_LAYOUT_SHIFT_SCORE": {"percentile": 5},
                    "INTERACTION_TO_NEXT_PAINT": {"percentile": 180},
                }
            },
            "lighthouseResult": {"categories": {"performance": {"score": 0.91}}},
        }
        vitals = psi.parse_psi(body, "mobile")
        assert vitals.available is True
        assert vitals.lcp_ms == 2100
        assert vitals.cls == pytest.approx(0.05)
        assert vitals.inp_ms == 180
        assert vitals.performance_score == pytest.approx(91.0)
        assert vitals.strategy == "mobile/field"

    def test_falls_back_to_lab_data_and_labels_it(self):
        body = {
            "lighthouseResult": {
                "categories": {"performance": {"score": 0.5}},
                "audits": {
                    "largest-contentful-paint": {"numericValue": 4100.0},
                    "cumulative-layout-shift": {"numericValue": 0.24},
                },
            }
        }
        vitals = psi.parse_psi(body, "desktop")
        assert vitals.lcp_ms == 4100.0
        assert vitals.cls == pytest.approx(0.24)
        assert vitals.strategy == "desktop/lab"

    def test_empty_response_is_unavailable_not_zero(self):
        vitals = psi.parse_psi({}, "mobile")
        assert vitals.available is False
        assert vitals.lcp_ms is None
        assert vitals.reason
