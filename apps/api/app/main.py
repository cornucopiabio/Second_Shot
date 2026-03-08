from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field


app = FastAPI(title="Second Shot API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


RUNS: dict[str, dict[str, Any]] = {}


class ResolveIndicationRequest(BaseModel):
    query: str = Field(min_length=2)


class Match(BaseModel):
    label: str
    mondo_id: str


class ResolveIndicationResponse(BaseModel):
    matches: list[Match]


class RunCreateRequest(BaseModel):
    mondo_id: str
    top_k: int = Field(default=20, ge=1, le=100)
    enable_docking: bool = False


class DockPair(BaseModel):
    drug: str
    target: str


class DockRequest(BaseModel):
    pairs: list[DockPair] = Field(min_length=1)


class RunResponse(BaseModel):
    run_id: str
    status: Literal["queued", "running", "completed", "failed", "partial", "docking_running"]
    stage: str
    mondo_id: str
    top_k: int
    docking_enabled: bool
    candidates: list[dict[str, Any]] = Field(default_factory=list)
    score_breakdown: list[dict[str, Any]] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    created_at: str
    updated_at: str


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def resolve_query(query: str) -> list[Match]:
    q = query.lower().strip()
    lookup = {
        "lung fibrosis": [
            Match(label="pulmonary fibrosis", mondo_id="MONDO:0005950"),
            Match(label="idiopathic pulmonary fibrosis", mondo_id="MONDO:0006374"),
        ],
        "idiopathic pulmonary fibrosis": [
            Match(label="idiopathic pulmonary fibrosis", mondo_id="MONDO:0006374")
        ],
        "ulcerative colitis": [
            Match(label="ulcerative colitis", mondo_id="MONDO:0005101")
        ],
        "glioblastoma": [Match(label="glioblastoma", mondo_id="MONDO:0018177")],
    }

    if q in lookup:
        return lookup[q]

    # Fallback keeps flow usable for hackathon demo without failing hard.
    return [Match(label=query.strip().title(), mondo_id="MONDO:UNRESOLVED")]


def build_mock_candidates(mondo_id: str, top_k: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    pool = [
        {
            "drug": "Nintedanib",
            "target": "VEGFR2",
            "action": "inhibitor",
            "repurposing_score": 0.79,
            "why": "Antifibrotic-aligned kinase inhibition with mature safety data.",
        },
        {
            "drug": "Pirfenidone",
            "target": "TGF-beta axis",
            "action": "modulator",
            "repurposing_score": 0.74,
            "why": "Pathway-level alignment with profibrotic signaling control.",
        },
        {
            "drug": "Fasudil",
            "target": "ROCK1",
            "action": "inhibitor",
            "repurposing_score": 0.68,
            "why": "Upstream cytoskeletal/fibrotic remodeling intervention point.",
        },
    ]

    candidates: list[dict[str, Any]] = []
    for item in pool[: min(len(pool), top_k)]:
        candidate = {
            **item,
            "mondo_id": mondo_id,
            "score_breakdown": {
                "disease_target_relevance": round(item["repurposing_score"] * 0.95, 3),
                "pathway_intervention_fit": round(item["repurposing_score"] * 0.9, 3),
                "mechanism_directionality_fit": round(item["repurposing_score"] * 0.93, 3),
                "structural_plausibility": None,
                "repurposability_score": round(item["repurposing_score"] * 0.88, 3),
            },
        }
        candidates.append(candidate)

    score_breakdown = [
        {
            "drug": c["drug"],
            "target": c["target"],
            "score": c["repurposing_score"],
            "components": c["score_breakdown"],
        }
        for c in candidates
    ]

    return candidates, score_breakdown


@app.get("/health")
def health() -> dict[str, str]:
    return {
        "status": "ok",
        "service": "second-shot-api",
        "time": now_iso(),
    }


@app.post("/resolve-indication", response_model=ResolveIndicationResponse)
def resolve_indication(payload: ResolveIndicationRequest) -> ResolveIndicationResponse:
    return ResolveIndicationResponse(matches=resolve_query(payload.query))


@app.post("/runs", response_model=RunResponse)
def create_run(payload: RunCreateRequest) -> RunResponse:
    run_id = f"run_{uuid4().hex[:8]}"
    ts = now_iso()
    candidates, score_breakdown = build_mock_candidates(payload.mondo_id, payload.top_k)

    run = {
        "run_id": run_id,
        "status": "partial" if payload.enable_docking else "completed",
        "stage": "docking_pending" if payload.enable_docking else "finalized",
        "mondo_id": payload.mondo_id,
        "top_k": payload.top_k,
        "docking_enabled": payload.enable_docking,
        "candidates": candidates,
        "score_breakdown": score_breakdown,
        "limitations": [
            "Research-use only; not medical advice.",
            "Off-patent status is a heuristic unless jurisdictional data is integrated.",
        ],
        "created_at": ts,
        "updated_at": ts,
    }
    RUNS[run_id] = run
    return RunResponse(**run)


@app.get("/runs/{run_id}", response_model=RunResponse)
def get_run(run_id: str) -> RunResponse:
    run = RUNS.get(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return RunResponse(**run)


@app.post("/runs/{run_id}/dock", response_model=RunResponse)
def run_docking(run_id: str, payload: DockRequest) -> RunResponse:
    run = RUNS.get(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    run["status"] = "docking_running"
    run["stage"] = "docking"
    run["updated_at"] = now_iso()

    # MVP behavior: immediate mocked docking completion.
    for pair in payload.pairs:
        for candidate in run["candidates"]:
            if candidate["drug"] == pair.drug and candidate["target"] == pair.target:
                candidate["score_breakdown"]["structural_plausibility"] = 0.72
                candidate["repurposing_score"] = round(candidate["repurposing_score"] + 0.03, 3)

    run["candidates"] = sorted(
        run["candidates"], key=lambda c: c["repurposing_score"], reverse=True
    )
    run["score_breakdown"] = [
        {
            "drug": c["drug"],
            "target": c["target"],
            "score": c["repurposing_score"],
            "components": c["score_breakdown"],
        }
        for c in run["candidates"]
    ]
    run["status"] = "completed"
    run["stage"] = "finalized"
    run["updated_at"] = now_iso()
    return RunResponse(**run)


@app.get("/runs/{run_id}/report")
def get_report(run_id: str) -> dict[str, Any]:
    run = RUNS.get(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    top = run["candidates"][:3]
    summary = (
        f"Run {run_id} prioritized {len(run['candidates'])} candidates for {run['mondo_id']}. "
        f"Top candidate: {top[0]['drug']} targeting {top[0]['target']}."
        if top
        else f"Run {run_id} has no candidates yet."
    )
    return {
        "summary": summary,
        "top_candidates": top,
        "limitations": run["limitations"],
    }
