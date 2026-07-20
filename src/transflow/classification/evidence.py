"""把统一 PageFacts 包装为分类规则与模型共用的匿名 typed evidence。"""

from __future__ import annotations

import base64
import itertools
import logging
import statistics
from typing import Any

from transflow.pdf_kernel.facts import ExtractedPageFacts

LOGGER = logging.getLogger("transflow.classification.evidence")


def _cluster_positions(
    values: list[tuple[float, int]],
    tolerance: float,
) -> list[dict[str, Any]]:
    """按横坐标容差聚类 span 起点，保留命中的行序号集合。"""

    clusters: list[list[tuple[float, int]]] = []
    for value, row_index in sorted(values):
        cluster = next(
            (
                items
                for items in clusters
                if abs(value - statistics.median(item[0] for item in items)) <= tolerance
            ),
            None,
        )
        if cluster is None:
            clusters.append([(value, row_index)])
        else:
            cluster.append((value, row_index))
    return [
        {"rows": {item[1] for item in items}, "x": statistics.median(item[0] for item in items)}
        for items in clusters
    ]


def _inside(rect: tuple[float, float, float, float], point: tuple[float, float]) -> bool:
    """判断一个文字块中心点是否位于表格矩形内部。"""

    x0, y0, x1, y1 = rect
    return x0 <= point[0] <= x1 and y0 <= point[1] <= y1


def _empty_borderless(text_blocks: list[dict[str, Any]]) -> dict[str, Any]:
    """构造没有可靠无边框表格时的完整证据对象。"""

    return {
        "aligned_row_count": 0,
        "area_ratio": 0.0,
        "bbox": None,
        "column_anchors": [],
        "confidence": 0.0,
        "evidence_id": "BTABLE1",
        "outside_chars": sum(int(block["char_count"]) for block in text_blocks),
    }


