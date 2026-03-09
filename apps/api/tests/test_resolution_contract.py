import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from app.main import app
from app.pipeline import PipelineError, PipelineResult, ResolvedTerm, TermOption


class ResolutionContractTest(unittest.TestCase):
    def test_resolve_indication_returns_enriched_matches(self) -> None:
        fake_pipeline = Mock()
        fake_pipeline.resolve_indication.return_value = [
            ResolvedTerm(
                label="cardiomyopathy",
                mondo_id="MONDO:0004994",
                open_targets_id="EFO_0000318",
                target_count=812,
                runnable=False,
                requires_refinement=True,
                synonyms=["Cardiomyopathies"],
                parents=[TermOption(label="heart disease", mondo_id="MONDO:0000001")],
                refinements=[
                    TermOption(
                        label="hypertrophic cardiomyopathy",
                        mondo_id="MONDO:0007266",
                        open_targets_id="EFO_0000538",
                        target_count=5175,
                        runnable=True,
                    )
                ],
            )
        ]

        with patch("app.main.get_pipeline", return_value=fake_pipeline):
            client = TestClient(app)
            response = client.post("/resolve-indication", json={"query": "cardiomyopathy"})

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["matches"][0]["open_targets_id"], "EFO_0000318")
        self.assertTrue(payload["matches"][0]["requires_refinement"])
        self.assertEqual(payload["matches"][0]["refinements"][0]["mondo_id"], "MONDO:0007266")

    def test_create_run_forwards_normalized_disease_id(self) -> None:
        fake_pipeline = Mock()
        fake_pipeline.build_run.return_value = PipelineResult(
            disease_id="EFO_0000538",
            candidates=[
                {
                    "drug": "Nintedanib",
                    "target": "KDR",
                    "action": "inhibitor",
                    "approved_status": "approved",
                    "mondo_id": "MONDO:0007266",
                    "disease_id": "EFO_0000538",
                    "repurposing_score": 0.81,
                    "why": "Test rationale",
                    "score_breakdown": {
                        "disease_target_relevance": 0.9,
                        "pathway_intervention_fit": 0.7,
                        "mechanism_directionality_fit": 0.78,
                        "structural_plausibility": None,
                        "repurposability_score": 0.82,
                    },
                }
            ],
            score_breakdown=[],
            limitations=[],
            targets=[{"id": "ENSG00000128052", "gene_symbol": "KDR", "name": "KDR", "evidence_score": 0.9}],
            pathways=[],
        )
        fake_ranker = Mock()
        fake_ranker.rerank.return_value = (fake_pipeline.build_run.return_value.candidates, None)

        with (
            patch("app.main.get_pipeline", return_value=fake_pipeline),
            patch("app.main.get_anthropic_ranker", return_value=fake_ranker),
        ):
            client = TestClient(app)
            response = client.post(
                "/runs",
                json={
                    "mondo_id": "MONDO:0007266",
                    "disease_id": "EFO_0000538",
                    "label": "hypertrophic cardiomyopathy",
                    "top_k": 20,
                    "enable_docking": False,
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["disease_id"], "EFO_0000538")
        fake_pipeline.build_run.assert_called_once_with(
            mondo_id="MONDO:0007266",
            top_k=20,
            disease_id="EFO_0000538",
            label="hypertrophic cardiomyopathy",
        )

    def test_create_run_returns_failed_state_for_refinement_error(self) -> None:
        fake_pipeline = Mock()
        fake_pipeline.build_run.side_effect = PipelineError(
            "Select a more specific disease subtype before starting a run."
        )

        with patch("app.main.get_pipeline", return_value=fake_pipeline):
            client = TestClient(app)
            response = client.post(
                "/runs",
                json={"mondo_id": "MONDO:0004994", "top_k": 20, "enable_docking": False},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "failed")
        self.assertIn("specific disease subtype", payload["limitations"][0])
