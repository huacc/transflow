from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

import fitz

from page_toolbox_puncture.contracts import PageTranslationRequest, TranslationUnit, write_json
from page_toolbox_puncture.sample_snapshot import sha256_file
from page_toolbox_puncture.translation import ProviderError, TranslationProvider
from shared_pdf_kernel.facts import extract_page_facts
from toolboxes.body.chart.tools.engine import build_chart_translation_request, translation_validation
from toolboxes.body.chart.tools.layout_planner import (
    layout_rule_trace,
    materialize_translated_diagnostic_plan,
    plan_chart_layout,
)
from toolboxes.body.chart.tools.models import ChartDecision, ChartFinding
from toolboxes.body.chart.tools.renderer import render_chart_candidate
from toolboxes.body.chart.tools.template_builder import build_chart_template

from .models import ChartTableTemplate
from .template_builder import ChartTableCapabilityError, build_chart_table_template


@dataclass(frozen=True)
class P16RunResult:
    page_id: str
    run_dir: str
    candidate_pdf: str | None
    process_verdict: str
    product_verdict: str
    terminal_state: str
    failure_owner: str | None
    chart_region_count: int
    table_region_count: int
    chart_container_count: int
    table_container_count: int
    shared_container_count: int
    requested_container_count: int
    protected_object_count: int


_PROCESS_FAILURE_CODES = {
    "CHART_DATA_VISUAL_CHANGED",
    "CHART_PROTECTED_TEXT_CHANGED",
    "CHART_OUTSIDE_TEXT_REGION_CHANGED",
}
_CAPABILITY_FAILURE_CODES = {
    "FONT_NOT_EMBEDDED",
    "FONT_GLYPH_MISSING",
    "CHART_TEXT_SLOT_OVERFLOW",
    "CHART_SAFE_REDACTION_REGION_NOT_FOUND",
    "TRANSLATION_REQUIRED_LITERAL_MISSING",
    "TRANSLATION_SOURCE_LANGUAGE_RESIDUE",
    "TRANSLATION_MAGNITUDE_UNIT_MISMATCH",
    "TRANSLATION_PLACEHOLDER_OUTPUT",
    "TRANSLATION_INADEQUATE_OUTPUT",
}


def build_chart_table_translation_request(
    template: ChartTableTemplate,
    source_language: str,
    target_language: str,
) -> PageTranslationRequest | None:
    units = tuple(
        TranslationUnit(
            container_id=container.container_id,
            source_text=container.source_text,
            reading_order=index,
            required_literals=container.required_literals,
        )
        for index, container in enumerate(
            item for item in template.containers if _requires_translation(item.source_text, target_language)
        )
    )
    if not units:
        return None
    return PageTranslationRequest(
        request_id=f"p16-{template.page_id}-{source_language}-{target_language}",
        page_id=template.page_id,
        source_language=source_language,
        target_language=target_language,
        units=units,
    )


def _requires_translation(text: str, target_language: str) -> bool:
    if target_language.lower().startswith("zh"):
        return bool(re.search(r"[A-Za-z]", text)) and not bool(re.search(r"[\u3400-\u9fff]", text))
    if target_language.lower().startswith("en"):
        return bool(re.search(r"[\u3400-\u9fff]", text))
    return bool(re.search(r"[A-Za-z\u3400-\u9fff]", text))


