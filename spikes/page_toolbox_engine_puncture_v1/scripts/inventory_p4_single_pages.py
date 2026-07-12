from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

import fitz

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from page_toolbox_puncture.contracts import write_json
from page_toolbox_puncture.sample_snapshot import sha256_file


TOOLBOX_ROOT = ROOT / "toolboxes" / "body" / "flow_text" / "single"
INSPECTED_IDS = {"S2P0043", "S2P0044", "S2P0103", "S2P0106", "S2P0188", "S2P0524", "S2P0786"}


def main() -> int:
    parser = argparse.ArgumentParser(description="Inventory every classified body.flow_text.single PDF for P4")
    parser.add_argument("--input-dir", required=True)
    parser.add_argument("--lineage-manifest")
    args = parser.parse_args()
    input_dir = Path(args.input_dir).resolve()
    lineage = _load_lineage(Path(args.lineage_manifest)) if args.lineage_manifest else {}
    rows = [_inspect(path, lineage.get(path.stem)) for path in sorted(input_dir.glob("*.pdf"))]
    if not rows:
        raise RuntimeError("p4_input_is_empty")
    _assign_splits(rows)

    inventory_path = TOOLBOX_ROOT / "samples" / "p4_inventory.jsonl"
    inventory_path.parent.mkdir(parents=True, exist_ok=True)
    inventory_path.write_text("\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows) + "\n", encoding="utf-8")
    manifest_path = TOOLBOX_ROOT / "samples" / "p4_all_manifest.jsonl"
    manifest_rows = [
        {
            "sample_id": row["sample_id"],
            "toolbox_key": "body.flow_text.single",
            "split": row["split"],
            "source_ref": row["source_pdf"],
            "sha256": row["sha256"],
            "original_document_id": row["original_document_id"],
            "original_page_number": row["original_page_number"],
            "source_language": row["source_language"],
            "target_language": row["target_language"],
            "density_band": row["density_band"],
        }
        for row in rows
    ]
    manifest_path.write_text("\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in manifest_rows) + "\n", encoding="utf-8")
    summary = {
        "schema_version": "p4-inventory-summary/v1",
        "input_dir": str(input_dir),
        "pdf_count": len(rows),
        "source_language_counts": dict(Counter(row["source_language"] for row in rows)),
        "density_counts": dict(Counter(row["density_band"] for row in rows)),
        "split_counts": dict(Counter(row["split"] for row in rows)),
        "image_page_count": sum(row["image_count"] > 0 for row in rows),
        "drawing_page_count": sum(row["drawing_count"] > 0 for row in rows),
        "list_page_count": sum(row["list_marker_count"] > 0 for row in rows),
        "inspected_before_holdout_assignment": sorted(INSPECTED_IDS),
        "holdout_excludes_previously_inspected": all(row["sample_id"] not in INSPECTED_IDS for row in rows if row["split"] == "holdout"),
        "manifest": manifest_path.relative_to(ROOT).as_posix(),
        "inventory": inventory_path.relative_to(ROOT).as_posix(),
    }
    write_json(TOOLBOX_ROOT / "reports" / "p4_inventory_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def _load_lineage(path: Path) -> dict[str, dict[str, object]]:
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            values[str(row["sample_id"])] = row
    return values


def _inspect(path: Path, lineage: dict[str, object] | None) -> dict[str, object]:
    with fitz.open(path) as document:
        if document.page_count != 1:
            raise RuntimeError(f"p4_source_must_be_single_page:{path.name}")
        page = document[0]
        text = page.get_text("text")
        dictionary = page.get_text("dict")
        blocks = [block for block in dictionary.get("blocks", []) if block.get("type") == 0]
        spans = [span for block in blocks for line in block.get("lines", []) for span in line.get("spans", []) if str(span.get("text") or "").strip()]
        han = len(re.findall(r"[\u3400-\u9fff]", text))
        latin = len(re.findall(r"[A-Za-z]", text))
        source_language = "zh" if han >= 80 and han >= latin * 0.25 else "en"
        char_count = len(text.strip())
        density_band = "dense" if char_count >= 3500 else ("medium" if char_count >= 900 else "sparse")
        list_markers = len(re.findall(r"(?m)^\s*(?:[•●▪]|\(?\d+[.)])\s*", text))
        return {
            "sample_id": path.stem,
            "source_pdf": str(path),
            "sha256": sha256_file(path),
            "source_language": source_language,
            "target_language": "en" if source_language == "zh" else "zh-CN",
            "char_count": char_count,
            "han_count": han,
            "latin_count": latin,
            "density_band": density_band,
            "native_block_count": len(blocks),
            "native_span_count": len(spans),
            "list_marker_count": list_markers,
            "image_count": len(page.get_images(full=True)),
            "drawing_count": len(page.get_drawings()),
            "page_width": round(float(page.rect.width), 4),
            "page_height": round(float(page.rect.height), 4),
            "original_document_id": str((lineage or {}).get("report_id") or "unknown"),
            "original_page_number": int((lineage or {}).get("source_page_number") or 1),
            "source_document_sha256": str((lineage or {}).get("source_sha256") or "0" * 64),
            "split": "UNASSIGNED",
        }


def _assign_splits(rows: list[dict[str, object]]) -> None:
    groups: dict[tuple[str, str], list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        groups[(str(row["source_language"]), str(row["density_band"]))].append(row)

    holdout_ids: set[str] = set()
    for group in groups.values():
        eligible = sorted((row for row in group if row["sample_id"] not in INSPECTED_IDS), key=lambda row: str(row["sha256"]))
        count = max(1, round(len(group) * 0.15))
        holdout_ids.update(str(row["sample_id"]) for row in eligible[:count])

    development_ids = set(INSPECTED_IDS)
    candidates = [row for row in rows if row["sample_id"] not in holdout_ids and row["sample_id"] not in development_ids]
    ranked = sorted(
        candidates,
        key=lambda row: (
            -int(row["list_marker_count"] > 0),
            -int(row["drawing_count"] > 0),
            -int(row["image_count"] > 1),
            -int(row["char_count"]),
            str(row["sha256"]),
        ),
    )
    for row in ranked:
        if len(development_ids) >= 40:
            break
        development_ids.add(str(row["sample_id"]))

    for row in rows:
        sample_id = str(row["sample_id"])
        row["split"] = "holdout" if sample_id in holdout_ids else ("development" if sample_id in development_ids else "regression")


if __name__ == "__main__":
    raise SystemExit(main())
