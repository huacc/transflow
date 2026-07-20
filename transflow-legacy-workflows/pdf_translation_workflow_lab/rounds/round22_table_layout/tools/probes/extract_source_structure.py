import argparse
import json
import sys
from pathlib import Path

import fitz

ROUND_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROUND_ROOT / "tools"))

from generate_round22_layout_candidate import extract_lines, page_stats, rgb255_from_int  # noqa: E402


def rect_values(rect: fitz.Rect) -> list[float]:
    return [round(rect.x0, 3), round(rect.y0, 3), round(rect.x1, 3), round(rect.y1, 3)]


def run(source_pdf: Path, output: Path) -> None:
    doc = fitz.open(source_pdf)
    extracted_pages = extract_lines(doc)
    pages = []
    for page_index, lines in enumerate(extracted_pages):
        stats = page_stats(lines, doc[page_index].rect)
        pages.append(
            {
                "page_index": page_index,
                "page_rect": rect_values(doc[page_index].rect),
                "page_stats": {
                    "font_q25": stats.font_q25,
                    "font_q50": stats.font_q50,
                    "font_q75": stats.font_q75,
                    "font_q90": stats.font_q90,
                    "font_max": stats.font_max,
                    "width_q25": stats.width_q25,
                    "width_q50": stats.width_q50,
                    "width_q75": stats.width_q75,
                    "text_y_median": stats.text_y_median,
                    "body_color_int": stats.body_color_int,
                    "body_color_rgb": rgb255_from_int(stats.body_color_int),
                    "accent_colors": sorted(stats.accent_colors),
                    "accent_color_rgbs": [rgb255_from_int(color) for color in sorted(stats.accent_colors)],
                },
                "lines": [
                    {
                        "unit_id": line.unit_id,
                        "page_index": line.page_index,
                        "block_index": line.block_index,
                        "line_index": line.line_index,
                        "text": line.text,
                        "bbox": rect_values(line.rect),
                        "font_size": line.font_size,
                        "font": line.font,
                        "color_int": line.color_int,
                        "color_rgb": rgb255_from_int(line.color_int),
                        "first_color_int": line.first_color_int,
                        "first_color_rgb": rgb255_from_int(line.first_color_int),
                        "has_symbol_span": line.has_symbol_span,
                    }
                    for line in lines
                ],
            }
        )
    report = {
        "tool": "extract_source_structure",
        "source_pdf": str(source_pdf),
        "page_count": len(pages),
        "pages": pages,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    doc.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-pdf", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run(args.source_pdf, args.output)


if __name__ == "__main__":
    main()
