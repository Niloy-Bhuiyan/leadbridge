"""Shared application state and request guards.

One httpx.AsyncClient for the process, because a new client per request
throws away connection pooling and TLS session reuse -- which matters when
HubSpot and PageSpeed are both called on every lead.
"""

from typing import Annotated

import httpx
from fastapi import Depends, Header, HTTPException, Request, status

from .config import Settings, get_settings
from .crm.registry import CRMRegistry
from .db import Store
from .providers.registry import LLMRegistry


class AppState:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.store = Store(settings.database_path)
        self.http = httpx.AsyncClient(
            timeout=httpx.Timeout(20.0, connect=5.0),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            follow_redirects=False,
        )
        self.llm = LLMRegistry(settings, self.http)
        self.crm = CRMRegistry(settings, self.http)

    async def aclose(self) -> None:
        await self.http.aclose()
        self.store.close()


def get_state(request: Request) -> AppState:
    return request.app.state.services


StateDep = Annotated[AppState, Depends(get_state)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


async def require_secret(
    request: Request,
    x_leadbridge_secret: Annotated[str | None, Header()] = None,
) -> None:
    """Shared-secret guard on every mutating endpoint.

    n8n sends the header. An unset secret disables the check, which is
    acceptable only on a loopback-bound local run -- the README says so and
    /ops/health reports `auth: open` so the state is never invisible.
    """
    expected = request.app.state.services.settings.ingest_secret
    if not expected:
        return
    if x_leadbridge_secret != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing X-Leadbridge-Secret",
        )


SecretDep = Depends(require_secret)
