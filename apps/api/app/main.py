from __future__ import annotations

from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Literal
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from app.pipeline import BioPipeline, PipelineError, ResolvedTerm, TermOption
from app.providers import AnthropicRanker, TamarindDockingClient


app = FastAPI(title="Second Shot API", version="0.2.0")

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
    open_targets_id: str | None = None
    target_count: int = 0
    runnable: bool = False
    requires_refinement: bool = False
    synonyms: list[str] = Field(default_factory=list)
    parents: list["TermNode"] = Field(default_factory=list)
    refinements: list["TermNode"] = Field(default_factory=list)


class TermNode(BaseModel):
    label: str
    mondo_id: str
    open_targets_id: str | None = None
    target_count: int = 0
    runnable: bool = False


class ResolveIndicationResponse(BaseModel):
    matches: list[Match]


class RunCreateRequest(BaseModel):
    mondo_id: str
    disease_id: str | None = None
    label: str | None = None
    top_k: int = Field(default=20, ge=1, le=100)
    enable_docking: bool = False


class DockPair(BaseModel):
    drug: str
    target: str


class DockRequest(BaseModel):
    pairs: list[DockPair] = Field(min_length=1)


class RunResponse(BaseModel):
    run_id: str
    status: Literal[
        "queued",
        "running",
        "completed",
        "failed",
        "partial",
        "docking_running",
    ]
    stage: str
    mondo_id: str
    disease_id: str | None = None
    top_k: int
    docking_enabled: bool
    candidates: list[dict[str, Any]] = Field(default_factory=list)
    score_breakdown: list[dict[str, Any]] = Field(default_factory=list)
    targets: list[dict[str, Any]] = Field(default_factory=list)
    pathways: list[dict[str, Any]] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    created_at: str
    updated_at: str


Match.model_rebuild()


@lru_cache(maxsize=1)
def get_pipeline() -> BioPipeline:
    return BioPipeline()


@lru_cache(maxsize=1)
def get_anthropic_ranker() -> AnthropicRanker:
    return AnthropicRanker()


@lru_cache(maxsize=1)
def get_tamarind_client() -> TamarindDockingClient:
    return TamarindDockingClient()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_score_breakdown(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "drug": candidate["drug"],
            "target": candidate["target"],
            "score": candidate["repurposing_score"],
            "components": candidate["score_breakdown"],
        }
        for candidate in candidates
    ]


@app.get("/health")
def health() -> dict[str, str]:
    return {
        "status": "ok",
        "service": "second-shot-api",
        "time": now_iso(),
    }


@app.post("/resolve-indication", response_model=ResolveIndicationResponse)
def resolve_indication(payload: ResolveIndicationRequest) -> ResolveIndicationResponse:
    pipeline = get_pipeline()
    matches: list[ResolvedTerm] = pipeline.resolve_indication(payload.query)

    normalized = [
        Match(
            label=match.label,
            mondo_id=match.mondo_id,
            open_targets_id=match.open_targets_id,
            target_count=match.target_count,
            runnable=match.runnable,
            requires_refinement=match.requires_refinement,
            synonyms=match.synonyms,
            parents=[
                TermNode(
                    label=parent.label,
                    mondo_id=parent.mondo_id,
                    open_targets_id=parent.open_targets_id,
                    target_count=parent.target_count,
                    runnable=parent.runnable,
                )
                for parent in match.parents
            ],
            refinements=[
                TermNode(
                    label=refinement.label,
                    mondo_id=refinement.mondo_id,
                    open_targets_id=refinement.open_targets_id,
                    target_count=refinement.target_count,
                    runnable=refinement.runnable,
                )
                for refinement in match.refinements
            ],
        )
        for match in matches
    ]
    return ResolveIndicationResponse(matches=normalized)


