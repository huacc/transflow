from __future__ import annotations

from pathlib import Path
from typing import Iterable

import fitz
from PIL import Image, ImageChops, ImageDraw


def render_page(pdf_path: Path, out_png: Path, *, page_index: int = 0, zoom: float = 2.0, clip: tuple[float, float, float, float] | None = None) -> dict[str, object]:
    with fitz.open(pdf_path) as document:
        page = document[page_index]
        pixmap = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=fitz.Rect(clip) if clip else None, alpha=False)
        out_png.parent.mkdir(parents=True, exist_ok=True)
        pixmap.save(out_png)
        return {"path": str(out_png), "page_index": page_index, "zoom": zoom, "width": pixmap.width, "height": pixmap.height}


def render_contact_sheet(source_pdf: Path, candidate_pdf: Path, out_png: Path, *, page_index: int = 0, zoom: float = 2.0, clip: tuple[float, float, float, float] | None = None) -> dict[str, object]:
    source = _render_image(source_pdf, page_index, zoom, clip)
    candidate = _render_image(candidate_pdf, page_index, zoom, clip)
    pad, label_height = 20, 28
    canvas = Image.new("RGB", (source.width + candidate.width + pad * 3, max(source.height, candidate.height) + label_height + pad * 2), "white")
    draw = ImageDraw.Draw(canvas)
    draw.text((pad, 8), "source", fill="black")
    draw.text((source.width + pad * 2, 8), "candidate", fill="black")
    canvas.paste(source, (pad, label_height + pad))
    canvas.paste(candidate, (source.width + pad * 2, label_height + pad))
    out_png.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_png)
    return {"path": str(out_png), "width": canvas.width, "height": canvas.height}


def outside_region_diff_ratio(
    source_pdf: Path,
    candidate_pdf: Path,
    allowed_regions: Iterable[tuple[float, float, float, float]],
    *,
    page_index: int = 0,
    zoom: float = 2.0,
    padding_points: float = 1.5,
    channel_tolerance: int = 3,
) -> float:
    source = _render_image(source_pdf, page_index, zoom, None)
    candidate = _render_image(candidate_pdf, page_index, zoom, None)
    if source.size != candidate.size:
        return 1.0
    difference = ImageChops.difference(source, candidate).convert("RGB")
    mask = Image.new("1", source.size, 1)
    draw = ImageDraw.Draw(mask)
    for rect in allowed_regions:
        x0, y0, x1, y1 = rect
        box = (
            int(round((x0 - padding_points) * zoom)),
            int(round((y0 - padding_points) * zoom)),
            int(round((x1 + padding_points) * zoom)),
            int(round((y1 + padding_points) * zoom)),
        )
        draw.rectangle(box, fill=0)
    changed = 0
    considered = 0
    difference_bytes = difference.tobytes()
    mask_bytes = mask.convert("L").tobytes()
    for index, enabled in enumerate(mask_bytes):
        if not enabled:
            continue
        considered += 1
        offset = index * 3
        if max(difference_bytes[offset:offset + 3]) > channel_tolerance:
            changed += 1
    return changed / max(1, considered)


def _render_image(pdf_path: Path, page_index: int, zoom: float, clip: tuple[float, float, float, float] | None) -> Image.Image:
    with fitz.open(pdf_path) as document:
        page = document[page_index]
        pixmap = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=fitz.Rect(clip) if clip else None, alpha=False)
        return Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
