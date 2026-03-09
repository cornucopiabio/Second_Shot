from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import httpx

from app.env import load_repo_env


load_repo_env()


logger = logging.getLogger(__name__)


class PipelineError(RuntimeError):
    """Raised when a run cannot produce a usable biological context."""


@dataclass(frozen=True)
class MondoMatch:
    label: str
    mondo_id: str


@dataclass(frozen=True)
class TermOption:
    label: str
    mondo_id: str
    open_targets_id: str | None = None
    target_count: int = 0
    runnable: bool = False


@dataclass(frozen=True)
class ResolvedTerm:
    label: str
    mondo_id: str
    open_targets_id: str | None
    target_count: int
    runnable: bool
    requires_refinement: bool
    synonyms: list[str] = field(default_factory=list)
    parents: list[TermOption] = field(default_factory=list)
    refinements: list[TermOption] = field(default_factory=list)


@dataclass(frozen=True)
class TargetEvidence:
    id: str
    symbol: str
    name: str
    evidence_score: float


@dataclass(frozen=True)
class DrugRecord:
    drug_id: str
    name: str
    target_gene: str
    action: str
    source: str
    repurposability_score: float
    approved_status: str
    max_phase: int = 0
    disease_id: str | None = None
    disease_name: str | None = None
    mechanism_of_action: str | None = None
    is_withdrawn: bool = False
    black_box_warning: bool = False


@dataclass(frozen=True)
class Pathway:
    pathway_id: str
    name: str
    source: str
    relevance_score: float
    druggable_nodes: list[str]


@dataclass(frozen=True)
class PipelineResult:
    disease_id: str
    candidates: list[dict[str, Any]]
    score_breakdown: list[dict[str, Any]]
    limitations: list[str]
    targets: list[dict[str, Any]]
    pathways: list[dict[str, Any]]


