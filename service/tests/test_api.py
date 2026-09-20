"""End-to-end tests over the HTTP surface, fully offline.

These run with LLM_PROVIDER=mock and CRM_PROVIDER=mock, so the whole
pipeline is exercised with no API keys and no network. That is the property
that lets someone review this repo without being given credentials.
"""

import pytest

VALID_LEAD = {
    "email": "Ayesha@Octopi-Digital.Example",
    "name": "AYESHA RAHMAN",
    "company": "Octopi Digital Ltd.",
    "phone": "01868686062",
    "message": "We need a CRM automation workflow and a technical SEO audit.",
    "source": "webhook",
}


class TestOps:
    def test_health_reports_the_active_providers(self, client):
        body = client.get("/ops/health").json()
        assert body["status"] == "ok"
        assert body["llm_active"] == "mock"
        assert body["crm_active"] == "mock"
        assert body["auth"] == "open"

    def test_availability_explains_every_provider(self, client):
        body = client.get("/ops/availability").json()
        assert body["llm"]["active"] == "mock"
        assert body["crm"]["active"] == "mock"
        providers = {p["provider"]: p for p in body["crm"]["providers"]}
        assert providers["hubspot"]["available"] is False
        assert "HUBSPOT_TOKEN" in providers["hubspot"]["reason"]

    def test_unknown_run_is_404(self, client):
        assert client.get("/ops/runs/nope").status_code == 404


class TestNormalizeEndpoint:
    def test_normalizes_and_derives_keys(self, client):
        body = client.post("/leads/normalize", json=VALID_LEAD).json()
        lead = body["lead"]
        assert lead["email"] == "ayesha@octopi-digital.example"
        assert lead["name"] == "Ayesha Rahman"
        assert lead["phone_e164"] == "+8801868686062"
        assert lead["fingerprint"]
        assert lead["dedupe_key"]

    def test_invalid_email_is_422(self, client):
        response = client.post("/leads/normalize", json={"email": "nope"})
        assert response.status_code == 422

    def test_missing_email_is_422_from_the_schema(self, client):
        assert client.post("/leads/normalize", json={"name": "x"}).status_code == 422


class TestPipeline:
    def test_completes_offline_end_to_end(self, client):
        response = client.post(
            "/leads/pipeline",
            json={"lead": VALID_LEAD, "run_seo_audit": False},
        )
        assert response.status_code == 200
        body = response.json()

        assert body["status"] == "completed"
        assert body["classification"]["provider"] == "mock"
        assert body["contact"]["created"] is True
        assert body["note"]["provider"] == "mock"
        assert body["run_id"]

        steps = {s["step"]: s["status"] for s in body["steps"]}
        assert steps["normalize"] == "ok"
        assert steps["dedupe"] == "ok"
        assert steps["classify"] == "degraded"  # mock always declares itself
        assert steps["crm"] == "ok"

    def test_degraded_is_propagated_to_the_response(self, client):
        body = client.post(
            "/leads/pipeline", json={"lead": VALID_LEAD, "run_seo_audit": False}
        ).json()
        assert body["degraded"] is True

    def test_resubmitting_the_same_lead_is_skipped_not_duplicated(self, client):
        payload = {"lead": VALID_LEAD, "run_seo_audit": False}
        first = client.post("/leads/pipeline", json=payload).json()
        second = client.post("/leads/pipeline", json=payload).json()

        assert first["status"] == "completed"
        assert second["status"] == "skipped_duplicate"
        assert second["duplicate"]["matched_on"] == "fingerprint"
        assert second["contact"] is None
        assert second["run_id"] != first["run_id"]

    def test_a_skipped_duplicate_is_still_recorded_as_a_run(self, client):
        payload = {"lead": VALID_LEAD, "run_seo_audit": False}
        client.post("/leads/pipeline", json=payload)
        client.post("/leads/pipeline", json=payload)

        runs = client.get("/ops/runs").json()
        statuses = [r["status"] for r in runs["runs"]]
        assert "completed" in statuses
        assert "skipped_duplicate" in statuses
        assert runs["stats"]["leads"] == 1

    def test_invalid_lead_fails_the_run_and_records_why(self, client):
        response = client.post(
            "/leads/pipeline", json={"lead": {"email": "broken"}, "run_seo_audit": False}
        )
        assert response.status_code == 422
        assert response.json()["detail"]["step"] == "normalize"

        runs = client.get("/ops/runs").json()["runs"]
        assert runs[0]["status"] == "failed"
        assert runs[0]["summary"]

    def test_run_detail_lists_every_step(self, client):
        run_id = client.post(
            "/leads/pipeline", json={"lead": VALID_LEAD, "run_seo_audit": False}
        ).json()["run_id"]

        detail = client.get(f"/ops/runs/{run_id}").json()
        assert detail["status"] == "completed"
        assert [s["step"] for s in detail["steps"]] == [
            "normalize",
            "dedupe",
            "classify",
            "crm",
        ]

    def test_seo_audit_is_skipped_when_the_lead_has_no_website(self, client):
        body = client.post(
            "/leads/pipeline", json={"lead": VALID_LEAD, "run_seo_audit": True}
        ).json()
        step = next(s for s in body["steps"] if s["step"] == "seo_audit")
        assert step["status"] == "skipped"
        assert step["detail"]["reason"] == "no website on lead"

    def test_crm_can_be_disabled(self, client):
        body = client.post(
            "/leads/pipeline",
            json={"lead": VALID_LEAD, "run_seo_audit": False, "push_to_crm": False},
        ).json()
        assert body["contact"] is None
        step = next(s for s in body["steps"] if s["step"] == "crm")
        assert step["status"] == "skipped"


