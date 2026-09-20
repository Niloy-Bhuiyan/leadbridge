"""The end-to-end lead pipeline.

n8n can call each stage separately -- and the workflow does, so the stages
are visible on the canvas rather than hidden inside one opaque call. This
module exists for the single-call path (/leads/pipeline), which the CSV
importer and the tests use.

Both paths share the same step functions, so there is one implementation of
the business rules and two ways to drive it.

Failure policy, stated once:
  normalize  fatal. No valid email means no lead.
  dedupe     fatal-by-design. A duplicate short-circuits to a recorded skip.
  classify   degradable. Falls back to offline rules, marked degraded.
  crm        fatal. A lead believed filed but not filed is the worst
             outcome in the system, so this failure is raised, not absorbed.
  seo        degradable. A failed audit must not lose the lead.
"""

import time
from typing import Any

from .crm.base import render_audit_note, render_note
from .db import Store
from .dedupe import find_duplicate
from .http import UpstreamError
from .models import (
    Classification,
    CRMContact,
    CRMNote,
    PipelineRequest,
    PipelineResponse,
    SEOAudit,
    SEOAuditRequest,
)
from .normalize import normalize_lead
from .seo.audit import findings_html, run_audit


class PipelineError(Exception):
    def __init__(self, step: str, detail: str) -> None:
        super().__init__(f"{step}: {detail}")
        self.step = step
        self.detail = detail


class StepRecorder:
    """Collects step outcomes for both the run log and the HTTP response.

    The same records go to both places deliberately: what n8n sees and what
    an operator later reads out of SQLite must not be able to disagree.
    """

    def __init__(self, store: Store, run_id: str) -> None:
        self._store = store
        self._run_id = run_id
        self.steps: list[dict[str, Any]] = []
        self.degraded = False

    def record(
        self,
        step: str,
        status: str,
        started: float,
        detail: dict[str, Any] | None = None,
    ) -> None:
        duration_ms = int((time.perf_counter() - started) * 1000)
        if status == "degraded":
            self.degraded = True
        entry = {
            "step": step,
            "status": status,
            "duration_ms": duration_ms,
            "detail": detail,
        }
        self.steps.append(entry)
        self._store.log_step(self._run_id, step, status, duration_ms, detail)


async def run_pipeline(
    request: PipelineRequest,
    *,
    store: Store,
    llm,
    crm,
    http_client,
    settings,
) -> PipelineResponse:
    run_id = store.start_run()
    steps = StepRecorder(store, run_id)

    # --- normalize --------------------------------------------------------
    started = time.perf_counter()
    lead, errors = normalize_lead(request.lead)
    if lead is None:
        steps.record("normalize", "failed", started, {"errors": errors})
        store.finish_run(run_id, "failed", summary="; ".join(errors))
        raise PipelineError("normalize", "; ".join(errors))
    steps.record(
        "normalize", "ok", started, {"warnings": lead.warnings} if lead.warnings else None
    )

    # --- dedupe -----------------------------------------------------------
    started = time.perf_counter()
    known = store.known_leads(lead.email, lead.dedupe_key)
    verdict = find_duplicate(lead, known)
    if verdict.duplicate:
        steps.record(
            "dedupe",
            "skipped",
            started,
            {"matched_on": verdict.matched_on, "matched": verdict.matched_fingerprint},
        )
        store.record_lead(lead)
        store.finish_run(
            run_id,
            "skipped_duplicate",
            summary=f"duplicate matched on {verdict.matched_on}",
            fingerprint=lead.fingerprint,
        )
        return PipelineResponse(
            run_id=run_id,
            status="skipped_duplicate",
            lead=lead,
            duplicate=verdict,
            steps=steps.steps,
        )
    steps.record("dedupe", "ok", started, {"checked_against": len(known)})
    store.record_lead(lead)

    # --- classify ---------------------------------------------------------
    started = time.perf_counter()
    classification: Classification | None = None
    try:
        classification = await llm.classify(lead)
        steps.record(
            "classify",
            "degraded" if classification.degraded else "ok",
            started,
            {
                "provider": classification.provider,
                "intent": classification.intent,
                "priority": classification.priority,
                "reason": classification.reason,
            },
        )
    except UpstreamError as exc:
        steps.record("classify", "failed", started, {"error": exc.message})
        store.finish_run(
            run_id, "failed", degraded=True, summary=f"classify: {exc.message}",
            fingerprint=lead.fingerprint,
        )
        raise PipelineError("classify", exc.message) from exc

    # --- crm --------------------------------------------------------------
    contact: CRMContact | None = None
    note: CRMNote | None = None
    if request.push_to_crm:
        started = time.perf_counter()
        try:
            contact = await crm.upsert_contact(lead)
            note = await crm.add_note(contact.id, render_note(lead, classification))
        except UpstreamError as exc:
            steps.record("crm", "failed", started, {"error": exc.message})
            store.finish_run(
                run_id, "failed", degraded=steps.degraded,
                summary=f"crm: {exc.message}", fingerprint=lead.fingerprint,
            )
            raise PipelineError("crm", exc.message) from exc
        steps.record(
            "crm",
            "ok",
            started,
            {
                "provider": contact.provider,
                "contact_id": contact.id,
                "created": contact.created,
            },
        )
    else:
        steps.record("crm", "skipped", time.perf_counter(), {"reason": "disabled"})

    # --- seo audit --------------------------------------------------------
    audit: SEOAudit | None = None
    if request.run_seo_audit and lead.website:
        started = time.perf_counter()
        try:
            audit = await run_audit(
                http_client, SEOAuditRequest(url=lead.website), settings
            )
            steps.record(
                "seo_audit",
                "degraded" if audit.degraded else "ok",
                started,
                {"score": audit.score, "findings": len(audit.findings)},
            )
            if contact is not None:
                await crm.add_note(
                    contact.id,
                    render_audit_note(audit.url, audit.score, findings_html(audit)),
                )
        except Exception as exc:  # audit must never lose the lead
            steps.record("seo_audit", "degraded", started, {"error": str(exc)[:300]})
    elif request.run_seo_audit:
        steps.record(
            "seo_audit", "skipped", time.perf_counter(), {"reason": "no website on lead"}
        )

    store.finish_run(
        run_id,
        "completed",
        degraded=steps.degraded,
        summary=f"{classification.intent}/{classification.priority}",
        fingerprint=lead.fingerprint,
    )
    return PipelineResponse(
        run_id=run_id,
        status="completed",
        lead=lead,
        duplicate=verdict,
        classification=classification,
        contact=contact,
        note=note,
        audit=audit,
        steps=steps.steps,
        degraded=steps.degraded,
    )
