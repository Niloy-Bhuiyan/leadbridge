"""CRM selection.

Deliberately stricter than the LLM registry: a failed HubSpot write does
NOT silently fall back to the in-memory mock. Degrading a model call costs
a worse summary; degrading a CRM write means the agency believes a lead is
filed when it is not. That failure is raised, recorded on the run, and
surfaced to the workflow so n8n can route it to a human.
"""


import httpx

from ..config import Settings
from .hubspot import HubSpotClient
from .mock import MockCRMClient


class CRMRegistry:
    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._mock = MockCRMClient()
        # Always constructed, even when not selected. Building it costs
        # nothing and it must still be able to report why it is idle.
        self._hubspot: HubSpotClient | None = HubSpotClient(settings, client)

    def availability(self) -> list[dict[str, object]]:
        """Report every known provider, including ones configuration has
        ruled out. An endpoint that silently omits the provider you expected
        to be running cannot answer the question it exists to answer."""
        rows: list[dict[str, object]] = []
        for provider in (self._hubspot, self._mock):
            if provider is None:
                continue
            usable, reason = provider.available()
            selected = provider.name == self.active_name()
            if usable and not selected:
                reason = (
                    f"{reason}; not selected "
                    f"(CRM_PROVIDER={self._settings.crm_provider})"
                )
            rows.append(
                {
                    "provider": provider.name,
                    "available": usable,
                    "selected": selected,
                    "reason": reason,
                }
            )
        return rows

    def active(self):
        if self._settings.crm_provider == "mock":
            return self._mock
        if self._hubspot is not None and self._hubspot.available()[0]:
            return self._hubspot
        return self._mock

    def active_name(self) -> str:
        return self.active().name

    @property
    def mock(self) -> MockCRMClient:
        """Exposed so tests can assert on what was written."""
        return self._mock
