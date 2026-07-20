"""Build and apply generic layout-policy repair patches.

tool_name: repair_policy_patch
category: repairs
input_contract: layout policy JSON plus selected visual repair plan entry
output_contract: repair patch operations and applied policy change records
failure_signals: missing repair selection, unsupported atom, invalid patch operation
fallback: caller records not_executed_unrepairable or product-quality failure
anti_overfit_statement: branches only on failure class, repair atom, target language, and region role; never on filename, known page, literal text, document identity, page number, or fixed sample coordinates
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any


EXECUTABLE_REPAIR_ATOMS = {
    "target_composition_body_reflow_repair",
    "expandable_text_slot_reflow_repair",
    "heading_frame_fit_or_short_title_variant",
    "heading_font_fit_curve_repair",
    "event_card_local_fit_repair",
    "footnote_fit_curve_repair",
    "side_navigation_rotated_image_repair",
    "matrix_diagram_table_cell_preserve_repair",
    "short_continuation_and_reflow_frame_repair",
    "body_flow_grouping",
    "body_flow_region_reflow",
    "body_flow_line_joining_or_line_height_adjust",
    "body_flow_paragraph_gap_rebalance",
    "font_size_and_region_density_rebalance",
    "dense_page_body_band_flow_repair",
    "constrained_slot_layout_fit_repair",
    "metric_value_font_hierarchy_repair",
}


def select_executable_repair_plan(
    repair_data: dict[str, Any],
    attempted_repairs: set[tuple[str, str]] | None = None,
    gate_id: str | None = None,
    repair_atom: str | None = None,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    attempted_repairs = attempted_repairs or set()
    plans = [item for item in repair_data.get("plans", []) if item.get("gate_status") == "fail"]
    for plan in plans:
        plan_gate = str(plan.get("gate_id") or "")
        plan_atom = str(plan.get("repair_atom") or "")
        key = (plan_gate, plan_atom)
        if gate_id and plan_gate != gate_id:
            continue
        if repair_atom and plan_atom != repair_atom:
            continue
        if key in attempted_repairs:
            continue
        if plan_atom in EXECUTABLE_REPAIR_ATOMS and str(plan.get("target_state")) in {"S6_LayoutPlan", "S7_GenerateCandidate"}:
            return plan, plans
    return None, plans


def _get_path(data: dict[str, Any], path: list[str]) -> Any:
    current: Any = data
    for part in path:
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _set_path(data: dict[str, Any], path: list[str], value: Any) -> None:
    current: Any = data
    for part in path[:-1]:
        if not isinstance(current, dict):
            raise ValueError(f"cannot set nested path through non-object at {'.'.join(path)}")
        child = current.setdefault(part, {})
        if not isinstance(child, dict):
            child = {}
            current[part] = child
        current = child
    if not isinstance(current, dict):
        raise ValueError(f"cannot set path on non-object at {'.'.join(path)}")
    current[path[-1]] = value


def _list_value(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item)]


def _append_op(ops: list[dict[str, Any]], policy: dict[str, Any], op: dict[str, Any]) -> None:
    path = [str(item) for item in op["path"]]
    before = deepcopy(_get_path(policy, path))
    kind = op["op"]
    if kind == "set":
        after = deepcopy(op["value"])
    elif kind == "add_unique":
        current = _list_value(before)
        after = list(current)
        for value in [str(item) for item in op.get("values", []) if str(item)]:
            if value not in after:
                after.append(value)
    elif kind == "remove_values":
        blocked = {str(item) for item in op.get("values", [])}
        current = _list_value(before)
        after = [item for item in current if item not in blocked]
    elif kind == "raise_float":
        minimum = float(op["minimum"])
        try:
            old = float(before)
        except (TypeError, ValueError):
            old = 0.0
        after = minimum if old < minimum else before
    else:
        raise ValueError(f"unsupported repair patch op: {kind}")
    if after != before:
        recorded = dict(op)
        recorded["before"] = before
        recorded["after"] = after
        recorded["path_text"] = ".".join(path)
        ops.append(recorded)


def _op_set(ops: list[dict[str, Any]], policy: dict[str, Any], path: list[str], value: Any, reason: str) -> None:
    _append_op(ops, policy, {"op": "set", "path": path, "value": value, "reason": reason})


def _op_add_unique(ops: list[dict[str, Any]], policy: dict[str, Any], path: list[str], values: list[str], reason: str) -> None:
    _append_op(ops, policy, {"op": "add_unique", "path": path, "values": values, "reason": reason})


def _op_remove_values(ops: list[dict[str, Any]], policy: dict[str, Any], path: list[str], values: list[str], reason: str) -> None:
    _append_op(ops, policy, {"op": "remove_values", "path": path, "values": values, "reason": reason})


def _op_raise_float(ops: list[dict[str, Any]], policy: dict[str, Any], path: list[str], minimum: float, reason: str) -> None:
    _append_op(ops, policy, {"op": "raise_float", "path": path, "minimum": minimum, "reason": reason})


def build_policy_patch_operations(policy: dict[str, Any], selected: dict[str, Any]) -> list[dict[str, Any]]:
    atom = str(selected.get("repair_atom") or "")
    gate_id = str(selected.get("gate_id") or selected.get("failure_class") or "")
    target_language = str(policy.get("target_language") or "").lower()
    language_pair_profile = str(policy.get("language_pair_profile") or "")
    ops: list[dict[str, Any]] = []

    _op_set(ops, policy, ["constrained_text_image_fit", "enabled"], True, "repair loop enables declared constrained text-image fit before tiny point fallback")

    critical_roles: list[str] = []
    wrapped_roles: list[str] = []
    no_constrained_roles: set[str] = set()

    if atom in {
        "target_composition_body_reflow_repair",
        "short_continuation_and_reflow_frame_repair",
        "body_flow_grouping",
        "body_flow_region_reflow",
        "body_flow_line_joining_or_line_height_adjust",
        "body_flow_paragraph_gap_rebalance",
        "font_size_and_region_density_rebalance",
        "dense_page_body_band_flow_repair",
    }:
        critical_roles.extend(["body", "body_flow"])
        wrapped_roles.extend(["body", "body_flow"])
        if target_language == "en" or language_pair_profile == "zh_to_en":
            _op_add_unique(ops, policy, ["target_composition", "disable_page_type_guesses"], ["mixed_image_text"], "zh->en mixed image/text pages keep local anchors instead of page-wide body composition")
            _op_add_unique(ops, policy, ["target_language_reflow", "disable_page_type_guesses"], ["mixed_image_text"], "zh->en mixed image/text pages keep local anchors instead of page-wide reflow")
            _op_raise_float(ops, policy, ["target_composition", "min_width_page_ratio"], 0.78, "body readability repair widens fluid body frames before shrinking font")
            _op_raise_float(ops, policy, ["target_composition", "min_source_width_page_ratio_for_composition"], 0.42, "body readability repair skips page-wide composition for narrow source columns")
            _op_raise_float(ops, policy, ["target_composition", "height_expand_ratio"], 1.55, "body readability repair gives expanded English prose more vertical room")
            _op_raise_float(ops, policy, ["target_composition", "max_bottom_page_ratio"], 0.96, "body readability repair can use normal lower-page body area")
            _op_raise_float(ops, policy, ["target_language_reflow", "min_width_page_ratio"], 0.72, "target-language reflow repair widens paragraph frames")
            _op_raise_float(ops, policy, ["target_language_reflow", "min_source_width_page_ratio_for_reflow"], 0.42, "target-language reflow repair skips frame expansion for narrow source columns")
            _op_raise_float(ops, policy, ["target_language_reflow", "height_expand_ratio"], 1.55, "target-language reflow repair gives expanded English prose more vertical room")
            _op_set(ops, policy, ["flow_grouping", "body", "candidate_region_kinds"], ["body"], "repair prevents compact labels and short labels from being merged into English body_flow")
            _op_add_unique(ops, policy, ["flow_grouping", "body", "disable_page_type_guesses"], ["mixed_image_text"], "mixed image/text regions are local constrained cards, not continuous body flow")

    if atom in {"heading_frame_fit_or_short_title_variant", "heading_font_fit_curve_repair"} or gate_id in {"title_readability", "hero_banner_text_readability"}:
        critical_roles.append("heading")
        wrapped_roles.append("heading")
        _op_set(ops, policy, ["expandable_text_slots", "enabled"], True, "heading repair enables declared expandable text slots")
        _op_add_unique(ops, policy, ["expandable_text_slots", "region_kinds"], ["heading"], "readability repair lets page headings use current-page whitespace instead of hard source bbox fitting")

    if atom == "event_card_local_fit_repair" or gate_id == "event_card_readability":
        critical_roles.append("event_card")
        wrapped_roles.append("event_card")

    if atom == "footnote_fit_curve_repair" or gate_id == "footnote_readability":
        critical_roles.extend(["footnote", "table_note"])
        wrapped_roles.extend(["footnote", "table_note"])

    if atom == "matrix_diagram_table_cell_preserve_repair" or gate_id == "matrix_diagram_integrity":
        for profile_name in ["target_composition", "target_language_reflow"]:
            _op_add_unique(ops, policy, [profile_name, "hard_disable_page_type_guesses"], ["matrix_or_table_diagram"], "matrix/table diagrams preserve two-dimensional structure")
        _op_add_unique(ops, policy, ["flow_grouping", "body", "hard_disable_page_type_guesses"], ["matrix_or_table_diagram"], "matrix/table diagrams must not be routed through body_flow")

    if atom == "expandable_text_slot_reflow_repair" or gate_id == "short_label_legibility":
        _op_set(ops, policy, ["expandable_text_slots", "enabled"], True, "short-label repair enables declared expandable text slots")
        _op_add_unique(ops, policy, ["expandable_text_slots", "region_kinds"], ["short_label", "compact_label", "heading"], "expanded target text slots fix long labels before font shrink")
        _op_add_unique(ops, policy, ["expandable_text_slots", "disable_page_type_guesses"], ["chart_or_dashboard", "table_or_chart_dense"], "dense chart/table labels remain hard constrained slots")
        _op_add_unique(ops, policy, ["expandable_text_slots", "hard_disable_page_type_guesses"], ["matrix_or_table_diagram"], "matrix/table diagrams preserve two-dimensional structure")
        _op_raise_float(ops, policy, ["expandable_text_slots", "min_width_page_ratio"], 0.38, "long target labels can use nearby whitespace before shrink")
        _op_raise_float(ops, policy, ["expandable_text_slots", "max_width_page_ratio"], 0.78, "long target labels can expand within page margins without crossing into hard structures")
        _op_raise_float(ops, policy, ["expandable_text_slots", "height_expand_ratio"], 1.8, "long target labels need enough line height after expansion")
        _op_raise_float(ops, policy, ["expandable_text_slots", "min_height_source_ratio"], 2.4, "expanded labels keep readable local height derived from source font size")
        _op_raise_float(ops, policy, ["expandable_text_slots", "compact_label_min_width_page_ratio"], 0.18, "compact labels use current-page width before font shrink")
        _op_raise_float(ops, policy, ["expandable_text_slots", "compact_label_max_width_page_ratio"], 0.42, "compact label expansion remains local")
        _op_raise_float(ops, policy, ["expandable_text_slots", "compact_label_height_expand_ratio"], 1.35, "compact labels get enough source-relative line height")
        _op_set(ops, policy, ["expandable_text_slots", "compact_label_min_y_ratio"], 0.0, "top page labels can expand when geometry allows")
        _op_set(ops, policy, ["expandable_text_slots", "compact_label_min_text_expansion_ratio"], 0.0, "compact label expansion is allowed for readability, not only length growth")
        _op_set(ops, policy, ["expandable_text_slots", "compact_label_min_target_chars"], 1, "compact labels can be short but still need readable font size")
        _op_add_unique(ops, policy, ["constrained_text_image_fit", "wrapped_region_kinds"], ["short_label", "compact_label"], "expanded labels wrap locally if textbox probing still fails")

    if atom == "metric_value_font_hierarchy_repair" or gate_id == "metric_value_hierarchy":
        critical_roles.append("metric_value")
        no_constrained_roles.add("metric_value")
        _op_add_unique(ops, policy, ["reflow", "preserve_line_kinds"], ["metric_value"], "metric/KPI values preserve local hierarchy and are not paragraph-reflowed")
        _op_set(ops, policy, ["layout_text_variants", "metric_value_en"], ["metric_value_en", "compact_en", "display_en"], "metric/KPI values may use semantic compact display variants before geometry shrink")
        _op_set(ops, policy, ["layout_text_variants", "metric_value_zh"], ["metric_value_zh", "compact_zh", "display_zh"], "metric/KPI values may use semantic compact display variants before geometry shrink")
        _op_set(ops, policy, ["expandable_text_slots", "enabled"], True, "metric/KPI repair enables declared expandable text slots")
        _op_add_unique(ops, policy, ["expandable_text_slots", "region_kinds"], ["metric_value"], "metric/KPI values may expand into current-page whitespace before shrink")
        _op_raise_float(ops, policy, ["expandable_text_slots", "metric_value_min_width_page_ratio"], 0.12, "metric/KPI repair derives width from current page, not fixed point size")
        _op_raise_float(ops, policy, ["expandable_text_slots", "metric_value_max_width_page_ratio"], 0.34, "metric/KPI repair caps expansion within local page geometry")
        _op_raise_float(ops, policy, ["expandable_text_slots", "metric_value_height_expand_ratio"], 1.2, "metric/KPI repair allows one readable line-height extension")
        _op_raise_float(ops, policy, ["expandable_text_slots", "metric_value_min_height_source_ratio"], 0.85, "metric/KPI minimum visual height is derived from source font size")
        _op_set(ops, policy, ["expandable_text_slots", "metric_value_min_text_expansion_ratio"], 0.0, "metric/KPI expansion is driven by role hierarchy, not only text length expansion")
        _op_set(ops, policy, ["expandable_text_slots", "metric_value_min_target_chars"], 1, "metric/KPI values can be short but still visually dominant")
        _op_set(ops, policy, ["classification_rules", "metric_value", "enabled"], True, "metric/KPI hierarchy repair enables generic value-role classification")
        _op_set(ops, policy, ["classification_rules", "metric_value", "source_size_page_quantile"], "q75", "metric/KPI role is relative to current-page font hierarchy")
        _op_set(ops, policy, ["classification_rules", "metric_value", "min_source_to_page_quantile_ratio"], 1.45, "metric/KPI role uses a current-page ratio rather than fixed point size")
        _op_set(ops, policy, ["classification_rules", "metric_value", "value_token_regex"], r"([%\uFF05$]|US\$|HK\$|GBP|EUR|\u7f8e\u5143|\u6e2f\u5143|\u5104\u5143|\u4ebf|bn|billion|million|m|bps|\u57fa\u9ede|\u57fa\u70b9)", "metric/KPI role uses generic value tokens, not literal values")
        _op_set(ops, policy, ["classification_rules", "metric_value", "value_amount_regex"], r"((?:US\$|HK\$|\$|GBP|EUR)?\s*\d[\d,]*(?:\.\d+)?\s*(?:[%\uFF05]|\u7f8e\u5143|\u6e2f\u5143|\u5104\u5143|\u4ebf|bn|billion|millions?|m|bps|\u57fa\u9ede|\u57fa\u70b9)?)|((?:US\$|HK\$|\$|GBP|EUR)\s*\d)", "metric/KPI role must include a generic numeric amount, so unit labels alone are not promoted to metric callouts")
        for key, value in {
            "sizing_mode": "source_relative",
            "source_scale": 1.0,
            "min_source_ratio": 0.70,
            "max_source_ratio": 1.05,
            "page_quantile_floor": "q75",
            "page_quantile_floor_scale": 1.10,
            "page_quantile_ceiling": "max",
            "page_quantile_ceiling_scale": 1.05,
            "min_insert_source_ratio": 0.62,
            "shrink_scales": [1.0, 0.96, 0.92, 0.88, 0.84, 0.80, 0.76, 0.70],
        }.items():
            _op_set(ops, policy, ["font_profiles", "metric_value", key], value, "metric/KPI actual point size is resolved from source_size and current-page font quantiles")
        _op_add_unique(ops, policy, ["constrained_text_image_fit", "forbid_region_kinds"], ["metric_value"], "metric/KPI hierarchy failures must not be repaired by generic compressed text images")
        _op_remove_values(ops, policy, ["constrained_text_image_fit", "region_kinds"], ["metric_value"], "metric/KPI values are repaired by source-relative font and geometry expansion, not constrained image compression")
        _op_remove_values(ops, policy, ["constrained_text_image_fit", "wrapped_region_kinds"], ["metric_value"], "metric/KPI values are repaired as callouts, not wrapped compressed images")

    if atom == "constrained_slot_layout_fit_repair" or gate_id in {"table_text_legibility", "legend_label_alignment"}:
        _op_add_unique(ops, policy, ["constrained_text_image_fit", "region_kinds"], ["table_cell", "legend", "short_label", "compact_label"], "constrained slots use local fit repair before any translation regeneration is considered")
        _op_add_unique(ops, policy, ["constrained_text_image_fit", "wrapped_region_kinds"], ["legend", "short_label", "compact_label"], "multi-line constrained labels wrap locally instead of falling back to point text")
        _op_set(ops, policy, ["constrained_text_image_fit", "keep_proportion_for_wrapped"], True, "constrained wrapped labels preserve source-relative proportions")
        _op_raise_float(ops, policy, ["constrained_text_image_fit", "max_font_source_ratio"], 0.96, "constrained slot repair keeps labels source-relative without changing translation semantics")

    if critical_roles:
        unique_critical = sorted(set(critical_roles))
        _op_add_unique(ops, policy, ["fallback", "forbid_region_kinds"], unique_critical, "critical visual roles must fail visibly instead of falling back to tiny point text")
        constrained_critical = [role for role in unique_critical if role not in no_constrained_roles]
        if constrained_critical:
            _op_add_unique(ops, policy, ["constrained_text_image_fit", "region_kinds"], constrained_critical, "critical roles may use policy-declared constrained text images after textbox probing fails")

    if wrapped_roles:
        _op_add_unique(ops, policy, ["constrained_text_image_fit", "wrapped_region_kinds"], sorted(set(wrapped_roles)), "multi-line critical roles use wrapped constrained text images, not single-line compression")
        _op_set(ops, policy, ["constrained_text_image_fit", "keep_proportion_for_wrapped"], True, "wrapped critical-role text preserves local proportions")
        _op_raise_float(ops, policy, ["constrained_text_image_fit", "max_font_source_ratio"], 1.05, "repair keeps wrapped critical-role text source-relative")
        _op_raise_float(ops, policy, ["constrained_text_image_fit", "min_font_source_ratio"], 0.62, "repair avoids unreadable wrapped text images through source-relative sizing")

    return ops


def apply_operations(policy: dict[str, Any], operations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    for operation in operations:
        path = [str(item) for item in operation["path"]]
        before = deepcopy(_get_path(policy, path))
        op = operation["op"]
        if op == "set":
            after = deepcopy(operation["value"])
        elif op == "add_unique":
            after = _list_value(before)
            for value in [str(item) for item in operation.get("values", []) if str(item)]:
                if value not in after:
                    after.append(value)
        elif op == "remove_values":
            blocked = {str(item) for item in operation.get("values", [])}
            after = [item for item in _list_value(before) if item not in blocked]
        elif op == "raise_float":
            minimum = float(operation["minimum"])
            try:
                old = float(before)
            except (TypeError, ValueError):
                old = 0.0
            after = minimum if old < minimum else before
        else:
            raise ValueError(f"unsupported repair patch op: {op}")
        if after != before:
            _set_path(policy, path, after)
            changes.append(
                {
                    "op": op,
                    "path": ".".join(path),
                    "before": before,
                    "after": after,
                    "reason": operation.get("reason"),
                }
            )
    return changes
