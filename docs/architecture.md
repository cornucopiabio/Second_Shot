# Architecture

This project uses a two-app MVP architecture:

- `apps/web`: Next.js frontend for indication resolution, run execution, and result display
- `apps/api`: FastAPI orchestrator for retrieval, ranking, and optional docking workflows

Shared roadmap directories are pre-created for `services/*` and `packages/*` so the system can evolve into modular services without restructure churn.