def normalize_label(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def mondo_to_iri(mondo_id: str) -> str:
    return f"http://purl.obolibrary.org/obo/{mondo_id.replace(':', '_')}"


def mondo_from_ols_id(raw_id: str) -> str:
    if raw_id.startswith("MONDO_"):
        return raw_id.replace("MONDO_", "MONDO:", 1)
    return raw_id


def xref_to_open_targets_id(xref: str) -> str | None:
    prefix, _, value = xref.partition(":")
    if not prefix or not value:
        return None
    if prefix in {"EFO", "MONDO", "HP"}:
        return f"{prefix}_{value}"
    if prefix == "Orphanet":
        return f"Orphanet_{value}"
    return None


def relation_from_term(term: dict[str, Any], relation: str) -> str | None:
    links = term.get("_links") or {}
    href = (links.get(relation) or {}).get("href")
    if not href:
        return None
    return str(href).replace("http://", "https://", 1)


def term_option_from_payload(
    label: str,
    mondo_id: str,
    open_targets_id: str | None = None,
    target_count: int = 0,
) -> TermOption:
    return TermOption(
        label=label,
        mondo_id=mondo_id,
        open_targets_id=open_targets_id,
        target_count=target_count,
        runnable=target_count > 0 and bool(open_targets_id),
    )


class MondoResolver:
    """Resolve free-text disease queries and related hierarchy from OLS4."""

    def __init__(self, base_url: str, timeout_seconds: float = 8.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout_seconds
        self._term_cache: dict[str, dict[str, Any]] = {}
        self._relation_cache: dict[tuple[str, str, int], list[dict[str, Any]]] = {}

    def search(self, query: str, limit: int = 5) -> list[MondoMatch]:
        response = httpx.get(
            f"{self.base_url}/search",
            params={
                "q": query,
                "ontology": "mondo",
                "type": "class",
                "rows": str(max(limit, 1)),
            },
            timeout=self.timeout,
        )
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

            mondo_id = mondo_from_ols_id(str(doc.get("obo_id") or doc.get("short_form") or ""))
            if not label or not mondo_id.startswith("MONDO:"):
                continue

            matches.append(MondoMatch(label=label, mondo_id=mondo_id))
            if len(matches) >= limit:
                break

        return matches

    def get_term(self, mondo_id: str) -> dict[str, Any] | None:
        if mondo_id in self._term_cache:
            cached = self._term_cache[mondo_id]
            return cached or None

        response = httpx.get(
            f"{self.base_url}/terms",
            params={"iri": mondo_to_iri(mondo_id), "lang": "en", "ontology": "mondo"},
            timeout=self.timeout,
        )
        response.raise_for_status()

        payload = response.json()
        terms = payload.get("_embedded", {}).get("terms", [])
        term = terms[0] if terms else {}
        self._term_cache[mondo_id] = term
        return term or None

    def get_related_terms(self, mondo_id: str, relation: str, size: int = 10) -> list[dict[str, Any]]:
        cache_key = (mondo_id, relation, size)
        if cache_key in self._relation_cache:
            return self._relation_cache[cache_key]

        term = self.get_term(mondo_id)
        if not term:
            self._relation_cache[cache_key] = []
            return []

        href = relation_from_term(term, relation)
        if not href:
            self._relation_cache[cache_key] = []
            return []

        joiner = "&" if "?" in href else "?"
        response = httpx.get(f"{href}{joiner}size={max(size, 1)}", timeout=self.timeout)
        response.raise_for_status()

        payload = response.json()
        related = payload.get("_embedded", {}).get("terms", [])
        self._relation_cache[cache_key] = related
        return related


class OpenTargetsClient:
    """Fetch disease-associated targets from Open Targets GraphQL."""

    def __init__(self, endpoint: str, timeout_seconds: float = 12.0) -> None:
        self.endpoint = endpoint
        self.timeout = timeout_seconds
        self._search_cache: dict[str, list[dict[str, str]]] = {}
        self._disease_cache: dict[str, dict[str, Any] | None] = {}
        self._known_drugs_cache: dict[tuple[str, int], list[DrugRecord]] = {}

    def _post(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        response = httpx.post(
            self.endpoint,
            json={"query": query, "variables": variables},
            timeout=self.timeout,
        )
        response.raise_for_status()

        payload = response.json()
        if payload.get("errors"):
            raise PipelineError(f"Open Targets returned GraphQL errors: {payload['errors']}")
        return payload

    def search_diseases(self, query_string: str, limit: int = 10) -> list[dict[str, str]]:
        normalized = query_string.strip().lower()
        if normalized in self._search_cache:
            return self._search_cache[normalized]

        payload = self._post(
            """
            query Search($queryString: String!) {
              search(queryString: $queryString) {
                hits {
                  id
                  entity
                  object {
                    ... on Disease {
                      id
                      name
                    }
                  }
                }
              }
            }
            """,
            {"queryString": query_string},
        )
        hits = payload.get("data", {}).get("search", {}).get("hits", [])

        results: list[dict[str, str]] = []
        for hit in hits:
            if str(hit.get("entity") or "") != "disease":
                continue
            obj = hit.get("object") or {}
            disease_id = str(obj.get("id") or hit.get("id") or "").strip()
            name = str(obj.get("name") or "").strip()
            if not disease_id or not name:
                continue
            results.append({"id": disease_id, "name": name})
            if len(results) >= limit:
                break

        self._search_cache[normalized] = results
        return results

    def get_disease(self, disease_id: str) -> dict[str, Any] | None:
        if disease_id in self._disease_cache:
            return self._disease_cache[disease_id]

        payload = self._post(
            """
            query DiseaseSummary($diseaseId: String!) {
              disease(efoId: $diseaseId) {
                id
                name
                associatedTargets(page: {index: 0, size: 1}) {
                  count
                }
              }
            }
            """,
            {"diseaseId": disease_id},
        )
        disease = payload.get("data", {}).get("disease")
        self._disease_cache[disease_id] = disease
        return disease

    def fetch_target_count(self, disease_id: str) -> int | None:
        disease = self.get_disease(disease_id)
        if not disease:
            return None
        return int(disease.get("associatedTargets", {}).get("count") or 0)

    def fetch_associated_targets(self, disease_id: str, limit: int) -> list[TargetEvidence]:
        payload = self._post(
            """
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
            """,
            {"diseaseId": disease_id, "size": max(limit, 1)},
        )
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

        targets.sort(key=lambda target: target.evidence_score, reverse=True)
        return targets

    def fetch_known_drugs_for_target(self, target_id: str, limit: int = 20) -> list[DrugRecord]:
        cache_key = (target_id, limit)
        if cache_key in self._known_drugs_cache:
            return self._known_drugs_cache[cache_key]

        payload = self._post(
            """
            query KnownDrugs($ensemblId: String!, $size: Int!) {
              target(ensemblId: $ensemblId) {
                approvedSymbol
                knownDrugs(size: $size) {
                  rows {
                    drug {
                      id
                      name
                      isApproved
                      yearOfFirstApproval
                      hasBeenWithdrawn
                      blackBoxWarning
                      maximumClinicalTrialPhase
                    }
                    disease {
                      id
                      name
                    }
                    phase
                    status
                    mechanismOfAction
                  }
                }
              }
            }
            """,
            {"ensemblId": target_id, "size": max(limit, 1)},
        )
        target = payload.get("data", {}).get("target")
        if not target:
            self._known_drugs_cache[cache_key] = []
            return []

        symbol = str(target.get("approvedSymbol") or "").strip()
        rows = target.get("knownDrugs", {}).get("rows", [])
        merged: dict[str, DrugRecord] = {}

        for row in rows:
            drug = row.get("drug") or {}
            drug_id = str(drug.get("id") or "").strip()
            drug_name = str(drug.get("name") or "").strip()
            if not drug_id or not drug_name or not symbol:
                continue

            is_approved = bool(drug.get("isApproved"))
            is_withdrawn = bool(drug.get("hasBeenWithdrawn"))
            black_box_warning = bool(drug.get("blackBoxWarning"))
            max_phase = int(
                max(
                    float(drug.get("maximumClinicalTrialPhase") or 0),
                    float(row.get("phase") or 0),
                )
            )
            approved_status = "withdrawn" if is_withdrawn else ("approved" if is_approved else f"phase_{max_phase}")
            mechanism = str(row.get("mechanismOfAction") or "").strip()
            action = self._action_from_text(mechanism)
            disease = row.get("disease") or {}
            record = DrugRecord(
                drug_id=drug_id,
                name=drug_name,
                target_gene=symbol,
                action=action,
                source="OpenTargets",
                repurposability_score=BioPipeline._repurposability_from_phase(
                    max_phase=max_phase,
                    approved=is_approved,
                    withdrawn=is_withdrawn,
                    black_box_warning=black_box_warning,
                ),
                approved_status=approved_status,
                max_phase=max_phase,
                disease_id=str(disease.get("id") or "").strip() or None,
                disease_name=str(disease.get("name") or "").strip() or None,
                mechanism_of_action=mechanism or None,
                is_withdrawn=is_withdrawn,
                black_box_warning=black_box_warning,
            )
            existing = merged.get(drug_id)
            if existing is None or record.repurposability_score > existing.repurposability_score:
                merged[drug_id] = record

        records = sorted(
            merged.values(),
            key=lambda record: (
                -record.repurposability_score,
                record.name.lower(),
            ),
        )[:limit]
        self._known_drugs_cache[cache_key] = records
        return records

    @staticmethod
    def _action_from_text(text: str) -> str:
        normalized = text.lower()
        if "inhibitor" in normalized:
            return "inhibitor"
        if "antagonist" in normalized:
            return "antagonist"
        if "agonist" in normalized:
            return "agonist"
        if "modulator" in normalized:
            return "modulator"
        return "mechanism_unspecified"


class ChEMBLClient:
    """Retrieve target-linked drugs from ChEMBL REST."""

    def __init__(self, base_url: str, timeout_seconds: float = 12.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout_seconds
        self._target_cache: dict[str, str | None] = {}
        self._drug_cache: dict[tuple[str, int], list[DrugRecord]] = {}

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        response = httpx.get(f"{self.base_url}/{path}", params=params, timeout=self.timeout)
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, dict) else {}

    def find_target_chembl_id(self, gene_symbol: str) -> str | None:
        normalized = gene_symbol.strip().upper()
        if normalized in self._target_cache:
            return self._target_cache[normalized]

        payload = self._get("target/search.json", {"q": gene_symbol})
        for target in payload.get("targets", []):
            if str(target.get("organism") or "").strip() != "Homo sapiens":
                continue
            for component in target.get("target_components", []):
                synonyms = component.get("target_component_synonyms", [])
                for synonym in synonyms:
                    if str(synonym.get("component_synonym") or "").strip().upper() == normalized:
                        target_id = str(target.get("target_chembl_id") or "").strip() or None
                        self._target_cache[normalized] = target_id
                        return target_id

        self._target_cache[normalized] = None
        return None

    def fetch_drugs_for_target(self, gene_symbol: str, limit: int = 20) -> list[DrugRecord]:
        cache_key = (gene_symbol.upper(), limit)
        if cache_key in self._drug_cache:
            return self._drug_cache[cache_key]

        target_chembl_id = self.find_target_chembl_id(gene_symbol)
        if not target_chembl_id:
            self._drug_cache[cache_key] = []
            return []

        payload = self._get("mechanism.json", {"target_chembl_id": target_chembl_id, "limit": 100})
        mechanisms = payload.get("mechanisms", [])
        if not mechanisms:
            self._drug_cache[cache_key] = []
            return []

        molecule_ids: list[str] = []
        for mechanism in mechanisms:
            molecule_id = str(
                mechanism.get("parent_molecule_chembl_id")
                or mechanism.get("molecule_chembl_id")
                or ""
            ).strip()
            if molecule_id:
                molecule_ids.append(molecule_id)

        molecules = self._fetch_molecules(molecule_ids)
        merged: dict[str, DrugRecord] = {}
        for mechanism in mechanisms:
            molecule_id = str(
                mechanism.get("parent_molecule_chembl_id")
                or mechanism.get("molecule_chembl_id")
                or ""
            ).strip()
            if not molecule_id:
                continue

            molecule = molecules.get(molecule_id, {})
            drug_name = str(molecule.get("pref_name") or molecule_id).strip()
            max_phase = int(
                max(
                    float(mechanism.get("max_phase") or 0),
                    float(molecule.get("max_phase") or 0),
                )
            )
            is_withdrawn = bool(molecule.get("withdrawn_flag"))
            black_box_warning = bool(molecule.get("black_box_warning"))
            is_approved = bool(molecule.get("first_approval")) or max_phase >= 4
            approved_status = "withdrawn" if is_withdrawn else ("approved" if is_approved else f"phase_{max_phase}")
            mechanism_of_action = str(mechanism.get("mechanism_of_action") or "").strip()
            action = str(mechanism.get("action_type") or "").strip().lower() or "mechanism_unspecified"
            record = DrugRecord(
                drug_id=molecule_id,
                name=drug_name,
                target_gene=gene_symbol,
                action=action,
                source="ChEMBL",
                repurposability_score=BioPipeline._repurposability_from_phase(
                    max_phase=max_phase,
                    approved=is_approved,
                    withdrawn=is_withdrawn,
                    black_box_warning=black_box_warning,
                ),
                approved_status=approved_status,
                max_phase=max_phase,
                mechanism_of_action=mechanism_of_action or None,
                is_withdrawn=is_withdrawn,
                black_box_warning=black_box_warning,
            )
            existing = merged.get(molecule_id)
            if existing is None or record.repurposability_score > existing.repurposability_score:
                merged[molecule_id] = record

        records = sorted(
            merged.values(),
            key=lambda record: (-record.repurposability_score, record.name.lower()),
        )[:limit]
        self._drug_cache[cache_key] = records
        return records

    def _fetch_molecules(self, molecule_ids: list[str]) -> dict[str, dict[str, Any]]:
        deduped = list(dict.fromkeys(molecule_ids))
        if not deduped:
            return {}

        payload = self._get(f"molecule/set/{';'.join(deduped)}.json")
        molecules = payload.get("molecules", [])
        return {
            str(molecule.get("molecule_chembl_id") or "").strip(): molecule
            for molecule in molecules
            if str(molecule.get("molecule_chembl_id") or "").strip()
        }


class ReactomeClient:
    """Retrieve pathway memberships from Reactome Content Service."""

    def __init__(self, base_url: str, timeout_seconds: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout_seconds

    def _fetch_entity_pathways(self, identifier: str) -> list[dict[str, Any]]:
        encoded = quote(identifier, safe="")
        response = httpx.get(
            f"{self.base_url}/data/pathways/low/entity/{encoded}/allForms",
            timeout=self.timeout,
        )

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

        totals = sum(target.evidence_score for target in targets) or 1.0
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

        pathways.sort(key=lambda pathway: pathway.relevance_score, reverse=True)

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
            MondoMatch(label="idiopathic pulmonary fibrosis", mondo_id="MONDO:0800504"),
        ],
        "idiopathic pulmonary fibrosis": [
            MondoMatch(label="idiopathic pulmonary fibrosis", mondo_id="MONDO:0800504")
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
        "MONDO:0800504": [
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
        "MONDO:0800504": [
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
        chembl_url = os.getenv("CHEMBL_API_BASE_URL", "https://www.ebi.ac.uk/chembl/api/data")

        timeout = float(os.getenv("API_TIMEOUT_SECONDS", "8"))

        self.mondo = MondoResolver(base_url=mondo_url, timeout_seconds=timeout)
        self.open_targets = OpenTargetsClient(endpoint=open_targets_url, timeout_seconds=timeout + 2)
        self.reactome = ReactomeClient(base_url=reactome_url, timeout_seconds=timeout)
        self.chembl = ChEMBLClient(base_url=chembl_url, timeout_seconds=timeout + 2)

    def resolve_indication(self, query: str, limit: int = 5) -> list[ResolvedTerm]:
        normalized = query.strip().lower()

        try:
            matches = self.mondo.search(query, limit=limit)
            if matches:
                return [self.resolve_term(match.mondo_id, label_hint=match.label) for match in matches]
        except (httpx.HTTPError, ValueError):
            pass

        if normalized in self.MONDO_FALLBACKS:
            return [
                self.resolve_term(match.mondo_id, label_hint=match.label)
                for match in self.MONDO_FALLBACKS[normalized]
            ]

        fallback_label = query.strip().title()
        return [
            self._build_resolved_term(
                label=fallback_label,
                mondo_id="MONDO:UNRESOLVED",
                open_targets_id=None,
                target_count=0,
                synonyms=[],
                parents=[],
                refinements=[],
            )
        ]

    def resolve_term(self, mondo_id: str, label_hint: str | None = None) -> ResolvedTerm:
        if mondo_id == "MONDO:UNRESOLVED":
            return self._build_resolved_term(
                label=label_hint or "Unresolved indication",
                mondo_id=mondo_id,
                open_targets_id=None,
                target_count=0,
                synonyms=[],
                parents=[],
                refinements=[],
            )

        term = self.mondo.get_term(mondo_id)
        label = str((term or {}).get("label") or label_hint or mondo_id)
        synonyms = sorted(
            {str(item).strip() for item in (term or {}).get("synonyms", []) if str(item).strip()}
        )

        parents: list[TermOption] = []
        for parent in self.mondo.get_related_terms(mondo_id, "hierarchicalParents", size=4):
            parent_label = str(parent.get("label") or "").strip()
            parent_id = mondo_from_ols_id(str(parent.get("obo_id") or parent.get("short_form") or ""))
            if parent_label and parent_id.startswith("MONDO:"):
                parents.append(term_option_from_payload(parent_label, parent_id))

        open_targets_id, target_count = self._resolve_open_targets_mapping(
            label=label,
            mondo_id=mondo_id,
            synonyms=synonyms,
            term=term,
        )
        refinements = self._suggest_refinements(mondo_id, label, synonyms)

        return self._build_resolved_term(
            label=label,
            mondo_id=mondo_id,
            open_targets_id=open_targets_id,
            target_count=target_count,
            synonyms=synonyms[:6],
            parents=parents[:3],
            refinements=refinements,
        )

    def build_run(
        self,
        mondo_id: str,
        top_k: int,
        disease_id: str | None = None,
        label: str | None = None,
    ) -> PipelineResult:
        resolved = self.resolve_term(mondo_id, label_hint=label)
        effective_disease_id = disease_id or resolved.open_targets_id

        logger.info(
            "Starting run build for mondo_id=%s disease_id=%s label=%s refinement_required=%s",
            mondo_id,
            effective_disease_id,
            resolved.label,
            resolved.requires_refinement,
        )

        # If the UI already chose a concrete downstream disease id, trust that
        # selection rather than forcing another ontology refinement step here.
        if resolved.requires_refinement and not disease_id:
            raise PipelineError("Select a more specific disease subtype before starting a run.")
        if mondo_id == "MONDO:UNRESOLVED":
            raise PipelineError("Could not resolve this indication to a supported MONDO disease term.")
        if not effective_disease_id:
            raise PipelineError(
                "Could not normalize this indication to a supported Open Targets disease identifier."
            )

        limitations = [
            "Research-use only; not medical advice.",
            "Off-patent status is a heuristic unless jurisdictional data is integrated.",
        ]

        try:
            targets = self.open_targets.fetch_associated_targets(
                disease_id=effective_disease_id,
                limit=max(top_k * 3, 30),
            )
            logger.info(
                "Fetched %s associated targets from Open Targets for disease_id=%s",
                len(targets),
                effective_disease_id,
            )
            if not targets:
                limitations.append(
                    "Open Targets returned no associated targets for the normalized disease identifier."
                )
                targets = self.TARGET_FALLBACKS.get(mondo_id, [])
        except (httpx.HTTPError, PipelineError, ValueError):
            limitations.append("Open Targets unavailable; used fallback target set when possible.")
            targets = self.TARGET_FALLBACKS.get(mondo_id, [])

        if not targets:
            raise PipelineError("No targets available for this indication.")

        try:
            pathways, target_pathway_signal = self.reactome.fetch_pathways_for_targets(targets)
            logger.info(
                "Fetched %s Reactome pathways for disease_id=%s",
                len(pathways),
                effective_disease_id,
            )
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

        drug_records = self._fetch_drug_records_for_targets(targets)
        if not drug_records:
            limitations.append(
                "Open Targets knownDrugs and ChEMBL returned no usable drug candidates; using static demo drug set when possible."
            )
            drug_records = self._fallback_drug_records_for_targets(targets)

        candidates = self._build_candidates(
            mondo_id=mondo_id,
            disease_id=effective_disease_id,
            targets=targets,
            pathways=pathways,
            target_pathway_signal=target_pathway_signal,
            drug_records=drug_records,
            top_k=top_k,
        )
        if not candidates:
            target_symbols = [target.symbol for target in targets[:10]]
            fallback_symbols = sorted({str(item["target_gene"]) for item in self.DRUG_KB})
            overlap = sorted(set(target_symbols).intersection(fallback_symbols))

            logger.warning(
                "No candidate overlap for mondo_id=%s disease_id=%s. top_targets=%s fallback_targets=%s",
                mondo_id,
                effective_disease_id,
                target_symbols,
                fallback_symbols,
            )
            raise PipelineError(
                "No candidate drugs could be generated from retrieved context. "
                f"Open Targets returned genes such as {', '.join(target_symbols) or 'none'}, "
                "and neither Open Targets knownDrugs nor ChEMBL produced usable target-linked drugs. "
                f"The static demo fallback only covers {', '.join(fallback_symbols)}. "
                f"Target overlap: {', '.join(overlap) or 'none'}."
            )

        score_breakdown = [
            {
                "drug": candidate["drug"],
                "target": candidate["target"],
                "score": candidate["repurposing_score"],
                "components": candidate["score_breakdown"],
            }
            for candidate in candidates
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
            disease_id=effective_disease_id,
            candidates=candidates,
            score_breakdown=score_breakdown,
            limitations=limitations,
            targets=target_payload,
            pathways=pathway_payload,
        )

    def _resolve_open_targets_mapping(
        self,
        label: str,
        mondo_id: str,
        synonyms: list[str],
        term: dict[str, Any] | None,
    ) -> tuple[str | None, int]:
        candidates: list[str] = []

        annotations = (term or {}).get("annotation") or {}
        for raw in annotations.get("database_cross_reference", []):
            candidate = xref_to_open_targets_id(str(raw))
            if candidate:
                candidates.append(candidate)

        normalized_aliases = {normalize_label(label)}
        normalized_aliases.update(normalize_label(item) for item in synonyms)
        for query in [label, *synonyms[:2]]:
            for hit in self.open_targets.search_diseases(query, limit=10):
                if normalize_label(hit["name"]) in normalized_aliases:
                    candidates.append(hit["id"])

        own_mondo_id = mondo_id.replace(":", "_")
        if own_mondo_id.startswith("MONDO_"):
            candidates.append(own_mondo_id)

        seen: set[str] = set()
        ordered_candidates: list[str] = []
        for candidate in candidates:
            if candidate in seen:
                continue
            seen.add(candidate)
            ordered_candidates.append(candidate)

        ordered_candidates.sort(key=self._disease_id_priority)
        for candidate in ordered_candidates:
            target_count = self.open_targets.fetch_target_count(candidate)
            if target_count is None:
                continue
            return candidate, target_count

        return None, 0

    @staticmethod
    def _disease_id_priority(candidate: str) -> tuple[int, str]:
        if candidate.startswith("EFO_"):
            return (0, candidate)
        if candidate.startswith("MONDO_"):
            return (1, candidate)
        if candidate.startswith("Orphanet_"):
            return (2, candidate)
        if candidate.startswith("HP_"):
            return (3, candidate)
        return (4, candidate)

    def _suggest_refinements(
        self,
        mondo_id: str,
        label: str,
        synonyms: list[str],
        limit: int = 6,
    ) -> list[TermOption]:
        descendants = self.mondo.get_related_terms(mondo_id, "hierarchicalDescendants", size=40)
        if not descendants:
            return []

        descendant_lookup: dict[str, MondoMatch] = {}
        for descendant in descendants:
            descendant_label = str(descendant.get("label") or "").strip()
            descendant_id = mondo_from_ols_id(
                str(descendant.get("obo_id") or descendant.get("short_form") or "")
            )
            if not descendant_label or descendant_id == mondo_id or not descendant_id.startswith("MONDO:"):
                continue
            descendant_lookup.setdefault(
                normalize_label(descendant_label),
                MondoMatch(label=descendant_label, mondo_id=descendant_id),
            )

        suggestions: dict[str, TermOption] = {}
        for query in [label, *synonyms[:2]]:
            for hit in self.open_targets.search_diseases(query, limit=20):
                descendant = descendant_lookup.get(normalize_label(hit["name"]))
                if not descendant:
                    continue
                target_count = self.open_targets.fetch_target_count(hit["id"]) or 0
                if target_count <= 0:
                    continue
                suggestions[descendant.mondo_id] = term_option_from_payload(
                    label=descendant.label,
                    mondo_id=descendant.mondo_id,
                    open_targets_id=hit["id"],
                    target_count=target_count,
                )
                if len(suggestions) >= limit:
                    break
            if len(suggestions) >= limit:
                break

        if not suggestions:
            for child in self.mondo.get_related_terms(mondo_id, "hierarchicalChildren", size=12):
                child_label = str(child.get("label") or "").strip()
                child_id = mondo_from_ols_id(str(child.get("obo_id") or child.get("short_form") or ""))
                if not child_label or child_id == mondo_id or not child_id.startswith("MONDO:"):
                    continue
                child_term = self.resolve_term(child_id, label_hint=child_label)
                if not child_term.open_targets_id or child_term.target_count <= 0:
                    continue
                suggestions[child_id] = term_option_from_payload(
                    label=child_label,
                    mondo_id=child_id,
                    open_targets_id=child_term.open_targets_id,
                    target_count=child_term.target_count,
                )
                if len(suggestions) >= limit:
                    break

        return sorted(
            suggestions.values(),
            key=lambda option: (-option.target_count, option.label.lower()),
        )[:limit]

    @staticmethod
    def _build_resolved_term(
        label: str,
        mondo_id: str,
        open_targets_id: str | None,
        target_count: int,
        synonyms: list[str],
        parents: list[TermOption],
        refinements: list[TermOption],
    ) -> ResolvedTerm:
        requires_refinement = len(refinements) > 0
        runnable = bool(open_targets_id) and target_count > 0 and not requires_refinement
        return ResolvedTerm(
            label=label,
            mondo_id=mondo_id,
            open_targets_id=open_targets_id,
            target_count=target_count,
            runnable=runnable,
            requires_refinement=requires_refinement,
            synonyms=synonyms,
            parents=parents,
            refinements=refinements,
        )

    def _fetch_drug_records_for_targets(
        self,
        targets: list[TargetEvidence],
        per_target_limit: int = 12,
    ) -> list[DrugRecord]:
        combined: dict[tuple[str, str], DrugRecord] = {}
        for target in targets[:12]:
            records = self.open_targets.fetch_known_drugs_for_target(target.id, limit=per_target_limit)
            if len(records) < 3:
                fallback_records = self.chembl.fetch_drugs_for_target(
                    target.symbol,
                    limit=max(per_target_limit - len(records), 3),
                )
                records = self._merge_drug_records(records, fallback_records)

            for record in records:
                key = (record.drug_id, record.target_gene)
                existing = combined.get(key)
                if existing is None or record.repurposability_score > existing.repurposability_score:
                    combined[key] = record

        return sorted(
            combined.values(),
            key=lambda record: (-record.repurposability_score, record.name.lower()),
        )

    def _fallback_drug_records_for_targets(self, targets: list[TargetEvidence]) -> list[DrugRecord]:
        target_by_symbol = {target.symbol: target for target in targets}
        fallback_records: list[DrugRecord] = []
        for record in self.DRUG_KB:
            symbol = str(record["target_gene"])
            if symbol not in target_by_symbol:
                continue
            fallback_records.append(
                DrugRecord(
                    drug_id=str(record["drug"]).upper().replace(" ", "_"),
                    name=str(record["drug"]),
                    target_gene=symbol,
                    action=str(record["action"]),
                    source="StaticFallback",
                    repurposability_score=float(record["repurposability_score"]),
                    approved_status=str(record["approved_status"]),
                    mechanism_of_action=str(record["action"]),
                )
            )
        return fallback_records

    @staticmethod
    def _merge_drug_records(
        primary: list[DrugRecord], fallback: list[DrugRecord]
    ) -> list[DrugRecord]:
        merged: dict[tuple[str, str], DrugRecord] = {
            (record.drug_id, record.target_gene): record for record in primary
        }
        for record in fallback:
            merged.setdefault((record.drug_id, record.target_gene), record)
        return list(merged.values())

    @staticmethod
    def _repurposability_from_phase(
        max_phase: int,
        approved: bool,
        withdrawn: bool,
        black_box_warning: bool,
    ) -> float:
        score = 0.35 + min(max_phase, 4) * 0.12
        if approved:
            score += 0.1
        if black_box_warning:
            score -= 0.05
        if withdrawn:
            score -= 0.25
        return round(max(0.05, min(score, 0.95)), 3)

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
        raw_score = sum(values[key] * weight for key, weight in weights.items())
        return round(raw_score / weight_sum, 3)

    def _build_candidates(
        self,
        mondo_id: str,
        disease_id: str,
        targets: list[TargetEvidence],
        pathways: list[Pathway],
        target_pathway_signal: dict[str, float],
        drug_records: list[DrugRecord],
        top_k: int,
    ) -> list[dict[str, Any]]:
        pathway_lookup: dict[str, float] = {symbol: 0.0 for symbol in target_pathway_signal}
        for symbol, signal in target_pathway_signal.items():
            pathway_lookup[symbol] = signal

        target_by_symbol = {target.symbol: target for target in targets}
        candidates: list[dict[str, Any]] = []

        for record in drug_records:
            symbol = record.target_gene
            target = target_by_symbol.get(symbol)
            if target is None:
                continue

            disease_target_relevance = round(target.evidence_score, 3)
            pathway_intervention_fit = round(max(pathway_lookup.get(symbol, 0.0), 0.15), 3)
            action = record.action
            mechanism_directionality_fit = 0.78 if action in {"inhibitor", "antagonist"} else 0.68
            repurposability_score = float(record.repurposability_score)

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
                    "drug": record.name,
                    "drug_id": record.drug_id,
                    "target": symbol,
                    "action": action,
                    "approved_status": record.approved_status,
                    "max_phase": record.max_phase,
                    "source": record.source,
                    "mondo_id": mondo_id,
                    "disease_id": disease_id,
                    "repurposing_score": repurposing_score,
                    "why": (
                        f"{record.name} ({record.source}) targets {symbol} for selected term {mondo_id}. "
                        f"Drug context: {record.mechanism_of_action or action}. "
                        f"Associated indication: {record.disease_name or 'not specified'}. "
                        f"Pathway context: {pathway_hits or ['context unavailable']}."
                    ),
                    "score_breakdown": score_breakdown,
                }
            )

        candidates.sort(key=lambda candidate: candidate["repurposing_score"], reverse=True)
        return candidates[:top_k]
