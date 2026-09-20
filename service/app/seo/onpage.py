"""On-page HTML parsing.

Pure: takes HTML text, returns an OnPage record. No network here, which is
what lets the test suite assert on hand-written fixtures covering the cases
that matter -- a missing canonical, a noindex, three H1s, malformed JSON-LD
-- without depending on a live site that may change tomorrow.
"""

import json
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..models import OnPage

WORD_RE = re.compile(r"[\w'ঀ-৿]+", re.UNICODE)
NON_CONTENT_TAGS = ("script", "style", "noscript", "template", "svg")


def _text_of(node) -> str | None:
    if node is None:
        return None
    text = node.get_text(" ", strip=True) if hasattr(node, "get_text") else str(node)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def _jsonld_types(soup: BeautifulSoup) -> list[str]:
    """Collect schema.org @type values.

    Malformed JSON-LD is common and is not a parse failure for the whole
    audit -- it is itself a finding. A block that will not parse is skipped
    and reported by the rules layer as missing structured data.
    """
    found: list[str] = []
    for block in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = block.string or block.get_text() or ""
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue

        def walk(node) -> None:
            if isinstance(node, dict):
                value = node.get("@type")
                if isinstance(value, str):
                    found.append(value)
                elif isinstance(value, list):
                    found.extend(str(v) for v in value)
                for child in node.values():
                    walk(child)
            elif isinstance(node, list):
                for child in node:
                    walk(child)

        walk(data)
    # Stable order, no duplicates, so the output diffs cleanly between runs.
    return sorted(set(found))


def parse_onpage(url: str, status_code: int, html: str) -> OnPage:
    soup = BeautifulSoup(html, "html.parser")

    title = _text_of(soup.title)

    def meta_content(name: str) -> str | None:
        tag = soup.find("meta", attrs={"name": re.compile(rf"^{name}$", re.I)})
        if tag is None:
            return None
        content = tag.get("content")
        return content.strip() if isinstance(content, str) and content.strip() else None

    description = meta_content("description")
    robots_meta = meta_content("robots")

    canonical_tag = soup.find("link", attrs={"rel": re.compile(r"^canonical$", re.I)})
    canonical = None
    if canonical_tag is not None:
        href = canonical_tag.get("href")
        if isinstance(href, str) and href.strip():
            canonical = urljoin(url, href.strip())

    h1 = [t for t in (_text_of(tag) for tag in soup.find_all("h1")) if t]

    og_tags: dict[str, str] = {}
    for tag in soup.find_all("meta", attrs={"property": re.compile(r"^og:", re.I)}):
        prop = tag.get("property")
        content = tag.get("content")
        if isinstance(prop, str) and isinstance(content, str) and content.strip():
            og_tags[prop.lower()] = content.strip()

    images = soup.find_all("img")
    missing_alt = sum(
        1
        for img in images
        if not isinstance(img.get("alt"), str) or not img.get("alt", "").strip()
    )

    # Collect structured data before stripping <script>, since JSON-LD lives
    # in a script tag and the word count must not include it.
    jsonld = _jsonld_types(soup)

    for tag in soup(list(NON_CONTENT_TAGS)):
        tag.decompose()
    body_text = soup.get_text(" ", strip=True)
    word_count = len(WORD_RE.findall(body_text))

    html_tag = soup.find("html")
    html_lang = None
    if html_tag is not None:
        lang = html_tag.get("lang")
        if isinstance(lang, str) and lang.strip():
            html_lang = lang.strip()

    return OnPage(
        url=url,
        status_code=status_code,
        title=title,
        title_length=len(title) if title else 0,
        meta_description=description,
        meta_description_length=len(description) if description else 0,
        canonical=canonical,
        robots_meta=robots_meta,
        h1=h1,
        jsonld_types=jsonld,
        og_tags=og_tags,
        images_total=len(images),
        images_missing_alt=missing_alt,
        word_count=word_count,
        html_lang=html_lang,
    )
