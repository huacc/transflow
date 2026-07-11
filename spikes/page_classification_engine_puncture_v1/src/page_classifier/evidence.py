from __future__ import annotations

import statistics
from pathlib import Path
from typing import Any

import fitz

from .io_utils import sha256_file


def _cluster_positions(values: list[tuple[float, int]], tolerance: float) -> list[dict[str, Any]]:
    clusters: list[list[tuple[float, int]]] = []
    for value, row_index in sorted(values):
        cluster = next(
            (items for items in clusters if abs(value - statistics.median(item[0] for item in items)) <= tolerance),
            None,
        )
        if cluster is None:
            clusters.append([(value, row_index)])
        else:
            cluster.append((value, row_index))
    return [
        {
            "x": statistics.median(item[0] for item in items),
            "rows": {item[1] for item in items},
        }
        for items in clusters
    ]


def _detect_borderless_table(
    layout_spans: list[dict[str, Any]],
    text_blocks: list[dict[str, Any]],
    page_width: float,
    page_height: float,
) -> dict[str, Any]:
    if not layout_spans:
        return {
            "evidence_id": "BTABLE1",
            "confidence": 0.0,
            "aligned_row_count": 0,
            "column_anchors": [],
            "area_ratio": 0.0,
            "outside_chars": sum(int(block["char_count"]) for block in text_blocks),
            "bbox": None,
        }

    median_height = statistics.median(float(span["bbox"][3]) - float(span["bbox"][1]) for span in layout_spans)
    y_tolerance = max(1.5, median_height * 0.22)
    rows: list[list[dict[str, Any]]] = []
    for span in sorted(layout_spans, key=lambda item: ((float(item["bbox"][1]) + float(item["bbox"][3])) / 2, float(item["bbox"][0]))):
        center_y = (float(span["bbox"][1]) + float(span["bbox"][3])) / 2
        row = next(
            (
                items
                for items in rows
                if abs(
                    center_y
                    - statistics.median((float(item["bbox"][1]) + float(item["bbox"][3])) / 2 for item in items)
                )
                <= y_tolerance
            ),
            None,
        )
        if row is None:
            rows.append([span])
        else:
            row.append(span)

    cell_gap = max(7.0, page_width * 0.012)
    minimum_column_gap = page_width * 0.1
    candidate_rows: list[dict[str, Any]] = []
    for items in rows:
        cells: list[dict[str, Any]] = []
        for span in sorted(items, key=lambda item: float(item["bbox"][0])):
            x0, y0, x1, y1 = (float(value) for value in span["bbox"])
            if cells and x0 - float(cells[-1]["bbox"][2]) <= cell_gap:
                cell = cells[-1]
                cell["bbox"] = [
                    min(float(cell["bbox"][0]), x0),
                    min(float(cell["bbox"][1]), y0),
                    max(float(cell["bbox"][2]), x1),
                    max(float(cell["bbox"][3]), y1),
                ]
                cell["text"] += str(span["text"])
                cell["block_ids"].add(str(span["block_id"]))
            else:
                cells.append(
                    {
                        "bbox": [x0, y0, x1, y1],
                        "text": str(span["text"]),
                        "block_ids": {str(span["block_id"])},
                    }
                )
        if len(cells) < 2 or float(cells[-1]["bbox"][0]) - float(cells[0]["bbox"][0]) < minimum_column_gap:
            continue
        candidate_rows.append(
            {
                "cells": cells,
                "center_y": statistics.median((float(cell["bbox"][1]) + float(cell["bbox"][3])) / 2 for cell in cells),
            }
        )

    anchor_tolerance = max(7.0, page_width * 0.015)
    clusters = _cluster_positions(
        [
            (float(cell["bbox"][0]), row_index)
            for row_index, row in enumerate(candidate_rows)
            for cell in row["cells"]
        ],
        anchor_tolerance,
    )
    supported = [cluster for cluster in clusters if len(cluster["rows"]) >= 4]
    pairs = [
        (left, right)
        for index, left in enumerate(supported)
        for right in supported[index + 1 :]
        if float(right["x"]) - float(left["x"]) >= minimum_column_gap
    ]
    if not pairs:
        return {
            "evidence_id": "BTABLE1",
            "confidence": 0.0,
            "aligned_row_count": 0,
            "column_anchors": [],
            "area_ratio": 0.0,
            "outside_chars": sum(int(block["char_count"]) for block in text_blocks),
            "bbox": None,
        }

    left_anchor, right_anchor = max(
        pairs,
        key=lambda pair: (
            len(pair[0]["rows"] & pair[1]["rows"]),
            min(len(pair[0]["rows"]), len(pair[1]["rows"])),
            float(pair[1]["x"]) - float(pair[0]["x"]),
        ),
    )
    aligned_rows = []
    same_block_count = 0
    for row in candidate_rows:
        left_cells = [cell for cell in row["cells"] if abs(float(cell["bbox"][0]) - float(left_anchor["x"])) <= anchor_tolerance]
        right_cells = [cell for cell in row["cells"] if abs(float(cell["bbox"][0]) - float(right_anchor["x"])) <= anchor_tolerance]
        if not left_cells or not right_cells:
            continue
        aligned_rows.append(row)
        if any(left["block_ids"] & right["block_ids"] for left in left_cells for right in right_cells):
            same_block_count += 1

    aligned_count = len(aligned_rows)
    if aligned_count < 4:
        confidence = 0.0
    else:
        row_gaps = [
            right["center_y"] - left["center_y"]
            for left, right in zip(sorted(aligned_rows, key=lambda item: item["center_y"]), sorted(aligned_rows, key=lambda item: item["center_y"])[1:])
        ]
        median_gap = statistics.median(row_gaps) if row_gaps else 0.0
        short_left_ratio = sum(
            min(len(str(cell["text"])) for cell in row["cells"] if abs(float(cell["bbox"][0]) - float(left_anchor["x"])) <= anchor_tolerance)
            <= 40
            for row in aligned_rows
        ) / aligned_count
        cross_block_pattern = median_gap >= median_height * 1.8 and short_left_ratio >= 0.75
        alignment_ratio = aligned_count / max(len(candidate_rows), 1)
        confidence = 0.82
        confidence += min(0.08, max(0, aligned_count - 3) * 0.02)
        if alignment_ratio >= 0.75:
            confidence += 0.04
        if same_block_count >= 4 or cross_block_pattern:
            confidence += 0.06
        confidence = min(round(confidence, 2), 0.99)
        if same_block_count < 4 and not cross_block_pattern:
            confidence = min(confidence, 0.89)

    if confidence < 0.9:
        return {
            "evidence_id": "BTABLE1",
            "confidence": confidence,
            "aligned_row_count": aligned_count,
            "column_anchors": [round(float(left_anchor["x"]), 2), round(float(right_anchor["x"]), 2)],
            "area_ratio": 0.0,
            "outside_chars": sum(int(block["char_count"]) for block in text_blocks),
            "bbox": None,
        }

    all_cells = [cell for row in aligned_rows for cell in row["cells"]]
    bbox = [
        min(float(cell["bbox"][0]) for cell in all_cells),
        min(float(cell["bbox"][1]) for cell in all_cells),
        max(float(cell["bbox"][2]) for cell in all_cells),
        max(float(cell["bbox"][3]) for cell in all_cells),
    ]
    area_ratio = max(0.0, (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])) / max(page_width * page_height, 1.0)
    outside_chars = 0
    for block in text_blocks:
        x0, y0, x1, y1 = (float(value) for value in block["bbox"])
        center = ((x0 + x1) / 2, (y0 + y1) / 2)
        if not _inside(tuple(bbox), center):
            outside_chars += int(block["char_count"])
    return {
        "evidence_id": "BTABLE1",
        "confidence": confidence,
        "aligned_row_count": aligned_count,
        "column_anchors": [round(float(left_anchor["x"]), 2), round(float(right_anchor["x"]), 2)],
        "area_ratio": round(min(area_ratio, 1.0), 5),
        "outside_chars": outside_chars,
        "bbox": [round(value, 2) for value in bbox],
    }


