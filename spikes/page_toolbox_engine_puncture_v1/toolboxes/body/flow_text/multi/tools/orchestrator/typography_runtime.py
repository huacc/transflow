from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from page_toolbox_puncture.contracts import PageFacts, write_json
from shared_pdf_kernel.render import render_page
from toolboxes.body.flow_text.single.tools.models import ToolboxDecision, ToolboxFinding
from toolboxes.body.flow_text.single.tools.renderer import render_candidate

from ..judge import judge_multi_candidate
from ..layout_pattern import infer_multi_band_variant
from ..layout_planner import refresh_post_repair_planning_findings
from ..models import MultiColumnLayoutPlan, MultiColumnTemplate
from ..probes.semantic_paragraph_spacing_probe import probe_semantic_paragraph_transitions
from ..repairs.font_scale_recovery import build_font_scale_recovery_candidates
from ..repairs.line_height_recovery import build_line_height_recovery_candidates
from ..repairs.typography_profile_recovery import profile_evidence, typography_plan_state_hash
from ..typography_adjudication import TypographyAdjudicator
from ..validators.rendered_semantic_spacing_rule import evaluate_rendered_semantic_spacing
from ..validators.typography_density_rule import evaluate_typography_density_failure
from .typography_repair_loop import (
    TypographyRepairCandidate,
    classify_typography_attempt,
    new_typography_repair_memory,
    record_typography_attempt,
    select_next_typography_action,
)


@dataclass(frozen=True)
class TypographyRuntimeResult:
    plan: MultiColumnLayoutPlan
    render_findings: tuple[ToolboxFinding, ...]
    render_evidence: dict[str, object]
    semantic_transitions: tuple[dict[str, object], ...]
    rendered_spacing_decision: dict[str, object]
    typography_decision: dict[str, object]
    typography_findings: tuple[ToolboxFinding, ...]
    repair_memory: dict[str, object]


