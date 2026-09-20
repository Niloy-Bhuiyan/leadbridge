"""SEO audit endpoints."""

from fastapi import APIRouter, HTTPException, status

from ..deps import SecretDep, StateDep
from ..models import SEOAudit, SEOAuditRequest
from ..normalize import normalize_website
from ..seo.audit import run_audit

router = APIRouter(prefix="/seo", tags=["seo"], dependencies=[SecretDep])


@router.post("/audit", response_model=SEOAudit)
async def audit(request: SEOAuditRequest, state: StateDep) -> SEOAudit:
    url, error = normalize_website(request.url)
    if error or url is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=error or "url could not be parsed",
        )
    return await run_audit(
        state.http, request.model_copy(update={"url": url}), state.settings
    )
