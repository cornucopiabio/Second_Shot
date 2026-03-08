# API Contracts (MVP)

## Endpoints

- `GET /health`
- `POST /resolve-indication`
- `POST /runs`
- `GET /runs/{run_id}`
- `POST /runs/{run_id}/dock`
- `GET /runs/{run_id}/report`

## Status Model

Run status values:

- `queued`
- `running`
- `partial`
- `docking_running`
- `completed`
- `failed`