@app.post("/runs", response_model=RunResponse)
def create_run(payload: RunCreateRequest) -> RunResponse:
    run_id = f"run_{uuid4().hex[:8]}"
    ts = now_iso()

    pipeline = get_pipeline()

    try:
        pipeline_result = pipeline.build_run(
            mondo_id=payload.mondo_id,
            top_k=payload.top_k,
            disease_id=payload.disease_id,
            label=payload.label,
        )
    except PipelineError as error:
        run = {
            "run_id": run_id,
            "status": "failed",
            "stage": "failed",
            "mondo_id": payload.mondo_id,
            "disease_id": payload.disease_id,
            "top_k": payload.top_k,
            "docking_enabled": payload.enable_docking,
            "candidates": [],
            "score_breakdown": [],
            "targets": [],
            "pathways": [],
            "limitations": [f"Pipeline failure: {error}"],
            "created_at": ts,
            "updated_at": ts,
        }
        RUNS[run_id] = run
        return RunResponse(**run)

    run = {
        "run_id": run_id,
        "status": "partial" if payload.enable_docking else "completed",
        "stage": "docking_pending" if payload.enable_docking else "finalized",
        "mondo_id": payload.mondo_id,
        "disease_id": pipeline_result.disease_id,
        "top_k": payload.top_k,
        "docking_enabled": payload.enable_docking,
        "candidates": pipeline_result.candidates,
        "score_breakdown": pipeline_result.score_breakdown,
        "targets": pipeline_result.targets,
        "pathways": pipeline_result.pathways,
        "limitations": pipeline_result.limitations,
        "created_at": ts,
        "updated_at": ts,
    }

    anthro_ranker = get_anthropic_ranker()
    reranked_candidates, anthropic_note = anthro_ranker.rerank(
        mondo_id=payload.mondo_id,
        targets=pipeline_result.targets,
        pathways=pipeline_result.pathways,
        candidates=run["candidates"],
    )
    run["candidates"] = reranked_candidates
    run["score_breakdown"] = build_score_breakdown(run["candidates"])
    if anthropic_note:
        run["limitations"].append(anthropic_note)

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

    tamarind = get_tamarind_client()
    pair_payload = [{"drug": pair.drug, "target": pair.target} for pair in payload.pairs]
    docking_scores, docking_note = tamarind.dock_pairs(pair_payload)
    if docking_note:
        run["limitations"].append(docking_note)

    for pair in payload.pairs:
        key = (pair.drug, pair.target)
        structural_score = docking_scores.get(key, 0.72)
        for candidate in run["candidates"]:
            if candidate["drug"] == pair.drug and candidate["target"] == pair.target:
                candidate["score_breakdown"]["structural_plausibility"] = structural_score
                candidate["repurposing_score"] = BioPipeline._weighted_score(
                    disease_target_relevance=float(
                        candidate["score_breakdown"].get("disease_target_relevance", 0.0)
                    ),
                    pathway_intervention_fit=float(
                        candidate["score_breakdown"].get("pathway_intervention_fit", 0.0)
                    ),
                    mechanism_directionality_fit=float(
                        candidate["score_breakdown"].get("mechanism_directionality_fit", 0.0)
                    ),
                    repurposability_score=float(
                        candidate["score_breakdown"].get("repurposability_score", 0.0)
                    ),
                    structural_plausibility=structural_score,
                )

    run["candidates"] = sorted(
        run["candidates"], key=lambda c: c["repurposing_score"], reverse=True
    )
    run["score_breakdown"] = build_score_breakdown(run["candidates"])
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
    top_target = run["targets"][0]["gene_symbol"] if run.get("targets") else "unknown"
    top_pathway = run["pathways"][0]["pathway_name"] if run.get("pathways") else "n/a"

    summary = (
        f"Run {run_id} prioritized {len(run['candidates'])} candidates for {run['mondo_id']}. "
        f"Top candidate: {top[0]['drug']} targeting {top[0]['target']}. "
        f"Primary target signal: {top_target}. Primary pathway: {top_pathway}."
        if top
        else f"Run {run_id} has no candidates yet."
    )
    return {
        "summary": summary,
        "top_candidates": top,
        "limitations": run["limitations"],
    }
