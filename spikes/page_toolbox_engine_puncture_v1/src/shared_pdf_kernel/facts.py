from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path
from typing import Any

import fitz

from page_toolbox_puncture.contracts import DrawingObjectFact, ImageObjectFact, PageFacts, TextObjectFact
from page_toolbox_puncture.sample_snapshot import sha256_file


def canonical_sha256(value: Any) -> str:
    payload = json.dumps(_normal(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _normal(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return {field.name: _normal(getattr(value, field.name)) for field in dataclasses.fields(value)}
    if isinstance(value, fitz.Rect):
        return _rect(value)
    if isinstance(value, fitz.Point):
        return [round(float(value.x), 4), round(float(value.y), 4)]
    if isinstance(value, bytes):
        return {"sha256": hashlib.sha256(value).hexdigest(), "length": len(value)}
    if isinstance(value, dict):
        return {str(key): _normal(item) for key, item in sorted(value.items(), key=lambda row: str(row[0]))}
    if isinstance(value, (list, tuple)):
        return [_normal(item) for item in value]
    if isinstance(value, float):
        return round(value, 4)
    return value


def _rect(value: Any) -> tuple[float, float, float, float]:
    rect = fitz.Rect(value)
    return tuple(round(float(item), 4) for item in rect)


def extract_page_facts(pdf_path: Path, *, page_index: int = 0, page_id: str | None = None) -> PageFacts:
    source_sha256 = sha256_file(pdf_path)
    with fitz.open(pdf_path) as document:
        if page_index < 0 or page_index >= document.page_count:
            raise IndexError("page_index_out_of_range")
        page = document[page_index]
        text_objects: list[TextObjectFact] = []
        image_objects: list[ImageObjectFact] = []
        blocks = page.get_text("dict").get("blocks", [])
        for block_index, block in enumerate(blocks):
            if block.get("type") == 1:
                bbox = fitz.Rect(block.get("bbox", (0, 0, 0, 0)))
                image = block.get("image") or b""
                if not bbox.is_empty:
                    image_objects.append(
                        ImageObjectFact(
                            object_id=f"p{page_index}-image-{len(image_objects):04d}",
                            bbox=_rect(bbox),
                            width=int(block.get("width") or 0),
                            height=int(block.get("height") or 0),
                            content_sha256=hashlib.sha256(image).hexdigest(),
                        )
                    )
                continue
            if block.get("type") != 0:
                continue
            for line_index, line in enumerate(block.get("lines", [])):
                for span_index, span in enumerate(line.get("spans", [])):
                    text = str(span.get("text") or "")
                    bbox = fitz.Rect(span.get("bbox", (0, 0, 0, 0)))
                    if not text.strip() or bbox.is_empty:
                        continue
                    text_objects.append(
                        TextObjectFact(
                            object_id=f"p{page_index}-b{block_index:04d}-l{line_index:03d}-s{span_index:03d}",
                            text=text,
                            bbox=_rect(bbox),
                            font_name=str(span.get("font") or ""),
                            font_size=round(float(span.get("size") or 0.0), 4),
                            color_srgb=int(span.get("color") or 0),
                            block_index=block_index,
                            line_index=line_index,
                            span_index=span_index,
                        )
                    )

        drawing_objects: list[DrawingObjectFact] = []
        for drawing_index, drawing in enumerate(page.get_drawings()):
            bbox = fitz.Rect(drawing.get("rect", (0, 0, 0, 0)))
            if bbox.is_empty:
                continue
            normalized = {
                "rect": _rect(bbox),
                "items": _normal(drawing.get("items", [])),
                "type": drawing.get("type"),
                "color": _normal(drawing.get("color")),
                "fill": _normal(drawing.get("fill")),
                "width": round(float(drawing.get("width") or 0.0), 4),
                "closePath": bool(drawing.get("closePath")),
                "fill_opacity": round(float(drawing.get("fill_opacity") or 0.0), 4),
                "stroke_opacity": round(float(drawing.get("stroke_opacity") or 0.0), 4),
            }
            drawing_objects.append(
                DrawingObjectFact(
                    object_id=f"p{page_index}-drawing-{drawing_index:04d}",
                    bbox=_rect(bbox),
                    content_sha256=canonical_sha256(normalized),
                )
            )

        geometry = {
            "page_rect": _rect(page.rect),
            "mediabox": _rect(page.mediabox),
            "cropbox": _rect(page.cropbox),
            "rotation": int(page.rotation),
        }
        geometry_sha256 = canonical_sha256(geometry)
        text_objects_sha256 = canonical_sha256(text_objects)
        locked_objects_sha256 = canonical_sha256(
            {
                "geometry_sha256": geometry_sha256,
                "images": image_objects,
                "drawings": drawing_objects,
            }
        )
        return PageFacts(
            page_id=page_id or f"page-{page_index + 1}",
            source_pdf_sha256=source_sha256,
            width=round(float(page.rect.width), 4),
            height=round(float(page.rect.height), 4),
            native_text_object_count=len(text_objects),
            origin="shared_pdf_kernel.fitz",
            page_index=page_index,
            rotation=int(page.rotation),
            text_objects=tuple(text_objects),
            image_objects=tuple(image_objects),
            drawing_objects=tuple(drawing_objects),
            geometry_sha256=geometry_sha256,
            text_objects_sha256=text_objects_sha256,
            locked_objects_sha256=locked_objects_sha256,
        )

