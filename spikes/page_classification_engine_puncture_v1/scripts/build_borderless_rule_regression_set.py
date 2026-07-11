from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from page_classifier.io_utils import read_jsonl, sha256_file


PROBLEMS = {
    "S2P0440": "body/table",
    "S2P0650": "body/composite/flow_text_table",
    "S2P0799": "body/table",
    "S2P1000": "body/table",
    "S2P0060": "body/table",
    "S2P0104": "body/composite/flow_text_table",
    "S2P0125": "body/composite/flow_text_table",
    "S2P0140": "body/table",
    "S2P0216": "body/table",
    "S2P0217": "body/composite/flow_text_table",
    "S2P0296": "body/composite/flow_text_table",
    "S2P0339": "body/composite/flow_text_table",
    "S2P0340": "body/table",
    "S2P0489": "body/composite/flow_text_table",
    "S2P0540": "body/table",
    "S2P0609": "body/composite/flow_text_chart",
    "S2P0628": "body/anchored_blocks",
    "S2P0816": "body/composite/flow_text_table",
    "S2P0817": "body/composite/flow_text_table",
    "S2P0880": "body/table",
    "S2P0920": "body/table",
    "S2P0945": "body/composite/flow_text_table",
    "S2P0946": "body/composite/flow_text_table",
    "S2P0955": "body/composite/flow_text_table",
    "S2P0970": "body/composite/flow_text_table",
    "S2P0972": "body/composite/flow_text_table",
}

CONFIRMED_CORRECT = {
    "S2P0026": "body/table",
    "S2P0049": "body/table",
    "S2P0054": "body/table",
    "S2P0057": "body/table",
    "S2P0059": "body/table",
    "S2P0091": "body/table",
    "S2P0115": "body/table",
    "S2P0116": "body/table",
    "S2P0117": "body/table",
    "S2P0118": "body/table",
    "S2P0055": "body/composite/flow_text_table",
    "S2P0093": "body/composite/flow_text_table",
    "S2P0136": "body/composite/flow_text_table",
    "S2P0174": "body/composite/flow_text_table",
    "S2P0293": "body/composite/flow_text_table",
    "S2P0355": "body/composite/flow_text_table",
    "S2P0555": "body/composite/flow_text_table",
    "S2P0665": "body/composite/flow_text_table",
    "S2P0788": "body/composite/flow_text_table",
    "S2P0977": "body/composite/flow_text_table",
    "S2P0168": "body/flow_text/multi",
    "S2P0624": "body/flow_text/multi",
    "S2P0629": "body/flow_text/multi",
    "S2P0905": "body/flow_text/multi",
    "S2P0986": "body/flow_text/multi",
    "S2P0043": "body/flow_text/single",
    "S2P0145": "body/flow_text/single",
    "S2P0304": "body/flow_text/single",
    "S2P0503": "body/flow_text/single",
    "S2P0964": "body/flow_text/single",
}

UNRELATED = {
    "S2P0001": "cover",
    "S2P0021": "cover",
    "S2P0041": "cover",
    "S2P0061": "cover",
    "S2P0081": "cover",
    "S2P0101": "cover",
    "S2P0042": "contents",
    "S2P0083": "contents",
    "S2P0102": "contents",
    "S2P0122": "contents",
    "S2P0142": "contents",
    "S2P0162": "contents",
    "S2P0020": "end",
    "S2P0040": "end",
    "S2P0100": "end",
    "S2P0120": "end",
    "S2P0240": "end",
    "S2P0260": "end",
    "S2P0080": "visual_only",
    "S2P0442": "visual_only",
    "S2P0579": "visual_only",
    "S2P0639": "visual_only",
    "S2P0860": "visual_only",
    "S2P0962": "visual_only",
    "S2P0450": "body/flow_text/single",
    "S2P0523": "body/flow_text/single",
    "S2P0625": "body/flow_text/visual_anchored",
    "S2P0626": "body/anchored_blocks",
    "S2P0627": "body/anchored_blocks",
    "S2P0869": "body/flow_text/multi",
}


def main() -> None:
    cohorts = {
        "problem": PROBLEMS,
        "confirmed_correct": CONFIRMED_CORRECT,
        "unrelated": UNRELATED,
    }
    all_ids = [sample_id for items in cohorts.values() for sample_id in items]
    if len(all_ids) != 86 or len(set(all_ids)) != 86:
        raise RuntimeError("regression_set_must_contain_86_unique_pages")

    sample_source = ROOT / "样本2"
    sample_target = ROOT / "样本_无边框规则回归"
    source_by_id = {
        row["sample_id"]: row for row in read_jsonl(ROOT / "manifests" / "sample2_source_manifest.jsonl")
    }
    if sample_target.exists():
        shutil.rmtree(sample_target)
    sample_target.mkdir()

    source_rows = []
    gold_rows = []
    for cohort, items in cohorts.items():
        for sample_id, expected_leaf in items.items():
            source_pdf = sample_source / f"{sample_id}.pdf"
            source_row = source_by_id.get(sample_id)
            if not source_pdf.exists() or source_row is None:
                raise RuntimeError(f"missing_regression_source:{sample_id}")
            destination = sample_target / source_pdf.name
            shutil.copy2(source_pdf, destination)
            if sha256_file(destination) != source_row["sample_sha256"]:
                raise RuntimeError(f"regression_copy_hash_mismatch:{sample_id}")
            source_rows.append(source_row)
            gold_rows.append(
                {
                    "sample_id": sample_id,
                    "cohort": cohort,
                    "expected_leaf": expected_leaf,
                }
            )

    manifest_root = ROOT / "manifests"
    (manifest_root / "borderless_rule_regression_source.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in source_rows),
        encoding="utf-8",
    )
    (manifest_root / "borderless_rule_regression_gold.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in gold_rows),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "REGRESSION_SET_READY": True,
                "sample_count": len(all_ids),
                "cohort_counts": {name: len(items) for name, items in cohorts.items()},
                "sample_dir": str(sample_target),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