def run_typography_repair_loop(
    *,
    source_pdf: Path,
    candidate_pdf: Path,
    run_dir: Path,
    facts: PageFacts,
    template: MultiColumnTemplate,
    plan: MultiColumnLayoutPlan,
    planning_findings: tuple[ToolboxFinding, ...],
    render_findings: tuple[ToolboxFinding, ...],
    render_evidence: dict[str, object],
    semantic_transitions: tuple[dict[str, object], ...],
    rendered_spacing_decision: dict[str, object],
    pattern_rule: dict[str, object],
    adjudicator: TypographyAdjudicator,
) -> TypographyRuntimeResult:
    reports_dir = run_dir / "reports"
    previews_dir = run_dir / "previews"
    output_dir = run_dir / "output"
    current_plan = plan
    current_render_findings = render_findings
    current_render_evidence = render_evidence
    current_transitions = semantic_transitions
    current_spacing_decision = rendered_spacing_decision
    current_mechanical = judge_multi_candidate(
        candidate_pdf=candidate_pdf,
        template=template,
        plan=current_plan,
        upstream_findings=planning_findings + current_render_findings,
        rendered_spacing_decision=current_spacing_decision,
    )
    current_pdf = candidate_pdf
    current_decision = adjudicator.adjudicate(
        source_png=previews_dir / "source.png",
        candidate_png=previews_dir / "candidate.png",
        evidence=_adjudication_evidence(
            pattern_rule=pattern_rule,
            plan=current_plan,
            evaluation_scope="full_page",
            target_column_ids=(),
        ),
    )
    write_json(reports_dir / "typography_density_qwen_decision.initial.json", current_decision)
    memory = new_typography_repair_memory(template.page_id, typography_plan_state_hash(current_plan))
    rule_index = 0

    while current_decision["verdict"] != "acceptable":
        rule_index += 1
        rule_decision = evaluate_typography_density_failure(current_decision)
        write_json(reports_dir / f"typography_rule_{rule_index:04d}.json", rule_decision)
        accepted = False
        while True:
            binding, candidates = _dispatch_candidates(
                template=template,
                plan=current_plan,
                failure_class=str(rule_decision["selected_failure_class"]),
                attempted_action_keys=frozenset(str(item) for item in memory["attempted_action_keys"]),
            )
            selection = select_next_typography_action(memory, tuple(item.action for item in candidates))
            if selection.status != "CANDIDATE_READY":
                break
            candidate = next(item for item in candidates if item.action.action_key == selection.action.action_key)
            attempt_index = len(memory["attempts"]) + 1
            patch = _repair_patch(candidate, binding)
            write_json(reports_dir / f"typography_patch_{attempt_index:04d}.json", patch)
            candidate_planning_findings = refresh_post_repair_planning_findings(
                facts=facts,
                template=template,
                plan=candidate.plan,
                findings=planning_findings + candidate.planning_findings,
            )
            hard_planning = [item for item in candidate_planning_findings if item.severity == "HARD"]
            if hard_planning:
                gate = {
                    "schema_version": "p5-typography-mechanical-gate/v1",
                    "verdict": "FAIL",
                    "reason": "candidate planning introduced a hard finding",
                    "new_hard_findings": [_finding_evidence(item) for item in hard_planning],
                }
                write_json(reports_dir / f"typography_mechanical_gate_{attempt_index:04d}.json", gate)
                outcome = classify_typography_attempt(
                    before_verdict=str(current_decision["verdict"]),
                    after_verdict=None,
                    mechanical_gate="FAIL",
                )
                record_typography_attempt(
                    memory,
                    action=candidate.action,
                    before_verdict=str(current_decision["verdict"]),
                    after_verdict=None,
                    mechanical_gate="FAIL",
                    outcome=outcome,
                    evidence={"repair_patch": patch, "mechanical_gate": gate},
                )
                continue

            attempt_dir = previews_dir / f"typography_attempt_{attempt_index:04d}"
            attempt_pdf = output_dir / f"typography_attempt_{attempt_index:04d}.pdf"
            try:
                attempt_render_findings, attempt_render_evidence = render_candidate(
                    source_pdf=source_pdf,
                    candidate_pdf=attempt_pdf,
                    facts=facts,
                    template=template,
                    plan=candidate.plan,
                    evidence_dir=attempt_dir,
                )
                attempt_transitions = probe_semantic_paragraph_transitions(
                    candidate_pdf=attempt_pdf,
                    facts=facts,
                    template=template,
                    plan=candidate.plan,
                )
                attempt_spacing_decision = evaluate_rendered_semantic_spacing(
                    attempt_transitions,
                    ignore_relative_spacing_columns=_ignored_spacing_columns(template),
                )
                attempt_mechanical = judge_multi_candidate(
                    candidate_pdf=attempt_pdf,
                    template=template,
                    plan=candidate.plan,
                    upstream_findings=candidate_planning_findings + attempt_render_findings,
                    rendered_spacing_decision=attempt_spacing_decision,
                )
                new_hard = _new_hard_findings(current_mechanical, attempt_mechanical)
                gate = {
                    "schema_version": "p5-typography-mechanical-gate/v1",
                    "verdict": "PASS" if not new_hard else "FAIL",
                    "baseline_hard_findings": [_finding_evidence(item) for item in current_mechanical.findings if item.severity == "HARD"],
                    "candidate_hard_findings": [_finding_evidence(item) for item in attempt_mechanical.findings if item.severity == "HARD"],
                    "new_hard_findings": [_finding_evidence(item) for item in new_hard],
                    "render_evidence": attempt_render_evidence,
                    "rendered_spacing_decision": attempt_spacing_decision,
                }
            except (RuntimeError, ValueError) as exc:
                gate = {
                    "schema_version": "p5-typography-mechanical-gate/v1",
                    "verdict": "FAIL",
                    "reason": f"{type(exc).__name__}:{exc}",
                    "new_hard_findings": [],
                }
                attempt_render_findings = ()
                attempt_render_evidence = {}
                attempt_transitions = ()
                attempt_spacing_decision = {}
                attempt_mechanical = current_mechanical
            write_json(reports_dir / f"typography_mechanical_gate_{attempt_index:04d}.json", gate)
            if gate["verdict"] != "PASS":
                outcome = classify_typography_attempt(
                    before_verdict=str(current_decision["verdict"]),
                    after_verdict=None,
                    mechanical_gate="FAIL",
                )
                record_typography_attempt(
                    memory,
                    action=candidate.action,
                    before_verdict=str(current_decision["verdict"]),
                    after_verdict=None,
                    mechanical_gate="FAIL",
                    outcome=outcome,
                    evidence={"repair_patch": patch, "mechanical_gate": gate},
                )
                continue

            target_source_png, target_candidate_png = _render_target_scope(
                source_pdf=source_pdf,
                candidate_pdf=attempt_pdf,
                output_dir=attempt_dir,
                facts=facts,
                template=template,
                target_column_ids=candidate.action.target_column_ids,
            )
            target_decision = adjudicator.adjudicate(
                source_png=target_source_png,
                candidate_png=target_candidate_png,
                evidence=_adjudication_evidence(
                    pattern_rule=pattern_rule,
                    plan=candidate.plan,
                    evaluation_scope="target_columns",
                    target_column_ids=candidate.action.target_column_ids,
                ),
            )
            write_json(reports_dir / f"typography_qwen_rejudgement_{attempt_index:04d}.json", target_decision)
            outcome = classify_typography_attempt(
                before_verdict=str(current_decision["verdict"]),
                after_verdict=str(target_decision["verdict"]),
                mechanical_gate="PASS",
            )
            evidence = {
                "repair_patch": patch,
                "mechanical_gate": gate,
                "target_qwen_rejudgement": target_decision,
                "candidate_pdf": str(attempt_pdf.relative_to(run_dir)),
            }
            if outcome in {"ACCEPTED", "ACCEPTED_NEW_FAILURE"}:
                current_plan = candidate.plan
                current_render_findings = attempt_render_findings
                current_render_evidence = attempt_render_evidence
                current_transitions = attempt_transitions
                current_spacing_decision = attempt_spacing_decision
                current_mechanical = attempt_mechanical
                current_pdf = attempt_pdf
                current_decision = adjudicator.adjudicate(
                    source_png=attempt_dir / "source.png",
                    candidate_png=attempt_dir / "candidate.png",
                    evidence=_adjudication_evidence(
                        pattern_rule=pattern_rule,
                        plan=current_plan,
                        evaluation_scope="full_page_after_accepted_repair",
                        target_column_ids=(),
                    ),
                )
                write_json(reports_dir / f"typography_qwen_full_recheck_{attempt_index:04d}.json", current_decision)
                evidence["full_page_qwen_recheck"] = current_decision
                accepted = True
            record_typography_attempt(
                memory,
                action=candidate.action,
                before_verdict=str(rule_decision["observed_verdict"]),
                after_verdict=str(target_decision["verdict"]),
                mechanical_gate="PASS",
                outcome=outcome,
                evidence=evidence,
            )
            if accepted:
                break
        if selection.status != "CANDIDATE_READY" or not accepted:
            break

    if current_decision["verdict"] == "acceptable":
        memory["terminal_reason"] = "ACCEPTABLE"
    memory["final_state_hash"] = typography_plan_state_hash(current_plan)
    memory["final_verdict"] = current_decision["verdict"]
    memory["final_profiles"] = list(profile_evidence(current_plan, tuple(item.column_id for item in template.columns)))

    if current_pdf != candidate_pdf:
        current_render_findings, current_render_evidence = render_candidate(
            source_pdf=source_pdf,
            candidate_pdf=candidate_pdf,
            facts=facts,
            template=template,
            plan=current_plan,
            evidence_dir=previews_dir,
        )
        current_transitions = probe_semantic_paragraph_transitions(
            candidate_pdf=candidate_pdf,
            facts=facts,
            template=template,
            plan=current_plan,
        )
        current_spacing_decision = evaluate_rendered_semantic_spacing(
            current_transitions,
            ignore_relative_spacing_columns=_ignored_spacing_columns(template),
        )
    write_json(reports_dir / "typography_density_qwen_decision.json", current_decision)
    write_json(reports_dir / "typography_repair_memory.json", memory)
    findings = ()
    if current_decision["verdict"] != "acceptable":
        findings = (
            ToolboxFinding(
                f"P5_TYPOGRAPHY_{str(current_decision['verdict']).upper()}",
                "HARD",
                "p5_typography_density_qwen",
                None,
                str(current_decision["reason"]),
            ),
        )
    return TypographyRuntimeResult(
        current_plan,
        current_render_findings,
        current_render_evidence,
        current_transitions,
        current_spacing_decision,
        current_decision,
        findings,
        memory,
    )