def _inside(rect: tuple[float, float, float, float], point: tuple[float, float]) -> bool:
    x0, y0, x1, y1 = rect
    return x0 <= point[0] <= x1 and y0 <= point[1] <= y1


def build_evidence(pdf_path: Path, source_meta: dict[str, Any], render_path: Path) -> dict[str, Any]:
    with fitz.open(pdf_path) as document:
        if document.page_count != 1:
            raise ValueError(f"sample_must_be_single_page:{pdf_path.name}:{document.page_count}")
        page = document[0]
        width = max(float(page.rect.width), 1.0)
        height = max(float(page.rect.height), 1.0)
        page_area = width * height
        raw = page.get_text("dict", flags=fitz.TEXTFLAGS_DICT & ~fitz.TEXT_PRESERVE_IMAGES)
        blocks: list[dict[str, Any]] = []
        font_sizes: list[float] = []
        text_parts: list[str] = []
        layout_spans: list[dict[str, Any]] = []
        line_count = 0
        text_area = 0.0
        evidence_ids = {"IMG1", "PAGE1", "TEXT1", "IMAGE1", "DRAWING1", "TABLE1"}
        for index, block in enumerate(raw.get("blocks", []), 1):
            if block.get("type") != 0:
                continue
            lines: list[str] = []
            for line in block.get("lines", []):
                parts: list[str] = []
                for span in line.get("spans", []):
                    text = str(span.get("text", ""))
                    parts.append(text)
                    if text.strip():
                        font_sizes.append(float(span.get("size", 0)))
                        layout_spans.append(
                            {
                                "block_id": f"B{index:03d}",
                                "bbox": [round(float(value), 2) for value in span.get("bbox", (0, 0, 0, 0))],
                                "text": text.strip(),
                            }
                        )
                joined = "".join(parts).strip()
                if joined:
                    lines.append(joined)
            text = "\n".join(lines)
            if not text:
                continue
            bbox = tuple(round(float(value), 2) for value in block.get("bbox", (0, 0, 0, 0)))
            block_id = f"B{index:03d}"
            evidence_ids.add(block_id)
            area = max(0.0, (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]))
            text_area += area
            line_count += len(lines)
            text_parts.append(text)
            blocks.append(
                {
                    "block_id": block_id,
                    "bbox": list(bbox),
                    "line_count": len(lines),
                    "char_count": len(text),
                    "text": text,
                }
            )

        table_rects: list[tuple[float, float, float, float]] = []
        try:
            table_rects = [tuple(float(value) for value in table.bbox) for table in page.find_tables().tables]
        except Exception:
            table_rects = []
        table_area_ratio = min(
            sum(max(0.0, (rect[2] - rect[0]) * (rect[3] - rect[1])) for rect in table_rects) / page_area,
            1.0,
        )
        borderless_table = _detect_borderless_table(layout_spans, blocks, width, height)
        evidence_ids.add("BTABLE1")
        outside_table_chars = 0
        for block in blocks:
            x0, y0, x1, y1 = (float(value) for value in block["bbox"])
            center = ((x0 + x1) / 2, (y0 + y1) / 2)
            if not any(_inside(rect, center) for rect in table_rects):
                outside_table_chars += int(block["char_count"])

        images = page.get_image_info(hashes=False, xrefs=False)
        image_area = 0.0
        for image in images:
            x0, y0, x1, y1 = (float(value) for value in image.get("bbox", (0, 0, 0, 0)))
            image_area += max(0.0, (x1 - x0) * (y1 - y0))
        try:
            drawing_count = len(page.get_drawings())
        except Exception:
            drawing_count = 0

        render_path.parent.mkdir(parents=True, exist_ok=True)
        page.get_pixmap(matrix=fitz.Matrix(1.0, 1.0), alpha=False).save(render_path)
        text = "\n".join(text_parts)
        original_page = source_meta.get("source_page_number")
        original_count = source_meta.get("source_page_count")
        known_position = isinstance(original_page, int) and isinstance(original_count, int) and original_count > 0
        position = {
            "source_page_number": original_page,
            "source_page_count": original_count,
            "is_first": bool(known_position and original_page == 1),
            "is_last": bool(known_position and original_page == original_count),
            "relative": round(original_page / original_count, 5) if known_position else None,
        }
        return {
            "sample_id": source_meta["sample_id"],
            "page_image": {"evidence_id": "IMG1", "sha256": sha256_file(render_path)},
            "page": {
                "evidence_id": "PAGE1",
                "width": round(width, 2),
                "height": round(height, 2),
                "orientation": "landscape" if width > height else "portrait",
                "rotation": page.rotation,
                "position": position,
            },
            "text": {
                "evidence_id": "TEXT1",
                "native_char_count": len(text),
                "line_count": line_count,
                "block_count": len(blocks),
                "text_area_ratio": round(min(text_area / page_area, 1.0), 5),
                "median_font_size": round(statistics.median(font_sizes), 2) if font_sizes else None,
                "max_font_size": round(max(font_sizes), 2) if font_sizes else None,
                "outside_table_chars": outside_table_chars,
                "editable_text_scope_hint": "native" if text.strip() else "image_only_or_none",
            },
            "images": {
                "evidence_id": "IMAGE1",
                "count": len(images),
                "area_ratio": round(min(image_area / page_area, 1.0), 5),
                "classification_only": True,
            },
            "drawings": {"evidence_id": "DRAWING1", "count": drawing_count},
            "tables": {
                "evidence_id": "TABLE1",
                "count": len(table_rects),
                "area_ratio": round(table_area_ratio, 5),
                "bboxes": [[round(value, 2) for value in rect] for rect in table_rects],
            },
            "borderless_table": borderless_table,
            "blocks": blocks,
            "native_text": text,
            "evidence_ids": sorted(evidence_ids),
        }


def compact_evidence(evidence: dict[str, Any], max_text_chars: int = 12000) -> dict[str, Any]:
    result = {
        "page_image_ref": "IMG1",
        "page": evidence["page"],
        "text": evidence["text"],
        "images": evidence["images"],
        "drawings": evidence["drawings"],
        "tables": evidence["tables"],
        "borderless_table": evidence["borderless_table"],
        "blocks": [],
        "evidence_ids": evidence["evidence_ids"],
    }
    used = 0
    for block in evidence["blocks"]:
        item = dict(block)
        remaining = max_text_chars - used
        text = str(item["text"])
        if remaining <= 0:
            item["text"] = ""
            item["text_truncated"] = True
        elif len(text) > remaining:
            item["text"] = text[:remaining]
            item["text_truncated"] = True
            used = max_text_chars
        else:
            used += len(text)
        result["blocks"].append(item)
    return result
