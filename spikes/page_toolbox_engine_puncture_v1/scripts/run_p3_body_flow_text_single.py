from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from page_toolbox_puncture.contracts import write_json
from page_toolbox_puncture.sample_snapshot import sha256_file
from page_toolbox_puncture.translation import QwenConfig, QwenPageTranslationProvider
from toolbox_cadence.lifecycle import read_sample_manifest
from toolbox_cadence.models import SampleSplit
from toolboxes.body.flow_text.single.tools.engine import run_page
from toolboxes.body.flow_text.single.tools.run_package import initialize_batch_package, publish_case_outputs, write_artifact_index


TOOLBOX_ROOT = ROOT / "toolboxes" / "body" / "flow_text" / "single"


def main() -> int:
    parser = argparse.ArgumentParser(description="Run P3 development pages through body.flow_text.single")
    parser.add_argument("--run-id")
    parser.add_argument("--font-file", default="C:/Windows/Fonts/simhei.ttf")
    args = parser.parse_args()

    records = read_sample_manifest(TOOLBOX_ROOT / "samples" / "manifest.jsonl", "body.flow_text.single")
    development = [record for record in records if record.split is SampleSplit.DEVELOPMENT]
    run_id = args.run_id or "p3-qwen-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_root = TOOLBOX_ROOT / "runs" / run_id
    prompt_path = TOOLBOX_ROOT / "prompts" / "page_translation.zh-CN.md"
    prompt = prompt_path.read_text(encoding="utf-8")
    provider = QwenPageTranslationProvider(QwenConfig.from_environment(), prompt)
    initialize_batch_package(
        run_root=run_root,
        toolbox_root=TOOLBOX_ROOT,
        run_id=run_id,
        records=records,
        prompt_path=prompt_path,
        model=provider.model_name,
    )

    page_results = []
    for record in development:
        source_pdf = TOOLBOX_ROOT / record.source_ref
        if sha256_file(source_pdf) != record.sha256:
            raise RuntimeError(f"development_sample_hash_mismatch:{record.sample_id}")
        result = run_page(
            source_pdf=source_pdf,
            page_id=record.sample_id,
            run_dir=run_root / "cases" / record.sample_id,
            provider=provider,
            font_file=args.font_file,
        )
        publish_case_outputs(run_root=run_root, page_id=record.sample_id, case_root=run_root / "cases" / record.sample_id)
        page_results.append(
            {
                "page_id": result.page_id,
                "candidate_pdf": result.candidate_pdf,
                "process_verdict": result.process_verdict,
                "product_verdict": result.product_verdict,
                "terminal_state": result.terminal_state,
                "failure_owner": result.failure_owner,
            }
        )
    summary = {
        "schema_version": "p3-development-run/v1",
        "run_id": run_id,
        "toolbox_key": "body.flow_text.single",
        "provider": "qwen",
        "model": provider.model_name,
        "split": "development",
        "holdout_accessed": False,
        "pages": page_results,
        "all_process_pass": all(item["process_verdict"] == "PASS" for item in page_results),
        "all_pages_have_product_decision": all(item["product_verdict"] in {"PASS", "FAIL"} for item in page_results),
    }
    write_json(run_root / "reports" / "batch_summary.json", summary)
    write_artifact_index(run_root)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["all_process_pass"] and summary["all_pages_have_product_decision"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
