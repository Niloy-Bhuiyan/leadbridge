"""The CRM boundary.

Written as a protocol with two implementations because the agency use case
genuinely has two: a live HubSpot portal, and an in-memory twin used by the
test suite and by anyone reviewing the workflow without portal access.

Only the operations the pipeline actually performs are on the interface.
There is no generic `query` or `delete` here, because nothing calls them.
"""

from typing import Protocol, runtime_checkable

from ..models import Classification, CRMContact, CRMNote, NormalizedLead


def render_note(lead: NormalizedLead, classification: Classification) -> str:
    """The note body written onto the contact.

    HubSpot renders note bodies as HTML, so this is HTML. It states which
    provider produced the triage and whether it was degraded, because a
    salesperson reading it needs to know whether a model or a keyword rule
    decided this lead was high priority.
    """
    services = ", ".join(classification.services) or "not specified"
    provenance = (
        f"{classification.provider}"
        + (" (degraded)" if classification.degraded else "")
        + (f" - {classification.reason}" if classification.reason else "")
    )
    warnings = ""
    if lead.warnings:
        items = "".join(f"<li>{w}</li>" for w in lead.warnings)
        warnings = f"<p><b>Intake warnings</b></p><ul>{items}</ul>"

    return (
        "<h3>Automated lead triage</h3>"
        f"<p>{classification.summary}</p>"
        "<ul>"
        f"<li><b>Intent:</b> {classification.intent}</li>"
        f"<li><b>Priority:</b> {classification.priority}</li>"
        f"<li><b>Services:</b> {services}</li>"
        f"<li><b>Confidence:</b> {classification.confidence:.2f}</li>"
        f"<li><b>Source:</b> {lead.source}</li>"
        f"<li><b>Triaged by:</b> {provenance}</li>"
        "</ul>"
        f"{warnings}"
        "<p><b>Suggested first reply (not sent automatically)</b></p>"
        f"<blockquote>{classification.draft_reply}</blockquote>"
    )


def render_audit_note(url: str, score: int, findings_html: str) -> str:
    return (
        "<h3>Technical SEO audit</h3>"
        f"<p>Audited <a href=\"{url}\">{url}</a> &mdash; score {score}/100.</p>"
        f"{findings_html}"
        "<p><i>Automated audit. Findings are signals to verify, not "
        "conclusions.</i></p>"
    )


@runtime_checkable
class CRMClient(Protocol):
    name: str

    def available(self) -> tuple[bool, str]: ...

    async def upsert_contact(self, lead: NormalizedLead) -> CRMContact: ...

    async def add_note(self, contact_id: str, body: str) -> CRMNote: ...
