from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from page_toolbox_puncture.contracts import write_json
from page_toolbox_puncture.sample_snapshot import sha256_file
from toolbox_cadence.lifecycle import read_sample_manifest
from toolbox_cadence.models import SampleSplit
from toolboxes.body.flow_text.single.tools.run_package import initialize_batch_package, publish_case_outputs, write_artifact_index


TOOLBOX_ROOT = ROOT / "toolboxes" / "body" / "flow_text" / "single"


def main() -> int:
    parser = argparse.ArgumentParser(description="Materialize a self-contained audit package from a legacy P3 run")
    parser.add_argument("--source-run-id", required=True)
    parser.add_argument("--target-run-id")
    args = parser.parse_args()

    source_run = TOOLBOX_ROOT / "runs" / args.source_run_id
    target_run_id = args.target_run_id or args.source_run_id + "-audit"
    target_run = TOOLBOX_ROOT / "runs" / target_run_id
    records = read_sample_manifest(TOOLBOX_ROOT / "samples" / "manifest.jsonl", "body.flow_text.single")
    prompt_path = TOOLBOX_ROOT / "prompts" / "page_translation.zh-CN.md"
    initialize_batch_package(
        run_root=target_run,
        toolbox_root=TOOLBOX_ROOT,
        run_id=target_run_id,
        records=records,
        prompt_path=prompt_path,
        model="Qwen/Qwen3.6-35B-A3B",
    )

    pages = []
    for record in records:
        if record.split is not SampleSplit.DEVELOPMENT:
            continue
        legacy = source_run / record.sample_id
        case = target_run / "cases" / record.sample_id
        for name in ("contracts", "docs", "input", "output", "previews", "reports"):
            (case / name).mkdir(parents=True, exist_ok=True)
        source_pdf = TOOLBOX_ROOT / record.source_ref
        shutil.copy2(source_pdf, case / "input" / "source.pdf")
        if sha256_file(case / "input" / "source.pdf") != record.sha256:
            raise RuntimeError(f"source_hash_mismatch:{record.sample_id}")

        mapping = {
            "01_page_facts.json": "input/page_facts.json",
            "02_page_template.json": "input/page_template.json",
            "03_translation_request.json": "input/translation_request.json",
            "04_translation_bundle.json": "output/translation_bundle.json",
            "05_layout_plan.json": "output/layout_plan.json",
            "06_layout_findings.json": "reports/layout_findings.json",
            "candidate.pdf": "output/candidate.pdf",
            "renders/source.png": "previews/source.png",
            "renders/candidate.png": "previews/candidate.png",
            "renders/comparison.png": "previews/comparison.png",
            "07_render_evidence.json": "reports/render_evidence.json",
            "08_quality_decision.json": "reports/quality_decision.json",
            "state_trace.json": "reports/state_trace.json",
            "run_result.json": "reports/run_result.json",
        }
        for source_relative, target_relative in mapping.items():
            source = legacy / source_relative
            if not source.is_file():
                raise FileNotFoundError(f"legacy_artifact_missing:{record.sample_id}:{source_relative}")
            shutil.copy2(source, case / target_relative)

        render_evidence_path = case / "reports" / "render_evidence.json"
        render_evidence = json.loads(render_evidence_path.read_text(encoding="utf-8"))
        render_evidence.update(
            {
                "source_png": "previews/source.png",
                "candidate_png": "previews/candidate.png",
                "comparison_png": "previews/comparison.png",
            }
        )
        write_json(render_evidence_path, render_evidence)
        run_result_path = case / "reports" / "run_result.json"
        run_result = json.loads(run_result_path.read_text(encoding="utf-8"))
        run_result["run_dir"] = "."
        run_result["candidate_pdf"] = "output/candidate.pdf"
        write_json(run_result_path, run_result)

        write_json(
            case / "contracts" / "page_run_contract.json",
            {
                "schema_version": "page-run-package/v1",
                "toolbox_key": "body.flow_text.single",
                "page_id": record.sample_id,
                "source_snapshot": "input/source.pdf",
                "source_sha256": record.sha256,
                "candidate_output": "output/candidate.pdf",
                "required_directories": ["contracts", "docs", "input", "output", "previews", "reports"],
                "source_is_immutable": True,
                "materialized_from": f"{args.source_run_id}/{record.sample_id}",
            },
        )
        (case / "docs" / "README.md").write_text(
            f"# {record.sample_id} 页级审计包\n\n"
            "原文：`input/source.pdf`；候选：`output/candidate.pdf`；并排图：`previews/comparison.png`；最终结论：`reports/quality_decision.json`。\n",
            encoding="utf-8",
        )
        publish_case_outputs(run_root=target_run, page_id=record.sample_id, case_root=case)
        decision = json.loads((case / "reports" / "quality_decision.json").read_text(encoding="utf-8"))
        pages.append(
            {
                "page_id": record.sample_id,
                "source_pdf": f"cases/{record.sample_id}/input/source.pdf",
                "candidate_pdf": f"cases/{record.sample_id}/output/candidate.pdf",
                "comparison_png": f"cases/{record.sample_id}/previews/comparison.png",
                "process_verdict": decision["process_verdict"],
                "product_verdict": decision["product_verdict"],
            }
        )

    write_json(
        target_run / "reports" / "batch_summary.json",
        {
            "schema_version": "p3-development-run/v2",
            "run_id": target_run_id,
            "materialized_from_run_id": args.source_run_id,
            "provider": "qwen",
            "model": "Qwen/Qwen3.6-35B-A3B",
            "model_recalled_during_materialization": False,
            "split": "development",
            "holdout_accessed": False,
            "pages": pages,
            "all_process_pass": all(page["process_verdict"] == "PASS" for page in pages),
            "all_product_pass": all(page["product_verdict"] == "PASS" for page in pages),
        },
    )
    write_json(
        target_run / "reports" / "materialization_record.json",
        {
            "source_run_id": args.source_run_id,
            "target_run_id": target_run_id,
            "operation": "structure_only_copy",
            "translation_or_layout_reexecuted": False,
            "source_pdfs_copied": len(pages),
            "candidate_pdfs_copied": len(pages),
        },
    )
    write_artifact_index(target_run)
    print(target_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
