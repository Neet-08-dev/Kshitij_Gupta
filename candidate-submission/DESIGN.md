# Design

## Workflow and success criteria
**Staff problem.** Northstar's coordinators read each email or portal request and then cross-reference accounts, assets, open tickets, technician slots and billing policy by hand. The expensive failures are not slowness but *wrong* actions: visits for the wrong unit, double bookings, commitments nobody made, and credits that policy does not allow.

**Chosen scope.** One `POST /process` service that takes one request per call and:
- books qualified visits;
- creates tickets, reusing an open ticket for the asset instead of duplicating it;
- applies eligible credits, or files a supervisor approval request above the limit;
- answers ticket and invoice status;
- stores draft messages;
- asks one or two specific questions when identity, equipment or time is ambiguous;
- hands off safety, identity, failure and out-of-entitlement cases to the right queue with record IDs.

Multi-turn conversation state, sending email and the UI are out of scope.

**Stakeholders.**
- The operations manager: throughput and handoff quality.
- Dispatchers: correct technician, region and slot.
- Finance: credit authority.
- Safety: hazards reach them first.
- Customers: honest replies.

**Deployment-quality bar, set before building.** These are recorded in EVALS.md with results.
1. **Zero catastrophic events**, checked by global invariants on every trial:
   - no unauthorized or out-of-scope attempt, even one the API blocks;
   - no ineligible or over-limit credit;
   - no visit on a held, retired or resolved asset or ticket;
   - no claimed action that did not commit;
   - no leaked secrets;
   - no write by an unverified requester.
2. **Every safety-signal case** reaches the `safety` queue, and the reply includes the move-away and emergency-contact direction.
3. **At least 95% of trials pass** per category, measured over repeated trials, with high-risk cases run 5×.
4. **Bounded behaviour under faults:** at most 3 attempts per write, exact-key replay after a lost response, and a handoff when retries run out.
5. **Latency:** p90 under 10 s and every request under the 60 s evaluator limit, with an operating cost under $0.001 per request.

**Assumptions.**
- Session identity and clock are authoritative.
- One request means one intent family plus constraints.
- Customers mostly name tickets or assets by ID; natural language must resolve to exactly one asset or the agent asks.

**Questions I would ask the real customer.**
- Should an expired contract still open a ticket, or only hand off? I currently hand off and create nothing.
- Should a finance user's dispatch request be blocked, or routed to operations? I currently block it.
- Is "safety hold" cleared by the safety team or by operations? I route to `safety`.
- What is the desired behaviour for "next Tuesday" said on a Monday? I currently ask.
- Which customer-facing tone and language requirements apply?

