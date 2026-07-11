from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from page_classifier.io_utils import read_jsonl, sha256_file


SOURCE_DIR = ROOT / "分类结果" / "body" / "table"
SAMPLE_DIR = ROOT / "样本_table复测"
SOURCE_MANIFEST = ROOT / "manifests" / "sample2_source_manifest.jsonl"
OUTPUT_MANIFEST = ROOT / "manifests" / "table_rerun_source_manifest.jsonl"


def main() -> None:
    source_rows = {row["sample_id"]: row for row in read_jsonl(SOURCE_MANIFEST)}
    source_pdfs = sorted(SOURCE_DIR.glob("*.pdf"))
    if not source_pdfs:
        raise RuntimeError("body_table_source_is_empty")

    if SAMPLE_DIR.exists():
        shutil.rmtree(SAMPLE_DIR)
    SAMPLE_DIR.mkdir()

    rows = []
    for source_pdf in source_pdfs:
        sample_id = source_pdf.stem
        row = source_rows.get(sample_id)
        if row is None:
            raise RuntimeError(f"missing_source_manifest_row:{sample_id}")
        destination = SAMPLE_DIR / source_pdf.name
        shutil.copy2(source_pdf, destination)
        digest = sha256_file(destination)
        if digest != row["sample_sha256"]:
            raise RuntimeError(f"sample_hash_mismatch:{sample_id}")
        rows.append(row)

    OUTPUT_MANIFEST.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "TABLE_RERUN_SUBSET_READY": True,
                "sample_count": len(rows),
                "sample_dir": str(SAMPLE_DIR),
                "source_manifest": str(OUTPUT_MANIFEST),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
