import httpx
import pytest

from app.http import RetryPolicy, UpstreamError, request_with_retry

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def no_real_sleeping(monkeypatch):
    """Backoff correctness is asserted separately; the tests must not
    actually wait for it."""
    async def instant(_seconds):
        return None

    monkeypatch.setattr("app.http.asyncio.sleep", instant)


FAST = RetryPolicy(max_attempts=3, base=0.0, cap=0.0)


class Counter:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def __call__(self, request):
        self.calls += 1
        item = self.responses[min(self.calls - 1, len(self.responses) - 1)]
        if isinstance(item, Exception):
            raise item
        return item


async def call(handler, **kwargs):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return await request_with_retry(
        client, "GET", "https://example.test/x", service="test", policy=FAST, **kwargs
    )


async def test_succeeds_without_retrying():
    handler = Counter([httpx.Response(200, json={"ok": True})])
    response = await call(handler)
    assert response.status_code == 200
    assert handler.calls == 1


async def test_retries_a_500_then_succeeds():
    handler = Counter([httpx.Response(500), httpx.Response(200)])
    response = await call(handler)
    assert response.status_code == 200
    assert handler.calls == 2


async def test_retries_a_429():
    handler = Counter([httpx.Response(429), httpx.Response(429), httpx.Response(200)])
    assert (await call(handler)).status_code == 200
    assert handler.calls == 3


async def test_does_not_retry_a_401():
    """A credential error will not fix itself. Retrying it delays the error
    the operator needs and spends rate limit doing nothing."""
    handler = Counter([httpx.Response(401, text="bad token")])
    with pytest.raises(UpstreamError) as excinfo:
        await call(handler)
    assert handler.calls == 1
    assert excinfo.value.status_code == 401
    assert "bad token" in excinfo.value.body


async def test_exhausts_the_budget_then_raises():
    handler = Counter([httpx.Response(503)])
    with pytest.raises(UpstreamError) as excinfo:
        await call(handler)
    assert handler.calls == 3
    assert "after 3 attempts" in excinfo.value.message


async def test_retries_transport_errors():
    handler = Counter(
        [httpx.ConnectError("refused"), httpx.Response(200)]
    )
    assert (await call(handler)).status_code == 200
    assert handler.calls == 2


async def test_honours_retry_after(monkeypatch):
    """A server that says how long to wait is obeyed, up to the cap."""
    slept: list[float] = []

    async def record(seconds):
        slept.append(seconds)

    monkeypatch.setattr("app.http.asyncio.sleep", record)
    handler = Counter(
        [httpx.Response(429, headers={"Retry-After": "2"}), httpx.Response(200)]
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await request_with_retry(
        client,
        "GET",
        "https://example.test/x",
        service="test",
        policy=RetryPolicy(max_attempts=3, base=0.0, cap=10.0),
        expected=(200,),
    )
    assert slept == [2.0]


async def test_retry_after_is_clamped_to_the_cap():
    """A hostile or broken server cannot park the worker for an hour."""
    slept: list[float] = []

    async def record(seconds):
        slept.append(seconds)

    import app.http as http_module

    original = http_module.asyncio.sleep
    http_module.asyncio.sleep = record
    try:
        handler = Counter(
            [httpx.Response(429, headers={"Retry-After": "3600"}), httpx.Response(200)]
        )
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        await request_with_retry(
            client,
            "GET",
            "https://example.test/x",
            service="test",
            policy=RetryPolicy(max_attempts=2, base=0.0, cap=5.0),
            expected=(200,),
        )
    finally:
        http_module.asyncio.sleep = original
    assert slept == [5.0]


async def test_expected_status_is_configurable():
    handler = Counter([httpx.Response(201)])
    assert (await call(handler, expected=(201,))).status_code == 201


class TestBackoff:
    def test_delay_is_bounded_by_the_cap(self):
        policy = RetryPolicy(max_attempts=5, base=1.0, cap=4.0)
        for attempt in range(5):
            assert 0.0 <= policy.delay_for(attempt) <= 4.0

    def test_delay_grows_with_the_attempt_ceiling(self):
        """Full jitter means individual samples are random, so assert on the
        ceiling each attempt draws from: base * 2**attempt."""
        policy = RetryPolicy(base=1.0, cap=100.0)
        for attempt, ceiling in [(0, 1.0), (1, 2.0), (2, 4.0), (3, 8.0)]:
            samples = [policy.delay_for(attempt) for _ in range(200)]
            assert max(samples) <= ceiling
            # With 200 draws the sample max should approach the ceiling;
            # if it never does, the exponent is not being applied.
            assert max(samples) > ceiling * 0.5
