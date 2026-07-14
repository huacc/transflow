"""
tool_name: structural_anchor_zone_rule
category: validators
input_contract: current template and layout plan with source structural anchors
output_contract: zero or more structural_anchor_zone_crossing findings
failure_signals: translated text changes the source structural band or materially crosses a source anchor
fallback: product FAIL and return to layout planning; never move the anchor
anti_overfit_statement: zones and tolerances derive from current-page anchors, source bboxes and typography only
"""

from __future__ import annotations

from ..models import MultiColumnLayoutPlan, MultiColumnTemplate
from ..probes.structural_anchor_probe import structural_zone


def evaluate_structural_anchor_zones(
    *,
    template: MultiColumnTemplate,
    plan: MultiColumnLayoutPlan,
) -> tuple[dict[str, object], ...]:
    if not plan.structural_anchors:
        return ()
    by_id = {item.container_id: item for item in template.containers}
    findings: list[dict[str, object]] = []
    for placement in plan.placements:
        source = by_id[placement.container_id]
        source_zone, lower, upper = structural_zone(
            source.source_bbox,
            plan.structural_anchors,
            template.height,
        )
        candidate_zone, _, _ = structural_zone(
            placement.output_bbox,
            plan.structural_anchors,
            template.height,
        )
        tolerance = placement.font_size * 0.15
        crosses = placement.output_bbox[1] < lower - tolerance or placement.output_bbox[3] > upper + tolerance
        if source_zone == candidate_zone and not crosses:
            continue
        findings.append(
            {
                "rule_verdict": "FAIL",
                "selected_failure_class": "structural_anchor_zone_crossing",
                "repair_atom": "owner_local_vertical_reflow_with_anchor_guard",
                "container_id": placement.container_id,
                "source_zone": source_zone,
                "candidate_zone": candidate_zone,
                "source_zone_bounds": [round(lower, 4), round(upper, 4)],
                "candidate_bbox": list(placement.output_bbox),
                "evidence": {
                    "anchor_count": len(plan.structural_anchors),
                    "anchor_positions_unchanged": True,
                    "candidate_crosses_anchor": crosses,
                },
            }
        )
    return tuple(findings)