def _dispatch_candidates(
    *,
    template: MultiColumnTemplate,
    plan: MultiColumnLayoutPlan,
    failure_class: str,
    attempted_action_keys: frozenset[str],
) -> tuple[dict[str, object], tuple[TypographyRepairCandidate, ...]]:
    dispatch_path = Path(__file__).resolve().parents[2] / "contracts" / "failure_dispatch_table.json"
    dispatch = json.loads(dispatch_path.read_text(encoding="utf-8"))
    binding = next((item for item in dispatch["bindings"] if item["failure_class"] == failure_class), None)
    if binding is None:
        raise RuntimeError(f"p5_typography_dispatch_binding_missing:{failure_class}")
    variant = infer_multi_band_variant(template)
    if variant not in binding.get("applicable_variants", []):
        raise RuntimeError(f"p5_typography_dispatch_variant_rejected:{failure_class}:{variant}")
    builders = {
        "line_height_recovery": build_line_height_recovery_candidates,
        "font_scale_recovery": build_font_scale_recovery_candidates,
    }
    repair_atom = str(binding["repair_atom"])
    if repair_atom not in builders:
        raise RuntimeError(f"p5_typography_repair_atom_unbound:{repair_atom}")
    return binding, builders[repair_atom](
        template=template,
        plan=plan,
        attempted_action_keys=attempted_action_keys,
        candidate_limit=1,
    )


