from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import fitz

from scripts.finalize_p5_multi_batch import _read_json, main as finalize_p5_batch
from scripts.run_p5_seeded_case import _publish_result_artifacts
from toolboxes.body.flow_text.multi.tools.engine import P5RunResult


class P5FailureArtifactTest(unittest.TestCase):
    def test_batch_finalizer_reads_powershell_utf8_bom_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            contract = Path(temporary) / "batch_run_contract.json"
            contract.write_bytes(b"\xef\xbb\xbf" + json.dumps({"execution_order": []}).encode("utf-8"))

            self.assertEqual([], _read_json(contract)["execution_order"])

    def test_capability_failure_does_not_materialize_result_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            for name in ("input", "output", "previews", "reports"):
                (run_dir / name).mkdir()
            source_pdf = run_dir / "input" / "source.pdf"
            document = fitz.open()
            page = document.new_page(width=300.0, height=400.0)
            page.insert_text((36.0, 48.0), "Immutable source page")
            document.save(source_pdf)
            document.close()
            quality_path = run_dir / "reports" / "quality_decision.json"
            quality_path.write_text(
                json.dumps(
                    {
                        "process_verdict": "FAIL",
                        "product_verdict": "NOT_REACHED",
                        "terminal_state": "P5_CAPABILITY_FAILED",
                        "findings": [
                            {
                                "code": "ProviderError",
                                "message": "backend failure must stay in JSON",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            result = P5RunResult(
                page_id="synthetic-failure",
                run_dir=str(run_dir),
                candidate_pdf=None,
                process_verdict="FAIL",
                product_verdict="NOT_REACHED",
                terminal_state="P5_CAPABILITY_FAILED",
                failure_owner="translation_provider",
                selected_column_profiles=(),
            )

            _publish_result_artifacts(result=result, run_dir=run_dir)

            self.assertTrue((run_dir / "previews" / "source.png").is_file())
            self.assertFalse((run_dir / "output" / "result.pdf").exists())
            self.assertFalse((run_dir / "previews" / "result.png").exists())
            self.assertFalse((run_dir / "previews" / "comparison.png").exists())
            self.assertFalse((run_dir / "reports" / "diagnostic_render_evidence.json").exists())
            self.assertIn("backend failure must stay in JSON", quality_path.read_text(encoding="utf-8"))

    def test_batch_finalizer_allows_missing_product_pdf_without_creating_batch_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            (run_dir / "contracts").mkdir()
            reports_dir = run_dir / "reports"
            reports_dir.mkdir()
            case_dir = run_dir / "cases" / "failed-case"
            (case_dir / "reports").mkdir(parents=True)
            (run_dir / "contracts" / "batch_run_contract.json").write_text(
                json.dumps(
                    {
                        "execution_order": ["failed-case"],
                        "execution_mode": "regression",
                        "translation_provider": {"model": "test-model"},
                    }
                ),
                encoding="utf-8",
            )
            (case_dir / "reports" / "run_result.json").write_text(
                json.dumps(
                    {
                        "candidate_pdf": None,
                        "process_verdict": "FAIL",
                        "product_verdict": "NOT_REACHED",
                        "terminal_state": "P5_CAPABILITY_FAILED",
                        "failure_owner": "translation_provider",
                        "selected_column_profiles": [],
                    }
                ),
                encoding="utf-8",
            )
            (case_dir / "reports" / "quality_decision.json").write_text(
                json.dumps({"findings": [{"code": "ProviderError"}]}),
                encoding="utf-8",
            )

            with patch.object(sys, "argv", ["finalize_p5_multi_batch.py", "--run-dir", str(run_dir)]):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(0, finalize_p5_batch())

            summary = json.loads((reports_dir / "batch_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(0, summary["result_pdf_count"])
            self.assertEqual(1, summary["missing_result_pdf_count"])
            self.assertIsNone(summary["batch_result_pdf"])
            self.assertIsNone(summary["pages"][0]["result_pdf"])
            self.assertFalse((reports_dir / "batch_result.pdf").exists())


if __name__ == "__main__":
    unittest.main()
