"""Shared outbound HTTP with retry and backoff.

Every third-party call in this service -- Anthropic, HubSpot, PageSpeed
Insights, and the audited site itself -- goes through here, so retry policy
is defined once instead of drifting per integration.

What is retried: connection errors, timeouts, 429, and 5xx.
What is not:     4xx other than 429. A 401 will not fix itself, and
                 retrying it just burns the rate limit and delays the
                 error the operator actually needs to see.
"""

import asyncio
import random
from dataclasses import dataclass
from typing import Any

import httpx

RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})


class UpstreamError(Exception):
    """A third-party call that did not succeed after the retry budget.

    Carries the status and body so the caller can log why rather than
    re-raising an opaque failure.
    """

    def __init__(
        self,
        service: str,
        message: str,
        status_code: int | None = None,
        body: str | None = None,
    ) -> None:
        super().__init__(f"{service}: {message}")
        self.service = service
        self.message = message
        self.status_code = status_code
        self.body = (body or "")[:500]


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    base: float = 0.25
    cap: float = 4.0

    def delay_for(self, attempt: int) -> float:
        """Exponential backoff with full jitter.

        Full jitter rather than fixed backoff because n8n can fire several
        workflow executions at once; synchronized retries from a burst are
        how a transient 429 becomes a sustained one.
        """
        ceiling = min(self.cap, self.base * (2**attempt))
        return random.uniform(0, ceiling)


def _retry_after_seconds(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        # Only the delta-seconds form is handled. The HTTP-date form is
        # rare in practice and guessing wrong is worse than backing off.
        return max(0.0, float(raw.strip()))
    except ValueError:
        return None


async def request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    service: str,
    policy: RetryPolicy,
    expected: tuple[int, ...] = (200, 201, 204),
    **kwargs: Any,
) -> httpx.Response:
    last_error: str | None = None
    last_status: int | None = None
    last_body: str | None = None

    for attempt in range(policy.max_attempts):
        try:
            response = await client.request(method, url, **kwargs)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            last_status = None
            last_body = None
        else:
            if response.status_code in expected:
                return response
            last_status = response.status_code
            last_body = response.text
            last_error = f"unexpected status {response.status_code}"
            if response.status_code not in RETRYABLE_STATUS:
                raise UpstreamError(service, last_error, last_status, last_body)
            hinted = _retry_after_seconds(response)
            if hinted is not None and attempt < policy.max_attempts - 1:
                await asyncio.sleep(min(hinted, policy.cap))
                continue

        if attempt < policy.max_attempts - 1:
            await asyncio.sleep(policy.delay_for(attempt))

    raise UpstreamError(
        service,
        f"{last_error} after {policy.max_attempts} attempts",
        last_status,
        last_body,
    )