def _repair_patch(candidate: TypographyRepairCandidate, binding: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": "repair-patch/v1",
        "patch_verdict": "PATCH_READY",
        "selected_failure_class": candidate.action.failure_class,
        "dispatch_result": {
            "dispatch_table": "contracts/failure_dispatch_table.json",
            "selected_repair_family": binding["repair_family"],
            "selected_repair_atom": binding["repair_atom"],
            "bound_tool": binding["bound_tool"],
        },
        "operations": [
            {
                "operation_type": candidate.action.repair_atom,
                "target_column_ids": list(candidate.action.target_column_ids),
                "before_profiles": list(candidate.action.before_profiles),
                "after_profiles": list(candidate.action.after_profiles),
            }
        ],
        "anti_overfit_statement": "All values come from finite toolbox profiles and current-page column facts; no sample id, text, or fixed coordinate is used.",
    }


def _adjudication_evidence(
    *,
    pattern_rule: dict[str, object],
    plan: MultiColumnLayoutPlan,
    evaluation_scope: str,
    target_column_ids: tuple[str, ...],
) -> dict[str, object]:
    return {
        "toolbox_key": "body.flow_text.multi",
        "flow_pattern": pattern_rule["pattern"],
        "multi_band_variant": pattern_rule["multi_band_variant"],
        "column_profiles": profile_evidence(plan, tuple(item.column_id for item in plan.columns)),
        "evaluation_scope": evaluation_scope,
        "target_column_ids": target_column_ids,
        "rule_scope": "font_size_and_line_spacing_only",
    }


def _render_target_scope(
    *,
    source_pdf: Path,
    candidate_pdf: Path,
    output_dir: Path,
    facts: PageFacts,
    template: MultiColumnTemplate,
    target_column_ids: tuple[str, ...],
) -> tuple[Path, Path]:
    targets = [item for item in template.columns if item.column_id in set(target_column_ids)]
    clip = (
        max(0.0, min(item.left for item in targets) - 8.0),
        max(0.0, min(item.content_top for item in targets) - 8.0),
        min(template.width, max(item.right for item in targets) + 8.0),
        min(template.height, max(item.content_bottom for item in targets) + 8.0),
    )
    source_png = output_dir / "source.target.png"
    candidate_png = output_dir / "candidate.target.png"
    render_page(source_pdf, source_png, page_index=facts.page_index, zoom=2.0, clip=clip)
    render_page(candidate_pdf, candidate_png, page_index=facts.page_index, zoom=2.0, clip=clip)
    return source_png, candidate_png


def _ignored_spacing_columns(template: MultiColumnTemplate) -> tuple[str, ...]:
    if infer_multi_band_variant(template) == "paired_row_columns":
        return tuple(item.column_id for item in template.columns)
    return ()


def _new_hard_findings(before: ToolboxDecision, after: ToolboxDecision) -> tuple[ToolboxFinding, ...]:
    before_counts = Counter((item.code, item.container_id) for item in before.findings if item.severity == "HARD")
    seen: Counter[tuple[str, str | None]] = Counter()
    new: list[ToolboxFinding] = []
    for item in after.findings:
        if item.severity != "HARD":
            continue
        key = (item.code, item.container_id)
        seen[key] += 1
        if seen[key] > before_counts[key]:
            new.append(item)
    return tuple(new)


def _finding_evidence(finding: ToolboxFinding) -> dict[str, object]:
    return {
        "code": finding.code,
        "severity": finding.severity,
        "owner": finding.owner,
        "container_id": finding.container_id,
        "message": finding.message,
    }
