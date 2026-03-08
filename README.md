# Second Shot

Indication-first drug repurposing MVP scaffold.

## What is implemented

- `apps/api` FastAPI service with:
  - `GET /health`
  - `POST /resolve-indication`
  - `POST /runs`
  - `GET /runs/{run_id}`
  - `POST /runs/{run_id}/dock`
  - `GET /runs/{run_id}/report`
- `apps/web` Next.js UI for resolving an indication and running a ranking flow
- `infra/docker-compose.yml` for local Postgres + Redis
- `docs/system-design.md` with component and runtime sequence diagrams
- Live retrieval integrations:
  - MONDO resolution via OLS4 MONDO API
  - disease-target evidence via Open Targets GraphQL
  - pathway context via Reactome ContentService
- Anthropic-driven re-ranking in the candidate pipeline (no GPT dependency)
- Tamarind-backed docking integration in `POST /runs/{run_id}/dock`
- Offline-safe fallback behavior for demo continuity when external APIs are unavailable

## Local setup

1. Copy envs:
   - `cp .env.example .env`
2. Start infrastructure:
   - `docker compose -f infra/docker-compose.yml up -d`
3. API service:
   - `cd apps/api`
   - `python3 -m venv .venv && source .venv/bin/activate`
   - `pip install -r requirements.txt`
   - `uvicorn app.main:app --reload --host 0.0.0.0 --port 8000`
4. Web app (new terminal at repo root):
   - `npm install`
   - `npm run dev:web`

## Quality commands

- `npm run lint`
- `npm run test`