def run_p16_page(
    *,
    source_pdf: Path,
    page_id: str,
    run_dir: Path,
    provider: TranslationProvider,
    font_file: str,
    bold_font_file: str | None,
    source_language: str,
    target_language: str,
) -> P16RunResult:
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
            "schema_version": "p16-body-composite-chart-table-page-run/v1",
            "toolbox_key": "body.composite.chart_table",
            "page_id": page_id,
            "source_language": source_language,
            "target_language": target_language,
            "source_snapshot": "input/source.pdf",
            "source_sha256": source_hash,
            "candidate_output": "output/candidate.pdf",
            "translation_rule": "one page-level request over stable chart, table, and shared semantic container IDs",
            "ownership_rule": "every native text object has exactly one chart, table, shared, or protected owner",
            "immutable_rule": "chart data visuals, table rules, numeric anchors, drawings, images, and source PDF are immutable",
            "cross_region_rule": "chart placements cannot invade table regions and table placements cannot escape them",
            "prerequisite_gates": {"P6_body_table": "NOT_PROMOTED", "P13_body_chart": "NOT_PROMOTED"},
            "formal_promotion_eligible": False,
        },
    )
    (run_dir / "docs" / "README.md").write_text(
        f"# {page_id} P16 body.composite.chart_table run\n\n"
        "This package freezes source facts, the composite ownership map, one page-level translation request, "
        "the layout plan, the candidate PDF, and mechanical quality evidence. P6/P13 are not promoted, so "
        "this run is puncture evidence and cannot create a P16 promotion manifest.\n",
        encoding="utf-8",
    )

    trace: list[dict[str, str]] = [{"state": "SAMPLE_READY", "owner": "p16_orchestrator"}]
    counts = {"chart_regions": 0, "table_regions": 0, "chart": 0, "table": 0, "shared": 0, "requested": 0, "protected": 0}
    candidate_pdf: Path | None = None
    failure_owner: str | None = None
    translation_fallback_mode: str | None = None
    try:
        facts = extract_page_facts(source_snapshot, page_id=page_id)
        write_json(run_dir / "input" / "page_facts.json", facts)
        trace.append({"state": "FACTS_READY", "owner": "shared_pdf_kernel"})

        template = build_chart_table_template(source_snapshot, facts)
        counts.update(
            {
                "chart_regions": len(template.chart_regions),
                "table_regions": len(template.table_regions),
                "chart": sum(item.owner == "chart" for item in template.containers),
                "table": sum(item.owner == "table" for item in template.containers),
                "shared": sum(item.owner == "shared" for item in template.containers),
                "protected": len(template.protected_object_ids),
            }
        )
        write_json(run_dir / "input" / "page_template.json", template)
        write_json(
            run_dir / "reports" / "ownership_audit.json",
            {
                "status": "PASS",
                "owner_counts": {key: counts[key] for key in ("chart", "table", "shared", "protected")},
                "native_text_object_count": len(facts.text_objects),
                "owned_text_object_count": sum(len(item.source_object_ids) for item in template.containers) + len(template.protected_object_ids),
                "table_structure_sha256": template.table_template.structure.structure_sha256,
            },
        )
        trace.append({"state": "TEMPLATE_READY", "owner": "chart_table_template_builder"})

        request = build_chart_table_translation_request(template, source_language, target_language)
        if request is None:
            raise ChartTableCapabilityError("NO_TRANSLATABLE_COMPOSITE_TEXT")
        counts["requested"] = len(request.units)
        write_json(run_dir / "input" / "translation_request.json", request)

        bundle = provider.translate(request)
        bundle.validate_against(request)
        write_json(run_dir / "output" / "translation_bundle.raw.json", bundle)
        validation = translation_validation(request, bundle)
        write_json(run_dir / "reports" / "translation_validation.json", validation)
        validation_findings = _translation_findings(validation)
        if validation_findings:
            trace.append({"state": "TRANSLATION_REJECTED", "owner": "translation_provider"})
        else:
            write_json(run_dir / "output" / "translation_bundle.json", bundle)
            trace.append({"state": "TRANSLATION_READY", "owner": "translation_provider"})

        chart_template = template.as_chart_template()
        plan, plan_findings = plan_chart_layout(
            chart_template,
            bundle,
            font_file=font_file,
            bold_font_file=bold_font_file,
        )
        owner_findings = _owner_boundary_findings(template, plan)
        write_json(run_dir / "output" / "layout_plan.json", plan)
        write_json(run_dir / "reports" / "layout_findings.json", (*plan_findings, *owner_findings))
        write_json(run_dir / "reports" / "layout_rule_trace.json", layout_rule_trace(chart_template, plan))
        trace.append({"state": "PATCH_READY", "owner": "chart_table_layout_planner"})

        render_template = chart_template
        render_plan = plan
        unfit = [item.container_id for item in plan.placements if not item.fit]
        diagnostic_materialization: tuple[dict[str, object], ...] = ()
        if unfit:
            render_template, render_plan, diagnostic_materialization = materialize_translated_diagnostic_plan(
                chart_template,
                plan,
            )
            write_json(run_dir / "output" / "diagnostic_layout_plan.json", render_plan)
            write_json(run_dir / "reports" / "diagnostic_layout_trace.json", diagnostic_materialization)
        candidate_pdf = run_dir / "output" / "candidate.pdf"
        render_findings, render_evidence = render_chart_candidate(
            source_pdf=source_snapshot,
            candidate_pdf=candidate_pdf,
            facts=facts,
            template=render_template,
            plan=render_plan,
            evidence_dir=run_dir / "previews",
        )
        render_evidence.update(
            {
                "composite_owner_boundary_status": "FAIL" if owner_findings else "PASS",
                "table_structure_sha256": template.table_template.structure.structure_sha256,
                "table_rules_immutable": True,
                "diagnostic_candidate": bool(validation_findings or plan_findings or owner_findings),
                "translated_unfit_container_ids": unfit,
                "diagnostic_materialization": diagnostic_materialization,
            }
        )
        write_json(run_dir / "reports" / "render_evidence.json", render_evidence)
        trace.append({"state": "CANDIDATE_READY", "owner": "chart_table_pdf_renderer"})

        decision = _decision(
            page_id,
            (*validation_findings, *plan_findings, *owner_findings, *render_findings),
        )
        write_json(run_dir / "reports" / "quality_decision.json", decision)
        trace.extend(
            [
                {"state": "QUALITY_DECIDED", "owner": "chart_table_quality_judge"},
                {"state": decision.terminal_state, "owner": "chart_table_quality_judge"},
            ]
        )
        if decision.terminal_state != "PAGE_PASSED":
            failure_owner = decision.findings[0].owner if decision.findings else "chart_table_quality_judge"
    except ChartTableCapabilityError as exc:
        failure_owner = _next_owner(trace)
        finding = ChartFinding(
            str(exc).split(":", 1)[0],
            "HARD",
            failure_owner,
            None,
            None,
            str(exc),
            {},
        )
        trace.append({"state": "COMPOSITE_TEMPLATE_REJECTED", "owner": failure_owner})
        translation_fallback_mode = "chart_native_text"
        try:
            candidate_pdf, decision, fallback_counts = _render_chart_native_text_diagnostic(
                source_snapshot=source_snapshot,
                facts=facts,
                page_id=page_id,
                run_dir=run_dir,
                provider=provider,
                font_file=font_file,
                bold_font_file=bold_font_file,
                source_language=source_language,
                target_language=target_language,
                boundary_finding=finding,
                trace=trace,
            )
            counts.update(fallback_counts)
        except ProviderError as fallback_exc:
            failure_owner = "translation_provider"
            provider_finding = ChartFinding(
                fallback_exc.code,
                "HARD",
                failure_owner,
                None,
                None,
                str(fallback_exc),
                {},
            )
            decision = ChartDecision(
                page_id,
                "PASS",
                "NOT_REACHED",
                "CAPABILITY_FAILED",
                (finding, provider_finding),
            )
            trace.append({"state": "CAPABILITY_FAILED", "owner": failure_owner})
        write_json(run_dir / "reports" / "quality_decision.json", decision)
        if trace[-1]["state"] != decision.terminal_state:
            trace.append({"state": decision.terminal_state, "owner": failure_owner})
    except ProviderError as exc:
        failure_owner = _next_owner(trace)
        finding = ChartFinding(exc.code, "HARD", failure_owner, None, None, str(exc), {})
        decision = ChartDecision(page_id, "PASS", "NOT_REACHED", "CAPABILITY_FAILED", (finding,))
        write_json(run_dir / "reports" / "quality_decision.json", decision)
        trace.append({"state": "CAPABILITY_FAILED", "owner": failure_owner})
    except Exception as exc:
        failure_owner = _next_owner(trace)
        finding = ChartFinding(type(exc).__name__, "HARD", failure_owner, None, None, str(exc), {})
        decision = ChartDecision(page_id, "FAIL", "NOT_REACHED", "PROCESS_FAILED", (finding,))
        write_json(run_dir / "reports" / "quality_decision.json", decision)
        trace.append({"state": "PROCESS_FAILED", "owner": failure_owner})

    return _finish_run(
        source_pdf,
        source_hash,
        run_dir,
        page_id,
        candidate_pdf,
        decision,
        trace,
        counts,
        failure_owner,
        translation_fallback_mode,
    )