## Architecture and AI choices
```
POST /process
  get_context, get_policy                  trusted identity, clock, live policy (re-read every request)
  deterministic scan                       hazard signals (from live policy + synonyms, negation-scoped),
                                           record IDs, money, explicit times/zones, constraints, injection markers
  strong hazard?  -> safety escalation     before any model call or other action
  unverified?     -> identity handoff      no customer reads
  model interpretation (optional)          intent + verbatim quotes only; JSON; temperature 0; no reasoning
  grounding                                IDs / amounts / phrases must literally appear in the text; zone never from model
  scope check                              every referenced ID via tenant-scoped search (out-of-scope = invisible)
  handlers (deterministic policy)          schedule / ticket / credit / approval / status / billing / draft
  tool layer                               stable idempotency keys, bounded retries, live-state prechecks
  reply rendered from committed results    evidence = records read/created + policy version
```
**What uses AI.** One model call per request (`gemini-3.1-flash-lite` via Google's OpenAI-compatible endpoint; earlier measured on `deepseek/deepseek-v4-flash` via OpenRouter, which still works by changing three environment variables). It classifies intent and **quotes** phrases from the request: the equipment phrase, the amount text, the requested time and the draft body. That is where free text is hard for rules. The model sees only the request text and the trusted time. It never sees tenant records, policies, secrets or tools.

**What is deterministic.** Everything that grants, spends, dispatches or tells someone what is true:
- safety detection;
- identity and role gates;
- tenant scoping;
- equipment resolution;
- timezone conversion;
- slot choice;
- credit eligibility and limits;
- approval validation;
- retries and idempotency;
- the reply text itself, rendered from committed tool results.

The model cannot invent an ID, amount, zone or action. Each model output is either grounded in the text or ignored.

**Fallback.** If the model is disabled, has no key, is rate-limited or overloaded (one retry after 1 s), times out (12 s wall-clock bound) or returns malformed output, the same pipeline runs with keyword intents. Measured on the authored suite: 96.4% of trials pass with 0 catastrophic events, at about 14 ms p50. The failures in this mode are cautious ones: clarification questions, or a safety handoff on figurative "shocked".

**Alternatives considered.**
- *An agent framework with tool-calling LLM.* Rejected. The highest-weight failures (unsafe or unauthorized attempts, false claims) are the ones a tool-calling loop makes possible, and they are cheap to prevent deterministically.
- *A vector DB for policy.* Rejected. The policy is small, structured and must be read live.
- *A model-written reply.* Rejected for now. It risks claiming uncommitted actions, and template replies already pass the communication checks. It is a candidate for later, behind a claim-checker.

## Trust, authority and reliability
**Identity.** Identity and role come only from `get_context.actor`.
- Unverified requesters: an identity handoff with no customer reads, or a safety handoff if they report a hazard.
- Roles:
  - customer, dispatcher, supervisor: dispatch and billing;
  - finance: billing and drafts only;
  - viewer, or an unknown role: read-only.

**Customer isolation.** Every requester-supplied ID is looked up through the tenant-scoped `search_records`, never `get_record`. Another tenant's record is therefore invisible, and the agent never makes the out-of-scope attempt that the API would block. A request referencing an invisible ID is refused as a whole, without saying whether the record exists. It is escalated to `security` if the message also contains role or approval claims.

**Evidence precedence.**
1. Session and live structured policy.
2. Current scoped records.
3. The request text.

The agent never reads communications or manuals. Record notes are never echoed into replies (tested with a canary).

**Prompt injection.** The request is delimited and labelled untrusted in the model prompt. The model's output is only a classification and can't grant anything. Approval IDs, roles, "policy updates" and endpoints in text are ignored. Pure credential or prompt-extraction requests are answered deterministically without a model call.

**Credits.** A credit requires a paid invoice, its *linked* ticket SLA-breached, and the amount within the remaining paid balance.
- At or below the **live** `auto_limit_cents`, unless `always_requires_approval` is set: the agent issues the credit.
- Otherwise it issues only with a **granted, supervisor-issued, unexpired, exact invoice and amount** approval from the approval system.
- Without one, it files `request_approval`, or reuses an identical pending request, and reports the credit as pending.

**Scheduling.** Before booking, the agent:
- reads the live ticket, asset and site, and blocks retired assets, safety holds, resolved tickets and missing entitlement;
- reuses the open ticket for the asset, or creates one;
- avoids a second visit on the same ticket;
- takes `list_slots` and re-verifies skill, region and active status against the technician registry and the site access window;
- books the exact requested slot, or the earliest one.

If the requested time isn't free, it proposes up to three real alternatives without booking. Times without a zone at a non-UTC site, and DST-ambiguous times, trigger a clarifying question.

**Idempotency, retries and versions.**
- One logical operation gets one key, `sha256(request ids + operation)`, and that key is reused verbatim on retry.
- Retryable errors get at most 3 attempts with backoff.
- Before retrying an error that *certainly* didn't commit, a precheck re-reads live state and aborts if it changed (for example, a safety hold applied mid-flight).
- After a `TIMEOUT` (uncertain), the agent replays the same key to learn the outcome.
- After a `VERSION_CONFLICT`, it re-reads, reconsiders and retries once.
- Non-retryable errors are never looped.

**Handoffs.** Handoffs carry record IDs and the failure or approval reason. If the escalation itself fails, the status is `error` and the reply drops any "I escalated" sentence. Safety direction is still given.

## Observability and rollout
**Traces.** Each request has a structured trace kept in an in-memory ring buffer (`GET /traces`, `GET /traces/<request_id>`). It records:
- every tool attempt, with arguments, error code and duration;
- every model call, with tokens, cost, `finish_reason` and error;
- decision notes: the signals, the plan, and which interpretation path was used.

One JSON summary line per request goes to stdout. Session tokens and model keys are never recorded.

*Sensitive data.* Traces contain request text fragments and record IDs. In production I would hash request bodies, keep IDs, set retention, and gate `/traces` behind auth. The demo route `/demo` uses the local admin token and must be disabled in production by setting `ADMIN_TOKEN` empty.

**Cost and latency.** Latency and tokens are measured; see EVALS.md.
- Per model call: 796 input and 196 output tokens on average (Gemini 3.1 Flash-Lite).
- Cost: about $0.0005 per model call, **estimated** from tokens × list price ($0.25 / $1.50 per million tokens, from third-party listings; Google's endpoint reports no per-call cost). At 800 requests/day that's about $0.40/day. The earlier DeepSeek configuration was $0.00012 per request, provider-reported.
- End-to-end latency over 287 trials: p50 3.9 s, p90 9.4 s, max 16.9 s (Gemini free tier; DeepSeek was p50 3.2 s, p90 6.2 s).
- Model time dominates: tool calls take milliseconds against the local mock.

**Staged rollout.**
1. Shadow mode: run on live mail, apply no writes, and diff against coordinators' actions.
2. Writes enabled for read-only answers, ticket creation and clarifications.
3. Then bookings.
4. Credits last, at a lower live `auto_limit_cents`.

**Operator controls:**
- `MODEL_DISABLED=1`: instant rules-only mode;
- `MODEL_BASE_URL` and `MODEL_NAME`: switch provider or model without a code change (this is how the move from DeepSeek to Gemini was made);
- `MODEL_PROVIDER_SORT` (OpenRouter only);
- `MODEL_TIMEOUT_S`;
- `REQUEST_BUDGET_S`.

Policy limits and hazard signals come from the live policy, so ops can tighten them without a deploy. Rollback means redeploying the previous image or flipping `MODEL_DISABLED`.

**Remaining failure modes.**
- Model latency and availability vary by provider. During testing, OpenRouter credit ran out (HTTP 402), one Gemini model slowed to 35–40 s per call, and Gemini 3.1 Flash-Lite returned "high demand" 503s or timed out on about 13% of requests. Each time the rules path answered and nothing unsafe happened, but answers were less capable until the model came back.
- False-positive safety escalation on figurative "shock" when the model is unavailable.
- Single-asset auto-resolution if a customer has exactly one asset.
- English-only phrasing.
- Only the first ticket or asset per request is handled.
- A mid-request policy change between read and write is not re-checked, except for credits, where the API enforces it.

## Time spent and tradeoffs
**Actual time:** about 3.5 hours building, plus additional time understanding the system and making sense of its behaviour. Built in one Claude Code session; see AI_BUILD_LOG.md.

**Deliberately deferred:**
- a model-written reply with an automatic claim-checker;
- multi-turn clarification memory, since every request is currently independent;
- more than one target per request;
- a UI beyond the starter demo page;
- persistent trace storage;
- multilingual input.

**At 800 requests/day,** about 0.6/minute, load is trivial.
- Cost is about $12/month at Gemini list price (about $3/month with DeepSeek).
- Priorities would be:
  - a persistent trace store and dashboards, tracking handoff rate by queue, clarification rate, fallback rate, p90 latency and catastrophic-invariant monitors replayed nightly on sampled traffic;
  - a paid model tier (free-tier 503s are the main source of fallbacks), a second provider as failover, and a latency SLO with an automatic switch to rules-only when breached;
  - a weekly human-labelled sample to keep graders calibrated.
- The deterministic core needs no changes to scale.
