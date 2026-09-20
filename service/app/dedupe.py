"""Deduplication. Pure: the caller supplies what is already known.

Three layers, cheapest and most certain first:

  1. fingerprint  exact identity. A retried webhook hits this.
  2. email        same person, different wording. Still one lead.
  3. fuzzy        same company+name after suffix stripping. A candidate
                  match only -- reported with `matched_on="fuzzy"` so a
                  human can see why something was suppressed.

Layer 3 is deliberately the last resort. It will occasionally collide two
genuinely different people at the same company with similar names, and that
is the tradeoff: an agency would rather merge two leads and split them later
than double-contact a prospect.
"""

from collections.abc import Iterable

from .models import DedupeResponse, NormalizedLead


def find_duplicate(
    lead: NormalizedLead, existing: Iterable[NormalizedLead]
) -> DedupeResponse:
    by_fingerprint: dict[str, str] = {}
    by_email: dict[str, str] = {}
    by_key: dict[str, str] = {}

    for other in existing:
        by_fingerprint.setdefault(other.fingerprint, other.fingerprint)
        by_email.setdefault(other.email, other.fingerprint)
        # An empty fuzzy key means "no company and no name" -- matching on
        # that would collide every anonymous lead into one.
        if other.dedupe_key.strip("|"):
            by_key.setdefault(other.dedupe_key, other.fingerprint)

    if lead.fingerprint in by_fingerprint:
        return DedupeResponse(
            duplicate=True,
            matched_on="fingerprint",
            matched_fingerprint=by_fingerprint[lead.fingerprint],
        )
    if lead.email in by_email:
        return DedupeResponse(
            duplicate=True,
            matched_on="email",
            matched_fingerprint=by_email[lead.email],
        )
    if lead.dedupe_key.strip("|") and lead.dedupe_key in by_key:
        return DedupeResponse(
            duplicate=True,
            matched_on="fuzzy",
            matched_fingerprint=by_key[lead.dedupe_key],
        )
    return DedupeResponse(duplicate=False)


def dedupe_batch(
    incoming: Iterable[NormalizedLead], existing: list[NormalizedLead] | None = None
) -> tuple[list[NormalizedLead], list[tuple[NormalizedLead, DedupeResponse]]]:
    """Deduplicate a batch against prior state AND against itself, since a
    single CSV import routinely contains the same person twice."""
    seen: list[NormalizedLead] = list(existing or [])
    kept: list[NormalizedLead] = []
    dropped: list[tuple[NormalizedLead, DedupeResponse]] = []

    for lead in incoming:
        verdict = find_duplicate(lead, seen)
        if verdict.duplicate:
            dropped.append((lead, verdict))
            continue
        kept.append(lead)
        seen.append(lead)
    return kept, dropped
