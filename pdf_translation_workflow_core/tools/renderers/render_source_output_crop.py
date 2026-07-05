"""Render a source-vs-output PDF crop contact sheet.

tool_name: render_source_output_crop
category: renderers
input_contract: source PDF path, output PDF path, page index, crop rectangle, output PNG path, manifest path
output_contract: one PNG contact sheet plus JSON manifest
failure_signals: unreadable PDF, invalid page index, invalid crop rectangle, image output failure
fallback: use full-page render or mark visual evidence unavailable
anti_overfit_statement: renders caller-supplied page/crop evidence and never branches on sample filename, known page number, text, or coordinates
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import fitz
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import ensure_dir, rel, resolve_workspace_path, write_json  # noqa: E402


def parse_rect(value: str) -> fitz.Rect:
    parts = [float(item.strip()) for item in value.split(",")]
    if len(parts) != 4:
        raise ValueError("--crop must contain four comma-separated numbers: x0,y0,x1,y1")
    rect = fitz.Rect(parts)
    if rect.is_empty or rect.width <= 0 or rect.height <= 0:
        raise ValueError(f"invalid crop rectangle: {value}")
    return rect


def render_page(path: Path, page_index: int, zoom: float) -> Image.Image:
    doc = fitz.open(path)
    if page_index < 0 or page_index >= doc.page_count:
        doc.close()
        raise ValueError(f"page index {page_index} out of range for {path}")
    page = doc[page_index]
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    image = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
    doc.close()
    return image


def scaled_crop(image: Image.Image, rect: fitz.Rect, zoom: float, target_height: int | None) -> Image.Image:
    box = tuple(int(round(value * zoom)) for value in (rect.x0, rect.y0, rect.x1, rect.y1))
    crop = image.crop(box)
    if target_height and crop.height > 0:
        ratio = target_height / crop.height
        crop = crop.resize((max(1, int(round(crop.width * ratio))), target_height))
    return crop


def render_contact_sheet(
    source: Path,
    output: Path,
    page_index: int,
    crop: fitz.Rect,
    out_png: Path,
    manifest: Path,
    zoom: float,
    target_height: int | None,
    source_label: str,
    output_label: str,
) -> dict[str, Any]:
    source_image = render_page(source, page_index, zoom)
    output_image = render_page(output, page_index, zoom)
    crops = [
        (source_label, scaled_crop(source_image, crop, zoom, target_height)),
        (output_label, scaled_crop(output_image, crop, zoom, target_height)),
    ]

    pad = 24
    label_h = 28
    width = sum(image.width for _, image in crops) + pad * (len(crops) + 1)
    height = max(image.height for _, image in crops) + label_h + pad * 2
    sheet = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(sheet)
    x = pad
    for label, image in crops:
        draw.text((x, 8), label, fill="black")
        sheet.paste(image, (x, label_h + pad))
        x += image.width + pad

    ensure_dir(out_png.parent)
    sheet.save(out_png)
    result = {
        "tool": "render_source_output_crop",
        "source_pdf": rel(source),
        "output_pdf": rel(output),
        "page_index": page_index,
        "page_number": page_index + 1,
        "crop_rect": [round(crop.x0, 3), round(crop.y0, 3), round(crop.x1, 3), round(crop.y1, 3)],
        "zoom": zoom,
        "target_height": target_height,
        "source_label": source_label,
        "output_label": output_label,
        "contact_sheet": rel(out_png),
        "contact_sheet_width": width,
        "contact_sheet_height": height,
    }
    write_json(manifest, result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--page-index", type=int, required=True)
    parser.add_argument("--crop", required=True, help="x0,y0,x1,y1 in PDF points")
    parser.add_argument("--out", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--zoom", type=float, default=2.0)
    parser.add_argument("--target-height", type=int, default=None)
    parser.add_argument("--source-label", default="source")
    parser.add_argument("--output-label", default="output")
    args = parser.parse_args()
    result = render_contact_sheet(
        resolve_workspace_path(args.source),
        resolve_workspace_path(args.output),
        args.page_index,
        parse_rect(args.crop),
        Path(args.out),
        Path(args.manifest),
        args.zoom,
        args.target_height,
        args.source_label,
        args.output_label,
    )
    print(result["contact_sheet"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
