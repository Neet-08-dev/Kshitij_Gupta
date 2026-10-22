# Evaluation report

## Deployment-quality bar
State thresholds before presenting results; define catastrophic failures separately from aggregate performance.

## Dataset and graders
Link at least 30 authored cases. Explain categories, labels, state/outcome assertions, tool/argument checks and communication grading. Explain independence from the implementation, held-out data, leakage risks and human calibration.

## Repeated trials
Record model/version/settings, trial counts, independent reset procedure, pass rates by case/category, variance and worst-case failures. Avoid calling best-of-N success reliability.

## Measured results
Include reproducible commands, actual tool calls, latency distribution, token usage and pricing source/date or clearly label cost unavailable. Include baseline and changed-system results under comparable budgets.

## Error analysis and iteration
Show at least one consequential failure, root cause, improvement and regression test. Note false positives/negatives in your own graders and unsupported claims.