def _render_chart_native_text_diagnostic(
    *,
    source_snapshot: Path,
    facts,
    page_id: str,
    run_dir: Path,
    provider: TranslationProvider,
    font_file: str,
    bold_font_file: str | None,
    source_language: str,
    target_language: str,
    boundary_finding: ChartFinding,
    trace: list[dict[str, str]],
) -> tuple[Path, ChartDecision, dict[str, int]]:
    template = build_chart_template(facts)
    request = build_chart_translation_request(template, source_language, target_language)
    if request is None:
        raise ChartTableCapabilityError("NO_TRANSLATABLE_NATIVE_TEXT_DIAGNOSTIC")
    write_json(run_dir / "input" / "diagnostic_page_template.json", template)
    write_json(run_dir / "input" / "translation_request.json", request)
    trace.append({"state": "DIAGNOSTIC_TEMPLATE_READY", "owner": "chart_template_builder"})

    bundle = provider.translate(request)
    bundle.validate_against(request)
    write_json(run_dir / "output" / "translation_bundle.raw.json", bundle)
    validation = translation_validation(request, bundle)
    write_json(run_dir / "reports" / "translation_validation.json", validation)
    validation_findings = _translation_findings(validation)
    if not validation_findings:
        write_json(run_dir / "output" / "translation_bundle.json", bundle)
    trace.append(
        {
            "state": "TRANSLATION_REJECTED" if validation_findings else "TRANSLATION_READY",
            "owner": "translation_provider",
        }
    )

    plan, plan_findings = plan_chart_layout(
        template,
        bundle,
        font_file=font_file,
        bold_font_file=bold_font_file,
    )
    write_json(run_dir / "output" / "layout_plan.json", plan)
    write_json(run_dir / "reports" / "layout_findings.json", plan_findings)
    write_json(run_dir / "reports" / "layout_rule_trace.json", layout_rule_trace(template, plan))
    trace.append({"state": "PATCH_READY", "owner": "chart_layout_planner"})

    render_template = template
    render_plan = plan
    unfit = [item.container_id for item in plan.placements if not item.fit]
    diagnostic_materialization: tuple[dict[str, object], ...] = ()
    if unfit:
        render_template, render_plan, diagnostic_materialization = materialize_translated_diagnostic_plan(
            template,
            plan,
        )
        write_json(run_dir / "output" / "diagnostic_layout_plan.json", render_plan)
        write_json(run_dir / "reports" / "diagnostic_layout_trace.json", diagnostic_materialization)
    candidate_pdf = run_dir / "output" / "candidate.pdf"
    render_findings, render_evidence = render_chart_candidate(
        source_pdf=source_snapshot,
        candidate_pdf=candidate_pdf,
        facts=facts,
        template=render_template,
        plan=render_plan,
        evidence_dir=run_dir / "previews",
    )
    render_evidence.update(
        {
            "translation_fallback_mode": "chart_native_text",
            "composite_owner_boundary_status": "FAIL",
            "table_rules_immutable": True,
            "diagnostic_candidate": True,
            "translated_unfit_container_ids": unfit,
            "diagnostic_materialization": diagnostic_materialization,
        }
    )
    write_json(run_dir / "reports" / "render_evidence.json", render_evidence)
    trace.append({"state": "CANDIDATE_READY", "owner": "chart_pdf_renderer"})

    findings = (boundary_finding, *validation_findings, *plan_findings, *render_findings)
    mechanical = _decision(page_id, findings)
    if mechanical.terminal_state == "PROCESS_FAILED":
        decision = mechanical
    else:
        decision = ChartDecision(page_id, "PASS", "NOT_REACHED", "CAPABILITY_FAILED", findings)
    return (
        candidate_pdf,
        decision,
        {
            "chart_regions": len(template.visual_regions),
            "table_regions": 0,
            "chart": len(template.containers),
            "table": 0,
            "shared": 0,
            "requested": len(request.units),
            "protected": len(template.protected_object_ids),
        },
    )


