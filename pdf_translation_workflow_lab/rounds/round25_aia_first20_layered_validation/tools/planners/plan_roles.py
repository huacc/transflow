import argparse
import json
import sys
from pathlib import Path

import fitz

ROUND_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROUND_ROOT / "tools"))

from generate_round22_layout_candidate import Line, build_groups, load_translations, rgb255_from_int  # noqa: E402


def rect_values(rect: fitz.Rect) -> list[float]:
    return [round(rect.x0, 3), round(rect.y0, 3), round(rect.x1, 3), round(rect.y1, 3)]


def line_from_json(item: dict) -> Line:
    return Line(
        unit_id=item["unit_id"],
        page_index=int(item["page_index"]),
        block_index=int(item["block_index"]),
        line_index=int(item["line_index"]),
        text=item["text"],
        rect=fitz.Rect(item["bbox"]),
        font_size=float(item["font_size"]),
        font=item.get("font", ""),
        color_int=item.get("color_int"),
        first_color_int=item.get("first_color_int"),
        has_symbol_span=bool(item.get("has_symbol_span")),
    )


def run(source_structure: Path, translations_json: Path, output: Path) -> None:
    structure = json.loads(source_structure.read_text(encoding="utf-8"))
    translations = load_translations(translations_json)
    pages = []
    for page in structure["pages"]:
        page_rect = fitz.Rect(page["page_rect"])
        lines = [line_from_json(item) for item in page["lines"]]
        groups = build_groups(lines, page_rect, translations)
        pages.append(
            {
                "page_index": page["page_index"],
                "page_rect": page["page_rect"],
                "groups": [
                    {
                        "group_id": group.group_id,
                        "page_index": group.page_index,
                        "line_ids": [line.unit_id for line in group.lines],
                        "role": group.role,
                        "source_rect": rect_values(group.source_rect),
                        "target_text": group.target_text,
                        "source_font_size": group.source_font_size,
                        "color_int": group.color_int,
                        "color_rgb": rgb255_from_int(group.color_int),
                        "bullet_color_int": group.bullet_color_int,
                        "bullet_color_rgb": rgb255_from_int(group.bullet_color_int),
                        "role_evidence": {
                            "line_count": len(group.lines),
                            "has_symbol_span": any(line.has_symbol_span for line in group.lines),
                            "source_text_sample": " ".join(line.text for line in group.lines)[:180],
                        },
                    }
                    for group in groups
                ],
            }
        )
    report = {
        "tool": "plan_roles",
        "source_structure": str(source_structure),
        "translations_json": str(translations_json),
        "pages": pages,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-structure", type=Path, required=True)
    parser.add_argument("--translations-json", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run(args.source_structure, args.translations_json, args.output)


if __name__ == "__main__":
    main()
