import os

import pytest


@pytest.fixture(autouse=True)
def no_local_credentials(monkeypatch):
    """Make the suite hermetic.

    Settings reads a real .env, so a developer with working credentials
    would run a different test suite from CI -- and the difference shows up
    as a test that passes on one machine and fails on the other. Every
    credential is blanked for every test; the ones that want a key set it
    explicitly on their own Settings instance.
    """
    for name in ("HUBSPOT_TOKEN", "ANTHROPIC_API_KEY", "PAGESPEED_API_KEY",
                 "INGEST_SECRET"):
        monkeypatch.setenv(name, "")


@pytest.fixture
def offline_env(tmp_path, monkeypatch):
    """Force the fully offline configuration: mock LLM, mock CRM, temp DB."""
    from app import config

    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("CRM_PROVIDER", "mock")
    monkeypatch.setenv("INGEST_SECRET", "")
    monkeypatch.setenv("ENVIRONMENT", "ci")
    # Settings are cached per process; the cache must be dropped so each
    # test observes its own environment rather than the first test's.
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


@pytest.fixture
def client(offline_env):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def store(tmp_path):
    from app.db import Store

    s = Store(str(tmp_path / "store.db"))
    yield s
    s.close()


@pytest.fixture
def settings(tmp_path, monkeypatch):
    from app.config import Settings

    return Settings(database_path=str(tmp_path / "s.db"), environment="ci")


def make_lead(**overrides):
    """A valid NormalizedLead for tests that do not exercise normalization."""
    from app.models import RawLead
    from app.normalize import normalize_lead

    payload = {
        "email": "ayesha@octopi-digital.example",
        "name": "Ayesha Rahman",
        "company": "Octopi Digital Ltd.",
        "phone": "01868686062",
        "website": "octopi-digital.example",
        "message": "We need a CRM automation workflow and an SEO audit.",
        "source": "webhook",
    }
    payload.update(overrides)
    lead, errors = normalize_lead(RawLead(**payload))
    assert lead is not None, errors
    return lead


os.environ.setdefault("PYTHONHASHSEED", "0")
