"""Deterministic pre-render structure checks for body.diagram placements."""

from __future__ import annotations

from transflow.domain.toolbox import Finding
from transflow.toolboxes.leaves.body_diagram.layout import (
    local_flow_chains,
    paragraph_typography_cohorts,
    segment_hits_rect,
)
from transflow.toolboxes.leaves.body_diagram.models import (
    DiagramLayoutPlan,
    DiagramTemplate,
    Rect,
)
from transflow.toolboxes.leaves.body_diagram.template import (
    is_coordinate_locked_container,
)


def judge_diagram_plan(
    plan_id: str,
    template: DiagramTemplate,
    layout: DiagramLayoutPlan,
) -> tuple[Finding, ...]:
    """Reject owner drift, unsafe geometry expansion, and new connector collisions."""

    findings: list[Finding] = []
    if (
        layout.page_id != template.page_id
        or layout.toolbox_key != template.toolbox_key
        or layout.topology_sha256 != template.topology_sha256
    ):
        findings.append(
            Finding(
                f"{plan_id}-topology-drift",
                "DIAGRAM_TOPOLOGY_CHANGED",
                "HARD",
                (template.topology_sha256, layout.topology_sha256),
            )
        )
        return tuple(findings)

    container_by_id = {container.container_id: container for container in template.containers}
    node_by_id = {node.node_id: node for node in template.nodes}
    expected = tuple(container_by_id)
    actual = tuple(placement.container_id for placement in layout.placements)
    if actual != expected:
        findings.append(
            Finding(
                f"{plan_id}-container-order",
                "DIAGRAM_TRANSLATION_ID_MISMATCH",
                "HARD",
                actual,
            )
        )
        return tuple(findings)

    for index, placement in enumerate(layout.placements):
        container = container_by_id[placement.container_id]
        evidence = (container.container_id, container.owner_id)
        if (
            placement.owner_kind != container.owner_kind
            or placement.owner_id != container.owner_id
            or placement.node_id != container.node_id
        ):
            findings.append(
                Finding(
                    f"{plan_id}-owner-{index:03d}",
                    "DIAGRAM_LABEL_WRONG_OWNER",
                    "HARD",
                    evidence,
                )
            )
            continue
        if not placement.fit:
            findings.append(
                Finding(
                    f"{plan_id}-unfit-{index:03d}",
                    (
                        "DIAGRAM_NODE_TEXT_UNFIT"
                        if container.owner_kind == "node"
                        else "DIAGRAM_LOCAL_TEXT_UNFIT"
                    ),
                    "HARD",
                    evidence,
                )
            )
            continue
        if container.node_id is None and not _contains(
            container.allowed_bbox,
            placement.output_bbox,
        ):
            findings.append(
                Finding(
                    f"{plan_id}-outside-{index:03d}",
                    "DIAGRAM_TEXT_OUTSIDE_ALLOWED_REGION",
                    "HARD",
                    evidence,
                )
            )
        if is_coordinate_locked_container(template, container) and not _same_rect(
            placement.output_bbox,
            container.source_bbox,
            tolerance=0.15,
        ):
            findings.append(
                Finding(
                    f"{plan_id}-coordinate-{index:03d}",
                    "DIAGRAM_MAP_TEXT_COORDINATE_CHANGED",
                    "HARD",
                    evidence,
                )
            )
        if container.node_id is not None:
            node = node_by_id[container.node_id]
            coordinate_unchanged = is_coordinate_locked_container(
                template,
                container,
            ) and _same_rect(
                placement.output_bbox,
                container.source_bbox,
                tolerance=0.15,
            )
            if not coordinate_unchanged and not _contains(
                node.boundary_bbox,
                placement.output_bbox,
            ):
                findings.append(
                    Finding(
                        f"{plan_id}-node-outside-{index:03d}",
                        "DIAGRAM_NODE_TEXT_OUTSIDE_NODE",
                        "HARD",
                        tuple(dict.fromkeys((*evidence, node.node_id))),
                    )
                )
        if container.owner_kind == "local_label":
            source_hits = sum(
                segment_hits_rect(
                    connector.start,
                    connector.end,
                    container.source_bbox,
                )
                for connector in template.connectors
            )
            output_hits = sum(
                segment_hits_rect(
                    connector.start,
                    connector.end,
                    placement.glyph_bbox or placement.output_bbox,
                )
                for connector in template.connectors
            )
            if output_hits > source_hits:
                findings.append(
                    Finding(
                        f"{plan_id}-connector-{index:03d}",
                        "DIAGRAM_NEW_CONNECTOR_COLLISION",
                        "HARD",
                        evidence,
                    )
                )

    placement_by_id = {
        placement.container_id: placement for placement in layout.placements
    }
    for cohort_index, cohort in enumerate(
        paragraph_typography_cohorts(template)
    ):
        cohort_pairs = [
            (container, placement_by_id[container.container_id])
            for container in cohort
            if placement_by_id[container.container_id].fit
        ]
        if len(cohort_pairs) < 2:
            continue
        scales = {
            round(placement.font_size / max(container.font_size, 0.01), 3)
            for container, placement in cohort_pairs
        }
        line_heights = {
            round(placement.line_height, 3)
            for _, placement in cohort_pairs
        }
        if len(scales) > 1 or len(line_heights) > 1:
            findings.append(
                Finding(
                    f"{plan_id}-body-typography-{cohort_index:03d}",
                    "DIAGRAM_BODY_TYPOGRAPHY_INCONSISTENT",
                    "HARD",
                    tuple(container.container_id for container in cohort),
                )
            )

    for chain_index, chain in enumerate(local_flow_chains(template)):
        chain_placements = [
            placement_by_id[container.container_id]
            for container in chain
            if placement_by_id[container.container_id].fit
            and placement_by_id[container.container_id].glyph_bbox is not None
        ]
        collisions = tuple(
            (
                left.container_id,
                right.container_id,
            )
            for index, left in enumerate(chain_placements)
            for right in chain_placements[index + 1 :]
            if _intersection_area(
                left.glyph_bbox,
                right.glyph_bbox,
            )
            > 0.5
        )
        if collisions:
            findings.append(
                Finding(
                    f"{plan_id}-flow-collision-{chain_index:03d}",
                    "DIAGRAM_FLOW_TEXT_COLLISION",
                    "HARD",
                    tuple(
                        dict.fromkeys(
                            container_id
                            for collision in collisions
                            for container_id in collision
                        )
                    ),
                )
            )

    return _deduplicate(tuple(findings))


