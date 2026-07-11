from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from page_classifier.io_utils import read_jsonl, sha256_file, write_json


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--sample-dir", default="样本1")
    parser.add_argument("--source-manifest", default="manifests/source_manifest.jsonl")
    parser.add_argument("--result-dir", default="分类结果")
    args = parser.parse_args()
    run_id = args.run_id
    run_root = ROOT / "artifacts" / "runs" / run_id
    routes = read_jsonl(run_root / "routes" / "final_routes.jsonl")
    sample_root = (ROOT / args.sample_dir).resolve()
    source_rows = read_jsonl((ROOT / args.source_manifest).resolve())
    source_by_id = {row["sample_id"]: row for row in source_rows}
    target_root = (ROOT / args.result_dir).resolve()
    if target_root.parent != ROOT.resolve():
        raise RuntimeError(f"unsafe_target:{target_root}")
    definition_docs: dict[Path, str] = {}
    if target_root.exists():
        definition_docs = {
            path.relative_to(target_root): path.read_text(encoding="utf-8")
            for path in target_root.rglob("分类说明.md")
        }
        shutil.rmtree(target_root)
    target_root.mkdir()

    records = []
    counts: Counter[str] = Counter()
    for route in sorted(routes, key=lambda row: row["sample_id"]):
        sample_id = route["sample_id"]
        source_pdf = sample_root / f"{sample_id}.pdf"
        if route["complete_to_leaf"]:
            leaf = "/".join(route["final_path"])
        else:
            leaf = f"INCONCLUSIVE/{route['failed_node']}"
        destination = target_root / Path(leaf) / source_pdf.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_pdf, destination)
        source_hash = sha256_file(source_pdf)
        destination_hash = sha256_file(destination)
        if source_hash != destination_hash or source_hash != source_by_id[sample_id]["sample_sha256"]:
            raise RuntimeError(f"copy_hash_mismatch:{sample_id}")
        counts[leaf] += 1
        records.append(
            {
                "run_id": run_id,
                "sample_id": sample_id,
                "leaf": leaf,
                "source_pdf": str(source_pdf),
                "result_pdf": str(destination),
                "sha256": source_hash,
            }
        )

    for relative_path, content in definition_docs.items():
        definition_path = target_root / relative_path
        definition_path.parent.mkdir(parents=True, exist_ok=True)
        definition_path.write_text(content, encoding="utf-8")

    sample_pdfs = sorted(sample_root.glob("*.pdf"))
    result_pdfs = sorted(target_root.rglob("*.pdf"))
    sample_set = {(path.name, sha256_file(path)) for path in sample_pdfs}
    result_items = [(path.name, sha256_file(path)) for path in result_pdfs]
    result_set = set(result_items)
    verdict = {
        "run_id": run_id,
        "SAMPLE_RESULT_PDF_SET_EQUAL": sample_set == result_set and len(result_items) == len(result_set),
        "sample_pdf_count": len(sample_pdfs),
        "result_pdf_count": len(result_pdfs),
        "unique_result_pdf_count": len(result_set),
        "missing_from_results": sorted(name for name, digest in sample_set - result_set),
        "extra_in_results": sorted(name for name, digest in result_set - sample_set),
        "classification_counts": dict(sorted(counts.items())),
    }
    report_root = ROOT / "reports" / "runs" / run_id
    report_root.mkdir(parents=True, exist_ok=True)
    write_json(report_root / "set_equality_verdict.json", verdict)
    (report_root / "classification_manifest.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in records),
        encoding="utf-8",
    )
    if not verdict["SAMPLE_RESULT_PDF_SET_EQUAL"]:
        raise RuntimeError("sample_result_pdf_set_not_equal")
    print(json.dumps(verdict, ensure_ascii=False))


if __name__ == "__main__":
    main()
