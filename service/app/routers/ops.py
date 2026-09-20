"""Operational endpoints.

/ops/availability is the important one. It answers "which integrations
would actually run right now, and why not" without needing a lead to be
submitted. A pipeline that quietly ran on mocks for a week because a token
expired is the failure this endpoint exists to prevent.

These are readable without the shared secret, deliberately: an operator
diagnosing a misconfiguration is usually the person who has lost access to
the configuration. Nothing here returns a credential -- only whether one is
present.
"""

from fastapi import APIRouter, HTTPException, status

from ..deps import StateDep

router = APIRouter(prefix="/ops", tags=["ops"])


@router.get("/health")
async def health(state: StateDep) -> dict[str, object]:
    return {
        "status": "ok",
        "environment": state.settings.environment,
        "auth": "enforced" if state.settings.ingest_secret else "open",
        "llm_active": state.llm.active(),
        "crm_active": state.crm.active_name(),
    }


@router.get("/availability")
async def availability(state: StateDep) -> dict[str, object]:
    return {
        "llm": {
            "configured": state.settings.llm_provider,
            "active": state.llm.active(),
            "providers": state.llm.availability(),
        },
        "crm": {
            "configured": state.settings.crm_provider,
            "active": state.crm.active_name(),
            "providers": state.crm.availability(),
        },
        "pagespeed": {
            "available": True,
            "reason": "key set (higher rate limit)"
            if state.settings.pagespeed_api_key
            else "unauthenticated, low rate limit",
        },
    }


@router.get("/runs")
async def runs(state: StateDep, limit: int = 25) -> dict[str, object]:
    return {
        "runs": state.store.recent_runs(min(max(limit, 1), 200)),
        "stats": state.store.stats(),
    }


@router.get("/runs/{run_id}")
async def run_detail(run_id: str, state: StateDep) -> dict[str, object]:
    record = state.store.get_run(run_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="unknown run_id"
        )
    return record