def _owner_boundary_findings(template, plan) -> tuple[ChartFinding, ...]:
    container_by_id = {item.container_id: item for item in template.containers}
    findings: list[ChartFinding] = []
    table_regions = tuple(item.bbox for item in template.table_regions)
    placements = [item for item in plan.placements if item.fit]
    for placement in placements:
        container = container_by_id[placement.container_id]
        if container.owner == "table" and not any(_contains(region, placement.output_bbox) for region in table_regions):
            findings.append(
                ChartFinding(
                    "TABLE_OWNER_ESCAPE",
                    "HARD",
                    "table_layout_planner",
                    container.association_id,
                    container.container_id,
                    "Table-owned translated text escaped its table region.",
                    {"output_bbox": placement.output_bbox, "table_regions": table_regions},
                )
            )
        if container.owner == "chart" and any(_intersection_area(region, placement.output_bbox) > 0.5 for region in table_regions):
            findings.append(
                ChartFinding(
                    "CHART_OWNER_INVADES_TABLE",
                    "HARD",
                    "chart_layout_planner",
                    container.association_id,
                    container.container_id,
                    "Chart-owned translated text invaded a table region.",
                    {"output_bbox": placement.output_bbox, "table_regions": table_regions},
                )
            )
    for index, left in enumerate(placements):
        left_owner = container_by_id[left.container_id].owner
        if left_owner == "shared":
            continue
        for right in placements[index + 1 :]:
            right_owner = container_by_id[right.container_id].owner
            if right_owner == "shared" or left_owner == right_owner:
                continue
            if _intersection_area(left.output_bbox, right.output_bbox) > 0.5:
                findings.append(
                    ChartFinding(
                        "CROSS_REGION_COLLISION",
                        "HARD",
                        "chart_table_layout_planner",
                        None,
                        left.container_id,
                        "Translated placements owned by different regions overlap.",
                        {"left": left.container_id, "right": right.container_id},
                    )
                )
    return tuple(findings)


