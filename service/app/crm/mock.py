"""In-memory CRM twin.

It implements the same upsert semantics as the HubSpot client -- match on
email, update rather than duplicate, report whether the contact was created
-- so a test that passes here is testing the pipeline's contract and not a
stub that always says yes.

It is labelled at every boundary: provider="mock" rides on every object it
returns, and /ops/availability names it as the active CRM. Nothing written
here reaches a real portal, and the README says so.
"""

import itertools

from ..models import CRMContact, CRMNote, NormalizedLead


class MockCRMClient:
    name = "mock"

    def __init__(self) -> None:
        self._contacts: dict[str, CRMContact] = {}
        self.notes: list[CRMNote] = []
        self.note_bodies: dict[str, list[str]] = {}
        self._ids = itertools.count(1)

    def available(self) -> tuple[bool, str]:
        return True, "in-memory CRM, writes nothing to a real portal"

    def reset(self) -> None:
        self._contacts.clear()
        self.notes.clear()
        self.note_bodies.clear()

    async def upsert_contact(self, lead: NormalizedLead) -> CRMContact:
        properties = {
            "email": lead.email,
            "company": lead.company or "",
            "phone": lead.phone_e164 or "",
            "website": lead.website or "",
        }
        existing = self._contacts.get(lead.email)
        if existing is not None:
            updated = existing.model_copy(
                update={"created": False, "properties": properties}
            )
            self._contacts[lead.email] = updated
            return updated

        contact = CRMContact(
            id=f"mock-{next(self._ids)}",
            email=lead.email,
            created=True,
            properties=properties,
            provider=self.name,
        )
        self._contacts[lead.email] = contact
        return contact

    async def add_note(self, contact_id: str, body: str) -> CRMNote:
        note = CRMNote(
            id=f"mock-note-{len(self.notes) + 1}",
            contact_id=contact_id,
            provider=self.name,
        )
        self.notes.append(note)
        self.note_bodies.setdefault(contact_id, []).append(body)
        return note
