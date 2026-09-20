# LeadBridge

**Lead intake, AI triage, CRM upsert and technical SEO audit — orchestrated in n8n, with the logic in a tested Python service.**

An agency receives an enquiry. Something has to clean it up, decide whether it has
been seen before, work out what the person actually wants, file it in the CRM, and
— if they left a website — audit that site and attach the findings to their record
before anyone picks up the phone.

LeadBridge does that, and is explicit about which parts are real.

```
POST /webhook/leadbridge-intake
        │
        ▼
   ┌─────────────────────────── n8n ───────────────────────────┐
   │  Normalize → Dedupe → Already seen? ──yes──► respond      │
   │                            │no                            │
   │                       Classify (LLM)                      │
   │                            ▼                              │
   │                       CRM Upsert ──error──► respond 502   │
   │                            ▼                              │
   │                      Has website? ──yes──► SEO Audit      │
   │                            ▼                              │
   │                      Respond accepted                     │
   └───────────────────────────────────────────────────────────┘
        │  every step is an HTTP call to:
        ▼
   FastAPI service ──► HubSpot CRM      (or in-memory CRM)
                  ──► Anthropic API      (or offline rules)
                  ──► PageSpeed Insights (optional key)
                  ──► SQLite run log
```

## Why the work is split this way

n8n owns **orchestration**: scheduling, branching, retries, fan-out, and the visual
record of what ran. Those are the things an agency needs to be able to change without
a deploy, and they are legible on a canvas.

The Python service owns **logic**: normalization, deduplication, provider fallback,
and the audit rules. Those need tests, a diff history, and code review. Expressing
them as thirty chained Code nodes would make them unreviewable.

The seam between the two is plain HTTP with a shared secret.

## Quick start

Runs fully offline. No accounts, no API keys.

```bash
git clone https://github.com/Niloy-Bhuiyan/leadbridge
cd leadbridge
cp .env.example .env
docker compose up --build
```

Then:

1. Open n8n at <http://localhost:5678> and create the local owner account.
2. **Workflows → Import from File** → `workflows/lead-intake.json`.
3. Open the workflow and click **Execute workflow** to arm the test webhook.
4. Fire a lead:

```bash
curl -X POST http://localhost:5678/webhook-test/leadbridge-intake \
  -H 'Content-Type: application/json' \
  -d '{"email":"ayesha@example-client.com","name":"AYESHA RAHMAN","company":"Example Client Ltd.","phone":"01868686062","website":"example.com","message":"We need a CRM automation workflow and an SEO audit."}'
```

With no credentials set you get a complete run on the offline provider and the
in-memory CRM. Check what actually happened:

```bash
curl -s http://localhost:8000/ops/availability | python -m json.tool
curl -s http://localhost:8000/ops/runs | python -m json.tool
```

Interactive API docs: <http://localhost:8000/docs>

To connect the real integrations, fill in `.env` and `docker compose up -d`.
[`RUNBOOK.md`](./RUNBOOK.md) has the HubSpot setup and the failure playbook.

## What is real, and what is not

Written plainly, because a reviewer should not have to read the source to find out.

**Real and running:**

- The FastAPI service, all endpoints, **127 passing tests** covering normalization,
  the three dedupe layers, retry and backoff behaviour, HubSpot request shapes,
  SEO parsing and scoring, provider fallback, and the API end to end.
- The HubSpot v3 client: search-then-write upsert, the 409 race recovery, and
  note-to-contact association. Exercised against a mock transport in tests; verify
  against a real portal with the steps in the runbook.
- The technical SEO audit: robots.txt parsing, sitemap discovery and counting,
  title/meta/canonical/H1/JSON-LD/Open Graph/alt-text extraction, and Core Web
  Vitals from PageSpeed Insights with CrUX field data preferred over lab data.
- The offline classifier, run log, idempotency, and the `/ops` endpoints.
- Both n8n workflow graphs, validated in CI by `scripts/validate_workflows.py`.

**Not done, and not claimed:**