class TestStageEndpoints:
    def test_classify_returns_a_labelled_mock_verdict(self, client):
        lead = client.post("/leads/normalize", json=VALID_LEAD).json()["lead"]
        body = client.post("/leads/classify", json={"lead": lead}).json()
        assert body["provider"] == "mock"
        assert body["degraded"] is True
        assert body["intent"] == "new_project"

    def test_dedupe_endpoint_checks_the_supplied_batch(self, client):
        lead = client.post("/leads/normalize", json=VALID_LEAD).json()["lead"]
        body = client.post(
            "/leads/dedupe", json={"lead": lead, "against": [lead]}
        ).json()
        assert body["duplicate"] is True
        assert body["matched_on"] == "fingerprint"

    def test_crm_upsert_requires_a_classification(self, client):
        lead = client.post("/leads/normalize", json=VALID_LEAD).json()["lead"]
        response = client.post("/leads/crm-upsert", json={"lead": lead})
        assert response.status_code == 422


class TestSEOEndpoint:
    def test_rejects_an_unusable_url(self, client):
        assert client.post("/seo/audit", json={"url": "not a url"}).status_code == 422

    def test_accepts_a_bare_host(self, client, monkeypatch):
        """A bare host must be coerced rather than rejected; the audit itself
        is not run here because it would reach the network."""
        from app.models import CoreWebVitals, RobotsReport, SEOAudit, SitemapReport

        async def fake_audit(http_client, request, settings):
            assert request.url == "https://example.test/"
            return SEOAudit(
                url=request.url,
                score=100,
                robots=RobotsReport(found=True),
                sitemap=SitemapReport(found=True, url_count=1),
                vitals=CoreWebVitals(available=False, reason="stubbed"),
            )

        monkeypatch.setattr("app.routers.seo.run_audit", fake_audit)
        body = client.post("/seo/audit", json={"url": "example.test"}).json()
        assert body["url"] == "https://example.test/"


class TestSecretGuard:
    @pytest.fixture
    def secured_client(self, tmp_path, monkeypatch):
        from app import config

        monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "s.db"))
        monkeypatch.setenv("LLM_PROVIDER", "mock")
        monkeypatch.setenv("CRM_PROVIDER", "mock")
        monkeypatch.setenv("INGEST_SECRET", "s3cret")
        config.get_settings.cache_clear()

        from fastapi.testclient import TestClient

        from app.main import app

        with TestClient(app) as c:
            yield c
        config.get_settings.cache_clear()

    def test_mutating_endpoints_require_the_header(self, secured_client):
        assert secured_client.post(
            "/leads/normalize", json=VALID_LEAD
        ).status_code == 401

    def test_correct_secret_is_accepted(self, secured_client):
        response = secured_client.post(
            "/leads/normalize",
            json=VALID_LEAD,
            headers={"X-Leadbridge-Secret": "s3cret"},
        )
        assert response.status_code == 200

    def test_ops_stays_readable_for_diagnosis(self, secured_client):
        body = secured_client.get("/ops/health").json()
        assert body["auth"] == "enforced"
