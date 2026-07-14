from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from page_toolbox_puncture.contracts import write_json
from page_toolbox_puncture.sample_snapshot import sha256_file
from page_toolbox_puncture.translation import ProviderError, TranslationProvider
from shared_pdf_kernel.facts import extract_page_facts
from toolboxes.body.flow_text.single.tools.p4_judge import judge_p4_candidate

from .layout_planner import plan_composite_layout, repair_horizontal_table_rule_overlaps
from .models import CompositeDecision, CompositeFinding
from .renderer import render_composite_candidate
from .template_builder import CompositeCapabilityError, build_composite_template
from .translation_guard import translate_with_targeted_guard_retry
from .translation_request import build_translation_request


@dataclass(frozen=True)
class P7RunResult:
    page_id: str
    run_dir: str
    candidate_pdf: str | None
    process_verdict: str
    product_verdict: str
    terminal_state: str
    failure_owner: str | None


_PROCESS_FAILURE_CODES = {
    "LOCKED_OBJECT_CHANGED",
    "PROTECTED_OBJECT_CHANGED",
    "OUTSIDE_ALLOWED_REGION_CHANGED",
}


def run_p7_page(
    *,
    source_pdf: Path,
    page_id: str,
    run_dir: Path,
    provider: TranslationProvider,
    font_file: str,
    bold_font_file: str | None,
    source_language: str,
    target_language: str,
) -> P7RunResult:
    if not Path(font_file).is_file():
        raise CompositeCapabilityError(f"FONT_FILE_MISSING:{font_file}")
    run_dir.mkdir(parents=True, exist_ok=False)
    for name in ("contracts", "docs", "input", "output", "previews", "reports"):
        (run_dir / name).mkdir()
    source_snapshot = run_dir / "input" / "source.pdf"
    shutil.copy2(source_pdf, source_snapshot)
    source_hash = sha256_file(source_pdf)
    if sha256_file(source_snapshot) != source_hash:
        raise RuntimeError("source_snapshot_hash_mismatch")

    write_json(
        run_dir / "contracts" / "page_run_contract.json",
        {
            "schema_version": "p7-body-composite-flow-text-table-page-run/v1",
            "toolbox_key": "body.composite.flow_text_table",
            "page_id": page_id,
            "source_language": source_language,
            "target_language": target_language,
            "source_snapshot": "input/source.pdf",
            "source_sha256": source_hash,
            "candidate_output": "output/candidate.pdf",
            "translation_rule": "one page-level request; route returned text by immutable container_id",
            "ownership_rule": "each native text object belongs exactly once to flow, table, or protected",
            "horizontal_anchor_rule": "semantic left anchors, table column boundaries, widths and horizontal order are immutable",
            "vertical_reflow_rule": "unused flow-to-table whitespace may be reclaimed by moving table regions upward and redistributing row height; table bottoms remain fixed",
            "patch_rule": "flow writes cannot enter target table bounds and table writes cannot leave transformed owned cells",
            "locked_object_rule": "non-table drawings and images remain locked; table-owned vector graphics may receive the same vertical transform as their table region",
            "source_is_immutable": True,
        },
    )
    (run_dir / "docs" / "README.md").write_text(
        f"# {page_id} P7 body.composite.flow_text_table 运行包\n\n"
        "原页快照位于 `input/source.pdf`；唯一所有权、整页翻译请求位于 `input/`；"
        "分区布局和候选页位于 `output/`；跨区门禁与双裁决位于 `reports/`。\n",
        encoding="utf-8",
    )

    trace: list[dict[str, str]] = [{"state": "SAMPLE_READY", "owner": "p7_orchestrator"}]
    failure_owner: str | None = None
    candidate_pdf: Path | None = None
    try:
        facts = extract_page_facts(source_snapshot, page_id=page_id)
        write_json(run_dir / "input" / "page_facts.json", facts)
        trace.append({"state": "FACTS_READY", "owner": "shared_pdf_kernel"})

        template = build_composite_template(source_snapshot, facts)
        write_json(run_dir / "input" / "page_template.json", template)
        trace.append({"state": "TEMPLATE_READY", "owner": "composite_template_builder"})

        request = build_translation_request(
            template,
            source_language=source_language,
            target_language=target_language,
        )
        write_json(run_dir / "input" / "translation_request.json", request)
        bundle, translation_retries = translate_with_targeted_guard_retry(provider, request)
        write_json(run_dir / "output" / "translation_bundle.json", bundle)
        write_json(run_dir / "reports" / "translation_retry_trace.json", translation_retries)
        trace.append({"state": "TRANSLATION_READY", "owner": "translation_provider"})

        plan, layout_findings, plan_evidence = plan_composite_layout(
            facts=facts,
            template=template,
            translations=bundle,
            source_language=source_language,
            target_language=target_language,
            font_file=font_file,
            bold_font_file=bold_font_file,
        )
        write_json(run_dir / "output" / "layout_plan.json", plan)
        write_json(run_dir / "reports" / "layout_findings.json", layout_findings)
        write_json(run_dir / "reports" / "layout_attempts.json", plan_evidence)
        trace.append({"state": "PATCH_READY", "owner": "composite_layout_planner"})

        hard_layout = tuple(item for item in layout_findings if item.severity == "HARD")
        if hard_layout:
            decision = CompositeDecision(page_id, "PASS", "FAIL", "QUALITY_FAILED", layout_findings)
        else:
            candidate_pdf = run_dir / "output" / "candidate.pdf"
            render_findings, render_evidence = render_composite_candidate(
                source_pdf=source_snapshot,
                candidate_pdf=candidate_pdf,
                facts=facts,
                template=template,
                plan=plan,
                evidence_dir=run_dir / "previews",
            )
            repaired_plan, rule_repair_records = repair_horizontal_table_rule_overlaps(
                plan,
                render_findings,
            )
            if rule_repair_records:
                plan = repaired_plan
                write_json(run_dir / "output" / "layout_plan.json", plan)
                render_findings, render_evidence = render_composite_candidate(
                    source_pdf=source_snapshot,
                    candidate_pdf=candidate_pdf,
                    facts=facts,
                    template=template,
                    plan=plan,
                    evidence_dir=run_dir / "previews",
                )
                render_evidence["post_render_repairs"] = rule_repair_records
            write_json(run_dir / "reports" / "render_evidence.json", render_evidence)
            trace.append({"state": "CANDIDATE_READY", "owner": "composite_pdf_renderer"})

            flow_findings: list[CompositeFinding] = []
            for region, flow_plan in zip(template.flow_regions, plan.flow_plans):
                flow_decision = judge_p4_candidate(
                    candidate_pdf=candidate_pdf,
                    template=region.template,
                    plan=flow_plan,
                )
                flow_findings.extend(
                    CompositeFinding(
                        item.code,
                        item.severity,
                        item.owner,
                        item.container_id,
                        item.message,
                        {"region_id": region.region_id},
                    )
                    for item in flow_decision.findings
                )
            findings = _deduplicate(tuple(render_findings) + tuple(flow_findings))
            process_findings = tuple(item for item in findings if item.code in _PROCESS_FAILURE_CODES)
            capability_findings = tuple(item for item in findings if item.code == "FONT_NOT_EMBEDDED")
            product_findings = tuple(
                item for item in findings
                if item.severity == "HARD" and item not in process_findings and item not in capability_findings
            )
            if process_findings:
                decision = CompositeDecision(page_id, "FAIL", "NOT_REACHED", "PROCESS_FAILED", findings)
            elif capability_findings:
                decision = CompositeDecision(page_id, "PASS", "NOT_REACHED", "CAPABILITY_FAILED", findings)
            elif product_findings:
                decision = CompositeDecision(page_id, "PASS", "FAIL", "QUALITY_FAILED", findings)
            else:
                decision = CompositeDecision(page_id, "PASS", "PASS", "PAGE_PASSED", findings)
        write_json(run_dir / "reports" / "quality_decision.json", decision)
        trace.append({"state": "QUALITY_DECIDED", "owner": "composite_quality_judge"})
        trace.append({"state": decision.terminal_state, "owner": "composite_quality_judge"})
    except (CompositeCapabilityError, ProviderError) as exc:
        failure_owner = _owner(trace)
        code = exc.code if isinstance(exc, ProviderError) else str(exc).split(":", 1)[0]
        finding = CompositeFinding(code, "HARD", failure_owner, None, str(exc), {})
        decision = CompositeDecision(page_id, "PASS", "NOT_REACHED", "CAPABILITY_FAILED", (finding,))
        write_json(run_dir / "reports" / "quality_decision.json", decision)
        trace.append({"state": "CAPABILITY_FAILED", "owner": failure_owner})
    except Exception as exc:
        failure_owner = _owner(trace)
        finding = CompositeFinding(type(exc).__name__, "HARD", failure_owner, None, str(exc), {})
        decision = CompositeDecision(page_id, "FAIL", "NOT_REACHED", "PROCESS_FAILED", (finding,))
        write_json(run_dir / "reports" / "quality_decision.json", decision)
        trace.append({"state": "PROCESS_FAILED", "owner": failure_owner})

    if sha256_file(source_pdf) != source_hash:
        raise RuntimeError("upstream_sample_changed_during_run")
    write_json(run_dir / "reports" / "state_trace.json", trace)
    result = P7RunResult(
        page_id,
        str(run_dir),
        str(candidate_pdf) if candidate_pdf and candidate_pdf.is_file() else None,
        decision.process_verdict,
        decision.product_verdict,
        decision.terminal_state,
        failure_owner,
    )
    write_json(run_dir / "reports" / "run_result.json", result)
    return result


def _owner(trace: list[dict[str, str]]) -> str:
    return {
        "SAMPLE_READY": "shared_pdf_kernel",
        "FACTS_READY": "composite_template_builder",
        "TEMPLATE_READY": "translation_provider",
        "TRANSLATION_READY": "composite_layout_planner",
        "PATCH_READY": "composite_pdf_renderer",
        "CANDIDATE_READY": "composite_quality_judge",
    }.get(trace[-1]["state"] if trace else "", "p7_orchestrator")


def _deduplicate(findings: tuple[CompositeFinding, ...]) -> tuple[CompositeFinding, ...]:
    output = []
    seen = set()
    for finding in findings:
        key = (finding.code, finding.container_id, finding.message)
        if key in seen:
            continue
        seen.add(key)
        output.append(finding)
    return tuple(output)
