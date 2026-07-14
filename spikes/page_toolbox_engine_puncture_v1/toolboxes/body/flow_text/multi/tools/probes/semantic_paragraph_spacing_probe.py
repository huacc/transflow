"""
tool_name: semantic_paragraph_spacing_probe
category: probes
input_contract: current PageFacts, repaired template/layout plan, and rendered candidate PDF
output_contract: per-column adjacent body-paragraph source/candidate line-rhythm and visible-glyph-gap evidence
failure_signals: a planned container has no extractable rendered line
fallback: fail the candidate capability/quality path; do not infer spacing from textbox edges
anti_overfit_statement: every line, ratio and overlap comes from the current source/candidate PDFs and runtime container ownership
"""

from __future__ import annotations

from pathlib import Path
from statistics import median

import fitz

from page_toolbox_puncture.contracts import PageFacts

from ..models import MultiColumnLayoutPlan, MultiColumnTemplate
from .structural_anchor_probe import structural_zone


def probe_semantic_paragraph_transitions(
    *,
    candidate_pdf: Path,
    facts: PageFacts,
    template: MultiColumnTemplate,
    plan: MultiColumnLayoutPlan,
) -> tuple[dict[str, object], ...]:
    """逐栏测量段落行节奏；实际字形框优先于计划 textbox。"""

    by_id = {item.container_id: item for item in template.containers}
    assignment = {item.container_id: item.column_id for item in template.assignments}
    placement_by_id = {item.container_id: item for item in plan.placements}
    source_line_tops = {
        item.container_id: _source_line_tops(item.container_id, template, facts)
        for item in template.containers
    }
    with fitz.open(candidate_pdf) as document:
        candidate_lines = _page_lines(document[facts.page_index])

    rows: list[dict[str, object]] = []
    content_bands = [item for item in plan.flow_bands if item.mode in {"single", "multi"}]
    column_groups: list[tuple[str, object, tuple[str, ...], float]] = []
    if content_bands:
        for index, band in enumerate(content_bands):
            if band.mode != "multi":
                continue
            following_top = (
                min(
                    placement_by_id[container_id].output_bbox[1]
                    for container_id in content_bands[index + 1].container_ids
                )
                if index + 1 < len(content_bands)
                else None
            )
            for column in template.columns:
                ids = tuple(
                    container_id
                    for container_id in band.container_ids
                    if assignment[container_id] == column.column_id
                )
                if not ids:
                    continue
                band_bottom = following_top or max(
                    column.content_bottom,
                    max(placement_by_id[container_id].output_bbox[3] for container_id in ids),
                )
                column_groups.append((band.band_id, column, ids, band_bottom))
    else:
        for column in template.columns:
            ids = tuple(
                item.container_id
                for item in template.assignments
                if item.column_id == column.column_id
            )
            column_groups.append(("legacy-column-flow", column, ids, column.content_bottom))

    for band_id, column, ids, band_bottom in column_groups:
        placements = [placement_by_id[container_id] for container_id in ids]
        rendered = _assign_candidate_lines(
            candidate_lines=candidate_lines,
            placements=placements,
            column_bottom=band_bottom,
        )
        source_step = _line_step(tuple(source_line_tops[container_id] for container_id in ids))
        candidate_step = _line_step(
            tuple(
                tuple(float(line["bbox"][1]) for line in rendered[container_id])
                for container_id in ids
            )
        )
        for previous_id, next_id in zip(ids, ids[1:]):
            previous = by_id[previous_id]
            current = by_id[next_id]
            if previous.role != "body" or current.role != "body":
                continue
            previous_source = source_line_tops[previous_id]
            current_source = source_line_tops[next_id]
            previous_candidate = rendered[previous_id]
            current_candidate = rendered[next_id]
            if source_step is None or candidate_step is None or not previous_source or not current_source:
                continue
            source_transition = current_source[0] - previous_source[-1]
            candidate_transition = float(current_candidate[0]["bbox"][1]) - float(previous_candidate[-1]["bbox"][1])
            visible_gap = float(current_candidate[0]["bbox"][1]) - float(previous_candidate[-1]["bbox"][3])
            rows.append(
                {
                    "column_id": column.column_id,
                    "flow_band_id": band_id,
                    "previous_container_id": previous_id,
                    "next_container_id": next_id,
                    "source_line_step_pt": round(source_step, 4),
                    "candidate_line_step_pt": round(candidate_step, 4),
                    "source_transition_ratio": round(source_transition / source_step, 4),
                    "candidate_transition_ratio": round(candidate_transition / candidate_step, 4),
                    "candidate_visible_gap_pt": round(visible_gap, 4),
                    "candidate_visible_overlap_pt": round(max(0.0, -visible_gap), 4),
                    "candidate_typographic_scale_pt": round(
                        max(placement_by_id[previous_id].font_size, placement_by_id[next_id].font_size),
                        4,
                    ),
                    "evidence": {
                        "previous_last_line_bbox": previous_candidate[-1]["bbox"],
                        "next_first_line_bbox": current_candidate[0]["bbox"],
                        "measurement_basis": "rendered_glyph_lines",
                    },
                }
            )
    span_groups: list[tuple[str, tuple[str, ...], float]] = []
    if content_bands:
        for index, band in enumerate(content_bands):
            if band.mode != "single":
                continue
            if index + 1 < len(content_bands):
                next_band = content_bands[index + 1]
                band_bottom = min(
                    placement_by_id[container_id].output_bbox[1]
                    for container_id in next_band.container_ids
                )
            else:
                current_bottom = max(
                    placement_by_id[container_id].output_bbox[3]
                    for container_id in band.container_ids
                )
                margin_tops = [
                    placement.output_bbox[1]
                    for container_id, placement in placement_by_id.items()
                    if assignment[container_id] == "margin"
                    and placement.output_bbox[1] >= current_bottom - 0.01
                ]
                band_bottom = min(margin_tops, default=template.height)
            span_groups.append((band.band_id, band.container_ids, band_bottom))
    else:
        top_span_ids = tuple(
            item.container_id for item in template.containers
            if assignment[item.container_id] == "span"
            and item.source_bbox[1] <= min(column.content_top for column in template.columns) + template.height * 0.04
        )
        first_column_top = min(
            placement_by_id[item.container_id].output_bbox[1]
            for item in template.containers
            if assignment[item.container_id].startswith("column-")
        )
        span_groups.append(("legacy-page-prelude", top_span_ids, first_column_top))

    for band_id, span_ids, band_bottom in span_groups:
        if len(span_ids) < 2:
            continue
        span_placements = [placement_by_id[container_id] for container_id in span_ids]
        rendered = _assign_candidate_lines(
            candidate_lines=candidate_lines,
            placements=span_placements,
            column_bottom=band_bottom,
        )
        for previous_id, next_id in zip(span_ids, span_ids[1:]):
            previous = by_id[previous_id]
            current = by_id[next_id]
            if plan.structural_anchors:
                previous_zone = structural_zone(previous.source_bbox, plan.structural_anchors, template.height)[0]
                current_zone = structural_zone(current.source_bbox, plan.structural_anchors, template.height)[0]
                if previous_zone != current_zone:
                    continue
            previous_candidate = rendered[previous_id]
            current_candidate = rendered[next_id]
            source_scale = max(previous.font_size, current.font_size, 0.01)
            candidate_scale = max(placement_by_id[previous_id].font_size, placement_by_id[next_id].font_size, 0.01)
            source_gap = max(0.0, current.source_bbox[1] - previous.source_bbox[3])
            visible_gap = float(current_candidate[0]["bbox"][1]) - float(previous_candidate[-1]["bbox"][3])
            rows.append(
                {
                    "column_id": "span",
                    "flow_band_id": band_id,
                    "previous_container_id": previous_id,
                    "next_container_id": next_id,
                    "source_line_step_pt": round(source_scale, 4),
                    "candidate_line_step_pt": round(candidate_scale, 4),
                    "source_transition_ratio": round(source_gap / source_scale, 4),
                    "candidate_transition_ratio": round(max(0.0, visible_gap) / candidate_scale, 4),
                    "candidate_visible_gap_pt": round(visible_gap, 4),
                    "candidate_visible_overlap_pt": round(max(0.0, -visible_gap), 4),
                    "candidate_typographic_scale_pt": round(candidate_scale, 4),
                    "evidence": {
                        "previous_last_line_bbox": previous_candidate[-1]["bbox"],
                        "next_first_line_bbox": current_candidate[0]["bbox"],
                        "measurement_basis": "rendered_single_band_glyph_gap_over_typographic_scale",
                    },
                }
            )
    return tuple(rows)


