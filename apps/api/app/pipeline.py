from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx


class PipelineError(RuntimeError):
    """Raised when a run cannot produce a usable biological context."""


@dataclass(frozen=True)
class MondoMatch:
    label: str
    mondo_id: str


@dataclass(frozen=True)
class TargetEvidence:
    id: str
    symbol: str
    name: str
    evidence_score: float


@dataclass(frozen=True)
class Pathway:
    pathway_id: str
    name: str
    source: str
    relevance_score: float
    druggable_nodes: list[str]


@dataclass(frozen=True)
class PipelineResult:
    candidates: list[dict[str, Any]]
    score_breakdown: list[dict[str, Any]]
    limitations: list[str]
    targets: list[dict[str, Any]]
    pathways: list[dict[str, Any]]


class MondoResolver:
    """Resolve free-text disease queries to MONDO terms via OLS4."""

    def __init__(self, base_url: str, timeout_seconds: float = 8.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout_seconds

    def search(self, query: str, limit: int = 5) -> list[MondoMatch]:
        url = f"{self.base_url}/search"
        params = {
            "q": query,
            "ontology": "mondo",
            "type": "class",
            "rows": str(max(limit, 1)),
        }
        response = httpx.get(url, params=params, timeout=self.timeout)
        response.raise_for_status()

        payload = response.json()
        docs = payload.get("response", {}).get("docs", [])

        matches: list[MondoMatch] = []
        for doc in docs:
            label_value = doc.get("label")
            if isinstance(label_value, list):
                label = str(label_value[0]) if label_value else ""
            else:
                label = str(label_value or "")

            mondo_id = str(doc.get("obo_id") or doc.get("short_form") or "")
            if mondo_id.startswith("MONDO_"):
                mondo_id = mondo_id.replace("MONDO_", "MONDO:", 1)

            if not label or not mondo_id.startswith("MONDO:"):
                continue

            matches.append(MondoMatch(label=label, mondo_id=mondo_id))

            if len(matches) >= limit:
                break

        return matches


class OpenTargetsClient:
    """Fetch disease-associated targets from Open Targets GraphQL."""

    def __init__(self, endpoint: str, timeout_seconds: float = 12.0) -> None:
        self.endpoint = endpoint
        self.timeout = timeout_seconds

    def fetch_associated_targets(self, disease_id: str, limit: int) -> list[TargetEvidence]:
        query = """
        query DiseaseTargets($diseaseId: String!, $size: Int!) {
          disease(efoId: $diseaseId) {
            id
            name
            associatedTargets(page: {index: 0, size: $size}) {
              rows {
                score
                target {
                  id
                  approvedSymbol
                  approvedName
                }
              }
            }
          }
        }
        """
        body = {
            "query": query,
            "variables": {
                "diseaseId": disease_id,
                "size": max(limit, 1),
            },
        }
        response = httpx.post(self.endpoint, json=body, timeout=self.timeout)
        response.raise_for_status()

        payload = response.json()
        if payload.get("errors"):
            raise PipelineError(f"Open Targets returned GraphQL errors: {payload['errors']}")

        disease = payload.get("data", {}).get("disease")
        if not disease:
            return []

        rows = disease.get("associatedTargets", {}).get("rows", [])
        targets: list[TargetEvidence] = []
        for row in rows:
            target = row.get("target") or {}
            symbol = str(target.get("approvedSymbol") or "").strip()
            target_id = str(target.get("id") or "").strip()
            target_name = str(target.get("approvedName") or symbol)
            score = float(row.get("score") or 0.0)

            if not symbol:
                continue

            targets.append(
                TargetEvidence(
                    id=target_id,
                    symbol=symbol,
                    name=target_name,
                    evidence_score=max(0.0, min(score, 1.0)),
                )
            )

        targets.sort(key=lambda t: t.evidence_score, reverse=True)
        return targets


class ReactomeClient:
    """Retrieve pathway memberships from Reactome Content Service."""

    def __init__(self, base_url: str, timeout_seconds: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout_seconds

    def _fetch_entity_pathways(self, identifier: str) -> list[dict[str, Any]]:
        encoded = quote(identifier, safe="")
        url = f"{self.base_url}/data/pathways/low/entity/{encoded}/allForms"
        response = httpx.get(url, timeout=self.timeout)

        if response.status_code == 404:
            return []

        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, list) else []

    def fetch_pathways_for_targets(
        self, targets: list[TargetEvidence], limit: int = 25
    ) -> tuple[list[Pathway], dict[str, float]]:
        if not targets:
            return [], {}

        totals = sum(t.evidence_score for t in targets) or 1.0

        pathway_state: dict[str, dict[str, Any]] = {}
        target_pathway_signal: dict[str, float] = {}

        for target in targets:
            entries = self._fetch_entity_pathways(target.id) if target.id else []
            if not entries:
                entries = self._fetch_entity_pathways(target.symbol)

            local_max = 0.0
            for entry in entries:
                pathway_id = str(entry.get("stId") or entry.get("dbId") or "").strip()
                pathway_name = str(entry.get("displayName") or entry.get("name") or "").strip()
                species = str(entry.get("speciesName") or "")

                if not pathway_id or not pathway_name:
                    continue
                if species and species.lower() not in {"homo sapiens", "human"}:
                    continue

                state = pathway_state.setdefault(
                    pathway_id,
                    {
                        "pathway_id": pathway_id,
                        "name": pathway_name,
                        "source": "Reactome",
                        "score": 0.0,
                        "nodes": set(),
                    },
                )
                state["score"] += target.evidence_score
                state["nodes"].add(target.symbol)

                local_max = max(local_max, target.evidence_score)

            target_pathway_signal[target.symbol] = local_max

        pathways: list[Pathway] = []
        for state in pathway_state.values():
            nodes = sorted(str(node) for node in state["nodes"])
            relevance = max(0.0, min(state["score"] / totals, 1.0))
            pathways.append(
                Pathway(
                    pathway_id=state["pathway_id"],
                    name=state["name"],
                    source=state["source"],
                    relevance_score=relevance,
                    druggable_nodes=nodes,
                )
            )

        pathways.sort(key=lambda p: p.relevance_score, reverse=True)

        # Normalize per-target pathway signal into 0-1 range.
        max_signal = max(target_pathway_signal.values(), default=0.0)
        if max_signal > 0:
            target_pathway_signal = {
                symbol: round(signal / max_signal, 3)
                for symbol, signal in target_pathway_signal.items()
            }
        else:
            target_pathway_signal = {symbol: 0.0 for symbol in target_pathway_signal}

        return pathways[:limit], target_pathway_signal


class BioPipeline:
    """End-to-end retrieval + heuristic ranking pipeline for the MVP."""

    MONDO_FALLBACKS: dict[str, list[MondoMatch]] = {
        "lung fibrosis": [
            MondoMatch(label="pulmonary fibrosis", mondo_id="MONDO:0005950"),
            MondoMatch(label="idiopathic pulmonary fibrosis", mondo_id="MONDO:0006374"),
        ],
        "idiopathic pulmonary fibrosis": [
            MondoMatch(label="idiopathic pulmonary fibrosis", mondo_id="MONDO:0006374")
        ],
        "ulcerative colitis": [MondoMatch(label="ulcerative colitis", mondo_id="MONDO:0005101")],
        "glioblastoma": [MondoMatch(label="glioblastoma", mondo_id="MONDO:0018177")],
    }

    TARGET_FALLBACKS: dict[str, list[TargetEvidence]] = {
        "MONDO:0005950": [
            TargetEvidence("ENSG00000105329", "TGFB1", "transforming growth factor beta 1", 0.82),
            TargetEvidence("ENSG00000164362", "TGFBR1", "transforming growth factor beta receptor 1", 0.76),
            TargetEvidence("ENSG00000128052", "KDR", "kinase insert domain receptor", 0.61),
            TargetEvidence(
                "ENSG00000170421",
                "ROCK1",
                "Rho associated coiled-coil containing protein kinase 1",
                0.58,
            ),
        ],
        "MONDO:0006374": [
            TargetEvidence("ENSG00000105329", "TGFB1", "transforming growth factor beta 1", 0.86),
            TargetEvidence("ENSG00000164362", "TGFBR1", "transforming growth factor beta receptor 1", 0.79),
            TargetEvidence("ENSG00000157764", "BRAF", "B-Raf proto-oncogene", 0.57),
            TargetEvidence("ENSG00000128052", "KDR", "kinase insert domain receptor", 0.63),
            TargetEvidence("ENSG00000170421", "ROCK1", "Rho associated coiled-coil containing protein kinase 1", 0.6),
        ],
        "MONDO:0005101": [
            TargetEvidence("ENSG00000160712", "IL6R", "interleukin 6 receptor", 0.73),
            TargetEvidence("ENSG00000168032", "TNF", "tumor necrosis factor", 0.77),
            TargetEvidence("ENSG00000133703", "PTGS2", "prostaglandin-endoperoxide synthase 2", 0.58),
        ],
        "MONDO:0018177": [
            TargetEvidence("ENSG00000146648", "EGFR", "epidermal growth factor receptor", 0.81),
            TargetEvidence("ENSG00000157764", "BRAF", "B-Raf proto-oncogene", 0.62),
        ],
    }

    PATHWAY_FALLBACKS: dict[str, list[Pathway]] = {
        "MONDO:0005950": [
            Pathway(
                pathway_id="R-HSA-170834",
                name="Signaling by TGF-beta receptor complex",
                source="Reactome",
                relevance_score=0.91,
                druggable_nodes=["TGFB1", "TGFBR1", "SMAD3"],
            )
        ],
        "MONDO:0006374": [
            Pathway(
                pathway_id="R-HSA-170834",
                name="Signaling by TGF-beta receptor complex",
                source="Reactome",
                relevance_score=0.94,
                druggable_nodes=["TGFB1", "TGFBR1", "SMAD3"],
            ),
            Pathway(
                pathway_id="R-HSA-387039",
                name="PTK6 promotes HIF1A stabilization",
                source="Reactome",
                relevance_score=0.61,
                druggable_nodes=["KDR"],
            ),
        ],
        "MONDO:0005101": [
            Pathway(
                pathway_id="R-HSA-6785807",
                name="Interleukin-6 signaling",
                source="Reactome",
                relevance_score=0.83,
                druggable_nodes=["IL6R", "JAK1", "JAK2"],
            ),
            Pathway(
                pathway_id="R-HSA-5668541",
                name="TNF signaling",
                source="Reactome",
                relevance_score=0.86,
                druggable_nodes=["TNF", "NFKB1"],
            ),
        ],
        "MONDO:0018177": [
            Pathway(
                pathway_id="R-HSA-177929",
                name="Signaling by EGFR",
                source="Reactome",
                relevance_score=0.88,
                druggable_nodes=["EGFR", "GRB2", "SOS1"],
            )
        ],
    }

    DRUG_KB: list[dict[str, Any]] = [
        {
            "drug": "Nintedanib",
            "target_gene": "KDR",
            "action": "inhibitor",
            "repurposability_score": 0.82,
            "approved_status": "approved",
        },
        {
            "drug": "Fasudil",
            "target_gene": "ROCK1",
            "action": "inhibitor",
            "repurposability_score": 0.71,
            "approved_status": "approved_in_some_markets",
        },
        {
            "drug": "Tocilizumab",
            "target_gene": "IL6R",
            "action": "antagonist",
            "repurposability_score": 0.78,
            "approved_status": "approved",
        },
        {
            "drug": "Adalimumab",
            "target_gene": "TNF",
            "action": "antibody",
            "repurposability_score": 0.85,
            "approved_status": "approved",
        },
        {
            "drug": "Celecoxib",
            "target_gene": "PTGS2",
            "action": "inhibitor",
            "repurposability_score": 0.8,
            "approved_status": "approved",
        },
        {
            "drug": "Erlotinib",
            "target_gene": "EGFR",
            "action": "inhibitor",
            "repurposability_score": 0.76,
            "approved_status": "approved",
        },
    ]

    def __init__(self) -> None:
        mondo_url = os.getenv("MONDO_API_BASE_URL", "https://www.ebi.ac.uk/ols4/api")
        open_targets_url = os.getenv(
            "OPEN_TARGETS_GRAPHQL_URL", "https://api.platform.opentargets.org/api/v4/graphql"
        )
        reactome_url = os.getenv("REACTOME_BASE_URL", "https://reactome.org/ContentService")

        timeout = float(os.getenv("API_TIMEOUT_SECONDS", "8"))

        self.mondo = MondoResolver(base_url=mondo_url, timeout_seconds=timeout)
        self.open_targets = OpenTargetsClient(endpoint=open_targets_url, timeout_seconds=timeout + 2)
        self.reactome = ReactomeClient(base_url=reactome_url, timeout_seconds=timeout)

    def resolve_indication(self, query: str, limit: int = 5) -> list[MondoMatch]:
        normalized = query.strip().lower()
        limitations: list[str] = []

        try:
            matches = self.mondo.search(query, limit=limit)
            if matches:
                return matches
            limitations.append("MONDO resolver returned no ontology matches.")
        except (httpx.HTTPError, ValueError):
            limitations.append("MONDO resolver unavailable; used fallback disease mapping.")

        if normalized in self.MONDO_FALLBACKS:
            return self.MONDO_FALLBACKS[normalized]

        return [MondoMatch(label=query.strip().title(), mondo_id="MONDO:UNRESOLVED")]

    def build_run(self, mondo_id: str, top_k: int) -> PipelineResult:
        limitations = [
            "Research-use only; not medical advice.",
            "Off-patent status is a heuristic unless jurisdictional data is integrated.",
        ]

        targets: list[TargetEvidence]
        try:
            targets = self.open_targets.fetch_associated_targets(
                disease_id=mondo_id,
                limit=max(top_k * 3, 30),
            )
            if not targets:
                limitations.append("Open Targets returned no associated targets for this disease ID.")
                targets = self.TARGET_FALLBACKS.get(mondo_id, [])
        except (httpx.HTTPError, PipelineError, ValueError):
            limitations.append("Open Targets unavailable; used fallback target set when possible.")
            targets = self.TARGET_FALLBACKS.get(mondo_id, [])

        if not targets:
            raise PipelineError("No targets available for this indication.")

        pathways: list[Pathway]
        target_pathway_signal: dict[str, float]

        try:
            pathways, target_pathway_signal = self.reactome.fetch_pathways_for_targets(targets)
            if not pathways:
                limitations.append("Reactome returned no pathways; using target-only scoring.")
                pathways = self.PATHWAY_FALLBACKS.get(mondo_id, [])
                target_pathway_signal = {
                    target.symbol: round(target.evidence_score, 3) for target in targets
                }
        except (httpx.HTTPError, ValueError):
            limitations.append("Reactome unavailable; used fallback pathway context when possible.")
            pathways = self.PATHWAY_FALLBACKS.get(mondo_id, [])
            target_pathway_signal = {
                target.symbol: round(target.evidence_score, 3) for target in targets
            }

        candidates = self._build_candidates(mondo_id, targets, pathways, target_pathway_signal, top_k)
        if not candidates:
            raise PipelineError("No candidate drugs could be generated from retrieved context.")

        score_breakdown = [
            {
                "drug": c["drug"],
                "target": c["target"],
                "score": c["repurposing_score"],
                "components": c["score_breakdown"],
            }
            for c in candidates
        ]

        target_payload = [
            {
                "id": target.id,
                "gene_symbol": target.symbol,
                "name": target.name,
                "evidence_score": target.evidence_score,
            }
            for target in targets[: max(top_k, 10)]
        ]

        pathway_payload = [
            {
                "pathway_id": pathway.pathway_id,
                "pathway_name": pathway.name,
                "source": pathway.source,
                "relevance_score": pathway.relevance_score,
                "druggable_nodes": pathway.druggable_nodes,
            }
            for pathway in pathways[: max(top_k, 10)]
        ]

        return PipelineResult(
            candidates=candidates,
            score_breakdown=score_breakdown,
            limitations=limitations,
            targets=target_payload,
            pathways=pathway_payload,
        )

    @staticmethod
    def _weighted_score(
        disease_target_relevance: float,
        pathway_intervention_fit: float,
        mechanism_directionality_fit: float,
        repurposability_score: float,
        structural_plausibility: float | None,
    ) -> float:
        weights: dict[str, float] = {
            "disease_target_relevance": 0.30,
            "pathway_intervention_fit": 0.25,
            "mechanism_directionality_fit": 0.20,
            "repurposability_score": 0.10,
        }
        values: dict[str, float] = {
            "disease_target_relevance": disease_target_relevance,
            "pathway_intervention_fit": pathway_intervention_fit,
            "mechanism_directionality_fit": mechanism_directionality_fit,
            "repurposability_score": repurposability_score,
        }

        if structural_plausibility is not None:
            weights["structural_plausibility"] = 0.15
            values["structural_plausibility"] = structural_plausibility

        weight_sum = sum(weights.values()) or 1.0
        raw = sum(values[key] * weight for key, weight in weights.items())
        return round(raw / weight_sum, 3)

    def _build_candidates(
        self,
        mondo_id: str,
        targets: list[TargetEvidence],
        pathways: list[Pathway],
        target_pathway_signal: dict[str, float],
        top_k: int,
    ) -> list[dict[str, Any]]:
        pathway_lookup: dict[str, float] = {symbol: 0.0 for symbol in target_pathway_signal}
        for symbol, signal in target_pathway_signal.items():
            pathway_lookup[symbol] = signal

        target_by_symbol = {target.symbol: target for target in targets}

        candidates: list[dict[str, Any]] = []
        for record in self.DRUG_KB:
            symbol = record["target_gene"]
            target = target_by_symbol.get(symbol)
            if target is None:
                continue

            disease_target_relevance = round(target.evidence_score, 3)
            pathway_intervention_fit = round(max(pathway_lookup.get(symbol, 0.0), 0.15), 3)

            action = str(record["action"])
            mechanism_directionality_fit = 0.78 if action in {"inhibitor", "antagonist"} else 0.68
            repurposability_score = float(record["repurposability_score"])

            score_breakdown = {
                "disease_target_relevance": disease_target_relevance,
                "pathway_intervention_fit": pathway_intervention_fit,
                "mechanism_directionality_fit": mechanism_directionality_fit,
                "structural_plausibility": None,
                "repurposability_score": repurposability_score,
            }
            repurposing_score = self._weighted_score(
                disease_target_relevance=disease_target_relevance,
                pathway_intervention_fit=pathway_intervention_fit,
                mechanism_directionality_fit=mechanism_directionality_fit,
                repurposability_score=repurposability_score,
                structural_plausibility=None,
            )

            pathway_hits = [
                pathway.name
                for pathway in pathways
                if symbol in pathway.druggable_nodes
            ][:2]

            candidates.append(
                {
                    "drug": record["drug"],
                    "target": symbol,
                    "action": action,
                    "approved_status": record["approved_status"],
                    "mondo_id": mondo_id,
                    "repurposing_score": repurposing_score,
                    "why": (
                        f"Target {symbol} is associated with {mondo_id} in Open Targets and "
                        f"maps to Reactome pathways {pathway_hits or ['context unavailable']}."
                    ),
                    "score_breakdown": score_breakdown,
                }
            )

        candidates.sort(key=lambda c: c["repurposing_score"], reverse=True)
        return candidates[:top_k]
