"""Deterministic offline classifier.

Rule-based, not random: the same lead always produces the same verdict, so
tests assert real behaviour and a demo is reproducible. It is honest about
what it is -- every Classification it returns carries provider="mock" and
degraded=True, so a caller can never mistake it for model output.
"""

from ..models import Classification, NormalizedLead

SPAM_MARKERS = (
    "seo services at cheap",
    "guest post",
    "backlink",
    "crypto",
    "bitcoin",
    "loan offer",
    "viagra",
    "increase your traffic",
    "dear sir/madam we provide",
)

RECRUITMENT_MARKERS = ("my cv", "my resume", "job", "internship", "vacancy", "hiring")
PARTNERSHIP_MARKERS = ("partner", "collaboration", "reseller", "white label")
SUPPORT_MARKERS = ("not working", "broken", "bug", "error", "down", "issue", "urgent")

SERVICE_MARKERS = {
    "automation": ("automat", "workflow", "n8n", "zapier", "make.com", "integrat"),
    "crm": ("crm", "hubspot", "salesforce", "pipeline", "lead"),
    "web": ("website", "web app", "landing", "next.js", "react", "redesign"),
    "seo": ("seo", "ranking", "google", "search", "traffic", "keyword"),
    "mobile": ("mobile app", "android", "ios", "flutter"),
    "design": ("design", "branding", "logo", "ui", "ux"),
    "analytics": ("analytic", "dashboard", "report", "ga4", "tracking"),
}

URGENCY_MARKERS = ("asap", "urgent", "immediately", "this week", "deadline")


class MockLLMProvider:
    name = "mock"

    def available(self) -> tuple[bool, str]:
        return True, "offline rule-based classifier, always available"

    async def classify(self, lead: NormalizedLead) -> Classification:
        text = f"{lead.message or ''} {lead.company or ''}".casefold()

        if not (lead.message or "").strip():
            return Classification(
                intent="unclear",
                priority="low",
                services=[],
                summary="Lead submitted no message, so intent cannot be determined.",
                draft_reply=(
                    "Thank you for getting in touch. So that we can point you to "
                    "the right person, could you tell us a little about what you "
                    "are looking to build or improve? A sentence or two is plenty."
                ),
                confidence=0.25,
                provider=self.name,
                degraded=True,
                reason="no message supplied",
            )

        if any(marker in text for marker in SPAM_MARKERS):
            intent = "spam"
        elif any(marker in text for marker in RECRUITMENT_MARKERS):
            intent = "recruitment"
        elif any(marker in text for marker in PARTNERSHIP_MARKERS):
            intent = "partnership"
        elif any(marker in text for marker in SUPPORT_MARKERS):
            intent = "support"
        else:
            intent = "new_project"

        services = sorted(
            name
            for name, markers in SERVICE_MARKERS.items()
            if any(marker in text for marker in markers)
        )

        urgent = any(marker in text for marker in URGENCY_MARKERS)
        if intent == "spam":
            priority = "low"
        elif intent == "new_project" and (not lead.free_email or urgent):
            priority = "high"
        elif intent in ("support", "partnership"):
            priority = "high" if urgent else "medium"
        else:
            priority = "medium" if intent != "recruitment" else "low"

        who = lead.company or lead.name or lead.email_domain
        service_text = ", ".join(services) if services else "unspecified work"
        summary = f"{who} enquired about {service_text} via {lead.source}."

        greeting = f"Hello {lead.name.split()[0]}," if lead.name else "Hello,"
        draft_reply = (
            f"{greeting}\n\nThank you for reaching out. We have received your "
            f"enquiry regarding {service_text} and a member of the team will "
            "review the details and reply shortly. If it would help to talk it "
            "through, let us know a time that suits you and we will arrange a "
            "call.\n\nBest regards,\nThe team"
        )

        # Confidence reflects how much the rules actually matched. Reporting
        # a flat high number for a keyword match would be dishonest.
        matched = len(services) + (1 if intent != "new_project" else 0)
        confidence = min(0.75, 0.35 + 0.1 * matched)

        return Classification(
            intent=intent,  # type: ignore[arg-type]
            priority=priority,  # type: ignore[arg-type]
            services=services,
            summary=summary,
            draft_reply=draft_reply,
            confidence=round(confidence, 2),
            provider=self.name,
            degraded=True,
            reason="rule-based offline provider, not model output",
        )
