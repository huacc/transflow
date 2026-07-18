from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, replace
from pathlib import Path

import fitz

from page_toolbox_puncture.contracts import (
    PageTranslationRequest,
    TranslationUnit,
    write_json,
)
from page_toolbox_puncture.sample_snapshot import sha256_file
from page_toolbox_puncture.translation import ProviderError, TranslationProvider
from shared_pdf_kernel.facts import extract_page_facts
from toolboxes.body.chart.tools.engine import translation_validation
from toolboxes.body.chart.tools.layout_planner import (
    layout_rule_trace,
    materialize_translated_diagnostic_plan,
)
from toolboxes.body.chart.tools.models import ChartDecision, ChartFinding
from toolboxes.body.chart.tools.renderer import render_chart_candidate

from .. import TOOLBOX_KEY
from .layout_planner import plan_flow_text_chart_layout
from .models import FlowTextChartTemplate
from .template_builder import (
    FlowTextChartCapabilityError,
    build_flow_text_chart_template,
)


@dataclass(frozen=True)
class P17RunResult:
    page_id: str
    run_dir: str
    candidate_pdf: str | None
    process_verdict: str
    product_verdict: str
    terminal_state: str
    failure_owner: str | None
    flow_region_count: int
    flow_container_count: int
    chart_container_count: int
    shared_container_count: int
    protected_object_count: int
    requested_container_count: int


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
    "TRANSLATION_CROSS_CONTAINER_DUPLICATE",
    "P17_FLOW_OWNER_ESCAPE",
    "P17_FLOW_OWNER_INVADES_CHART",
}


def build_flow_text_chart_translation_request(
    template: FlowTextChartTemplate,
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
            item
            for item in template.render_template.containers
            if _requires_translation(item.source_text, target_language)
        )
    )
    if not units:
        return None
    return PageTranslationRequest(
        request_id=f"p17-{template.page_id}-{source_language}-{target_language}",
        page_id=template.page_id,
        source_language=source_language,
        target_language=target_language,
        units=units,
    )


