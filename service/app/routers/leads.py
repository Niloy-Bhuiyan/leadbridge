"""Lead endpoints.

Each stage is its own endpoint so the n8n canvas shows the pipeline as a
sequence of named steps a non-author can read. /pipeline runs the same
stages in one call for batch imports and for the test suite.
"""

from fastapi import APIRouter, HTTPException, status

from ..crm.base import render_note
from ..dedupe import find_duplicate
from ..deps import SecretDep, StateDep
from ..http import UpstreamError
from ..models import (
    Classification,
    ClassifyRequest,
    CRMNote,
    DedupeRequest,
    DedupeResponse,
    NormalizeResponse,
    PipelineRequest,
    PipelineResponse,
    RawLead,
    UpsertRequest,
)
from ..normalize import normalize_lead
from ..pipeline import PipelineError, run_pipeline

router = APIRouter(prefix="/leads", tags=["leads"], dependencies=[SecretDep])


@router.post("/normalize", response_model=NormalizeResponse)
async def normalize(raw: RawLead) -> NormalizeResponse:
    lead, errors = normalize_lead(raw)
    if lead is None:
        # 422 rather than 400: the payload parsed but failed business
        # validation, and n8n routes on the status code.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"errors": errors},
        )
    return NormalizeResponse(ok=True, lead=lead)


@router.post("/dedupe", response_model=DedupeResponse)
async def dedupe(request: DedupeRequest, state: StateDep) -> DedupeResponse:
    known = state.store.known_leads(request.lead.email, request.lead.dedupe_key)
    return find_duplicate(request.lead, [*known, *request.against])


@router.post("/classify", response_model=Classification)
async def classify(request: ClassifyRequest, state: StateDep) -> Classification:
    try:
        return await state.llm.classify(request.lead)
    except UpstreamError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"service": exc.service, "message": exc.message},
        ) from exc


@router.post("/crm-upsert", response_model=CRMNote)
async def crm_upsert(request: UpsertRequest, state: StateDep) -> CRMNote:
    if request.classification is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="classification is required to write the triage note",
        )
    client = state.crm.active()
    try:
        contact = await client.upsert_contact(request.lead)
        return await client.add_note(
            contact.id, render_note(request.lead, request.classification)
        )
    except UpstreamError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"service": exc.service, "message": exc.message},
        ) from exc


@router.post("/pipeline", response_model=PipelineResponse)
async def pipeline(request: PipelineRequest, state: StateDep) -> PipelineResponse:
    try:
        return await run_pipeline(
            request,
            store=state.store,
            llm=state.llm,
            crm=state.crm.active(),
            http_client=state.http,
            settings=state.settings,
        )
    except PipelineError as exc:
        code = (
            status.HTTP_422_UNPROCESSABLE_ENTITY
            if exc.step == "normalize"
            else status.HTTP_502_BAD_GATEWAY
        )
        raise HTTPException(
            status_code=code, detail={"step": exc.step, "message": exc.detail}
        ) from exc