def _translation_findings(validation: dict[str, object]) -> tuple[ChartFinding, ...]:
    checks = (
        ("TRANSLATION_REQUIRED_LITERAL_MISSING", "missing_required_literals"),
        ("TRANSLATION_SOURCE_LANGUAGE_RESIDUE", "source_language_residue"),
        ("TRANSLATION_MAGNITUDE_UNIT_MISMATCH", "magnitude_unit_mismatches"),
        ("TRANSLATION_PLACEHOLDER_OUTPUT", "placeholder_outputs"),
        ("TRANSLATION_INADEQUATE_OUTPUT", "inadequate_outputs"),
    )
    findings = []
    for code, key in checks:
        details = validation.get(key)
        if details:
            findings.append(
                ChartFinding(
                    code,
                    "HARD",
                    "translation_provider",
                    None,
                    None,
                    "Translation contract validation failed; candidate is diagnostic only.",
                    {"details": details},
                )
            )
    return tuple(findings)


def _decision(page_id: str, findings: tuple[ChartFinding, ...]) -> ChartDecision:
    if any(item.code in _PROCESS_FAILURE_CODES for item in findings):
        return ChartDecision(page_id, "FAIL", "NOT_REACHED", "PROCESS_FAILED", findings)
    if any(item.code in _CAPABILITY_FAILURE_CODES for item in findings):
        return ChartDecision(page_id, "PASS", "NOT_REACHED", "CAPABILITY_FAILED", findings)
    if findings:
        return ChartDecision(page_id, "PASS", "FAIL", "QUALITY_FAILED", findings)
    return ChartDecision(page_id, "PASS", "PASS", "PAGE_PASSED", ())


