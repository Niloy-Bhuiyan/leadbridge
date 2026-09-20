import json

import httpx
import pytest

from app.config import Settings
from app.crm.base import render_note
from app.crm.hubspot import NOTE_TO_CONTACT_TYPE_ID, HubSpotClient
from app.crm.mock import MockCRMClient
from app.crm.registry import CRMRegistry
from app.http import UpstreamError
from app.providers.mock import MockLLMProvider

from .conftest import make_lead

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def hubspot_client(handler) -> HubSpotClient:
    settings = Settings(hubspot_token="test-token", http_max_attempts=1)
    transport = httpx.MockTransport(handler)
    return HubSpotClient(settings, httpx.AsyncClient(transport=transport))


class Recorder:
    """Records every request so the tests can assert on what was sent, not
    just on what came back."""

    def __init__(self, routes):
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        for suffix, response in self.routes:
            if request.url.path.endswith(suffix):
                return response() if callable(response) else response
        return httpx.Response(404, json={"message": f"unrouted {request.url.path}"})


class TestHubSpotUpsert:
    async def test_creates_when_search_finds_nothing(self):
        recorder = Recorder(
            [
                ("/search", httpx.Response(200, json={"results": []})),
                (
                    "/contacts",
                    httpx.Response(201, json={"id": "501", "properties": {}}),
                ),
            ]
        )
        contact = await hubspot_client(recorder).upsert_contact(make_lead())
        assert contact.id == "501"
        assert contact.created is True
        assert contact.provider == "hubspot"

        created = json.loads(recorder.requests[-1].content)
        assert created["properties"]["email"] == "ayesha@octopi-digital.example"
        assert created["properties"]["firstname"] == "Ayesha"
        assert created["properties"]["lastname"] == "Rahman"
        assert created["properties"]["phone"] == "+8801868686062"

    async def test_patches_when_search_finds_an_existing_contact(self):
        recorder = Recorder(
            [
                ("/search", httpx.Response(200, json={"results": [{"id": "77"}]})),
                ("/77", httpx.Response(200, json={"properties": {"email": "x"}})),
            ]
        )
        contact = await hubspot_client(recorder).upsert_contact(make_lead())
        assert contact.id == "77"
        assert contact.created is False
        assert recorder.requests[-1].method == "PATCH"

    async def test_recovers_the_id_from_a_409_race(self):
        """Between search and create a concurrent run can win. HubSpot names
        the winning id in the error body; parsing it beats failing."""
        recorder = Recorder(
            [
                ("/search", httpx.Response(200, json={"results": []})),
                (
                    "/contacts",
                    httpx.Response(
                        409,
                        json={"message": "Contact already exists. Existing ID: 9182"},
                    ),
                ),
            ]
        )
        contact = await hubspot_client(recorder).upsert_contact(make_lead())
        assert contact.id == "9182"
        assert contact.created is False

    async def test_a_409_without_an_id_still_raises(self):
        recorder = Recorder(
            [
                ("/search", httpx.Response(200, json={"results": []})),
                ("/contacts", httpx.Response(409, json={"message": "nope"})),
            ]
        )
        with pytest.raises(UpstreamError):
            await hubspot_client(recorder).upsert_contact(make_lead())

    async def test_empty_properties_are_not_sent(self):
        recorder = Recorder(
            [
                ("/search", httpx.Response(200, json={"results": []})),
                ("/contacts", httpx.Response(201, json={"id": "1"})),
            ]
        )
        lead = make_lead(name=None, company=None, phone=None, website=None,
                         email="anon@gmail.com")
        await hubspot_client(recorder).upsert_contact(lead)
        sent = json.loads(recorder.requests[-1].content)["properties"]
        assert "firstname" not in sent
        assert "phone" not in sent
        assert sent["email"] == "anon@gmail.com"


class TestHubSpotNotes:
    async def test_note_is_associated_to_the_contact(self):
        recorder = Recorder([("/notes", httpx.Response(201, json={"id": "n-1"}))])
        note = await hubspot_client(recorder).add_note("77", "<p>body</p>")
        assert note.id == "n-1"
        assert note.contact_id == "77"

        payload = json.loads(recorder.requests[-1].content)
        association = payload["associations"][0]
        assert association["to"]["id"] == "77"
        assert (
            association["types"][0]["associationTypeId"] == NOTE_TO_CONTACT_TYPE_ID
        )
        assert payload["properties"]["hs_timestamp"].endswith("Z")


class TestNoteRendering:
    async def test_states_provenance_and_does_not_claim_the_reply_was_sent(self):
        lead = make_lead()
        classification = await MockLLMProvider().classify(lead)
        body = render_note(lead, classification)

        assert "mock" in body
        assert "degraded" in body
        assert "not sent automatically" in body
        assert classification.summary in body

    async def test_intake_warnings_are_surfaced(self):
        lead = make_lead(phone="garbage")
        classification = await MockLLMProvider().classify(lead)
        assert "Intake warnings" in render_note(lead, classification)


class TestMockCRM:
    async def test_upsert_matches_hubspot_semantics(self):
        crm = MockCRMClient()
        first = await crm.upsert_contact(make_lead())
        second = await crm.upsert_contact(make_lead(message="different"))
        assert first.created is True
        assert second.created is False
        assert first.id == second.id

    async def test_notes_are_retrievable_for_assertions(self):
        crm = MockCRMClient()
        contact = await crm.upsert_contact(make_lead())
        await crm.add_note(contact.id, "<p>hello</p>")
        assert crm.note_bodies[contact.id] == ["<p>hello</p>"]


class TestRegistry:
    def test_falls_back_to_mock_without_a_token(self):
        settings = Settings(hubspot_token="", crm_provider="auto")
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200))
        )
        assert CRMRegistry(settings, client).active_name() == "mock"

    def test_uses_hubspot_when_configured(self):
        settings = Settings(hubspot_token="t", crm_provider="auto")
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200))
        )
        assert CRMRegistry(settings, client).active_name() == "hubspot"

    def test_availability_explains_the_negative_case(self):
        settings = Settings(hubspot_token="", crm_provider="auto")
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200))
        )
        rows = CRMRegistry(settings, client).availability()
        hubspot_row = next(r for r in rows if r["provider"] == "hubspot")
        assert hubspot_row["available"] is False
        assert "HUBSPOT_TOKEN" in hubspot_row["reason"]
