from __future__ import annotations

from ..layout_pattern import infer_multi_band_variant
from ..models import MultiColumnLayoutPlan, MultiColumnTemplate
from ..orchestrator.typography_repair_loop import TypographyRepairAction, TypographyRepairCandidate
from .typography_profile_recovery import (
    apply_typography_profile_change,
    profile_evidence,
    prospective_profile_evidence,
    typography_plan_state_hash,
)


FONT_SCALE_LEVELS = (1.00, 1.10, 1.20)


def build_font_scale_recovery_candidates(
    *,
    template: MultiColumnTemplate,
    plan: MultiColumnLayoutPlan,
    attempted_action_keys: frozenset[str] = frozenset(),
    candidate_limit: int | None = None,
) -> tuple[TypographyRepairCandidate, ...]:
    selection_by_id = {item.column_id: item for item in plan.column_selections}
    if infer_multi_band_variant(template) == "paired_row_columns":
        scopes = (tuple(item.column_id for item in template.columns),)
    else:
        scopes = tuple((item.column_id,) for item in template.columns)

    specs: list[tuple[float, int, tuple[str, ...]]] = []
    column_order = {item.column_id: index for index, item in enumerate(template.columns)}
    for scope in scopes:
        current_font_scale = max(selection_by_id[column_id].font_scale for column_id in scope)
        for target_font_scale in FONT_SCALE_LEVELS:
            if target_font_scale <= current_font_scale + 0.001:
                continue
            specs.append((target_font_scale, min(column_order[item] for item in scope), scope))

    candidates: list[TypographyRepairCandidate] = []
    for target_font_scale, _, scope in sorted(specs, key=lambda item: (item[0], item[1])):
        prospective_action = TypographyRepairAction(
            failure_class="body_font_scale_too_small",
            repair_atom="font_scale_recovery",
            bound_tool="tools/repairs/font_scale_recovery.py",
            target_column_ids=scope,
            before_profiles=profile_evidence(plan, scope),
            after_profiles=prospective_profile_evidence(plan, scope, font_scale=target_font_scale),
            candidate_state_hash="",
        )
        if prospective_action.action_key in attempted_action_keys:
            continue
        candidate_plan, findings = apply_typography_profile_change(
            template=template,
            plan=plan,
            target_column_ids=scope,
            font_scale=target_font_scale,
        )
        action = TypographyRepairAction(
            failure_class=prospective_action.failure_class,
            repair_atom=prospective_action.repair_atom,
            bound_tool=prospective_action.bound_tool,
            target_column_ids=scope,
            before_profiles=prospective_action.before_profiles,
            after_profiles=profile_evidence(candidate_plan, scope),
            candidate_state_hash=typography_plan_state_hash(candidate_plan),
        )
        candidates.append(TypographyRepairCandidate(action, candidate_plan, findings))
        if candidate_limit is not None and len(candidates) >= candidate_limit:
            break
    return tuple(candidates)
