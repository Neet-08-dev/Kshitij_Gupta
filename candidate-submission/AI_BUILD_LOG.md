# AI development-tool build log

## Tools and workflow
- **Claude Code** (CLI, model Claude Opus 5.5) in a single interactive session for everything. It did:
  - read the brief, docs, policies, mock API and data, and summarized the assignment;
  - proposed the architecture: a deterministic policy core with model-assisted interpretation;
  - wrote `agent/`, `evals/` and the tests;
  - ran the mock, the agent and every eval;
  - diagnosed failures from traces and drafted these documents.
- **Runtime model:** `gemini-3.1-flash-lite` through Google's OpenAI-compatible endpoint. The system was first built and measured on `deepseek/deepseek-v4-flash` via OpenRouter; when that account ran out of credit I switched providers (see example 4). I chose the models and supplied the keys, which are kept in the git-ignored `.env`.
- **My role:**
  - gave direction (scope, model choice, "go ahead");
  - made the go/no-go calls;
  - reviewed outputs at checkpoints: the assignment summary, the baseline, the public results, the eval failures and these documents.
- **Approximate time:** about 3.5 hours building. Additional time went into understanding the system: reading traces, eval results and the code to make sense of why it behaves as it does.
- **What was delegated:** essentially all typing and running. Decisions that changed behaviour went through explicit evidence (traces, audit logs, eval rows) before being accepted.

## Evidence of judgment
1. **"Don't trust a green public run."**
   - *Instruction:* build the agent and get the public cases passing.
   - *Output:* 8/8 on the first run.
   - *Verification:* the per-request traces showed 10–33 s latency, model output tokens pinned at exactly 600, and an `interpretation=rules` path on 3 cases.
   - *Kept/changed:* the passes were real but came from the fallback. Reasoning was turned off, the trace now records `finish_reason`, and every eval row records which path answered.
2. **"Make the grader independent, then distrust it too."**
   - *Instruction:* write at least 30 cases with independent assertions.
   - *Output:* a grader that judges the API snapshot and audit, never the agent's claims.
   - *Verification:* the first full run reported 12 "catastrophic" false-claim events. Reading the replies showed they were all negated sentences ("no credit was applied"). The bug was in the grader, not the agent.
   - *Kept/changed:* negation handling and a unit test. That test then caught a second grader bug (a regex that never matched "Booked:").
3. **"Measure generalization separately."** The authored suite hit 100% while I was building. I had Claude write a held-out set *after* the implementation and run it once before touching code.
   - *Result:* 80% (12/15).
   - *What it found:* three real bugs, including a stored draft asserting an unconfirmed booking.
   - *Reporting:* the 80% is reported as the generalization estimate, and the set is now labelled a regression suite.
4. **"A provider switch is a model change, so re-measure it."** OpenRouter started returning HTTP 402 (out of credit), and the agent quietly answered everything through the rules path. I had Claude try three replacements with my keys and judge them by traces, not by pass rates:
   - *MiniMax M3.1 Flash (free):* reasoning cannot be disabled, and it truncated at the 450-token cap. Rejected.
   - *Groq gpt-oss-120b:* fastest (about 1.5 s), but every call first failed with HTTP 403 because Groq's edge blocks Python's default User-Agent (fixed in the client), and free-tier token limits caused 429s. Not chosen.
   - *Gemini 3.5 Flash-Lite:* passed, then slowed to 35–40 s per call mid-run, so the run was stopped rather than reported. *Gemini 3.1 Flash-Lite* was chosen and re-measured on all suites.
   - *What the switch exposed:* the first Gemini run found two interpretation bugs that DeepSeek had never triggered: H03 ("create an S2 ticket" also read as a ticket update) and trap T02 (a subject line of "Credit" over a status question read as a credit request). Both were fixed with deterministic rules that apply to every model and to the fallback, plus unit tests. The lesson: interpretation quality is a property of the model, so the evals have to be re-run on every model change.

## Most consequential AI-generated mistake
**The scope check made unauthorized attempts.** The first implementation verified requester-supplied IDs with `get_record`. For P06 (another tenant's invoice), the API returned `FORBIDDEN`, so the agent correctly refused and the public case **passed**. But the audit log now held an out-of-scope access attempt, and the brief says such attempts are reviewed even when the API blocks them.

- *How it was discovered:* while designing the global invariants, which treat any `FORBIDDEN` or `UNVERIFIED` in the audit as catastrophic, and checking them against the existing behaviour.
- *Fix:* all requester-supplied IDs are resolved through the tenant-scoped `search_records`, so another tenant's record is simply invisible. The reply is identical for "not yours" and "doesn't exist", so existence isn't disclosed.
- *Regression guards:*
  - the global invariant runs on every eval trial;
  - unit test `test_other_tenant_id_makes_no_forbidden_attempt`;
  - authorization cases C05, C06 and C08.

A close second was **the silent model misconfiguration**, which was masked by a working fallback (example 1 above). The guard there is the `interpretation` and `llm_errors` fields in every trace and eval row. That guard caught the problem twice more: the out-of-credit OpenRouter key (HTTP 402) and free-tier rate limits during eval runs. In the final Gemini run, 38 of 287 requests fell back because of provider timeouts, 503s or 429s (plus 5 deliberate model skips for secrets requests), and every one of them passed.

## Productivity and limits
**Observed:**
- From an empty `process()` (0/8 public, 0/73 authored) to the measured final system and these documents in one session.
- About 1,700 lines of agent code (including comments), a 73 + 15 case eval suite with about 370 lines of runner and grader, and 20 new tests.
- Every number in EVALS.md comes from a saved report in `evals/results/`.

**Subjective estimate:** _[candidate to fill in: how long this would have taken by hand]_.

**Where delegation stopped or needed a human check:**
- Policy interpretations that are business decisions:
  - an expired contract hands off rather than creating a ticket;
  - finance dispatch is blocked rather than routed;
  - a safety hold goes to `safety`;
  - "next Tuesday" said on a Monday triggers a question.

  These are listed in DESIGN.md as questions for the customer.
- Claims in these documents, which were checked against the reports after drafting. Three inaccurate statements were corrected, including an overstated "hand-labelled all cases" claim.
- Anything touching secrets: the API key stays in `.env`, and the eval results were scanned for key material before saving.

**Limit observed:** the tool was fast at building and at fixing what evals exposed, and willing to call its own grader wrong when the evidence said so. It would also have happily reported the first 8/8, which is why the traces and held-out data were made part of the workflow rather than an afterthought.
