from __future__ import annotations

import argparse
import json
import math
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[1]


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "__", value).strip("_") or "root"


def sheet_image(group: str, sample_ids: list[str], image_root: Path) -> Image.Image:
    columns = 5
    rows = 4
    cell_width = 220
    cell_height = 310
    header_height = 40
    canvas = Image.new("RGB", (columns * cell_width, header_height + rows * cell_height), "white")
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default()
    draw.text((10, 12), group, fill="black", font=font)
    for index, sample_id in enumerate(sample_ids):
        row, column = divmod(index, columns)
        x = column * cell_width
        y = header_height + row * cell_height
        with Image.open(image_root / f"{sample_id}.png") as source:
            page = source.convert("RGB")
            page.thumbnail((200, 275), Image.Resampling.LANCZOS)
        px = x + (cell_width - page.width) // 2
        py = y + 24
        canvas.paste(page, (px, py))
        draw.text((x + 8, y + 7), sample_id, fill="black", font=font)
        draw.rectangle((x, y, x + cell_width - 1, y + cell_height - 1), outline="#c8c8c8", width=1)
    return canvas


def exclusion_groups(path: Path, items_key: str) -> dict[str, list[str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {items_key: [str(row["sample_id"]) for row in data[items_key]]}


def route_groups(run_root: Path) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = defaultdict(list)
    for line in (run_root / "routes" / "final_routes.jsonl").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        leaf = "/".join(row["final_path"]) if row["complete_to_leaf"] else f"INCONCLUSIVE/{row['failed_node']}"
        groups[leaf].append(str(row["sample_id"]))
    return dict(sorted(groups.items()))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--mode", choices=("routes", "exclusions"), required=True)
    parser.add_argument("--items-json")
    parser.add_argument("--items-key", default="excluded")
    parser.add_argument("--output-dir")
    args = parser.parse_args()
    run_root = ROOT / "artifacts" / "runs" / args.run_id
    image_root = run_root / "evidence" / "page_images"
    if args.mode == "exclusions":
        if not args.items_json:
            raise ValueError("items_json_required_for_exclusions")
        groups = exclusion_groups((ROOT / args.items_json).resolve(), args.items_key)
    else:
        groups = route_groups(run_root)
    output_root = (
        (ROOT / args.output_dir).resolve()
        if args.output_dir
        else ROOT / "reports" / "runs" / args.run_id / f"review_contact_sheets_{args.mode}"
    )
    output_root.mkdir(parents=True, exist_ok=True)
    for path in output_root.glob("*.png"):
        path.unlink()

    manifest: list[dict[str, Any]] = []
    for group, sample_ids in groups.items():
        for sheet_index in range(math.ceil(len(sample_ids) / 20)):
            items = sample_ids[sheet_index * 20 : (sheet_index + 1) * 20]
            filename = f"{safe_name(group)}_{sheet_index + 1:03d}.png"
            target = output_root / filename
            sheet = sheet_image(f"{group} [{sheet_index + 1}]", items, image_root)
            sheet.save(target, format="PNG", optimize=True)
            manifest.append({"group": group, "sheet": filename, "sample_ids": items})
    (output_root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"CONTACT_SHEETS_READY": True, "mode": args.mode, "sheet_count": len(manifest), "item_count": sum(len(row["sample_ids"]) for row in manifest), "output": str(output_root)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
