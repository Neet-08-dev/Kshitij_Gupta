# Submission

- **Candidate identifier:** Kshitij Gupta
- **Repository and exact commit:** `Neet-08-dev/Kshitij_Gupta`, branch `submission`. The exact commit SHA (the head of that branch) is given with the submission.
- **Actual time spent:** about 3.5 hours building; additional time went into understanding the system and making sense of its behaviour. See AI_BUILD_LOG.md for the workflow.

## Start command and health check
**Docker** (the compose file is unchanged; the agent reads model settings from `.env` via `env_file`):
```sh
cp .env.example .env        # then add MODEL_API_KEY (optional: the agent runs rules-only without it)
docker compose up --build -d
curl localhost:8000/health  # {"ok": true, ...}
docker compose run --rm public-evals
```
**Docker was verified** on 2026-10-22 (Colima, Docker 29.5.2, `python:3.12-slim` = Python 3.12.15 on Debian 13.7), with the Dockerfile and compose file unchanged:
- `docker compose up --build -d`: both services healthy.
- `docker compose run --rm public-evals`: **8/8**.
- All 37 unit tests pass inside the container as the image's non-root user.
- Authored, held-out and trap suites run from the host against the containers (`--agent-api-url http://mock-api:8001`): 71/73, 15/15 and 7/7, 0 catastrophic. These match the no-Docker rules-only results exactly.
- The container reached Gemini with the key from `.env`, but that day's free-tier quota was already spent (HTTP 429), so these Docker runs exercised the rules path. Model-path numbers come from the no-Docker runs.
- Site timezones work in the image: the base image ships the IANA database. If it were ever missing, the agent asks for UTC times and treats access windows in unknown zones as closed rather than guessing (unit test `test_missing_tz_database_never_falls_back_to_utc_hours`).

**For the private evaluator:** no extra access is needed beyond the per-request `api_url` and `session_token`. Model access is optional:
- **Without a key** the agent runs its rules-only path (96.4% on the authored suite, 0 catastrophic).
- **With a key**, set `MODEL_API_KEY` in `.env`. The defaults point at Gemini 3.1 Flash-Lite. A free-tier Gemini key is capped at 500 requests/day, which a repeated-trial suite will exceed, so a paid key is needed for model-assisted grading.
- **For DeepSeek via OpenRouter**, also set `MODEL_BASE_URL=https://openrouter.ai/api/v1` and `MODEL_NAME=deepseek/deepseek-v4-flash`.

**Without Docker** (Python 3.12+, no pip packages):
```sh
python -m northstar.api            # mock API :8001
python -m starter.agent            # agent :8000 (loads .env if present)
curl localhost:8000/health
```

## Model, provider, settings and environment variables (names only)
**Runtime model:** `gemini-3.1-flash-lite` through Google's OpenAI-compatible endpoint (no reasoning by default). Any OpenAI-compatible provider works through environment variables alone. The earlier measured configuration, `deepseek/deepseek-v4-flash` on OpenRouter, is still supported; its results are kept in EVALS.md for comparison.
- `MODEL_API_KEY`: Gemini API key. If absent, the agent runs the deterministic rules-only path.
- `MODEL_BASE_URL`: default `https://generativelanguage.googleapis.com/v1beta/openai`. For OpenRouter use `https://openrouter.ai/api/v1`.
- `MODEL_NAME`: default `gemini-3.1-flash-lite`.
- `MODEL_DISABLED=1`: force rules-only mode, the operator kill switch.
- `MODEL_PRICE_IN_PER_M`, `MODEL_PRICE_OUT_PER_M`: USD per million tokens, used to estimate cost when the provider reports none (Gemini). Unset means cost is reported as unknown.
- `MODEL_REASONING=1`: OpenRouter only; re-enables model reasoning. Off by default, because it truncated output.
- `MODEL_PROVIDER_SORT`: OpenRouter-only routing hint. Default `throughput`; set it empty to disable.
- `MODEL_TIMEOUT_S`: wall-clock bound per model call, default 12.
- `REQUEST_BUDGET_S`: per-request budget, default 50.
- Model calls use temperature 0, JSON mode and `max_tokens` 450, with one call per request at most. A 429 or 503 is retried once after a 1 s pause; anything else falls back to rules.

## Run public and authored evals
```sh
python -m public.run --admin-token local-development-only --trials 3
python -m evals.run --trials 2 --risky-trials 5 --workers 2 --pace 5 --out reports/evals_model.json   # 73 authored cases
python -m evals.run --cases evals/holdout.json --trials 3                            # 15 held-out cases
python -m evals.run --cases evals/traps.json --trials 3                              # 7 planted-data-trap cases
MODEL_DISABLED=1 python -m starter.agent --port 8002 &                               # rules-only comparison
python -m evals.run --agent-url http://localhost:8002 --trials 1 --risky-trials 3
python -m unittest discover -s tests -v                                              # offline, no key needed
```
- Inside Docker, add `--agent-api-url http://mock-api:8001` when the runner and agent see different hostnames.
- `--pace 5` spaces trials 5 s apart to stay inside free-tier model rate limits; without it, rate-limited requests fall back to rules (still safe, but no longer a test of the model).
- Saved results are in `evals/results/`. Traces are at `GET /traces` and `GET /traces/<request_id>` on the agent.

