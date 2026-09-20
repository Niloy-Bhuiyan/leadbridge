"""Lead normalization. Pure functions, no I/O, so every branch is testable
without a network or a database.

The output carries two derived keys that the rest of the pipeline depends on:

  fingerprint  sha256 over the identity-bearing fields. Stable across
               retries, which is what makes the whole pipeline idempotent --
               n8n retrying a webhook must not create a second contact.

  dedupe_key   lowercased, punctuation-stripped `company|name`, with legal
               suffixes removed, so "Octopi Digital Ltd." and "octopi
               digital" collide. Deliberately lossy; it is a candidate
               generator, not an identity.
"""

import hashlib
import re
import unicodedata
from datetime import UTC, datetime
from urllib.parse import urlparse, urlunparse

from .models import NormalizedLead, RawLead

# Intentionally permissive. Real RFC 5322 is not worth implementing, and a
# stricter pattern rejects addresses that deliver fine.
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+(?:\.[^@\s.]+)+$")

FREE_EMAIL_DOMAINS = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "yahoo.com",
        "yahoo.co.uk",
        "ymail.com",
        "hotmail.com",
        "outlook.com",
        "live.com",
        "msn.com",
        "aol.com",
        "icloud.com",
        "me.com",
        "proton.me",
        "protonmail.com",
        "mail.com",
        "gmx.com",
        "zoho.com",
        "yandex.com",
    }
)

LEGAL_SUFFIX_RE = re.compile(
    r"\b(ltd|limited|inc|incorporated|llc|llp|plc|gmbh|pvt|pte|co|corp|"
    r"corporation|company|holdings|group|agency|studio|solutions)\b\.?",
    re.IGNORECASE,
)

# Keep Bengali codepoints: a Bangladeshi company name written in Bangla must
# survive the fuzzy key rather than collapsing to an empty string.
NON_KEY_CHARS_RE = re.compile(r"[^a-z0-9ঀ-৿]+")

WHITESPACE_RE = re.compile(r"\s+")


def _collapse(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = WHITESPACE_RE.sub(" ", value).strip()
    return cleaned or None


def normalize_email(raw: str) -> tuple[str | None, str | None]:
    """Return (email, error). Case-folded; the local part is case-sensitive
    per spec but no mail provider in practice treats it that way, and
    treating it as such would break deduplication."""
    candidate = (raw or "").strip().lower()
    if not candidate:
        return None, "email is empty"
    # Tolerate a pasted "Name <a@b.com>" form, which intake forms do produce.
    bracketed = re.search(r"<([^>]+)>", candidate)
    if bracketed:
        candidate = bracketed.group(1).strip()
    if not EMAIL_RE.match(candidate):
        return None, f"email is not a valid address: {raw!r}"
    return candidate, None


def normalize_phone_bd(raw: str | None) -> tuple[str | None, str | None]:
    """Normalize a Bangladeshi mobile number to E.164, or return a warning.

    Accepts 01868686062, 1868686062, 8801868686062, +880 1868-686062.
    Anything already in +<country> form for another country is passed
    through untouched rather than mangled into a wrong number.
    """
    if not raw:
        return None, None
    digits = re.sub(r"[^\d+]", "", raw)
    if not digits:
        return None, f"phone had no digits: {raw!r}"

    if digits.startswith("+"):
        if digits.startswith("+880"):
            digits = digits[1:]
        else:
            # Foreign number. Out of scope to validate; keep it verbatim.
            return digits, None

    if digits.startswith("880"):
        national = digits[3:]
    elif digits.startswith("0"):
        national = digits[1:]
    else:
        national = digits

    # BD mobile: 10 digits, starts with 1, operator prefix 13-19.
    if len(national) == 10 and national.startswith("1") and national[1] in "3456789":
        return f"+880{national}", None
    return None, f"phone is not a recognizable BD mobile number: {raw!r}"


def normalize_website(raw: str | None) -> tuple[str | None, str | None]:
    """Coerce to an absolute http(s) origin. Intake forms commonly receive
    a bare host, which urlparse reads as a path rather than a netloc."""
    if not raw:
        return None, None
    candidate = raw.strip()
    if not candidate:
        return None, None
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", candidate):
        candidate = f"https://{candidate}"
    parsed = urlparse(candidate)
    if parsed.scheme not in ("http", "https"):
        return None, f"website scheme is not http(s): {raw!r}"
    if not parsed.netloc or "." not in parsed.netloc:
        return None, f"website has no usable host: {raw!r}"
    normalized = urlunparse(
        (parsed.scheme, parsed.netloc.lower(), parsed.path or "/", "", "", "")
    )
    return normalized, None


def normalize_name(raw: str | None) -> str | None:
    collapsed = _collapse(raw)
    if not collapsed:
        return None
    # Only re-case if the input is shouting or whispering; preserve names
    # like "van der Berg" or "McDonald" that are already mixed case.
    if collapsed.isupper() or collapsed.islower():
        return " ".join(part.capitalize() for part in collapsed.split(" "))
    return collapsed


def fuzzy_key(company: str | None, name: str | None) -> str:
    def clean(value: str | None) -> str:
        if not value:
            return ""
        folded = unicodedata.normalize("NFKD", value).casefold()
        folded = LEGAL_SUFFIX_RE.sub(" ", folded)
        folded = NON_KEY_CHARS_RE.sub(" ", folded)
        return WHITESPACE_RE.sub(" ", folded).strip()

    return f"{clean(company)}|{clean(name)}"


def fingerprint(email: str, company: str | None, message: str | None) -> str:
    """Identity for idempotency. Message is included because the same person
    legitimately enquires twice about different projects, and those are two
    leads, not one."""
    payload = "\x1f".join(
        [
            email,
            (company or "").casefold().strip(),
            WHITESPACE_RE.sub(" ", (message or "")).strip().casefold(),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def normalize_lead(raw: RawLead) -> tuple[NormalizedLead | None, list[str]]:
    """Returns (lead, errors). Errors are fatal; warnings ride on the lead.

    The split matters operationally: a bad phone number must not discard a
    lead that has a valid email, because the email is the thing the agency
    can actually act on.
    """
    errors: list[str] = []
    warnings: list[str] = []

    email, email_error = normalize_email(raw.email)
    if email_error:
        errors.append(email_error)

    phone, phone_warning = normalize_phone_bd(raw.phone)
    if phone_warning:
        warnings.append(phone_warning)

    website, website_warning = normalize_website(raw.website)
    if website_warning:
        warnings.append(website_warning)

    if errors or email is None:
        return None, errors

    company = _collapse(raw.company)
    name = normalize_name(raw.name)
    message = _collapse(raw.message)
    domain = email.split("@", 1)[1]
    free_email = domain in FREE_EMAIL_DOMAINS

    if not company and not free_email:
        # A corporate domain with no company field is recoverable: the
        # domain is a better company guess than nothing.
        company = domain.rsplit(".", 1)[0].replace("-", " ").title()
        warnings.append(f"company inferred from email domain: {company}")

    submitted_at = raw.submitted_at or datetime.now(UTC)
    if submitted_at.tzinfo is None:
        submitted_at = submitted_at.replace(tzinfo=UTC)

    lead = NormalizedLead(
        email=email,
        email_domain=domain,
        name=name,
        company=company,
        phone_e164=phone,
        website=website,
        message=message,
        source=raw.source or "webhook",
        submitted_at=submitted_at,
        fingerprint=fingerprint(email, company, message),
        dedupe_key=fuzzy_key(company, name),
        free_email=free_email,
        warnings=warnings,
    )
    return lead, []