def run_p17_page(
    *,
    source_pdf: Path,
    page_id: str,
    run_dir: Path,
    provider: TranslationProvider,
    font_file: str,
    bold_font_file: str | None,
    source_language: str,
    target_language: str,
) -> P17RunResult:
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
            "schema_version": "p17-body-composite-flow-text-chart-page-run/v1",
            "toolbox_key": TOOLBOX_KEY,
            "page_id": page_id,
            "source_language": source_language,
            "target_language": target_language,
            "source_snapshot": "input/source.pdf",
            "source_sha256": source_hash,
            "candidate_output": "output/candidate.pdf",
            "ownership_rule": "every native text object has exactly one flow, chart, shared, or protected owner",
            "translation_rule": "one ordered page request over stable flow, chart, and shared container ids",
            "flow_rule": "flow text can reflow only inside its FlowBand and cannot cross a chart guard",
            "immutable_rule": "chart visuals, drawings, images, protected text, and source PDF are immutable",
            "render_rule": "all accepted placements are materialized in one source-PDF render pass",
            "prerequisite_gates": {
                "P4_body_flow_text_single": "ENGINEERING_ACCEPTED_NOT_FORMALLY_PROMOTED",
                "P5_body_flow_text_multi": "NOT_EVALUATED",
                "P13_body_chart": "PASS_NON_BLIND_NOT_FORMALLY_PROMOTED",
            },
            "formal_promotion_eligible": False,
        },
    )
    (run_dir / "docs" / "README.md").write_text(
        f"# {page_id} P17 body.composite.flow_text_chart run\n\n"
        "This package freezes source facts, exhaustive ownership, one page-level translation request, "
        "subplanner evidence, the single-pass translated candidate, and mechanical findings. "
        "Prerequisite promotion manifests are absent and the source pool is non-blind; this run cannot "
        "create a P17 promotion manifest.\n",
        encoding="utf-8",
    )

    trace: list[dict[str, str]] = [{"state": "SAMPLE_READY", "owner": "p17_orchestrator"}]
    counts = {
        "flow_regions": 0,
        "flow": 0,
        "chart": 0,
        "shared": 0,
        "protected": 0,
        "requested": 0,
    }
    candidate_pdf: Path | None = None
    failure_owner: str | None = None
    try:
        facts = extract_page_facts(source_snapshot, page_id=page_id)
        write_json(run_dir / "input" / "page_facts.json", facts)
        trace.append({"state": "FACTS_READY", "owner": "shared_pdf_kernel"})

        template = build_flow_text_chart_template(facts)
        owner_counts = {
            owner: sum(item.owner == owner for item in template.ownerships)
            for owner in ("flow", "chart", "shared", "protected")
        }
        container_counts = {
            owner: sum(item.owner == owner for item in template.container_ownerships)
            for owner in ("flow", "chart", "shared")
        }
        counts.update(
            {
                "flow_regions": len(template.flow_regions),
                "flow": container_counts["flow"],
                "chart": container_counts["chart"],
                "shared": container_counts["shared"],
                "protected": owner_counts["protected"],
            }
        )
        write_json(run_dir / "input" / "page_template.json", template)
        write_json(
            run_dir / "reports" / "ownership_audit.json",
            {
                "status": "PASS",
                "native_text_object_count": len(facts.text_objects),
                "owned_text_object_count": len(template.ownerships),
                "unique_owned_text_object_count": len({item.object_id for item in template.ownerships}),
                "object_owner_counts": owner_counts,
                "container_owner_counts": container_counts,
                "flow_region_modes": [item.mode for item in template.flow_regions],
                "chart_guard_regions": template.chart_guard_regions,
                "render_is_single_source_pass": True,
            },
        )
        trace.append({"state": "TEMPLATE_READY", "owner": "flow_text_chart_template_builder"})

        request = build_flow_text_chart_translation_request(
            template,
            source_language,
            target_language,
        )
        if request is None:
            raise FlowTextChartCapabilityError("P17_NO_TRANSLATABLE_NATIVE_TEXT")
        counts["requested"] = len(request.units)
        write_json(run_dir / "input" / "translation_request.json", request)
        write_json(
            run_dir / "reports" / "translation_request_audit.json",
            _translation_request_audit(template, request, target_language),
        )

        bundle = provider.translate(request)
        bundle.validate_against(request)
        write_json(run_dir / "output" / "translation_bundle.raw.json", bundle)
        provider_audit = getattr(provider, "last_audit", None)
        if provider_audit is not None:
            write_json(run_dir / "reports" / "translation_provider_audit.json", provider_audit)
        raw_validation = translation_validation(request, bundle)
        write_json(run_dir / "reports" / "translation_validation.raw.json", raw_validation)
        validation = _apply_provider_validation_audit(raw_validation, provider_audit)
        write_json(run_dir / "reports" / "translation_validation.json", validation)
        validation_findings = _translation_findings(validation)
        if validation_findings:
            trace.append({"state": "TRANSLATION_REJECTED", "owner": "translation_provider"})
        else:
            write_json(run_dir / "output" / "translation_bundle.json", bundle)
            trace.append({"state": "TRANSLATION_READY", "owner": "translation_provider"})

        layout, layout_findings, flow_attempts = plan_flow_text_chart_layout(
            facts=facts,
            template=template,
            bundle=bundle,
            source_language=source_language,
            target_language=target_language,
            font_file=font_file,
            bold_font_file=bold_font_file,
        )
        write_json(run_dir / "output" / "layout_plan.json", layout)
        write_json(run_dir / "reports" / "flow_layout_attempts.json", flow_attempts)
        write_json(run_dir / "reports" / "layout_findings.json", layout_findings)
        write_json(
            run_dir / "reports" / "layout_rule_trace.json",
            layout_rule_trace(template.render_template, layout.render_plan),
        )
        trace.append({"state": "PATCH_READY", "owner": "flow_text_chart_layout_planner"})

        render_template = template.render_template
        render_plan = layout.render_plan
        diagnostic_ids = {
            item.container_id
            for item in layout_findings
            if item.container_id is not None
        }
        if diagnostic_ids:
            render_plan = replace(
                render_plan,
                placements=tuple(
                    replace(item, fit=False)
                    if item.container_id in diagnostic_ids
                    else item
                    for item in render_plan.placements
                ),
            )
        unfit = [item.container_id for item in render_plan.placements if not item.fit]
        diagnostic_materialization: tuple[dict[str, object], ...] = ()
        if unfit:
            render_template, render_plan, diagnostic_materialization = materialize_translated_diagnostic_plan(
                render_template,
                render_plan,
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
                "single_source_render_pass": True,
                "flow_region_count": len(template.flow_regions),
                "chart_guard_regions": template.chart_guard_regions,
                "diagnostic_candidate": bool(validation_findings or layout_findings),
                "translated_unfit_container_ids": unfit,
                "diagnostic_materialization": diagnostic_materialization,
            }
        )
        write_json(run_dir / "reports" / "render_evidence.json", render_evidence)
        trace.append({"state": "CANDIDATE_READY", "owner": "flow_text_chart_pdf_renderer"})

        decision = _decision(
            page_id,
            (*validation_findings, *layout_findings, *render_findings),
        )
        write_json(run_dir / "reports" / "quality_decision.json", decision)
        trace.extend(
            [
                {"state": "QUALITY_DECIDED", "owner": "flow_text_chart_quality_judge"},
                {"state": decision.terminal_state, "owner": "flow_text_chart_quality_judge"},
            ]
        )
        if decision.terminal_state != "PAGE_PASSED":
            failure_owner = decision.findings[0].owner if decision.findings else "flow_text_chart_quality_judge"
    except (FlowTextChartCapabilityError, ProviderError) as exc:
        failure_owner = _next_owner(trace)
        code = exc.code if isinstance(exc, ProviderError) else str(exc).split(":", 1)[0]
        finding = ChartFinding(code, "HARD", failure_owner, None, None, str(exc), {})
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
    )


