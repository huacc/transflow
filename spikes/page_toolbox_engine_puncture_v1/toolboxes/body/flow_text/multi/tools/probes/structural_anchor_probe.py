"""
tool_name: structural_anchor_probe
category: probes
input_contract: one current source PDF page
output_contract: immutable long horizontal structural anchors detected from the rendered source page
failure_signals: source page cannot be rendered
fallback: return no raster anchors; native drawing/image constraints still apply
anti_overfit_statement: detection uses current-page pixel continuity and page-relative geometry only; no filename, page number, literal text or known coordinate is encoded
"""

from __future__ import annotations

from pathlib import Path

import fitz
import numpy as np

from ..models import StructuralAnchor


def probe_horizontal_structural_anchors(
    source_pdf: Path,
    *,
    page_index: int = 0,
    render_scale: float = 2.0,
) -> tuple[StructuralAnchor, ...]:
    """从背景图或矢量渲染结果中提取长而薄的水平分隔线。"""

    with fitz.open(source_pdf) as document:
        page = document[page_index]
        pixmap = page.get_pixmap(matrix=fitz.Matrix(render_scale, render_scale), alpha=False)
        page_width = float(page.rect.width)
        page_height = float(page.rect.height)
    image = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(
        pixmap.height,
        pixmap.width,
        pixmap.n,
    )[:, :, :3]
    vertical_edge = np.max(
        np.abs(image[1:].astype(np.int16) - image[:-1].astype(np.int16)),
        axis=2,
    )
    edge_mask = vertical_edge > 28
    row_candidates: list[tuple[int, int, int, float]] = []
    for row_index, row in enumerate(edge_mask, start=1):
        positions = np.flatnonzero(row)
        if not len(positions):
            continue
        runs = np.split(positions, np.where(np.diff(positions) > 1)[0] + 1)
        run = max(runs, key=len)
        if len(run) < pixmap.width * 0.42:
            continue
        contrast = float(vertical_edge[row_index - 1, run].mean())
        row_candidates.append((row_index, int(run[0]), int(run[-1]), contrast))

    groups: list[list[tuple[int, int, int, float]]] = []
    grouping_gap = max(2, round(pixmap.height * 0.004))
    for row in row_candidates:
        if groups and row[0] - groups[-1][-1][0] <= grouping_gap:
            groups[-1].append(row)
        else:
            groups.append([row])

    anchors: list[StructuralAnchor] = []
    for group in groups:
        y0 = min(item[0] for item in group) / render_scale
        y1 = (max(item[0] for item in group) + 1) / render_scale
        if y0 <= page_height * 0.02 or y1 >= page_height * 0.98:
            continue
        if y1 - y0 > page_height * 0.01:
            continue
        x0 = min(item[1] for item in group) / render_scale
        x1 = (max(item[2] for item in group) + 1) / render_scale
        anchors.append(
            StructuralAnchor(
                anchor_id=f"horizontal-rule-{len(anchors) + 1:03d}",
                anchor_kind="horizontal_rule",
                bbox=(round(x0, 4), round(y0, 4), round(x1, 4), round(y1, 4)),
                source="source_page_raster_continuity",
            )
        )
    return tuple(anchors)


def structural_zone(
    bbox: tuple[float, float, float, float],
    anchors: tuple[StructuralAnchor, ...],
    page_height: float,
) -> tuple[int, float, float]:
    """返回文本源中心所属的结构带编号和上下边界。"""

    center = (bbox[1] + bbox[3]) / 2.0
    boundaries = sorted((anchor.bbox[1] + anchor.bbox[3]) / 2.0 for anchor in anchors)
    zone_index = sum(center > value for value in boundaries)
    lower = 0.0 if zone_index == 0 else boundaries[zone_index - 1]
    upper = page_height if zone_index == len(boundaries) else boundaries[zone_index]
    return zone_index, lower, upper
