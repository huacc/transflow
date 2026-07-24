"""Deterministic pre-render quality checks for body.chart placements."""

from __future__ import annotations

import re

from transflow.domain.toolbox import Finding
from transflow.pdf_kernel.facts import ExtractedPageFacts
from transflow.toolboxes.leaves.body_chart.models import (
    ChartLayoutPlan,
    ChartTemplate,
    Rect,
)


def judge_chart_plan(
    plan_id: str,
    template: ChartTemplate,
    layout: ChartLayoutPlan,
    facts: ExtractedPageFacts,
    target_language: str,
) -> tuple[Finding, ...]:
    """Check identity, finite slots, protected visuals, and legend association."""

    findings: list[Finding] = []
    if (
        layout.page_id != template.page_identity
        or layout.toolbox_key != "body.chart"
        or layout.structure_sha256 != template.structure_hash
    ):
        findings.append(
            Finding(
                f"{plan_id}-structure",
                "CHART_STRUCTURE_SIGNATURE_MISMATCH",
                "HARD",
                (template.structure_hash, layout.structure_sha256),
            )
        )
        return tuple(findings)

    containers = {item.container_id: item for item in template.containers}
    expected = [
        item.container_id
        for item in template.containers
        if item.container_id in {placement.container_id for placement in layout.placements}
    ]
    actual = [item.container_id for item in layout.placements]
    if actual != expected or len(actual) != len(set(actual)):
        findings.append(
            Finding(
                f"{plan_id}-placement-order",
                "CHART_PLACEMENT_ORDER_MISMATCH",
                "HARD",
                tuple(actual),
            )
        )
        return tuple(findings)

    visual_by_id = {
        item.object_id: item.bbox
        for item in (*facts.image_objects, *facts.drawing_objects)
    }
    page_bbox = facts.crop_box
    for placement in layout.placements:
        container = containers[placement.container_id]
        evidence = (container.container_id, container.association_id)
        if not placement.fit:
            findings.append(
                Finding(
                    f"{plan_id}-{container.container_id}-overflow",
                    "CHART_TEXT_SLOT_OVERFLOW",
                    "HARD",
                    evidence,
                )
            )
            continue
        if not _contains(page_bbox, placement.output_bbox) or not _contains(
            container.allowed_bbox,
            placement.output_bbox,
        ):
            findings.append(
                Finding(
                    f"{plan_id}-{container.container_id}-outside-slot",
                    "CHART_WRITE_OUTSIDE_ALLOWED_REGION",
                    "HARD",
                    evidence,
                )
            )
        if any(
            not required_literal_preserved(
                literal,
                placement.translated_text,
                target_language,
            )
            for literal in container.required_literals
        ):
            findings.append(
                Finding(
                    f"{plan_id}-{container.container_id}-literal",
                    "TRANSLATION_REQUIRED_LITERAL_MISSING",
                    "HARD",
                    evidence,
                )
            )
        source_visual_ids = {
            object_id
            for object_id, bbox in visual_by_id.items()
            if _intersection_area(container.source_bbox, bbox) > 0.01
        } | set(container.anchor_object_ids)
        for object_id, bbox in visual_by_id.items():
            if object_id in source_visual_ids:
                continue
            if _intersection_area(placement.output_bbox, bbox) <= 0.05:
                continue
            # The layout probe guarantees glyphs remain inside this slot, but the
            # slot itself may include whitespace around a nearby visual. Only a
            # fully occupied visual intersection is a deterministic pre-render
            # failure; exact glyph collision is rechecked after PDF materialization.
            if _intersection_area(placement.output_bbox, bbox) >= min(
                _area(placement.output_bbox),
                _area(bbox),
            ) * 0.8:
                findings.append(
                    Finding(
                        f"{plan_id}-{container.container_id}-visual-{object_id}",
                        (
                            "CHART_IMAGE_TEXT_OVERLAID"
                            if object_id.startswith("image-")
                            else "CHART_TEXT_GRAPHIC_COLLISION"
                        ),
                        "HARD",
                        (*evidence, object_id),
                    )
                )
                break
        if container.role == "LEGEND_LABEL" and container.anchor_object_ids:
            anchor = visual_by_id.get(container.anchor_object_ids[-1])
            if anchor is not None:
                source_relation = _anchor_relation(container.source_bbox, anchor)
                output_relation = _anchor_relation(placement.output_bbox, anchor)
                if (
                    source_relation not in {"OVERLAY", output_relation}
                    and output_relation != "OVERLAY"
                ):
                    findings.append(
                        Finding(
                            f"{plan_id}-{container.container_id}-legend",
                            "CHART_LEGEND_ASSOCIATION_CHANGED",
                            "HARD",
                            (*evidence, container.anchor_object_ids[-1]),
                        )
                    )
    return tuple(findings)


def required_literal_preserved(
    literal: str,
    translated: str,
    target_language: str,
) -> bool:
    if literal in translated:
        return True
    year = re.fullmatch(r"Y((?:19|20)\d{2})", literal)
    return bool(
        year
        and target_language.casefold().startswith("zh")
        and re.search(
            rf"(?<!\d){re.escape(year.group(1))}\s*\u5e74",
            translated,
        )
    )


def _contains(outer: Rect, inner: Rect, tolerance: float = 0.05) -> bool:
    return (
        inner[0] >= outer[0] - tolerance
        and inner[1] >= outer[1] - tolerance
        and inner[2] <= outer[2] + tolerance
        and inner[3] <= outer[3] + tolerance
    )


def _intersection_area(left: Rect, right: Rect) -> float:
    return max(0.0, min(left[2], right[2]) - max(left[0], right[0])) * max(
        0.0, min(left[3], right[3]) - max(left[1], right[1])
    )


def _area(rect: Rect) -> float:
    return max(0.0, rect[2] - rect[0]) * max(0.0, rect[3] - rect[1])


def _anchor_relation(source: Rect, anchor: Rect) -> str:
    if _intersection_area(source, anchor) > 0.01:
        return "OVERLAY"
    if source[2] <= anchor[0]:
        return "LEFT_OF"
    if source[0] >= anchor[2]:
        return "RIGHT_OF"
    return "ABOVE" if source[3] <= anchor[1] else "BELOW"