def _source_line_tops(
    container_id: str,
    template: MultiColumnTemplate,
    facts: PageFacts,
) -> tuple[float, ...]:
    container = next(item for item in template.containers if item.container_id == container_id)
    source_by_id = {item.object_id: item for item in facts.text_objects}
    lines: dict[tuple[int, int], list[float]] = {}
    for object_id in container.source_object_ids:
        source = source_by_id.get(object_id)
        if source is None:
            continue
        lines.setdefault((source.block_index, source.line_index), []).append(source.bbox[1])
    return tuple(min(values) for _, values in sorted(lines.items(), key=lambda item: min(item[1])))


def _page_lines(page: fitz.Page) -> tuple[dict[str, object], ...]:
    rows: list[dict[str, object]] = []
    for block in page.get_text("dict").get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            text = "".join(str(span.get("text") or "") for span in line.get("spans", [])).strip()
            if not text:
                continue
            rows.append(
                {
                    "bbox": tuple(round(float(value), 4) for value in line["bbox"]),
                    "text": text,
                }
            )
    return tuple(rows)


def _assign_candidate_lines(
    *,
    candidate_lines: tuple[dict[str, object], ...],
    placements: list,
    column_bottom: float,
) -> dict[str, tuple[dict[str, object], ...]]:
    output: dict[str, tuple[dict[str, object], ...]] = {}
    for index, placement in enumerate(placements):
        tolerance = placement.font_size * 0.05
        lower = placement.output_bbox[1] - tolerance
        upper = (
            placements[index + 1].output_bbox[1] - placements[index + 1].font_size * 0.05
            if index + 1 < len(placements)
            else column_bottom
        )
        x0, _, x1, _ = placement.output_bbox
        matches = []
        for line in candidate_lines:
            lx0, ly0, lx1, _ = line["bbox"]
            line_width = max(lx1 - lx0, 0.01)
            horizontal_overlap = max(0.0, min(x1, lx1) - max(x0, lx0))
            if lower <= ly0 < upper and horizontal_overlap / line_width >= 0.80:
                matches.append(line)
        if not matches:
            raise ValueError(f"rendered_container_has_no_extractable_line:{placement.container_id}")
        output[placement.container_id] = tuple(sorted(matches, key=lambda item: (item["bbox"][1], item["bbox"][0])))
    return output


def _line_step(groups: tuple[tuple[float, ...], ...]) -> float | None:
    steps = [
        current - previous
        for values in groups
        for previous, current in zip(values, values[1:])
        if current > previous
    ]
    return median(steps) if steps else None
