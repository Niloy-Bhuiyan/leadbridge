"""Wire contracts. Pydantic v2 everywhere, so a malformed payload is a 422
from the framework rather than a KeyError three modules deeper."""

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Severity = Literal["critical", "warning", "info"]


def utcnow() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------- leads
class RawLead(BaseModel):
    """What arrives on the webhook. Deliberately permissive: real intake
    forms are messy, and rejecting a lead over a stray space loses revenue."""

    model_config = ConfigDict(str_strip_whitespace=True)

    email: str
    name: str | None = None
    company: str | None = None
    phone: str | None = None
    website: str | None = None
    message: str | None = None
    source: str = "webhook"
    submitted_at: datetime | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class NormalizedLead(BaseModel):
    email: str
    email_domain: str
    name: str | None = None
    company: str | None = None
    phone_e164: str | None = None
    website: str | None = None
    message: str | None = None
    source: str
    submitted_at: datetime
    fingerprint: str
    dedupe_key: str
    free_email: bool
    warnings: list[str] = Field(default_factory=list)


class NormalizeResponse(BaseModel):
    ok: bool
    lead: NormalizedLead | None = None
    errors: list[str] = Field(default_factory=list)


class DedupeRequest(BaseModel):
    lead: NormalizedLead
    against: list[NormalizedLead] = Field(default_factory=list)


class DedupeResponse(BaseModel):
    duplicate: bool
    matched_on: Literal["fingerprint", "email", "fuzzy"] | None = None
    matched_fingerprint: str | None = None


# ------------------------------------------------------------------ classify
class Classification(BaseModel):
    intent: Literal[
        "new_project", "support", "partnership", "recruitment", "spam", "unclear"
    ]
    priority: Literal["high", "medium", "low"]
    services: list[str] = Field(default_factory=list)
    summary: str
    draft_reply: str
    confidence: float = Field(ge=0.0, le=1.0)
    provider: str
    degraded: bool = False
    reason: str | None = None


class ClassifyRequest(BaseModel):
    lead: NormalizedLead


# ----------------------------------------------------------------------- crm
class CRMContact(BaseModel):
    id: str
    email: str
    created: bool
    properties: dict[str, Any] = Field(default_factory=dict)
    provider: str


class CRMNote(BaseModel):
    id: str
    contact_id: str
    provider: str


class UpsertRequest(BaseModel):
    lead: NormalizedLead
    classification: Classification | None = None


# ----------------------------------------------------------------------- seo
class SEOFinding(BaseModel):
    code: str
    severity: Severity
    message: str
    evidence: str | None = None


class OnPage(BaseModel):
    url: str
    status_code: int
    title: str | None = None
    title_length: int = 0
    meta_description: str | None = None
    meta_description_length: int = 0
    canonical: str | None = None
    robots_meta: str | None = None
    h1: list[str] = Field(default_factory=list)
    jsonld_types: list[str] = Field(default_factory=list)
    og_tags: dict[str, str] = Field(default_factory=dict)
    images_total: int = 0
    images_missing_alt: int = 0
    word_count: int = 0
    html_lang: str | None = None


class RobotsReport(BaseModel):
    found: bool
    status_code: int | None = None
    sitemaps: list[str] = Field(default_factory=list)
    blocks_all: bool = False


class SitemapReport(BaseModel):
    found: bool
    url: str | None = None
    status_code: int | None = None
    url_count: int = 0
    is_index: bool = False


class CoreWebVitals(BaseModel):
    available: bool
    reason: str | None = None
    performance_score: float | None = None
    lcp_ms: float | None = None
    cls: float | None = None
    inp_ms: float | None = None
    strategy: str | None = None


class SEOAuditRequest(BaseModel):
    url: str
    include_psi: bool = True
    strategy: Literal["mobile", "desktop"] = "mobile"


class SEOAudit(BaseModel):
    url: str
    fetched_at: datetime = Field(default_factory=utcnow)
    score: int = Field(ge=0, le=100)
    onpage: OnPage | None = None
    robots: RobotsReport
    sitemap: SitemapReport
    vitals: CoreWebVitals
    findings: list[SEOFinding] = Field(default_factory=list)
    degraded: bool = False


# ------------------------------------------------------------------ pipeline
class PipelineRequest(BaseModel):
    lead: RawLead
    run_seo_audit: bool = True
    push_to_crm: bool = True


class PipelineResponse(BaseModel):
    run_id: str
    status: Literal["completed", "skipped_duplicate", "failed"]
    lead: NormalizedLead | None = None
    duplicate: DedupeResponse | None = None
    classification: Classification | None = None
    contact: CRMContact | None = None
    note: CRMNote | None = None
    audit: SEOAudit | None = None
    steps: list[dict[str, Any]] = Field(default_factory=list)
    degraded: bool = False
