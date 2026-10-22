# Northstar Field Services — candidate assessment

Build an AI operations assistant for a fictional commercial field-services business. All records and policies are synthetic. Northstar handles HVAC, electrical and refrigeration service for factories, warehouses and food distributors. The scenario assumes roughly 800 requests/day; this fixture is deliberately a compact evaluation dataset.

AI coding and building tools are explicitly permitted and encouraged: Codex, Claude Code, Cursor, Copilot, Gemini CLI or any other tools. Choose any runtime model, provider, framework or deterministic components. No model key is required to start the scaffold.

## Get your assessment workspace

Your hiring team will give you a private submission repository, a timebox and a due date. Clone that repository and work there. If asked to create your own copy, select **Use this template → Create a new repository**, choose **Private**, and grant the hiring team access in the new repository's Settings. Use a template copy to keep your submission private; GitHub forks of this public project stay public.

Read `candidate-brief.md`, start the demo below, and implement the assistant. Submit your repository link and exact commit SHA with the completed files in `candidate-submission/`. Keep provider keys in environment variables or ignored local files. Hidden evaluations and interviewer notes are held separately by the hiring team.

## Start with Docker
Prerequisite: Docker Engine/Desktop with Compose v2 and Linux containers.

```sh
cp .env.example .env
docker compose up --build -d
docker compose run --rm public-evals
```

In PowerShell use `Copy-Item .env.example .env`. Open http://localhost:8000 for the starter demo. API health: http://localhost:8001/health. The initial agent deliberately returns a human-review handoff, so most public evals will fail until you implement it. That is expected. `docker compose down` stops the services. The mock starts fresh whenever it restarts; each evaluation additionally gets its own isolated session.

## Start without Docker
Python 3.12 or later; no pip packages are needed. From this repository root, use three terminals:

```sh
python -m northstar.api
python -m starter.agent
python -m public.run --agent-url http://localhost:8000 --api-url http://localhost:8001 --admin-token local-development-only
```

`python -m unittest discover -s tests -v` checks the tool contracts. Configuration defaults are development-only; keep real model keys in environment variables and never commit them.

## What to read and implement
- `candidate-brief.md`: scope, recommended six-hour timebox and deliverables.
- `docs/API.md`: tool/API and `/process` contract. Preserve this interface so the private evaluator can call your submission.
- `data/`: interconnected records; `knowledge/`: policies and SOPs.
- `starter/agent.py`: editable agent entry point; `northstar/`: public mock API.
- `public/cases.json`: eight published scenarios and expected outcomes.
- `candidate-submission/`: fill DESIGN.md, EVALS.md, AI_BUILD_LOG.md and README.md.

Implement your approach behind POST `/process`. You may replace the starter, add dependencies and a UI, and change the agent image. Preserve the tool protocol; the hiring team grades against its own original API/data. Local API modifications are not used for hiring scores. Never read fixtures directly at runtime as a substitute for the scoped API. Public scenarios are examples, not a fixed production distribution.

Return a reproducible branch or repository, a commit SHA, completed templates and a ten-minute demo plan. Do not submit model keys or private tool transcripts. See `docs/SCORING.md` for the published scoring dimensions.