def _detect_borderless_table(
    layout_spans: list[dict[str, Any]],
    text_blocks: list[dict[str, Any]],
    page_width: float,
    page_height: float,
) -> dict[str, Any]:
    """按重复行、稳定列锚点和同行配对识别无边框表格。"""

    if not layout_spans:
        return _empty_borderless(text_blocks)
    median_height = statistics.median(
        float(span["bbox"][3]) - float(span["bbox"][1]) for span in layout_spans
    )
    y_tolerance = max(1.5, median_height * 0.22)
    rows: list[list[dict[str, Any]]] = []
    for span in sorted(
        layout_spans,
        key=lambda item: (
            (float(item["bbox"][1]) + float(item["bbox"][3])) / 2,
            float(item["bbox"][0]),
        ),
    ):
        center_y = (float(span["bbox"][1]) + float(span["bbox"][3])) / 2
        matched_row = next(
            (
                items
                for items in rows
                if abs(
                    center_y
                    - statistics.median(
                        (float(item["bbox"][1]) + float(item["bbox"][3])) / 2
                        for item in items
                    )
                )
                <= y_tolerance
            ),
            None,
        )
        if matched_row is None:
            rows.append([span])
        else:
            matched_row.append(span)

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
                        "block_ids": {str(span["block_id"])},
                        "text": str(span["text"]),
                    }
                )
        if (
            len(cells) < 2
            or float(cells[-1]["bbox"][0]) - float(cells[0]["bbox"][0])
            < minimum_column_gap
        ):
            continue
        candidate_rows.append(
            {
                "cells": cells,
                "center_y": statistics.median(
                    (float(cell["bbox"][1]) + float(cell["bbox"][3])) / 2 for cell in cells
                ),
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
        return _empty_borderless(text_blocks)

    left_anchor, right_anchor = max(
        pairs,
        key=lambda pair: (
            len(pair[0]["rows"] & pair[1]["rows"]),
            min(len(pair[0]["rows"]), len(pair[1]["rows"])),
            float(pair[1]["x"]) - float(pair[0]["x"]),
        ),
    )
    aligned_rows: list[dict[str, Any]] = []
    same_block_count = 0
    for candidate_row in candidate_rows:
        left_cells = [
            cell
            for cell in candidate_row["cells"]
            if abs(float(cell["bbox"][0]) - float(left_anchor["x"])) <= anchor_tolerance
        ]
        right_cells = [
            cell
            for cell in candidate_row["cells"]
            if abs(float(cell["bbox"][0]) - float(right_anchor["x"])) <= anchor_tolerance
        ]
        if not left_cells or not right_cells:
            continue
        aligned_rows.append(candidate_row)
        if any(
            left["block_ids"] & right["block_ids"]
            for left in left_cells
            for right in right_cells
        ):
            same_block_count += 1

    aligned_count = len(aligned_rows)
    if aligned_count < 4:
        confidence = 0.0
    else:
        ordered_rows = sorted(aligned_rows, key=lambda item: item["center_y"])
        row_gaps = [
            right["center_y"] - left["center_y"]
            for left, right in itertools.pairwise(ordered_rows)
        ]
        median_gap = statistics.median(row_gaps) if row_gaps else 0.0
        short_left_ratio = sum(
            min(
                len(str(cell["text"]))
                for cell in row["cells"]
                if abs(float(cell["bbox"][0]) - float(left_anchor["x"])) <= anchor_tolerance
            )
            <= 40
            for row in aligned_rows
        ) / aligned_count
        cross_block_pattern = median_gap >= median_height * 1.8 and short_left_ratio >= 0.75
        alignment_ratio = aligned_count / max(len(candidate_rows), 1)
        confidence = 0.82 + min(0.08, max(0, aligned_count - 3) * 0.02)
        if alignment_ratio >= 0.75:
            confidence += 0.04
        if same_block_count >= 4 or cross_block_pattern:
            confidence += 0.06
        confidence = min(round(confidence, 2), 0.99)
        if same_block_count < 4 and not cross_block_pattern:
            confidence = min(confidence, 0.89)

    if confidence < 0.9:
        return {
            "aligned_row_count": aligned_count,
            "area_ratio": 0.0,
            "bbox": None,
            "column_anchors": [
                round(float(left_anchor["x"]), 2),
                round(float(right_anchor["x"]), 2),
            ],
            "confidence": confidence,
            "evidence_id": "BTABLE1",
            "outside_chars": sum(int(block["char_count"]) for block in text_blocks),
        }

    all_cells = [cell for row in aligned_rows for cell in row["cells"]]
    bbox = [
        min(float(cell["bbox"][0]) for cell in all_cells),
        min(float(cell["bbox"][1]) for cell in all_cells),
        max(float(cell["bbox"][2]) for cell in all_cells),
        max(float(cell["bbox"][3]) for cell in all_cells),
    ]
    area_ratio = max(0.0, (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])) / max(
        page_width * page_height,
        1.0,
    )
    outside_chars = 0
    for block in text_blocks:
        x0, y0, x1, y1 = (float(value) for value in block["bbox"])
        borderless_rect = (bbox[0], bbox[1], bbox[2], bbox[3])
        if not _inside(borderless_rect, ((x0 + x1) / 2, (y0 + y1) / 2)):
            outside_chars += int(block["char_count"])
    return {
        "aligned_row_count": aligned_count,
        "area_ratio": round(min(area_ratio, 1.0), 5),
        "bbox": [round(value, 2) for value in bbox],
        "column_anchors": [round(float(left_anchor["x"]), 2), round(float(right_anchor["x"]), 2)],
        "confidence": confidence,
        "evidence_id": "BTABLE1",
        "outside_chars": outside_chars,
    }


def build_evidence(
    extracted: ExtractedPageFacts,
    page_count: int,
) -> dict[str, Any]:
    """由一次性提取的页面事实生成不含文件身份和答案的分类证据。"""

    classification = extracted.classification
    if classification is None:
        raise ValueError("页面缺少分类事实，请由协调器启用分类提取")
    LOGGER.info("调用分类证据构建，意图=生成匿名 typed evidence page_no=%s", extracted.page.page_no)
    width = max(float(extracted.page.width_points), 1.0)
    height = max(float(extracted.page.height_points), 1.0)
    page_area = width * height
    blocks: list[dict[str, Any]] = [
        {
            "bbox": [round(value, 2) for value in block.bbox],
            "block_id": block.block_id,
            "char_count": len(block.text),
            "line_count": block.line_count,
            "text": block.text,
        }
        for block in classification.text_blocks
    ]
    layout_spans: list[dict[str, Any]] = [
        {"bbox": list(span.bbox), "block_id": span.block_id, "text": span.text}
        for span in classification.layout_spans
    ]
    table_rects = classification.table_bboxes
    table_area_ratio = min(
        sum(max(0.0, (rect[2] - rect[0]) * (rect[3] - rect[1])) for rect in table_rects)
        / page_area,
        1.0,
    )
    outside_table_chars = 0
    for block in blocks:
        x0, y0, x1, y1 = (float(value) for value in block["bbox"])
        if not any(_inside(rect, ((x0 + x1) / 2, (y0 + y1) / 2)) for rect in table_rects):
            outside_table_chars += int(block["char_count"])
    image_area: float = sum(
        max(0.0, (rect[2] - rect[0]) * (rect[3] - rect[1]))
        for rect in classification.image_bboxes
    )
    text_area: float = sum(
        max(
            0.0,
            (float(block["bbox"][2]) - float(block["bbox"][0]))
            * (float(block["bbox"][3]) - float(block["bbox"][1])),
        )
        for block in blocks
    )
    native_text = "\n".join(str(block["text"]) for block in blocks)
    evidence_ids = {
        "BTABLE1",
        "DRAWING1",
        "IMAGE1",
        "IMG1",
        "PAGE1",
        "TABLE1",
        "TEXT1",
        *(str(block["block_id"]) for block in blocks),
    }
    return {
        "blocks": blocks,
        "borderless_table": _detect_borderless_table(layout_spans, blocks, width, height),
        "drawings": {"count": classification.drawing_count, "evidence_id": "DRAWING1"},
        "evidence_ids": sorted(evidence_ids),
        "images": {
            "area_ratio": round(min(image_area / page_area, 1.0), 5),
            "classification_only": True,
            "count": len(classification.image_bboxes),
            "evidence_id": "IMAGE1",
        },
        "native_text": native_text,
        "page": {
            "evidence_id": "PAGE1",
            "height": round(height, 2),
            "orientation": "landscape" if width > height else "portrait",
            "position": {
                "is_first": extracted.page.page_no == 1,
                "is_last": extracted.page.page_no == page_count,
            },
            "rotation": extracted.rotation,
            "width": round(width, 2),
        },
        "page_image": {
            "bytes": classification.page_image_png,
            "evidence_id": "IMG1",
            "sha256": classification.page_image_sha256,
        },
        "tables": {
            "area_ratio": round(table_area_ratio, 5),
            "bboxes": [[round(value, 2) for value in rect] for rect in table_rects],
            "count": len(table_rects),
            "evidence_id": "TABLE1",
        },
        "text": {
            "block_count": len(blocks),
            "editable_text_scope_hint": "native" if native_text.strip() else "image_only_or_none",
            "evidence_id": "TEXT1",
            "line_count": sum(int(block["line_count"]) for block in blocks),
            "max_font_size": max(classification.font_sizes) if classification.font_sizes else None,
            "median_font_size": (
                round(statistics.median(classification.font_sizes), 2)
                if classification.font_sizes
                else None
            ),
            "native_char_count": len(native_text),
            "outside_table_chars": outside_table_chars,
            "text_area_ratio": round(min(text_area / page_area, 1.0), 5),
        },
    }


def compact_evidence(evidence: dict[str, Any], max_text_chars: int = 12000) -> dict[str, Any]:
    """裁剪文字并以内联匿名图片构造模型调用的有界 typed evidence。"""

    page_image = evidence["page_image"]
    image_bytes = page_image["bytes"]
    result = {
        "blocks": [],
        "borderless_table": evidence["borderless_table"],
        "drawings": evidence["drawings"],
        "evidence_ids": evidence["evidence_ids"],
        "images": evidence["images"],
        "page": evidence["page"],
        "page_image": {
            "data_url": "data:image/png;base64," + base64.b64encode(image_bytes).decode("ascii"),
            "evidence_id": page_image["evidence_id"],
            "sha256": page_image["sha256"],
        },
        "tables": evidence["tables"],
        "text": evidence["text"],
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


def main() -> int:
    """记录图片证据只归分类所有，不产生任何翻译单元。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    LOGGER.info("分类证据示例，意图=说明图片仅用于分类且翻译单元数量为零")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