def required_literal_preserved(text: str, literal: str) -> bool:
    """Compare mechanical literals while tolerating full-width percent signs."""

    normalized_text = (
        text.replace("％", "%").replace("，", ",").replace("（", "(").replace("）", ")")
    )
    normalized_literal = (
        literal.replace("％", "%").replace("，", ",").replace("（", "(").replace("）", ")")
    )
    return normalized_literal in normalized_text


def _same_rect(left: Rect, right: Rect, *, tolerance: float) -> bool:
    return all(abs(left[index] - right[index]) <= tolerance for index in range(4))


def _contains(outer: Rect, inner: Rect, tolerance: float = 0.10) -> bool:
    return (
        inner[0] >= outer[0] - tolerance
        and inner[1] >= outer[1] - tolerance
        and inner[2] <= outer[2] + tolerance
        and inner[3] <= outer[3] + tolerance
    )


def _intersection_area(left: Rect, right: Rect) -> float:
    return max(0.0, min(left[2], right[2]) - max(left[0], right[0])) * max(
        0.0,
        min(left[3], right[3]) - max(left[1], right[1]),
    )


def _deduplicate(findings: tuple[Finding, ...]) -> tuple[Finding, ...]:
    result: list[Finding] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for finding in findings:
        identity = (finding.code, finding.evidence_ids)
        if identity not in seen:
            seen.add(identity)
            result.append(finding)
    return tuple(result)
