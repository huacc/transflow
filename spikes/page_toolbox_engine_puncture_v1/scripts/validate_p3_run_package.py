from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from page_toolbox_puncture.contracts import write_json
from page_toolbox_puncture.sample_snapshot import sha256_file
from toolboxes.body.flow_text.single.tools.run_package import write_artifact_index


PAGE_REQUIRED = (
    "contracts/page_run_contract.json",
    "docs/README.md",
    "input/source.pdf",
    "input/page_facts.json",
    "input/page_template.json",
    "input/translation_request.json",
    "output/translation_bundle.json",
    "output/layout_plan.json",
    "output/candidate.pdf",
    "previews/source.png",
    "previews/candidate.png",
    "previews/comparison.png",
    "reports/layout_findings.json",
    "reports/render_evidence.json",
    "reports/quality_decision.json",
    "reports/state_trace.json",
    "reports/run_result.json",
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate a self-contained P3 audit run package")
    parser.add_argument("--run-root", required=True)
    args = parser.parse_args()
    run_root = Path(args.run_root).resolve()
    contract = json.loads((run_root / "contracts" / "batch_run_contract.json").read_text(encoding="utf-8"))
    failures: list[str] = []
    page_results = []

    for page_id in contract["page_ids"]:
        case = run_root / "cases" / page_id
        missing = [relative for relative in PAGE_REQUIRED if not (case / relative).is_file()]
        failures.extend(f"missing:{page_id}:{relative}" for relative in missing)
        if missing:
            continue
        page_contract = json.loads((case / "contracts" / "page_run_contract.json").read_text(encoding="utf-8"))
        source = case / "input" / "source.pdf"
        candidate = case / "output" / "candidate.pdf"
        source_hash = sha256_file(source)
        candidate_hash = sha256_file(candidate)
        checks = {
            "source_hash_matches_contract": source_hash == page_contract["source_sha256"],
            "batch_source_matches_case_source": sha256_file(run_root / "input" / "source_pdfs" / f"{page_id}_source.pdf") == source_hash,
            "batch_candidate_matches_case_candidate": sha256_file(run_root / "output" / f"{page_id}_candidate.pdf") == candidate_hash,
            "batch_preview_matches_case_preview": sha256_file(run_root / "previews" / f"{page_id}_comparison.png") == sha256_file(case / "previews" / "comparison.png"),
        }
        source_reader = PdfReader(str(source))
        candidate_reader = PdfReader(str(candidate))
        checks["pdf_reopens_and_page_count_matches"] = len(source_reader.pages) == len(candidate_reader.pages) == 1
        checks["mediabox_matches"] = source_reader.pages[0].mediabox == candidate_reader.pages[0].mediabox
        decision = json.loads((case / "reports" / "quality_decision.json").read_text(encoding="utf-8"))
        checks["quality_decision_present"] = decision.get("product_verdict") in {"PASS", "FAIL"}
        for name, passed in checks.items():
            if not passed:
                failures.append(f"check_failed:{page_id}:{name}")
        page_results.append({"page_id": page_id, "source_sha256": source_hash, "candidate_sha256": candidate_hash, "checks": checks})

    report = {
        "schema_version": "run-package-validation/v1",
        "run_root": str(run_root),
        "verdict": "PASS" if not failures else "FAIL",
        "page_count": len(page_results),
        "pages": page_results,
        "failures": failures,
    }
    write_json(run_root / "reports" / "package_validation.json", report)
    write_artifact_index(run_root)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not failures else 2


if __name__ == "__main__":
    raise SystemExit(main())
