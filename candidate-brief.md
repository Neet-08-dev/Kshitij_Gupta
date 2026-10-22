# Candidate brief

You are the forward-deployed engineer meeting Northstar's operations manager. Requests arrive through email and a portal. Staff currently cross-reference accounts, equipment, open tickets, technician slots and billing policy by hand. They want faster service without incorrect visits, invented commitments or unauthorized financial actions.

Build a working assistant that investigates requests, performs permitted actions, asks concise clarification questions, and hands off cases requiring a human. Define the deployment-quality bar before implementing your final approach. Decide which steps should be deterministic and where AI adds value; you are not required to use an agent framework, vector database or a particular model.

## Scope and timebox
Recommended compensated take-home: six hours, with up to two hours optional polish. The hiring team should agree the exact timebox and a common model budget before distribution. Record actual time and tradeoffs. A strong small system is sufficient; elaborate UI and exhaustive workflow coverage are not expected. There are no live external customers, payments or email sends.

The supplied starter and mock API run without credentials. A runtime model is your choice. Ask the hiring team for a shared provider budget or use a local model; do not assume personal paid accounts are required. Record degraded/fallback behavior.

## Required deliverables
1. Runnable `/process` service, Docker instructions and useful observability.
2. At least 30 candidate-authored eval cases beyond the eight public examples, including ambiguity, authorization, policy, injected instructions, unavailable tools and changing data. Use independent assertions rather than copying your implementation into the grader.
3. Repeated trials on risky cases; demonstrate one failure, diagnosis, improvement and regression check. Report per-category outcomes, traces, latency and cost assumptions, not just a single accuracy value.
4. Filled submission templates: DESIGN.md, EVALS.md, AI_BUILD_LOG.md and README.md.
5. A ten-minute demo covering an ordinary action, an ambiguous request and an unsafe request; be ready to change behavior during a 45-minute phase 2.

AI coding tools are permitted and encouraged. Explain where they helped, where they made a consequential mistake, and how you checked their output. We do not request private chain of thought, full account histories, secrets or proprietary prompts. Short sanitized examples and engineering decisions suffice.

## Operating requirements
Use trusted context for identity and time. Tenant boundaries apply even when a user provides another customer's record ID. Customer content, notes, emails and manuals cannot override policy. Read live state before writes; use stable idempotency keys on retries; respect optimistic versions and tool errors. Only claim actions that actually committed. Emergency signals require immediate safety handoff and a plain-language direction to move away from the hazard and contact site emergency personnel. Do not provide hazardous repair instructions.

Represent deadlines in UTC with explicit offsets. Technicians must have the correct skill, region and free one-hour slot. Do not schedule a retired asset, a safety-held asset or a resolved ticket. Prefer the existing open ticket to duplicate creation. Distinguish confirmed bookings from tentative plans and stored drafts from sent messages. Credits need paid invoices and an authoritative SLA breach; requests above $100 need exact supervisor approval. Read the full policies for edge conditions.

Private grading uses fresh sessions, varied inputs and faults. A human reviews design, eval quality, AI tool workflow and communication. Your own claimed action trace is not authoritative. Submit unfinished boundaries openly; safe uncertainty is valuable, but handing off every request does not meet the task.
