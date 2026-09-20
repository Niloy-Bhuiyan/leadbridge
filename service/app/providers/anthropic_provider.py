"""Anthropic Messages API provider.

Called over httpx rather than through the official SDK, deliberately: every
outbound call in this service already shares one retry policy, one timeout
budget and one error type, and adding an SDK would put this one integration
on a different path. The wire format is stable and small.

The model is asked for JSON. Model output is parsed defensively and
validated against the Classification schema -- a model that returns prose,
a code fence, or an out-of-range confidence degrades to the mock rather
than propagating a malformed object into the CRM.
"""

import json
import re
from typing import Any

import httpx

from ..config import Settings
from ..http import RetryPolicy, UpstreamError, request_with_retry
from ..models import Classification, NormalizedLead
from .base import CLASSIFY_SYSTEM, build_user_prompt

FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._client = client
        self._policy = RetryPolicy(
            max_attempts=settings.http_max_attempts,
            base=settings.http_backoff_base,
            cap=settings.http_backoff_cap,
        )

    def available(self) -> tuple[bool, str]:
        if not self._settings.anthropic_api_key:
            return False, "ANTHROPIC_API_KEY is not set"
        return True, f"model {self._settings.anthropic_model}"

    async def classify(self, lead: NormalizedLead) -> Classification:
        response = await request_with_retry(
            self._client,
            "POST",
            f"{self._settings.anthropic_base_url}/v1/messages",
            service="anthropic",
            policy=self._policy,
            expected=(200,),
            headers={
                "x-api-key": self._settings.anthropic_api_key,
                "anthropic-version": self._settings.anthropic_version,
                "content-type": "application/json",
            },
            json={
                "model": self._settings.anthropic_model,
                "max_tokens": 1024,
                "system": CLASSIFY_SYSTEM,
                "messages": [
                    {"role": "user", "content": build_user_prompt(lead)},
                    # Prefilling the assistant turn with an opening brace is
                    # the cheapest way to suppress a preamble; the brace is
                    # added back before parsing.
                    {"role": "assistant", "content": "{"},
                ],
            },
        )
        payload = self._extract_json(response.json())
        return self._to_classification(payload)

    @staticmethod
    def _extract_json(body: dict[str, Any]) -> dict[str, Any]:
        blocks = body.get("content") or []
        text = "".join(
            block.get("text", "")
            for block in blocks
            if isinstance(block, dict) and block.get("type") == "text"
        ).strip()
        if not text:
            raise UpstreamError("anthropic", "response contained no text block")

        # Order matters: strip any code fence FIRST, then restore the brace.
        # Doing it the other way round produces "{```json{...}```", which is
        # not valid JSON and no longer matches the fence pattern either.
        text = FENCE_RE.sub("", text).strip()

        # The assistant turn was prefilled with "{", so the model continues
        # from inside the object.
        if not text.startswith("{"):
            text = "{" + text

        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise UpstreamError(
                "anthropic", f"response was not valid JSON: {exc}", body=text
            ) from exc
        if not isinstance(parsed, dict):
            raise UpstreamError("anthropic", "response JSON was not an object")
        return parsed

    def _to_classification(self, payload: dict[str, Any]) -> Classification:
        confidence = payload.get("confidence", 0.5)
        try:
            confidence = min(1.0, max(0.0, float(confidence)))
        except (TypeError, ValueError):
            confidence = 0.5

        services = payload.get("services") or []
        if not isinstance(services, list):
            services = []

        try:
            return Classification(
                intent=payload.get("intent", "unclear"),
                priority=payload.get("priority", "medium"),
                services=[str(s) for s in services][:8],
                summary=str(payload.get("summary", "")).strip()[:400]
                or "Model returned no summary.",
                draft_reply=str(payload.get("draft_reply", "")).strip()[:2000]
                or "Model returned no draft reply.",
                confidence=confidence,
                provider=self.name,
                degraded=False,
            )
        except Exception as exc:  # pydantic validation of the literals
            raise UpstreamError(
                "anthropic",
                f"model output failed schema validation: {exc}",
                body=json.dumps(payload)[:500],
            ) from exc


def build_anthropic_provider(
    settings: Settings, client: httpx.AsyncClient
) -> AnthropicProvider | None:
    provider = AnthropicProvider(settings, client)
    usable, _ = provider.available()
    return provider if usable else None
