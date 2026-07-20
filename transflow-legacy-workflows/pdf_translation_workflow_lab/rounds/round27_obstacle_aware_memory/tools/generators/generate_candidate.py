import argparse
import json
import math
import sys
from pathlib import Path

import fitz

ROUND_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROUND_ROOT / "tools"))

from generate_round22_layout_candidate import color_int_from_rgb255, render_previews, rgb_from_int  # noqa: E402


def erase_planned_group(page: fitz.Page, group: dict) -> None:
    bg_rgb = group.get("background_rgb") or [255, 255, 255]
    bg = tuple(value / 255 for value in bg_rgb)
    page.draw_rect(fitz.Rect(group["erase_rect"]), color=bg, fill=bg, width=0, overlay=True)


def draw_planned_group(page: fitz.Page, group: dict) -> dict:
    text = group.get("target_text", "")
    if not text:
        group["fit_status"] = "skipped_empty_text"
        group["fit_attempts"] = []
        return group

    target_rect = fitz.Rect(group["target_rect"])

    role = group.get("role")
    color = rgb_from_int(group.get("color_int"))
    if role == "red_heading":
        color = (0.84, 0.18, 0.22)

    start_size = float(group.get("font_start") or 8.0)
    min_size = float(group.get("font_min") or max(4.0, start_size * 0.7))
    attempts = []
    scales = [1.0, 0.94, 0.88, 0.82, 0.76, min_size / max(start_size, 0.1)]

    for scale in scales:
        font_size = max(min_size, start_size * scale)
        trial_rect = fitz.Rect(target_rect)
        if role in {"body", "red_note", "compact_panel", "nav_footer"}:
            trial_rect.y1 = min(page.rect.height - 4.0, trial_rect.y1 + (start_size - font_size) * 3.5)
        if not math.isfinite(trial_rect.x0 + trial_rect.y0 + trial_rect.x1 + trial_rect.y1) or trial_rect.width <= 0 or trial_rect.height <= 0:
            attempts.append({"font_size": round(font_size, 3), "rect": list(trial_rect), "result": "invalid_rect"})
            continue

        if role == "red_note":
            text_rect = fitz.Rect(trial_rect)
            text_rect.x0 += max(7.0, font_size * 1.25)
            bullet_color = rgb_from_int(group.get("bullet_color_int") or color_int_from_rgb255((210, 50, 58)))
            bullet_size = max(3.2, font_size * 0.62)
            cy = trial_rect.y0 + font_size * 0.78
            x = trial_rect.x0 + 1.0
            result = page.insert_textbox(
                text_rect,
                text.lstrip("- ").strip(),
                fontsize=font_size,
                fontname="helv",
                color=color,
                align=fitz.TEXT_ALIGN_LEFT,
                overlay=True,
            )
            attempts.append({"font_size": round(font_size, 3), "rect": [round(v, 3) for v in trial_rect], "result": round(float(result), 3)})
            if result >= -0.1:
                triangle = [
                    fitz.Point(x, cy - bullet_size * 0.72),
                    fitz.Point(x, cy + bullet_size * 0.72),
                    fitz.Point(x + bullet_size * 1.05, cy),
                    fitz.Point(x, cy - bullet_size * 0.72),
                ]
                page.draw_polyline(triangle, color=bullet_color, fill=bullet_color, width=0.2, overlay=True)
                group["output_font_size"] = font_size
                group["output_rect"] = [round(v, 3) for v in trial_rect]
                group["fit_status"] = "fit"
                group["fit_attempts"] = attempts
                return group
            continue

        result = page.insert_textbox(
            trial_rect,
            text,
            fontsize=font_size,
            fontname="helv",
            color=color,
            align=fitz.TEXT_ALIGN_LEFT,
            overlay=True,
        )
        attempts.append({"font_size": round(font_size, 3), "rect": [round(v, 3) for v in trial_rect], "result": round(float(result), 3)})
        if result >= -0.1:
            group["output_font_size"] = font_size
            group["output_rect"] = [round(v, 3) for v in trial_rect]
            group["fit_status"] = "fit"
            group["fit_attempts"] = attempts
            return group

    group["output_font_size"] = min_size
    group["output_rect"] = [round(v, 3) for v in target_rect]
    group["fit_status"] = "overflow_after_fit"
    group["fit_attempts"] = attempts
    return group


def run(source_pdf: Path, layout_plan: Path, output_pdf: Path, reports_dir: Path, previews_dir: Path) -> None:
    plan = json.loads(layout_plan.read_text(encoding="utf-8"))
    doc = fitz.open(source_pdf)
    evidence_pages = []
    for page_plan in plan["pages"]:
        page = doc[int(page_plan["page_index"])]
        for group in page_plan["groups"]:
            erase_planned_group(page, group)
        groups = []
        for group in page_plan["groups"]:
            groups.append(draw_planned_group(page, dict(group)))
        evidence_pages.append({"page_index": page_plan["page_index"], "group_count": len(groups), "groups": groups})

    output_pdf.parent.mkdir(parents=True, exist_ok=True)
    doc.save(output_pdf, garbage=4, deflate=True)
    doc.close()

    reports_dir.mkdir(parents=True, exist_ok=True)
    evidence = {
        "tool": "generate_candidate",
        "source_pdf": str(source_pdf),
        "layout_plan": str(layout_plan),
        "output_pdf": str(output_pdf),
        "pages": evidence_pages,
    }
    (reports_dir / "generation_evidence.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    render_previews(output_pdf, previews_dir, "candidate")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-pdf", type=Path, required=True)
    parser.add_argument("--layout-plan", type=Path, required=True)
    parser.add_argument("--output-pdf", type=Path, required=True)
    parser.add_argument("--reports-dir", type=Path, required=True)
    parser.add_argument("--previews-dir", type=Path, required=True)
    args = parser.parse_args()
    run(args.source_pdf, args.layout_plan, args.output_pdf, args.reports_dir, args.previews_dir)


if __name__ == "__main__":
    main()