- No outbound email is sent. The classifier drafts a first reply; nothing delivers
  it, and the CRM note says "not sent automatically" so nobody assumes otherwise.
- The SEO audit reads one URL — the one submitted. It is not a site crawler.
- The audit **score is a deduction model**, not a ranking prediction. It counts
  known problems. Quoting it as an SEO forecast would be wrong.
- No multi-tenancy, no user accounts, no dashboard.
- The `intent` and `priority` labels are a triage aid for a human, not a decision.

## The design rules

Four rules run through the whole codebase, and most of the comments exist to
explain where one of them applies.

**Nothing is fabricated.** A step that did not run is absent from the run log, not
recorded as zero. PageSpeed metrics that were not returned are `available: false`
with a reason, never a default number. The mock classifier stamps every result it
produces with `provider: "mock"` and `degraded: true`.

**A misconfiguration must be visible.** `GET /ops/availability` reports every known
provider, whether it could run, and why not — including providers that configuration
has ruled out, so "the integration I expected is missing" is never the answer.
`/ops/health` reports `auth: open` when the shared secret is unset.

**Degrade where it is safe; fail where it is not.** A failed Anthropic call falls
back to offline rules and marks the run degraded — a worse summary is survivable.
A failed **CRM** write is raised, not absorbed: a lead believed filed but not filed
is the worst outcome in the system, so n8n routes it to a human. A failed SEO audit
never loses the lead.

**Untrusted input stays data.** The lead message is attacker-controlled — anyone can
type "ignore your instructions" into a contact form. It is fenced, labelled as
untrusted, and all instructions live in the system prompt. `test_providers.py`
asserts that boundary holds.

## Layout

```
workflows/          n8n exports — lead intake, and a weekly scheduled audit
service/app/
  normalize.py      email, BD phone → E.164, website, fingerprint, fuzzy key
  dedupe.py         three layers: fingerprint, email, fuzzy company|name
  db.py             SQLite lead store + append-only run log
  http.py           one retry/backoff policy for every outbound call
  providers/        Anthropic + offline mock, behind one protocol
  crm/              HubSpot + in-memory twin, behind one protocol
  seo/              robots, sitemap, on-page parsing, PageSpeed, rules engine
  pipeline.py       the single-call path, sharing the same stage functions
  routers/          leads, seo, ops
service/tests/      127 tests, fully offline
scripts/            workflow graph validator, committed-credential scanner
```

## Tests

```bash
cd service
pip install -e ".[dev]"
pytest -q
```

No network and no keys. Any test that reached the internet would fail rather than
pass quietly.

```bash
python scripts/validate_workflows.py   # n8n graph consistency
python scripts/check_no_secrets.py     # no committed credentials
```

The credential scanner exists for one specific hazard: an n8n workflow exported
after a token was typed into an HTTP node carries that token in the JSON. The
workflows here reference `$env` instead, and CI keeps it that way.

## Security notes

- Both containers bind to `127.0.0.1` only. The service holds the HubSpot token, so
  it is not reachable from the network merely because n8n needs to call it — n8n
  reaches it over the compose network.
- Every mutating endpoint is behind `X-Leadbridge-Secret`. `/ops` stays readable
  without it, deliberately: whoever is diagnosing a misconfiguration is usually the
  person who has lost access to the configuration.
- The service container runs as a non-root user; the database lives on a volume.
- `.env` is gitignored. `.env.example` documents every variable and contains no
  values.

## Status and provenance

Built by [Nurul Azam Bhuiyan](https://github.com/Niloy-Bhuiyan) as a working study
of agency automation: n8n orchestration, CRM integration, and technical SEO auditing.
It is a personal project, not client work, and the commit history shows when it was
written.

The dedupe approach is adapted from the job-listing ingestion pipeline in
[Shuru](https://github.com/Niloy-Bhuiyan/shuru); the provider-with-offline-mock
pattern is the one used in
[IncidentLens](https://github.com/Niloy-Bhuiyan/IncidentLens) and
[Agent-Paw](https://github.com/Niloy-Bhuiyan/Agent-Paw).

MIT licensed.
