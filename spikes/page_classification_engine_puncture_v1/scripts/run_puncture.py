from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from page_classifier.engine import ClassificationEngine


def clear_previous_result_pdfs(result_dir: str) -> int:
    target_root = (ROOT / result_dir).resolve()
    if target_root.parent != ROOT.resolve():
        raise RuntimeError(f"unsafe_target:{target_root}")
    if not target_root.exists():
        return 0
    pdfs = list(target_root.rglob("*.pdf"))
    for path in pdfs:
        path.unlink()
    return len(pdfs)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample-dir", default="样本1")
    parser.add_argument("--source-manifest", default="manifests/source_manifest.jsonl")
    parser.add_argument("--result-dir", default="分类结果")
    args = parser.parse_args()
    sample_dir = (ROOT / args.sample_dir).resolve()
    source_manifest = (ROOT / args.source_manifest).resolve()
    removed_pdf_count = clear_previous_result_pdfs(args.result_dir)
    run_id = ClassificationEngine(ROOT, sample_dir=sample_dir, source_manifest=source_manifest).run()
    print(
        json.dumps(
            {
                "run_id": run_id,
                "ENGINE_RUN_COMPLETE": True,
                "previous_result_pdf_count_removed_before_run": removed_pdf_count,
                "sample_dir": str(sample_dir),
                "source_manifest": str(source_manifest),
            },
            ensure_ascii=False,
        )
    )
