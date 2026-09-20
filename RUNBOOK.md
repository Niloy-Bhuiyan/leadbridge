# Runbook

Operating notes: connecting the real integrations, verifying a run end to end, and
what to do when a step fails.

---

## 1. Connect HubSpot

1. Create a free HubSpot account.
2. **Settings → Integrations → Private Apps → Create a private app.**
3. On the **Scopes** tab, enable exactly:
   - `crm.objects.contacts.read`
   - `crm.objects.contacts.write`
   - `crm.objects.notes.write`
4. Create the app and copy the access token.
5. Put it in `.env`:

   ```
   HUBSPOT_TOKEN=pat-na1-xxxxxxxx
   CRM_PROVIDER=auto
   ```

6. `docker compose up -d` and confirm the switch actually happened:

   ```bash
   curl -s http://localhost:8000/ops/availability | python -m json.tool
   ```

   `crm.active` must read `hubspot`. If it still says `mock`, the token is not
   reaching the container — check `.env` is in the repo root, not in `service/`.

> The token grants write access to your CRM. Keep it out of the repo; the committed
> `.gitignore` and `scripts/check_no_secrets.py` are both there to make that harder
> to get wrong.

## 2. Connect the LLM

```
ANTHROPIC_API_KEY=sk-ant-...
LLM_PROVIDER=auto
```

Leave it unset and the pipeline runs on the offline rule-based classifier. That is a
supported mode, not a broken one — but every result it produces is stamped
`degraded: true`, and the CRM note names the provider, so nobody mistakes a keyword
rule for a model.

## 3. Set the shared secret

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Put the same value in `.env` as `INGEST_SECRET`. Compose passes it to both
containers; the workflows read it as `{{ $env.INGEST_SECRET }}`.

Verify it is enforced:

```bash
curl -s -o /dev/null -w '%{http_code}\n' \
  -X POST http://localhost:8000/leads/normalize \
  -H 'Content-Type: application/json' -d '{"email":"a@b.com"}'
# expect 401
```

## 4. Import the workflows

n8n owns its copy once imported, so re-import after editing a file in the repo.

1. <http://localhost:5678> → **Workflows → Import from File**
2. `workflows/lead-intake.json`
3. `workflows/scheduled-seo-audit.json`

**Version drift.** These exports target n8n 1.x. If your n8n is older or much newer,
a node may import with a typeVersion it does not recognise — n8n flags the node and
you pick the current version from the node's own panel. The graph and the parameters
survive; only the version marker changes.

**Test vs production URLs.** `Execute workflow` arms `/webhook-test/...` for one
call. Activating the workflow serves `/webhook/...` permanently. Using the wrong one
is the most common reason a curl gets a 404.

## 5. Verify a real run

```bash
curl -X POST http://localhost:5678/webhook-test/leadbridge-intake \
  -H 'Content-Type: application/json' \
  -d '{"email":"you+test@yourdomain.com","name":"Test Person","company":"Test Co Ltd.","phone":"01868686062","website":"example.com","message":"We need CRM automation and an SEO audit."}'
```

Then check all four places:

| Where | What you should see |
|---|---|
| n8n execution list | Every node green; the `Already Seen?` branch taken to `Classify` |
| HubSpot → Contacts | The contact, with phone stored as `+8801868686062` |
| The contact's timeline | Two notes: the triage, and the SEO audit findings |
| `GET /ops/runs` | One run, with per-step durations |

**Now send the identical request again.** The second run must return
`status: skipped_duplicate` and must **not** create a second contact. That is the
idempotency guarantee, and it is the single most useful thing to verify by hand.

## 6. Failure playbook

| Symptom | Cause | Fix |
|---|---|---|
| `/ops/availability` shows `crm.active: mock` with a token set | `.env` not loaded | `.env` belongs in the repo root; `docker compose up -d` to reload |
| All calls return 401 | Secret mismatch | Same `INGEST_SECRET` in both containers; `docker compose config` to confirm |
| HubSpot calls return 403 | Missing scope | Add the three scopes above; the token changes when scopes change |
| Classification always `degraded: true` | No Anthropic key, or the call failed | Check `/ops/availability`; the run log records the reason on the `classify` step |
| `vitals.available: false`, reason mentions rate limit | Unauthenticated PageSpeed | Set `PAGESPEED_API_KEY` |
| SEO audit step degraded, lead still filed | Site unreachable or non-HTML | Intended: the audit must never lose the lead. Reason is on the step. |
| Webhook 404 | Test vs production URL | See §4 |
| Scheduled audit fails instantly | `AUDIT_SITES` unset | Intended: it fails loudly rather than reporting a healthy run over zero sites |

## 7. Reading the run log

```bash
curl -s http://localhost:8000/ops/runs | python -m json.tool
curl -s http://localhost:8000/ops/runs/<run_id> | python -m json.tool
```

Each step records status, duration, and a detail object. Statuses:

- `ok` — completed normally
- `degraded` — completed on a fallback; `detail.reason` says which and why
- `skipped` — deliberately not run; `detail.reason` says why
- `failed` — stopped the run

Failed runs are **never** deleted. A pipeline you cannot audit afterwards is a
pipeline you cannot trust.

## 8. Before committing a re-exported workflow

```bash
python scripts/check_no_secrets.py
python scripts/validate_workflows.py
```

If you typed a token into an n8n HTTP node instead of using `{{ $env.X }}`, the
export contains it. The first script catches that. If it ever fires on a real
credential, **rotate the credential** — removing the line is not enough once it is
in git history.