def _translation_request_audit(template, request, target_language: str) -> dict[str, object]:
    expected = [
        item.container_id
        for item in template.render_template.containers
        if _requires_translation(item.source_text, target_language)
    ]
    actual = [item.container_id for item in request.units]
    return {
        "status": "PASS" if actual == expected and len(actual) == len(set(actual)) else "FAIL",
        "expected_container_ids": expected,
        "actual_container_ids": actual,
        "duplicate_container_ids": sorted({item for item in actual if actual.count(item) > 1}),
        "missing_container_ids": [item for item in expected if item not in actual],
        "unexpected_container_ids": [item for item in actual if item not in expected],
        "ordered_page_request": actual == expected,
    }


def _apply_provider_validation_audit(
    validation: dict[str, object],
    provider_audit: object,
) -> dict[str, object]:
    if not isinstance(provider_audit, dict):
        return validation
    confirmed = {
        str(item)
        for item in provider_audit.get("confirmed_proper_name_ids", [])
    }
    residue = validation.get("source_language_residue")
    if not confirmed or not isinstance(residue, dict):
        return validation
    effective = dict(validation)
    effective["source_language_residue"] = {
        container_id: details
        for container_id, details in residue.items()
        if container_id not in confirmed
    }
    effective["confirmed_proper_name_ids"] = sorted(confirmed & set(residue))
    failure_keys = (
        "missing_required_literals",
        "source_language_residue",
        "placeholder_outputs",
        "inadequate_outputs",
        "magnitude_unit_mismatches",
    )
    effective["status"] = (
        "FAIL" if any(effective.get(key) for key in failure_keys) else "PASS"
    )
    return effective


