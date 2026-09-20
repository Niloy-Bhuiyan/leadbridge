"""HubSpot CRM v3 client.

Upsert is search-then-write rather than blind-create, because HubSpot
rejects a duplicate email with a 409 and the pipeline must be safe to
re-run. The 409 path is still handled: between the search and the create,
a concurrent run can win the race, and the error body carries the id that
was created. Parsing it is cheaper and more correct than failing.

Scopes required on the private app token:
    crm.objects.contacts.read
    crm.objects.contacts.write
    crm.objects.notes.write
"""

import re
from datetime import UTC, datetime
from typing import Any

import httpx

from ..config import Settings
from ..http import RetryPolicy, UpstreamError, request_with_retry
from ..models import CRMContact, CRMNote, NormalizedLead

# HubSpot's built-in association type for note -> contact.
NOTE_TO_CONTACT_TYPE_ID = 202

EXISTING_ID_RE = re.compile(r"Existing ID:\s*(\d+)")


class HubSpotClient:
    name = "hubspot"

    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._client = client
        self._policy = RetryPolicy(
            max_attempts=settings.http_max_attempts,
            base=settings.http_backoff_base,
            cap=settings.http_backoff_cap,
        )

    def available(self) -> tuple[bool, str]:
        if not self._settings.hubspot_token:
            return False, "HUBSPOT_TOKEN is not set"
        return True, "private app token configured"

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._settings.hubspot_token}",
            "Content-Type": "application/json",
        }

    def _url(self, path: str) -> str:
        return f"{self._settings.hubspot_base_url}{path}"

    @staticmethod
    def _properties(lead: NormalizedLead) -> dict[str, str]:
        """Map onto HubSpot's default contact properties only.

        Custom properties would have to exist in the portal first, and a
        workflow that only runs after manual portal setup is a workflow
        that does not run. Everything derived lives in the note instead.
        """
        first, last = "", ""
        if lead.name:
            parts = lead.name.split()
            first = parts[0]
            last = " ".join(parts[1:])

        props = {
            "email": lead.email,
            "firstname": first,
            "lastname": last,
            "company": lead.company or "",
            "phone": lead.phone_e164 or "",
            "website": lead.website or "",
            "hs_lead_status": "NEW",
        }
        return {k: v for k, v in props.items() if v}

    async def _find_by_email(self, email: str) -> str | None:
        response = await request_with_retry(
            self._client,
            "POST",
            self._url("/crm/v3/objects/contacts/search"),
            service="hubspot",
            policy=self._policy,
            expected=(200,),
            headers=self._headers,
            json={
                "filterGroups": [
                    {
                        "filters": [
                            {
                                "propertyName": "email",
                                "operator": "EQ",
                                "value": email,
                            }
                        ]
                    }
                ],
                "properties": ["email"],
                "limit": 1,
            },
        )
        results = response.json().get("results") or []
        return str(results[0]["id"]) if results else None

    async def upsert_contact(self, lead: NormalizedLead) -> CRMContact:
        properties = self._properties(lead)
        existing_id = await self._find_by_email(lead.email)

        if existing_id:
            response = await request_with_retry(
                self._client,
                "PATCH",
                self._url(f"/crm/v3/objects/contacts/{existing_id}"),
                service="hubspot",
                policy=self._policy,
                expected=(200,),
                headers=self._headers,
                json={"properties": properties},
            )
            return CRMContact(
                id=existing_id,
                email=lead.email,
                created=False,
                properties=response.json().get("properties", {}),
                provider=self.name,
            )

        try:
            response = await request_with_retry(
                self._client,
                "POST",
                self._url("/crm/v3/objects/contacts"),
                service="hubspot",
                policy=self._policy,
                expected=(201,),
                headers=self._headers,
                json={"properties": properties},
            )
        except UpstreamError as exc:
            # Lost the race. Recover the id HubSpot names in the error.
            if exc.status_code == 409 and (m := EXISTING_ID_RE.search(exc.body)):
                return CRMContact(
                    id=m.group(1),
                    email=lead.email,
                    created=False,
                    properties=properties,
                    provider=self.name,
                )
            raise

        body = response.json()
        return CRMContact(
            id=str(body["id"]),
            email=lead.email,
            created=True,
            properties=body.get("properties", {}),
            provider=self.name,
        )

    async def add_note(self, contact_id: str, body: str) -> CRMNote:
        timestamp = datetime.now(UTC).isoformat(timespec="milliseconds")
        payload: dict[str, Any] = {
            "properties": {
                "hs_note_body": body,
                "hs_timestamp": timestamp.replace("+00:00", "Z"),
            },
            "associations": [
                {
                    "to": {"id": contact_id},
                    "types": [
                        {
                            "associationCategory": "HUBSPOT_DEFINED",
                            "associationTypeId": NOTE_TO_CONTACT_TYPE_ID,
                        }
                    ],
                }
            ],
        }
        response = await request_with_retry(
            self._client,
            "POST",
            self._url("/crm/v3/objects/notes"),
            service="hubspot",
            policy=self._policy,
            expected=(201,),
            headers=self._headers,
            json=payload,
        )
        return CRMNote(
            id=str(response.json()["id"]),
            contact_id=contact_id,
            provider=self.name,
        )
