from app.dedupe import dedupe_batch, find_duplicate

from .conftest import make_lead


def test_no_duplicate_against_empty_history():
    assert find_duplicate(make_lead(), []).duplicate is False


def test_identical_submission_matches_on_fingerprint():
    lead = make_lead()
    verdict = find_duplicate(lead, [lead])
    assert verdict.duplicate is True
    assert verdict.matched_on == "fingerprint"


def test_same_person_different_message_matches_on_email():
    first = make_lead(message="We need a CRM workflow.")
    second = make_lead(message="Actually, also an SEO audit please.")
    assert first.fingerprint != second.fingerprint
    verdict = find_duplicate(second, [first])
    assert verdict.duplicate is True
    assert verdict.matched_on == "email"


def test_legal_suffix_variation_matches_on_fuzzy_key():
    first = make_lead(email="ayesha@one.example", company="Octopi Digital Ltd.")
    second = make_lead(email="a.rahman@two.example", company="octopi digital limited")
    verdict = find_duplicate(second, [first])
    assert verdict.duplicate is True
    assert verdict.matched_on == "fuzzy"


def test_anonymous_leads_do_not_all_collide():
    """The empty fuzzy key must not act as a match, or every lead with no
    company and no name would be suppressed as a duplicate of the first."""
    first = make_lead(email="a@gmail.com", company=None, name=None)
    second = make_lead(email="b@gmail.com", company=None, name=None)
    assert first.dedupe_key.strip("|") == ""
    assert find_duplicate(second, [first]).duplicate is False


def test_batch_deduplicates_against_itself():
    leads = [
        make_lead(),
        make_lead(),
        make_lead(email="rafi@other.example", company="Other Firm", name="Rafi Khan"),
    ]
    kept, dropped = dedupe_batch(leads)
    assert len(kept) == 2
    assert len(dropped) == 1
    assert dropped[0][1].matched_on == "fingerprint"


def test_batch_catches_a_colleague_with_the_same_company_and_name():
    """A different email at the same company with the same contact name is
    caught by the fuzzy layer. This is the deliberate over-match documented
    in dedupe.py: merging two leads is cheaper than double-contacting one
    prospect, and `matched_on` makes the reason visible."""
    leads = [make_lead(), make_lead(email="different@address.example")]
    kept, dropped = dedupe_batch(leads)
    assert len(kept) == 1
    assert dropped[0][1].matched_on == "fuzzy"


def test_batch_respects_prior_history():
    existing = make_lead()
    kept, dropped = dedupe_batch([make_lead()], existing=[existing])
    assert kept == []
    assert len(dropped) == 1
