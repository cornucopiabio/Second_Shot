import unittest
from unittest.mock import patch

from app.pipeline import BioPipeline, DrugRecord, PipelineError, ResolvedTerm, TargetEvidence


class DrugRetrievalTest(unittest.TestCase):
    def test_fetch_drug_records_combines_open_targets_and_chembl(self) -> None:
        pipeline = BioPipeline()
        targets = [TargetEvidence(id="ENSG00000157764", symbol="BRAF", name="BRAF", evidence_score=0.9)]

        ot_record = DrugRecord(
            drug_id="CHEMBL1229517",
            name="VEMURAFENIB",
            target_gene="BRAF",
            action="inhibitor",
            source="OpenTargets",
            repurposability_score=0.9,
            approved_status="approved",
            max_phase=4,
        )
        chembl_record = DrugRecord(
            drug_id="CHEMBL1946170",
            name="REGORAFENIB",
            target_gene="BRAF",
            action="inhibitor",
            source="ChEMBL",
            repurposability_score=0.84,
            approved_status="approved",
            max_phase=4,
        )

        with (
            patch.object(pipeline.open_targets, "fetch_known_drugs_for_target", return_value=[ot_record]),
            patch.object(pipeline.chembl, "fetch_drugs_for_target", return_value=[chembl_record]),
        ):
            records = pipeline._fetch_drug_records_for_targets(targets, per_target_limit=5)

        self.assertEqual({record.source for record in records}, {"OpenTargets", "ChEMBL"})

    def test_build_run_uses_dynamic_drug_records(self) -> None:
        pipeline = BioPipeline()
        resolved = ResolvedTerm(
            label="glioblastoma",
            mondo_id="MONDO:0018177",
            open_targets_id="EFO_0000519",
            target_count=200,
            runnable=True,
            requires_refinement=False,
            synonyms=[],
            parents=[],
            refinements=[],
        )
        targets = [TargetEvidence(id="ENSG00000157764", symbol="BRAF", name="BRAF", evidence_score=0.91)]
        dynamic_drugs = [
            DrugRecord(
                drug_id="CHEMBL1229517",
                name="VEMURAFENIB",
                target_gene="BRAF",
                action="inhibitor",
                source="OpenTargets",
                repurposability_score=0.9,
                approved_status="approved",
                max_phase=4,
                mechanism_of_action="BRAF inhibitor",
            )
        ]

        with (
            patch.object(pipeline, "resolve_term", return_value=resolved),
            patch.object(pipeline.open_targets, "fetch_associated_targets", return_value=targets),
            patch.object(pipeline.reactome, "fetch_pathways_for_targets", return_value=([], {"BRAF": 0.0})),
            patch.object(pipeline, "_fetch_drug_records_for_targets", return_value=dynamic_drugs),
        ):
            result = pipeline.build_run("MONDO:0018177", top_k=10, disease_id="EFO_0000519")

        self.assertEqual(result.disease_id, "EFO_0000519")
        self.assertEqual(result.candidates[0]["drug"], "VEMURAFENIB")
        self.assertEqual(result.candidates[0]["source"], "OpenTargets")

    def test_build_run_explains_dynamic_drug_gap(self) -> None:
        pipeline = BioPipeline()
        resolved = ResolvedTerm(
            label="dilated cardiomyopathy",
            mondo_id="MONDO:0005021",
            open_targets_id="EFO_0000407",
            target_count=100,
            runnable=True,
            requires_refinement=False,
            synonyms=[],
            parents=[],
            refinements=[],
        )
        targets = [TargetEvidence(id="ENSG00000198563", symbol="LMNA", name="LMNA", evidence_score=0.91)]

        with (
            patch.object(pipeline, "resolve_term", return_value=resolved),
            patch.object(pipeline.open_targets, "fetch_associated_targets", return_value=targets),
            patch.object(pipeline.reactome, "fetch_pathways_for_targets", return_value=([], {"LMNA": 0.0})),
            patch.object(pipeline, "_fetch_drug_records_for_targets", return_value=[]),
        ):
            with self.assertRaises(PipelineError) as error:
                pipeline.build_run("MONDO:0005021", top_k=10, disease_id="EFO_0000407")

        self.assertIn("knownDrugs", str(error.exception))
        self.assertIn("ChEMBL", str(error.exception))
