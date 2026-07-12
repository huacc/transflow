from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from page_toolbox_puncture.contracts import (
    ContractError,
    PageFacts,
    PageTemplate,
    PageTranslationBundle,
    PageTranslationRequest,
    TranslationResult,
    TranslationUnit,
)
from page_toolbox_puncture.runtime import run_translation_slice
from page_toolbox_puncture.sample_snapshot import snapshot_sample
from page_toolbox_puncture.state_machine import InvalidTransition, PageState, PageStateMachine
from page_toolbox_puncture.translation import FixedTranslationProvider, ProviderError, _normalize_translation_order, _translation_chunks


class FailingProvider:
    provider_name = "failing"
    model_name = "test-failure"

    def translate(self, request: PageTranslationRequest) -> PageTranslationBundle:
        raise ProviderError("TEST_PROVIDER_FAILURE")


class P1RuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.units = (
            TranslationUnit("c1", "First", 0),
            TranslationUnit("c2", "Second", 1),
        )
        self.request = PageTranslationRequest("r1", "p1", "en", "zh-CN", self.units)

    def test_request_rejects_duplicate_container_ids(self) -> None:
        with self.assertRaisesRegex(ContractError, "duplicate_container_id"):
            PageTranslationRequest("r1", "p1", "en", "zh-CN", (self.units[0], TranslationUnit("c1", "Again", 1)))

    def test_bundle_requires_exact_request_order(self) -> None:
        bundle = PageTranslationBundle(
            "r1", "p1", "fixed", "fixture",
            (TranslationResult("c2", "二"), TranslationResult("c1", "一")),
        )
        with self.assertRaisesRegex(ContractError, "must_match_request_order"):
            bundle.validate_against(self.request)

    def test_fixed_provider_is_deterministic(self) -> None:
        provider = FixedTranslationProvider({"c1": "一", "c2": "二"})
        first = provider.translate(self.request)
        second = provider.translate(self.request)
        self.assertEqual(first, second)

    def test_qwen_adapter_normalizes_complete_id_set_to_request_order(self) -> None:
        rows = [
            {"container_id": "c2", "translated_text": "二"},
            {"container_id": "c1", "translated_text": "一"},
        ]
        normalized = _normalize_translation_order(rows, ["c1", "c2"])
        self.assertEqual(["c1", "c2"], [item.container_id for item in normalized])

    def test_qwen_adapter_rejects_duplicate_or_incomplete_id_set(self) -> None:
        with self.assertRaisesRegex(ProviderError, "DUPLICATE"):
            _normalize_translation_order(
                [{"container_id": "c1", "translated_text": "一"}, {"container_id": "c1", "translated_text": "重复"}],
                ["c1", "c2"],
            )
        with self.assertRaisesRegex(ProviderError, "SET_MISMATCH"):
            _normalize_translation_order([{"container_id": "c1", "translated_text": "一"}], ["c1", "c2"])

    def test_qwen_adapter_chunks_large_page_without_splitting_containers(self) -> None:
        units = tuple(TranslationUnit(f"c{index}", "text", index) for index in range(25))
        chunks = _translation_chunks(units)
        self.assertEqual([12, 12, 1], [len(chunk) for chunk in chunks])
        self.assertEqual(list(units), [unit for chunk in chunks for unit in chunk])

    def test_state_machine_rejects_illegal_transition(self) -> None:
        state = PageStateMachine()
        with self.assertRaisesRegex(InvalidTransition, "illegal_transition"):
            state.transition(PageState.TRANSLATION_READY, "skip")

    def test_snapshot_preserves_source_and_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            project = repo / "spikes" / "toolbox"
            source = repo / "spikes" / "classifier" / "result.pdf"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"single-page-pdf-fixture")
            source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
            manifest = snapshot_sample(
                repo_root=repo,
                project_root=project,
                source_pdf=source,
                sample_id="p1",
                classification_path="body/flow_text/single",
                leaf_key="body.flow_text.single",
                original_document_id="doc-1",
                original_page_number=10,
                source_document_sha256="a" * 64,
                expected_source_sha256=source_hash,
            )
            self.assertEqual(source_hash, hashlib.sha256(source.read_bytes()).hexdigest())
            self.assertEqual(manifest.original_page_number, 10)
            self.assertFalse(Path(manifest.upstream_pdf).is_absolute())
            self.assertEqual(manifest.upstream_sha256, manifest.snapshot_sha256)

    def _runtime_inputs(self, root: Path):
        source = root / "classifier" / "result.pdf"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"single-page-pdf-fixture")
        source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        project = root / "spikes" / "toolbox"
        sample = snapshot_sample(
            repo_root=root,
            project_root=project,
            source_pdf=source,
            sample_id="p1",
            classification_path="body/flow_text/single",
            leaf_key="body.flow_text.single",
            original_document_id="doc-1",
            original_page_number=10,
            source_document_sha256="a" * 64,
            expected_source_sha256=source_hash,
        )
        facts = PageFacts("p1", source_hash, 100.0, 200.0, 2, "p1_fixture")
        template = PageTemplate("p1", "body.flow_text.single", self.units)
        return project, sample, facts, template

    def test_runtime_reaches_translation_ready_and_separates_verdicts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project, sample, facts, template = self._runtime_inputs(Path(tmp))
            result = run_translation_slice(
                project_root=project,
                sample=sample,
                page_facts=facts,
                page_template=template,
                request=self.request,
                provider=FixedTranslationProvider({"c1": "一", "c2": "二"}),
                prompt_sha256="b" * 64,
                run_id="fixed-success",
            )
            manifest = json.loads((result.run_dir / "run_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(result.terminal_state, "TRANSLATION_READY")
            self.assertEqual(manifest["process_verdict"], "PASS")
            self.assertEqual(manifest["product_verdict"], "NOT_REACHED")
            self.assertTrue((result.run_dir / "artifact_index.json").exists())

    def test_failure_run_has_terminal_state_and_error_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project, sample, facts, template = self._runtime_inputs(Path(tmp))
            result = run_translation_slice(
                project_root=project,
                sample=sample,
                page_facts=facts,
                page_template=template,
                request=self.request,
                provider=FailingProvider(),
                prompt_sha256="b" * 64,
                run_id="provider-failure",
            )
            self.assertEqual(result.terminal_state, "CAPABILITY_FAILED")
            self.assertEqual(result.error_code, "TEST_PROVIDER_FAILURE")
            self.assertEqual(result.process_verdict, "PASS")
            self.assertEqual(result.product_verdict, "NOT_REACHED")
            self.assertTrue((result.run_dir / "errors" / "failure.json").exists())

    def test_contract_failure_is_not_misreported_as_capability_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project, sample, facts, template = self._runtime_inputs(Path(tmp))
            wrong_facts = PageFacts("other-page", facts.source_pdf_sha256, 100.0, 200.0, 2, "p1_fixture")
            result = run_translation_slice(
                project_root=project,
                sample=sample,
                page_facts=wrong_facts,
                page_template=template,
                request=self.request,
                provider=FixedTranslationProvider({"c1": "一", "c2": "二"}),
                prompt_sha256="b" * 64,
                run_id="contract-failure",
            )
            self.assertEqual(result.terminal_state, "PROCESS_FAILED")
            self.assertEqual(result.process_verdict, "FAIL")
            self.assertEqual(result.product_verdict, "NOT_REACHED")

    def test_contract_schema_declares_all_p1_contracts(self) -> None:
        root = Path(__file__).resolve().parents[1]
        schema = json.loads((root / "contracts" / "contracts_v1.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(
            set(schema["$defs"]),
            {"SampleManifest", "PageFacts", "PageTemplate", "PageTranslationRequest", "PageTranslationBundle", "PagePatch", "Finding", "PageQualityDecision", "RunManifest", "PromotionManifest"},
        )


if __name__ == "__main__":
    unittest.main()