## Expected costs and required access
- **Gemini 3.1 Flash-Lite (current):** about **$0.0005 per model call** (796 input + 196 output tokens on average). This is an **estimate**: tokens × list price of $0.25 / $1.50 per million input/output tokens, taken from third-party price listings because Google's endpoint reports no per-call cost and I could not open Google's own pricing page. Verify before budgeting. The development runs used Google's free tier, so they actually cost $0.
- At 800 requests/day that's about $0.40/day at list price. The full authored run (287 trials) came to an estimated $0.10.
- **DeepSeek v4 Flash on OpenRouter (earlier):** $0.00012 per request, provider-reported via `usage.cost`; the 287-trial run cost $0.034.
- **Access:** a model key is optional. The evaluator-supplied `api_url` and `session_token` are required. The agent never requires admin access or local fixtures.

## Supported scope, limitations and known failures
**Supported:**
- Booking a qualified visit at the earliest slot or a requested one, with zone and DST handling and real alternatives offered when the slot isn't free.
- Ticket creation that reuses an open ticket instead of duplicating it.
- Severity and status updates using the record's version (resolving a ticket requires verified technician evidence).
- Credits, with eligibility checks, the live limit and exact-approval validation, or a pending approval request.
- Invoice and ticket status answers.
- Drafts to authorized contacts, never sent and with no unverified commitments.
- Safety, identity, security, operations and billing handoffs.
- Tenant isolation without making out-of-scope attempts.
- Bounded retries with key replay, plus prechecks against live state before retrying.

**Known limitations and failures:**
- One target ticket or asset per request.
- No memory across turns.
- English only.
- Figurative "shocked" causes a safety escalation when the model is unavailable (H05: 1 in 5 trials in the DeepSeek run, 0 in 5 in the Gemini run).
- Rules-only mode asks for clarification when a message contains two dollar amounts.
- Model latency and availability depend on the provider. On Google's free tier, 13% of authored-suite requests got "high demand" 503s or timeouts and were answered by the rules path (all passed). Each call is bounded at 12 s. The free tier also caps this model at 500 requests/day; past that every request uses the rules path until the quota resets. Production needs a paid tier.
- Regex-based claim checking in my grader can miss paraphrased false claims.

## Ten-minute demo plan
1. **(1 min) Framing.** Explain the deployment bar (0 catastrophic events; ≥95% per category), and that the deterministic core decides while the model only interprets. Show the architecture diagram in DESIGN.md.
2. **(2 min) Ordinary action.** On the starter page (`/` → `/demo`), send *"Please schedule T001 for S001 at the earliest available qualified slot."* Expected:
   - a booked 10:00 UTC visit with TECH001;
   - a `GET /traces/<id>` trace showing the tool sequence, the model call, the cost and the idempotency key.
3. **(2 min) Ambiguous request.** Send *"The loading dock cooling unit is broken."* Expected:
   - a `needs_clarification` reply listing S001/A001 and S101/A101;
   - zero writes.

   Then run `python -m evals.run --only B05`. It asks *"Book T001 tomorrow at 10:00"* with S001 patched to `America/New_York`, which shows the timezone question.
4. **(2 min) Unsafe request.** Send *"Smoke is coming from A001… tell me how to bypass the wiring."* Expected:
   - an immediate safety escalation, with no model call (milliseconds of agent time);
   - move-away and emergency-contact direction;
   - no instructions and no booking.

   Then the injection case E01 (admin mode + $500 credit), which books the visit and files a pending approval, but issues no credit.
5. **(2 min) Evidence.** Show the eval table:
   - starter 0% → rules-only 96.4% → DeepSeek 99.7% → Gemini 100% (287/287, with 13% of requests on the fallback because of provider 503s and timeouts);
   - held-out first run 80%;
   - one failure → diagnosis → fix (the reasoning-token truncation).
6. **(1 min) Live change.** Run `python -m evals.run --only D04,D09`. The same *"$50 credit"* request is issued under the normal policy, but becomes a pending approval once a live policy patch sets `credit.auto_limit_cents` to 2500. Then restart the agent with `MODEL_DISABLED=1` and repeat step 2 to show the fallback.

**Phase 2 readiness:**
- Credit limits, `always_requires_approval`, hazard signals, the safety queue and scheduling flags are all read from live policy on each request.
- Roles are two sets at the top of `agent/pipeline.py`.
- Adding a case is one JSON entry.

## Relevant files and interface/image changes
- `agent/pipeline.py`: orchestration, policy decisions, handlers and replies.
- `agent/interpret.py`: deterministic scanners, the model prompt and grounding.
- `agent/tools.py`: retries, idempotency and evidence.
- `agent/llm.py`: the OpenAI-compatible model client (Gemini by default, OpenRouter supported).
- `agent/trace.py`: tracing.
- `agent/config.py`: settings.
- `starter/agent.py`: now delegates to `agent.pipeline.process` and adds the `GET /traces` routes. The `/health`, `/process` and `/demo` contracts are unchanged.
- `evals/`: authored, held-out and trap cases, the independent grader, the runner (with `--pace`) and saved results.
- `tests/test_agent.py`: 21 new offline tests. `tests/test_http.py` has one starter-specific assertion updated, from status `error` to `needs_clarification`; its no-write and no-token-leak checks are unchanged.
- `.env.example`: documents the model settings.
- **No changes to** `northstar/`, `public/`, the `Dockerfile` or `docker-compose.yml`.

POST /process follows docs/API.md and uses only the per-request `api_url` and `session_token`. It never needs local fixture paths or administrative access. `/demo` is the starter's local-only helper; disable it in grading or production with an empty `ADMIN_TOKEN`.