def _requires_translation(text: str, target_language: str) -> bool:
    latin_count = len(re.findall(r"[A-Za-z]", text))
    cjk_count = len(re.findall(r"[\u3400-\u9fff]", text))
    if target_language.casefold().startswith("zh"):
        return latin_count > 0 and (
            cjk_count == 0 or latin_count >= max(6, cjk_count * 2)
        )
    if target_language.casefold().startswith("en"):
        return cjk_count > 0
    return latin_count > 0 or cjk_count > 0


def _translation_findings(validation: dict[str, object]) -> tuple[ChartFinding, ...]:
    checks = (
        ("TRANSLATION_REQUIRED_LITERAL_MISSING", "missing_required_literals"),
        ("TRANSLATION_SOURCE_LANGUAGE_RESIDUE", "source_language_residue"),
        ("TRANSLATION_MAGNITUDE_UNIT_MISMATCH", "magnitude_unit_mismatches"),
        ("TRANSLATION_PLACEHOLDER_OUTPUT", "placeholder_outputs"),
        ("TRANSLATION_INADEQUATE_OUTPUT", "inadequate_outputs"),
        ("TRANSLATION_CROSS_CONTAINER_DUPLICATE", "cross_container_duplicate_outputs"),
    )
    findings: list[ChartFinding] = []
    for code, key in checks:
        details = validation.get(key)
        if not details:
            continue
        container_id = next(iter(details), None) if isinstance(details, dict) and len(details) == 1 else None
        findings.append(
            ChartFinding(
                code,
                "HARD",
                "translation_provider",
                None,
                str(container_id) if container_id is not None else None,
                "Translation validation failed; candidate is diagnostic only.",
                {"details": details},
            )
        )
    return tuple(findings)


def _decision(page_id: str, findings: tuple[ChartFinding, ...]) -> ChartDecision:
    if any(item.code in _PROCESS_FAILURE_CODES for item in findings):
        return ChartDecision(page_id, "FAIL", "NOT_REACHED", "PROCESS_FAILED", findings)
    if any(
        item.code in _CAPABILITY_FAILURE_CODES or item.code.startswith("P4_")
        for item in findings
    ):
        return ChartDecision(page_id, "PASS", "NOT_REACHED", "CAPABILITY_FAILED", findings)
    if findings:
        return ChartDecision(page_id, "PASS", "FAIL", "QUALITY_FAILED", findings)
    return ChartDecision(page_id, "PASS", "PASS", "PAGE_PASSED", ())


def _next_owner(trace: list[dict[str, str]]) -> str:
    return {
        "SAMPLE_READY": "shared_pdf_kernel",
        "FACTS_READY": "flow_text_chart_template_builder",
        "TEMPLATE_READY": "translation_provider",
        "TRANSLATION_REJECTED": "flow_text_chart_layout_planner",
        "TRANSLATION_READY": "flow_text_chart_layout_planner",
        "PATCH_READY": "flow_text_chart_pdf_renderer",
        "CANDIDATE_READY": "flow_text_chart_quality_judge",
    }.get(trace[-1]["state"] if trace else "", "p17_orchestrator")


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
        },
    )
    write_json(run_dir / "reports" / "state_trace.json", trace)
    result = P17RunResult(
        page_id=page_id,
        run_dir=str(run_dir),
        candidate_pdf=str(candidate_pdf) if candidate_pdf is not None else None,
        process_verdict=decision.process_verdict,
        product_verdict=decision.product_verdict,
        terminal_state=decision.terminal_state,
        failure_owner=failure_owner,
        flow_region_count=counts["flow_regions"],
        flow_container_count=counts["flow"],
        chart_container_count=counts["chart"],
        shared_container_count=counts["shared"],
        protected_object_count=counts["protected"],
        requested_container_count=counts["requested"],
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
