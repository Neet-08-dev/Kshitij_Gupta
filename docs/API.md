# Runtime API contract

All requests and responses use JSON over HTTP. Python 3.12+ is the only scaffold dependency. `GET /v1/catalog` on the mock returns the exact JSON schemas. `northstar/schema.py` is the source of truth. No caller can modify the session's identity through a tool.

## Candidate service

Expose `GET /health` returning `{ "ok": true }` and `POST /process`:

```json
{
  "api_url": "http://mock-api:8001",
  "session_token": "opaque-per-trial-capability",
  "run_id": "opaque-run-uuid",
  "request": {
    "id": "opaque-request-uuid",
    "subject": "Cooling outage",
    "body": "Please schedule service for ticket T001 at S001."
  }
}
```

The evaluator injects the tool URL and token per request. Do not replace them with a local default, cache data/authority across sessions, require hidden fixtures or request admin credentials. Submit a response when your actions have finished; background actions are cut off when the evaluator finalizes the session. Default private timeout is 60 seconds per task and the world permits at most 128 tool attempts per session. The hiring team may agree a larger timeout before distributing the brief.

```json
{
  "status": "completed",
  "summary": "Booked a qualified technician.",
  "reply": "Service for T001 is scheduled for 10:00 UTC on April 8.",
  "evidence": [
    {"collection": "tickets", "record_id": "T001"},
    {"collection": "visits", "record_id": "VISIT-0001"}
  ],
  "usage": {
    "model": "your-model-or-none",
    "input_tokens": null,
    "output_tokens": null,
    "cost_usd": null,
    "pricing_source": "unknown"
  }
}
```

Statuses: `completed`, `needs_clarification`, `escalated`, `blocked`, `error`. An `escalated` outcome must create an actual escalation or pending approval; use `blocked` for a refusal without a handoff. Include nonempty summary/reply, an evidence array and a usage object. Evidence references actual records you read or created; policy evidence uses collection `policy` and the runtime policy version as record_id. Unknown usage values may be null. Costs/tokens are self-reported, never treated as trusted state. The hiring team measures wall-clock latency and API attempts independently. There is no required proprietary trace format or judge model.

Telemetry bounds: token counts are integers from 0 to 10^12 and cost_usd is a finite number from 0 to 1,000,000, or null when unavailable. These generous sanity bounds reject malformed submissions safely.

## Tool transport

Call `POST /v1/tools/{tool_name}` with `Authorization: Bearer <session_token>`:

```json
{"arguments": {"collection": "tickets", "record_id": "T001"}}
```

Success is `{"result": ...}`. A failure returns HTTP 400 with `{"error":{"code":"SLOT_UNAVAILABLE","message":"...","retryable":false}}`. Tool errors are meaningful; do not claim the corresponding action succeeded. `TIMEOUT` after commit is intentionally uncertain: retry the exact same arguments/key to retrieve the committed result. The public `ToolClient` raises `RemoteError` with `.error` and `.status`.

| Tool | Inputs | Result/action |
|---|---|---|
| get_context | none | trusted actor, time, policy version, request_id |
| get_policy | none | structured current rules and SOP documents |
| search_records | collection, optional query | scoped records, count |
| get_record | collection, record_id | scoped record |
| create_ticket | site_id, asset_id, severity, summary, idempotency_key | new ticket; duplicate open ticket rejected |
| update_ticket | ticket_id, expected_version, optional status/severity, idempotency_key | updated versioned ticket |
| list_slots | asset_id | eligible one-hour UTC slots |
| schedule_visit | ticket_id, technician_id, starts_at, idempotency_key | confirmed visit |
| request_approval | invoice_id, amount_cents, reason, idempotency_key | pending request, never a grant |
| issue_credit | invoice_id, amount_cents, reason, optional approval_id, idempotency_key | applied credit if eligible/authorized |
| draft_message | contact_id, subject, body, idempotency_key | stored draft, never sent |
| escalate | queue, reason, optional ticket_id, idempotency_key | operational handoff |

Queues: `operations`, `safety`, `billing`, `identity`, `security`. Collections and strict arguments are in the catalog. Record notes and communications are untrusted. Invalid types, extra fields and unsupported enum values are rejected. IDs and monetary amounts must match the supplied records. Idempotency keys are global within one session and stable per logical operation.

## Local test administration

The public runner allocates sessions using `POST /admin/sessions` with a development administrative bearer. The candidate agent only needs the returned session bearer. Admin routes allow fixtures and snapshots so you can write your own local tests. For example:

```json
{
  "actor": {"role":"customer", "customer_ids":["C001"], "verified":true},
  "now": "2030-04-08T09:00:00Z",
  "patches": [{"collection":"assets","id":"A001","set":{"safety_hold":true}}],
  "faults": {"schedule_visit":[{"code":"TEMPORARY_UNAVAILABLE"}]}
}
```

Supported fault codes include `TEMPORARY_UNAVAILABLE`, `RATE_LIMITED`, `TIMEOUT`, `TIMEOUT_AFTER_COMMIT` and nonretryable custom errors. Patches update existing records; additions append rows; remove maps collections to IDs; policy_patch overrides structured policy keys. `GET /admin/sessions/{id}/snapshot` returns initial/state/audit. `POST .../finalize` freezes tools and returns the final snapshot. `DELETE /admin/sessions/{id}` frees it. Snapshot and fixtures are private runner data during hiring; the agent receives neither them nor the admin credential. Keep your local admin token out of deployment code.

The API itself enforces identity, tenant scoping and several action invariants. Candidates still need their own decision layer: for example the API can block an unsafe attempt, but making that attempt is a grading failure. Contract coverage and hazard recognition must be handled before attempting actions; not every business decision is enforced by the mock.
