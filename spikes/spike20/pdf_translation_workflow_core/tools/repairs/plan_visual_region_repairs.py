"""Plan minimal repairs from region-level visual quality metrics.

tool_name: plan_visual_region_repairs
category: repairs
input_contract: visual_region_metrics JSON from collect_visual_region_metrics.py
output_contract: JSON repair plan with one repair atom per failed visual class
failure_signals: missing metrics, no failed or warning evidence
fallback: return no-op plan and keep product-quality failure
anti_overfit_statement: maps generic gate ids and quality roles to repair atoms; never branches on filename, known page, exact text, or fixed coordinates
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import read_json, rel, write_json  # noqa: E402


REPAIR_ROUTE = {
    "hero_banner_text_readability": {
        "repair_atom": "heading_frame_fit_or_short_title_variant",
        "target_state": "S6_LayoutPlan",
        "description": "Adjust heading font fit curve/frame policy or require D2 short title variant; do not allow fallback tiny point text.",
    },
    "title_readability": {
        "repair_atom": "heading_font_fit_curve_repair",
        "target_state": "S6_LayoutPlan",
        "description": "Extend heading shrink curve down to the readable floor and retry textbox probing before fallback.",
    },
    "body_paragraph_readability": {
        "repair_atom": "target_composition_body_reflow_repair",
        "target_state": "S6_LayoutPlan",
        "description": "Recompose body frame from current page margins/body band before shrinking text.",
    },
    "table_text_legibility": {
        "repair_atom": "D2_constrained_slot_layout_variants",
        "target_state": "S5_TranslationPlan",
        "description": "Generate compact semantic variants for table cells/headers; generator must not invent abbreviations.",
    },
    "footnote_readability": {
        "repair_atom": "footnote_fit_curve_repair",
        "target_state": "S6_LayoutPlan",
        "description": "Tune footnote fit curve and paragraph grouping without changing table/body layout.",
    },
    "legend_label_alignment": {
        "repair_atom": "D2_constrained_slot_layout_variants",
        "target_state": "S5_TranslationPlan",
        "description": "Generate compact legend variants and preserve swatch-label alignment.",
    },
    "sidebar_navigation_legibility": {
        "repair_atom": "side_navigation_rotated_image_repair",
        "target_state": "S6_LayoutPlan",
        "description": "Use horizontal text image then rotate as a unit; verify with back-rotated crop.",
    },
    "event_card_readability": {
        "repair_atom": "event_card_local_fit_repair",
        "target_state": "S6_LayoutPlan",
        "description": "Keep event text local to its anchor and tune event-card fit curve.",
    },
    "short_label_legibility": {
        "repair_atom": "D2_constrained_slot_layout_variants",
        "target_state": "S5_TranslationPlan",
        "description": "Generate compact display variant for short constrained label.",
    },
    "image_color_integrity": {
        "repair_atom": "image_redaction_exclusion_repair",
        "target_state": "S7_GenerateCandidate",
        "description": "Ensure redactions do not remove or recolor source images/drawings; preserve image XObjects.",
    },
    "background_color_delta": {
        "repair_atom": "background_fill_resample",
        "target_state": "S7_GenerateCandidate",
        "description": "Resample local background from surrounding pixels instead of glyph color or fixed color.",
    },
    "background_residue_artifact": {
        "repair_atom": "background_residue_fill_resample",
        "target_state": "S7_GenerateCandidate",
        "description": "Repair visible redaction/fill residue by sampling continuous local background and avoiding rectangular artifact patches.",
    },
    "matrix_diagram_integrity": {
        "repair_atom": "matrix_diagram_table_cell_preserve_repair",
        "target_state": "S6_LayoutPlan",
        "description": "Treat matrix/table-diagram pages as two-dimensional structures; preserve cells/labels and prevent body_flow/fallback insertion inside the diagram.",
    },
}


def plan(metrics_path: Path, out: Path) -> dict[str, Any]:
    metrics = read_json(metrics_path)
    plans: list[dict[str, Any]] = []
    for gate in metrics.get("role_gates", []):
        if gate.get("status") not in {"fail", "warn"}:
            continue
        route = REPAIR_ROUTE.get(str(gate.get("gate_id")))
        if route is None:
            continue
        sample = gate.get("sample", [])
        plans.append(
            {
                "gate_id": gate.get("gate_id"),
                "gate_status": gate.get("status"),
                "failure_count": gate.get("failure_count", 0),
                "warning_count": gate.get("warning_count", 0),
                "repair_atom": route["repair_atom"],
                "target_state": route["target_state"],
                "description": route["description"],
                "sample_regions": [
                    {
                        "page_number": item.get("page_number"),
                        "region_id": item.get("region_id"),
                        "quality_role": item.get("quality_role"),
                        "region_kind": item.get("region_kind"),
                        "generation_status": item.get("generation_status"),
                        "font_size": item.get("font_size"),
                        "crop_evidence": item.get("crop_evidence"),
                        "reasons": item.get("reasons", []),
                    }
                    for item in sample[:5]
                    if isinstance(item, dict)
                ],
            }
        )
    blocking = [item for item in plans if item.get("gate_status") == "fail"]
    result = {
        "tool": "plan_visual_region_repairs",
        "visual_region_metrics": rel(metrics_path),
        "repair_plan_count": len(plans),
        "blocking_repair_count": len(blocking),
        "next_state": "Lx_RepairLoop" if blocking else "S8_VerifyProductQuality",
        "plans": plans,
    }
    write_json(out, result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--visual-region-metrics", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    result = plan(Path(args.visual_region_metrics), Path(args.out))
    print(args.out)
    print(f"repair_plan_count={result['repair_plan_count']}; blocking_repair_count={result['blocking_repair_count']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
