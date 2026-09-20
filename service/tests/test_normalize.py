import pytest

from app.models import RawLead
from app.normalize import (
    fingerprint,
    fuzzy_key,
    normalize_email,
    normalize_lead,
    normalize_name,
    normalize_phone_bd,
    normalize_website,
)


class TestEmail:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("  Ayesha@Example.COM ", "ayesha@example.com"),
            ("Ayesha Rahman <a.rahman@firm.co.uk>", "a.rahman@firm.co.uk"),
            ("first+tag@sub.domain.org", "first+tag@sub.domain.org"),
        ],
    )
    def test_accepts_and_folds(self, raw, expected):
        email, error = normalize_email(raw)
        assert error is None
        assert email == expected

    @pytest.mark.parametrize("raw", ["", "   ", "not-an-email", "a@b", "a@@b.com"])
    def test_rejects_invalid(self, raw):
        email, error = normalize_email(raw)
        assert email is None
        assert error


class TestPhone:
    @pytest.mark.parametrize(
        "raw",
        ["01868686062", "1868686062", "8801868686062", "+880 1868-686062",
         "+8801868686062"],
    )
    def test_bd_forms_converge(self, raw):
        phone, warning = normalize_phone_bd(raw)
        assert warning is None
        assert phone == "+8801868686062"

    def test_foreign_number_passes_through_unmangled(self):
        phone, warning = normalize_phone_bd("+1 505 555 0142")
        assert warning is None
        assert phone == "+15055550142"

    @pytest.mark.parametrize("raw", ["12345", "0123", "abc", "019"])
    def test_unrecognizable_warns_without_raising(self, raw):
        phone, warning = normalize_phone_bd(raw)
        assert phone is None
        assert warning

    def test_none_is_not_a_warning(self):
        assert normalize_phone_bd(None) == (None, None)


class TestWebsite:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("octopi-digital.com", "https://octopi-digital.com/"),
            ("HTTP://Example.COM/path", "http://example.com/path"),
            ("https://a.co/x?utm=1", "https://a.co/x"),
        ],
    )
    def test_coerces_to_origin(self, raw, expected):
        url, error = normalize_website(raw)
        assert error is None
        assert url == expected

    @pytest.mark.parametrize("raw", ["ftp://a.com", "localhost", "just text"])
    def test_rejects_unusable(self, raw):
        url, error = normalize_website(raw)
        assert url is None
        assert error


class TestName:
    def test_recases_shouting_and_whispering(self):
        assert normalize_name("AYESHA RAHMAN") == "Ayesha Rahman"
        assert normalize_name("ayesha  rahman") == "Ayesha Rahman"

    def test_preserves_deliberate_mixed_case(self):
        assert normalize_name("Ayesha van der Berg") == "Ayesha van der Berg"
        assert normalize_name("Ronan McDonald") == "Ronan McDonald"


class TestFuzzyKey:
    def test_legal_suffixes_collide(self):
        assert fuzzy_key("Octopi Digital Ltd.", "A B") == fuzzy_key(
            "octopi digital limited", "a b"
        )

    def test_bengali_survives(self):
        key = fuzzy_key("অমার কোম্পানি", None)
        assert key.strip("|")

    def test_distinct_companies_do_not_collide(self):
        assert fuzzy_key("Octopi Digital", "A") != fuzzy_key("Octopus Media", "A")


class TestFingerprint:
    def test_stable_across_whitespace_and_case(self):
        a = fingerprint("a@b.com", "Acme Ltd", "We  need\nhelp")
        b = fingerprint("a@b.com", "acme ltd", "we need help")
        assert a == b

    def test_different_message_is_a_different_lead(self):
        a = fingerprint("a@b.com", "Acme", "project one")
        b = fingerprint("a@b.com", "Acme", "project two")
        assert a != b


class TestNormalizeLead:
    def test_bad_phone_does_not_discard_a_valid_lead(self):
        lead, errors = normalize_lead(
            RawLead(email="a@firm.com", phone="nonsense", message="hi")
        )
        assert errors == []
        assert lead is not None
        assert lead.phone_e164 is None
        assert any("phone" in w for w in lead.warnings)

    def test_invalid_email_is_fatal(self):
        lead, errors = normalize_lead(RawLead(email="nope"))
        assert lead is None
        assert errors

    def test_company_inferred_from_corporate_domain(self):
        lead, _ = normalize_lead(RawLead(email="a@octopi-digital.com"))
        assert lead is not None
        assert lead.company == "Octopi Digital"
        assert lead.free_email is False
        assert any("inferred" in w for w in lead.warnings)

    def test_free_email_does_not_infer_a_company(self):
        lead, _ = normalize_lead(RawLead(email="someone@gmail.com"))
        assert lead is not None
        assert lead.company is None
        assert lead.free_email is True

    def test_submitted_at_defaults_to_now_and_is_aware(self):
        lead, _ = normalize_lead(RawLead(email="a@b.com"))
        assert lead is not None
        assert lead.submitted_at.tzinfo is not None
