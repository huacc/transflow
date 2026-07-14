from __future__ import annotations

import argparse
import json
from pathlib import Path

import fitz

from page_toolbox_puncture.contracts import write_json


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args()

    run_dir = args.run_dir.resolve()
    contract = _read_json(run_dir / "contracts" / "batch_run_contract.json")
    case_order = [str(item) for item in contract["execution_order"]]
    reports_dir = run_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    pages: list[dict[str, object]] = []
    merged = fitz.open()
    for case_id in case_order:
        case_dir = run_dir / "cases" / case_id
        result = _read_json(case_dir / "reports" / "run_result.json")
        quality = _read_json(case_dir / "reports" / "quality_decision.json")
        result_pdf = case_dir / "output" / "result.pdf"
        has_candidate = bool(result.get("candidate_pdf"))
        result_pdf_available = has_candidate and result_pdf.is_file()
        if has_candidate and not result_pdf_available:
            raise FileNotFoundError(f"candidate_result_pdf_missing:{case_id}")
        if result_pdf_available:
            with fitz.open(result_pdf) as source:
                if source.page_count != 1:
                    raise ValueError(f"translated_result_page_count_invalid:{case_id}:{source.page_count}")
                merged.insert_pdf(source)

        pattern_path = case_dir / "reports" / "layout_pattern_qwen_decision.json"
        typography_path = case_dir / "reports" / "typography_density_qwen_decision.json"
        diagnostic_path = case_dir / "reports" / "diagnostic_render_evidence.json"
        pattern = _read_json(pattern_path) if pattern_path.is_file() else {}
        typography = _read_json(typography_path) if typography_path.is_file() else {}
        diagnostic = _read_json(diagnostic_path) if diagnostic_path.is_file() else {}
        if not result_pdf_available:
            result_kind = "no_product_pdf"
        elif diagnostic:
            result_kind = str(diagnostic.get("diagnostic_kind") or "diagnostic_candidate")
        elif result.get("product_verdict") == "PASS":
            result_kind = "product_candidate"
        else:
            result_kind = "product_failed_candidate"
        translated = result_pdf_available and (bool(diagnostic.get("translated", True)) if diagnostic else True)
        pages.append(
            {
                "page_id": case_id,
                "process_verdict": result.get("process_verdict"),
                "product_verdict": result.get("product_verdict"),
                "terminal_state": result.get("terminal_state"),
                "failure_owner": result.get("failure_owner"),
                "selected_column_profiles": result.get("selected_column_profiles", []),
                "layout_pattern": pattern.get("pattern"),
                "multi_band_variant": pattern.get("multi_band_variant"),
                "typography_verdict": typography.get("verdict"),
                "typography_reason": typography.get("reason"),
                "result_kind": result_kind,
                "result_pdf_available": result_pdf_available,
                "legacy_non_candidate_pdf_ignored": not has_candidate and result_pdf.is_file(),
                "diagnostic_page_extended": bool(diagnostic.get("page_extended", False)),
                "original_page_height": diagnostic.get("original_page_height"),
                "diagnostic_page_height": diagnostic.get("diagnostic_page_height"),
                "findings": quality.get("findings", []),
                "run_dir": str(case_dir),
                "result_pdf": str(result_pdf) if result_pdf_available else None,
                "comparison_png": str(case_dir / "previews" / "comparison.png") if result_pdf_available else None,
                "translated": translated,
            }
        )

    complete_result_set = all(bool(item["result_pdf_available"]) for item in pages)
    batch_pdf = reports_dir / "batch_result.pdf"
    if complete_result_set:
        merged.save(batch_pdf, garbage=4, deflate=True)
    elif batch_pdf.is_file():
        batch_pdf.unlink()
    merged.close()
    summary = {
        "schema_version": "p5-batch-summary/v7",
        "run_id": run_dir.name,
        "toolbox_key": "body.flow_text.multi",
        "execution_mode": contract["execution_mode"],
        "provider": "self_hosted_qwen",
        "model": contract["translation_provider"]["model"],
        "page_count": len(pages),
        "process_pass_count": sum(item["process_verdict"] == "PASS" for item in pages),
        "process_fail_count": sum(item["process_verdict"] == "FAIL" for item in pages),
        "product_pass_count": sum(item["product_verdict"] == "PASS" for item in pages),
        "product_fail_count": sum(item["product_verdict"] == "FAIL" for item in pages),
        "product_not_reached_count": sum(item["product_verdict"] == "NOT_REACHED" for item in pages),
        "result_pdf_count": sum(bool(item["result_pdf_available"]) for item in pages),
        "missing_result_pdf_count": sum(not bool(item["result_pdf_available"]) for item in pages),
        "translated_result_pdf_count": sum(bool(item["translated"]) for item in pages),
        "diagnostic_result_count": sum(item["result_kind"] == "capability_failure_diagnostic" for item in pages),
        "legacy_non_candidate_pdf_ignored_count": sum(bool(item["legacy_non_candidate_pdf_ignored"]) for item in pages),
        "batch_result_pdf": str(batch_pdf) if complete_result_set else None,
        "pages": pages,
    }
    write_json(reports_dir / "batch_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
