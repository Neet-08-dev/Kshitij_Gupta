# Design

## Workflow and success criteria
Describe the staff problem, chosen scope, stakeholders and deployment-quality bar. List assumptions and questions you would ask a real customer.

## Architecture and AI choices
Explain what uses AI, what is deterministic, tool boundaries, model/framework alternatives and why you chose this approach. Include a small diagram if useful.

## Trust, authority and reliability
Explain identity, customer isolation, evidence precedence, prompt-injection handling, credit approvals, scheduling, idempotency, retry limits, optimistic versions and human handoffs.

## Observability and rollout
Describe traces, sensitive-data handling, cost/latency measurement, staged rollout, operator controls, rollback and remaining failure modes. Distinguish measured facts from estimates.

## Time spent and tradeoffs
Record actual time, intentionally deferred work and how your design changes at 800 requests/day.