def _next_owner(trace: list[dict[str, str]]) -> str:
    return {
        "SAMPLE_READY": "shared_pdf_kernel",
        "FACTS_READY": "chart_table_template_builder",
        "TEMPLATE_READY": "translation_provider",
        "TRANSLATION_REJECTED": "chart_table_layout_planner",
        "TRANSLATION_READY": "chart_table_layout_planner",
        "PATCH_READY": "chart_table_pdf_renderer",
        "CANDIDATE_READY": "chart_table_quality_judge",
    }.get(trace[-1]["state"] if trace else "", "p16_orchestrator")


def _finish_run(
    source_pdf,
    source_hash,
    run_dir,
    page_id,
    candidate_pdf,
    decision,
    trace,
    counts,
    failure_owner,
    translation_fallback_mode=None,
):
    if sha256_file(source_pdf) != source_hash:
        raise RuntimeError("upstream_sample_changed_during_run")
    translated_candidate_ready = candidate_pdf is not None and _pdf_is_readable(candidate_pdf)
    if not translated_candidate_ready:
        candidate_pdf = None
    product_acceptance = decision.terminal_state == "PAGE_PASSED" and decision.product_verdict == "PASS"
    write_json(
        run_dir / "reports" / "candidate_artifact.json",
        {
            "artifact_kind": (
                "NO_TRANSLATED_CANDIDATE"
                if candidate_pdf is None
                else "PRODUCT_CANDIDATE" if product_acceptance else "TRANSLATED_DIAGNOSTIC_CANDIDATE"
            ),
            "diagnostic_candidate": not product_acceptance,
            "product_acceptance": product_acceptance,
            "terminal_state": decision.terminal_state,
            "process_verdict": decision.process_verdict,
            "product_verdict": decision.product_verdict,
            "failure_codes": [item.code for item in decision.findings],
            "source_sha256": source_hash,
            "candidate_sha256": sha256_file(candidate_pdf) if candidate_pdf is not None else None,
            "byte_identical_to_source": (
                sha256_file(candidate_pdf) == source_hash if candidate_pdf is not None else None
            ),
            "candidate_page_count": _pdf_page_count(candidate_pdf) if candidate_pdf is not None else 0,
            "translation_fallback_mode": translation_fallback_mode,
        },
    )
    write_json(run_dir / "reports" / "state_trace.json", trace)
    result = P16RunResult(
        page_id=page_id,
        run_dir=str(run_dir),
        candidate_pdf=str(candidate_pdf) if candidate_pdf is not None else None,
        process_verdict=decision.process_verdict,
        product_verdict=decision.product_verdict,
        terminal_state=decision.terminal_state,
        failure_owner=failure_owner,
        chart_region_count=counts["chart_regions"],
        table_region_count=counts["table_regions"],
        chart_container_count=counts["chart"],
        table_container_count=counts["table"],
        shared_container_count=counts["shared"],
        requested_container_count=counts["requested"],
        protected_object_count=counts["protected"],
    )
    write_json(run_dir / "reports" / "run_result.json", result)
    return result


def _pdf_is_readable(path: Path) -> bool:
    return path.is_file() and _pdf_page_count(path) > 0


def _pdf_page_count(path: Path) -> int:
    try:
        with fitz.open(path) as document:
            return document.page_count
    except (fitz.FileDataError, OSError, RuntimeError):
        return 0


def _contains(outer, inner, tolerance: float = 0.5) -> bool:
    return (
        inner[0] >= outer[0] - tolerance
        and inner[1] >= outer[1] - tolerance
        and inner[2] <= outer[2] + tolerance
        and inner[3] <= outer[3] + tolerance
    )


def _intersection_area(left, right) -> float:
    return max(0.0, min(left[2], right[2]) - max(left[0], right[0])) * max(
        0.0,
        min(left[3], right[3]) - max(left[1], right[1]),
    )
