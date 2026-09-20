"""Provider selection and fallback.

The selection rule is explicit rather than magic:

  llm_provider=anthropic   use it, and fail loudly if the key is missing
  llm_provider=mock        use the mock even if a key is present
  llm_provider=auto        use Anthropic when configured, mock otherwise

Fallback is one-way and always recorded. When a configured Anthropic call
fails, the request still completes -- via the mock -- but the result is
marked degraded and the reason is attached, so a silent downgrade is
impossible to mistake for a healthy run.
"""

import logging

import httpx

from ..config import Settings
from ..http import UpstreamError
from ..models import Classification, NormalizedLead
from .anthropic_provider import AnthropicProvider
from .mock import MockLLMProvider

logger = logging.getLogger(__name__)


class LLMRegistry:
    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._mock = MockLLMProvider()
        # Always constructed so it can report why it is idle, even when
        # LLM_PROVIDER has ruled it out. See CRMRegistry.availability.
        self._anthropic: AnthropicProvider | None = AnthropicProvider(
            settings, client
        )

    def availability(self) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        for provider in (self._anthropic, self._mock):
            if provider is None:
                continue
            usable, reason = provider.available()
            selected = provider.name == self.active()
            if usable and not selected:
                reason = (
                    f"{reason}; not selected "
                    f"(LLM_PROVIDER={self._settings.llm_provider})"
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

    def active(self) -> str:
        if self._settings.llm_provider == "mock":
            return self._mock.name
        if self._anthropic is not None and self._anthropic.available()[0]:
            return self._anthropic.name
        return self._mock.name

    async def classify(self, lead: NormalizedLead) -> Classification:
        if self._settings.llm_provider == "mock":
            return await self._mock.classify(lead)

        if self._anthropic is not None and self._anthropic.available()[0]:
            try:
                return await self._anthropic.classify(lead)
            except UpstreamError as exc:
                logger.warning("anthropic classify failed, degrading: %s", exc)
                fallback = await self._mock.classify(lead)
                return fallback.model_copy(
                    update={
                        "degraded": True,
                        "reason": f"anthropic unavailable ({exc.message}); "
                        "classified by offline rules",
                    }
                )

        if self._settings.llm_provider == "anthropic":
            # Explicitly demanded and not configured. Do not paper over it.
            raise UpstreamError(
                "anthropic", "provider forced but ANTHROPIC_API_KEY is not set"
            )

        return await self._mock.classify(lead)
