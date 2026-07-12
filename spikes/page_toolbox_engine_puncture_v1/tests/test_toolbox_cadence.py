from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from toolbox_cadence.lifecycle import (
    HoldoutAccessGuard,
    ToolboxMaturityMachine,
    ToolboxWorkLedger,
    read_sample_manifest,
    validate_acceptance_package,
    validate_kernel_change,
    validate_sample_partition,
)
from toolbox_cadence.models import CadenceError, SampleSplit, ToolboxMaturity
from toolbox_cadence.scaffold import scaffold_toolbox


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class ToolboxCadenceTests(unittest.TestCase):
    def test_dry_run_plans_one_leaf_without_creating_toolboxes_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = scaffold_toolbox(root, "body.flow_text.single", dry_run=True)
            self.assertEqual("toolboxes/body/flow_text/single", result.package_root)
            self.assertFalse((root / "toolboxes").exists())

    def test_scaffold_creates_only_requested_leaf_and_no_promotion_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shutil.copytree(PROJECT_ROOT / "templates", root / "templates")
            result = scaffold_toolbox(root, "body.flow_text.single")
            package = root / result.package_root
            self.assertTrue((package / "docs" / "分类边界与不变量.md").is_file())
            self.assertFalse((package / "promotion_manifest.json").exists())
            self.assertFalse((root / "toolboxes" / "body" / "flow_text" / "multi").exists())

    def test_scaffold_rejects_unknown_leaf(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(CadenceError, "unsupported_toolbox_key"):
                scaffold_toolbox(Path(temporary), "body.universal", dry_run=True)

    def test_holdout_is_hidden_until_freeze_and_final_validation(self) -> None:
        guard = HoldoutAccessGuard()
        with self.assertRaisesRegex(CadenceError, "before_workflow_freeze"):
            guard.require_content_access(SampleSplit.HOLDOUT, purpose="final_validation")
        guard.freeze_workflow()
        with self.assertRaisesRegex(CadenceError, "only_allowed_for_final_validation"):
            guard.require_content_access(SampleSplit.HOLDOUT, purpose="debug")
        guard.require_content_access(SampleSplit.HOLDOUT, purpose="final_validation")

    def test_maturity_requires_complete_acceptance_before_promotion(self) -> None:
        machine = ToolboxMaturityMachine()
        machine.transition(ToolboxMaturity.REGRESSION)
        with self.assertRaisesRegex(CadenceError, "promotion_requires"):
            machine.transition(ToolboxMaturity.PROMOTED)
        machine.transition(ToolboxMaturity.PROMOTED, promotion_ready=True)
        with self.assertRaisesRegex(CadenceError, "illegal_maturity_transition"):
            machine.transition(ToolboxMaturity.REGRESSION)

    def test_evidence_insufficient_can_return_to_experimental(self) -> None:
        machine = ToolboxMaturityMachine()
        machine.transition(ToolboxMaturity.EVIDENCE_INSUFFICIENT)
        machine.transition(ToolboxMaturity.EXPERIMENTAL)
        self.assertEqual(ToolboxMaturity.EXPERIMENTAL, machine.current)

    def test_only_one_toolbox_can_be_active(self) -> None:
        ledger = ToolboxWorkLedger()
        ledger.start("body.flow_text.single")
        with self.assertRaisesRegex(CadenceError, "toolbox_already_active"):
            ledger.start("body.flow_text.multi")
        ledger.record_gate("FAIL")
        self.assertEqual("body.flow_text.single", ledger.active_toolbox_key)
        ledger.record_gate("PASS")
        ledger.start("body.flow_text.multi")
        self.assertEqual("body.flow_text.multi", ledger.active_toolbox_key)

    def test_kernel_change_requires_all_promoted_toolboxes_to_pass(self) -> None:
        blocked = validate_kernel_change({"cover", "contents"}, {"cover": "PASS", "contents": "FAIL"})
        self.assertFalse(blocked.can_resume)
        self.assertEqual(("contents",), blocked.missing_or_failed)
        passed = validate_kernel_change({"cover", "contents"}, {"cover": "PASS", "contents": "PASS"})
        self.assertTrue(passed.can_resume)

    def test_manifest_requires_all_three_partitions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "manifest.jsonl"
            records = [self._sample("D1", "development"), self._sample("R1", "regression")]
            path.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")
            parsed = read_sample_manifest(path, "body.flow_text.single")
            self.assertEqual(("holdout",), validate_sample_partition(parsed))

    def test_acceptance_package_rejects_missing_evidence_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            package = Path(temporary)
            self._write_complete_package(package)
            (package / "artifacts" / "visual.png").unlink()
            result = validate_acceptance_package(package, "body.flow_text.single")
            self.assertFalse(result.passed)
            self.assertTrue(any(reason.startswith("evidence_ref_missing") for reason in result.reasons))

    def test_complete_acceptance_package_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            package = Path(temporary)
            self._write_complete_package(package)
            result = validate_acceptance_package(package, "body.flow_text.single")
            self.assertTrue(result.passed, result)

    def test_contract_schema_is_valid_json(self) -> None:
        schema = json.loads((PROJECT_ROOT / "contracts" / "toolbox_cadence_v1.schema.json").read_text(encoding="utf-8"))
        self.assertEqual("toolbox-cadence/v1", schema["$id"])

    @staticmethod
    def _sample(sample_id: str, split: str) -> dict[str, object]:
        return {
            "sample_id": sample_id,
            "toolbox_key": "body.flow_text.single",
            "split": split,
            "source_ref": f"samples/{split}/{sample_id}.pdf",
            "sha256": "a" * 64,
            "original_document_id": "document-1",
            "original_page_number": 1,
        }

    def _write_complete_package(self, package: Path) -> None:
        for relative in (
            "docs/分类边界与不变量.md",
            "docs/工具分类与调用流程.md",
            "docs/裁决与修复规则.md",
        ):
            path = package / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("frozen\n", encoding="utf-8")

        manifest = package / "samples" / "manifest.jsonl"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(
            "\n".join(
                json.dumps(self._sample(sample_id, split))
                for sample_id, split in (("D1", "development"), ("R1", "regression"), ("H1", "holdout"))
            )
            + "\n",
            encoding="utf-8",
        )

        evidence = package / "artifacts" / "visual.png"
        evidence.parent.mkdir(parents=True, exist_ok=True)
        evidence.write_bytes(b"evidence")
        report = {"verdict": "PASS", "evidence_refs": ["artifacts/visual.png"]}
        for name in ("fixed_translation_regression", "qwen_page_e2e", "regression", "holdout", "visual_review"):
            path = package / "reports" / f"{name}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(report), encoding="utf-8")
        (package / "stage_gate.json").write_text(
            json.dumps({"decision": "PASS", "evidence_refs": ["artifacts/visual.png"]}), encoding="utf-8"
        )
        (package / "promotion_manifest.json").write_text(
            json.dumps(
                {
                    "toolbox_key": "body.flow_text.single",
                    "status": "PROMOTED",
                    "evidence_refs": ["artifacts/visual.png"],
                }
            ),
            encoding="utf-8",
        )


if __name__ == "__main__":
    unittest.main()
