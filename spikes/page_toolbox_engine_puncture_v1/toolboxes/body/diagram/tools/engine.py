from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from page_toolbox_puncture.contracts import (
    PageTranslationBundle,
    PageTranslationRequest,
    TranslationResult,
    TranslationUnit,
    write_json,
)
from page_toolbox_puncture.sample_snapshot import sha256_file
from page_toolbox_puncture.translation import ProviderError, TranslationProvider
from shared_pdf_kernel.facts import extract_page_facts

from .layout_planner import plan_diagram_layout
from .models import DiagramDecision, DiagramFinding, DiagramTemplate, P14RunResult
from .renderer import render_diagram_candidate, render_diagram_passthrough
from .template_builder import DiagramCapabilityError, build_diagram_template


_PROCESS_FAILURE_CODES = {
    "DIAGRAM_PASSTHROUGH_BYTES_CHANGED",
    "DIAGRAM_TOPOLOGY_CHANGED",
    "DIAGRAM_PROTECTED_TEXT_CHANGED",
    "DIAGRAM_OUTSIDE_ALLOWED_REGION_CHANGED",
    "DIAGRAM_LABEL_WRONG_OWNER",
    "DIAGRAM_TEXT_OUTSIDE_ALLOWED_REGION",
    "DIAGRAM_NODE_TEXT_OUTSIDE_NODE",
    "DIAGRAM_MAP_TEXT_COORDINATE_CHANGED",
}
_CAPABILITY_FAILURE_CODES = {
    "FONT_GLYPH_MISSING",
    "FONT_NOT_EMBEDDED",
    "DIAGRAM_NODE_TEXT_UNFIT",
    "DIAGRAM_LOCAL_TEXT_UNFIT",
    "DIAGRAM_SAFE_REDACTION_REGION_NOT_FOUND",
}


def build_diagram_translation_request(template: DiagramTemplate, source_language: str, target_language: str) -> PageTranslationRequest:
    if template.mode != "translated" or not template.containers:
        raise ValueError("diagram_translation_request_requires_translated_template")
    return PageTranslationRequest(
        request_id=f"p14-{template.page_id}-{source_language}-{target_language}",
        page_id=template.page_id,
        source_language=source_language,
        target_language=target_language,
        units=tuple(
            TranslationUnit(
                container_id=item.container_id,
                source_text=item.source_text,
                reading_order=item.reading_order,
                required_literals=item.required_literals,
            )
            for item in template.containers
        ),
    )


def run_p14_page(
    *,
    source_pdf: Path,
    page_id: str,
    run_dir: Path,
    provider: TranslationProvider,
    font_file: str,
    bold_font_file: str | None,
    source_language: str,
    target_language: str,
    font_candidates: tuple[str, ...] = (),
) -> P14RunResult:
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
            "schema_version": "p14-body-diagram-page-run/v1",
            "toolbox_key": "body.diagram",
            "page_id": page_id,
            "source_language": source_language,
            "target_language": target_language,
            "source_snapshot": "input/source.pdf",
            "source_sha256": source_hash,
            "candidate_output": "output/candidate.pdf",
            "ownership_rule": "every native text object belongs to one diagram owner or the protected set",
            "topology_rule": "nodes, connectors, arrows, images, colors, hierarchy, and spatial relations are immutable",
            "translation_rule": "one page-level request; model returns text only",
            "repair_rule": "fit within the existing owner or fail capability; never resize nodes or reroute connectors",
            "map_coordinate_rule": "map-like pages split text by source PDF block and keep translated text inside the exact source frame",
            "font_candidates": [font_file, *([bold_font_file] if bold_font_file else []), *font_candidates],
            "failure_output_rule": "always materialize output/candidate.pdf; use a partial review candidate or source passthrough without changing the failure verdict",
            "source_is_immutable": True,
        },
    )
    (run_dir / "docs" / "README.md").write_text(
        f"# {page_id} P14 body.diagram 运行包\n\n"
        "`input/` 保存源页、PDF 事实、拓扑模板和翻译请求；`output/` 保存译文、布局计划和候选页；"
        "`reports/` 与 `previews/` 保存状态、裁决和并排证据。无论 Gate 结论如何，`output/candidate.pdf` 都必须存在；"
        "失败页可能是部分翻译审阅稿或源页透传，具体见 `reports/gate_failures.json`。\n",
        encoding="utf-8",
    )

    trace: list[dict[str, str]] = [{"state": "SAMPLE_READY", "owner": "p14_orchestrator"}]
    candidate_pdf = run_dir / "output" / "candidate.pdf"
    failure_owner: str | None = None
    mode = "unknown"
    counts = {"nodes": 0, "connectors": 0, "containers": 0, "protected": 0}
    try:
        facts = extract_page_facts(source_snapshot, page_id=page_id)
        write_json(run_dir / "input" / "page_facts.json", facts)
        trace.append({"state": "FACTS_READY", "owner": "shared_pdf_kernel"})

        template = build_diagram_template(facts, source_snapshot)
        mode = template.mode
        counts = {
            "nodes": len(template.nodes),
            "connectors": len(template.connectors),
            "containers": len(template.containers),
            "protected": len(template.protected_object_ids),
        }
        write_json(run_dir / "input" / "page_template.json", template)
        write_json(
            run_dir / "reports" / "topology_evidence.json",
            {
                "topology_sha256": template.topology_sha256,
                "diagram_geometry_sha256": template.diagram_geometry_sha256,
                "locked_objects_sha256": facts.locked_objects_sha256,
                "node_count": len(template.nodes),
                "connector_count": len(template.connectors),
                "layout_strategy": template.layout_strategy,
                "nodes": template.nodes,
                "connectors": template.connectors,
            },
        )
        trace.append({"state": "TEMPLATE_READY", "owner": "diagram_template_builder"})

        if template.mode == "passthrough":
            render_findings, render_evidence = render_diagram_passthrough(
                source_pdf=source_snapshot,
                candidate_pdf=candidate_pdf,
                facts=facts,
                template=template,
                evidence_dir=run_dir / "previews",
            )
            write_json(run_dir / "reports" / "render_evidence.json", render_evidence)
            trace.append({"state": "CANDIDATE_READY", "owner": "diagram_pdf_renderer"})
            decision = _decision(page_id, render_findings, passthrough=True)
        else:
            pre_render_findings: list[DiagramFinding] = []
            request = build_diagram_translation_request(template, source_language, target_language)
            write_json(run_dir / "input" / "translation_request.json", request)
            initial_bundle = provider.translate(request)
            initial_bundle.validate_against(request)
            write_json(run_dir / "output" / "translation_bundle.raw.json", initial_bundle)
            bundle, repair_report = _repair_incomplete_translations(request, initial_bundle, provider)
            bundle.validate_against(request)
            write_json(run_dir / "reports" / "translation_repair.json", repair_report)
            missing_literals = _missing_required_literals(request, bundle)
            language_mismatches = _target_language_mismatches(request, bundle, source_language, target_language)
            integrity_issues = _translation_integrity_issues(request, bundle)
            write_json(
                run_dir / "reports" / "translation_validation.json",
                {
                    "status": "FAIL" if language_mismatches or integrity_issues else "WARN" if missing_literals else "PASS",
                    "missing_required_literals": missing_literals,
                    "missing_required_literals_are_advisory": True,
                    "target_language_mismatches": language_mismatches,
                    "translation_integrity_issues": integrity_issues,
                },
            )
            if language_mismatches:
                pre_render_findings.append(
                    DiagramFinding(
                        "TRANSLATION_TARGET_LANGUAGE_MISMATCH",
                        "HARD",
                        "translation_provider",
                        None,
                        None,
                        "译文仍主要使用源语言，目标语言未落地",
                        {"target_language_mismatches": language_mismatches},
                    )
                )
            if integrity_issues:
                pre_render_findings.append(
                    DiagramFinding(
                        "TRANSLATION_INCOMPLETE",
                        "HARD",
                        "translation_provider",
                        None,
                        None,
                        "译文长度与源文显著不相称，疑似被截断",
                        {"translation_integrity_issues": integrity_issues},
                    )
                )
            write_json(run_dir / "output" / "translation_bundle.json", bundle)
            trace.append({"state": "TRANSLATION_READY", "owner": "translation_provider"})

            plan, plan_findings = plan_diagram_layout(
                template,
                bundle,
                font_file=font_file,
                bold_font_file=bold_font_file,
                font_candidates=font_candidates,
            )
            write_json(run_dir / "output" / "layout_plan.json", plan)
            write_json(run_dir / "reports" / "layout_findings.json", plan_findings)
            trace.append({"state": "PATCH_READY", "owner": "diagram_layout_planner"})

            render_findings, render_evidence = render_diagram_candidate(
                source_pdf=source_snapshot,
                candidate_pdf=candidate_pdf,
                facts=facts,
                template=template,
                plan=plan,
                evidence_dir=run_dir / "previews",
                allow_partial=True,
            )
            write_json(run_dir / "reports" / "render_evidence.json", render_evidence)
            trace.append({"state": "CANDIDATE_READY", "owner": "diagram_pdf_renderer"})
            decision = _decision(page_id, tuple(pre_render_findings) + plan_findings + render_findings)

        write_json(run_dir / "reports" / "quality_decision.json", decision)
        trace.extend(
            [
                {"state": "QUALITY_DECIDED", "owner": "diagram_quality_judge"},
                {"state": decision.terminal_state, "owner": "diagram_quality_judge"},
            ]
        )
        if decision.terminal_state not in {"PAGE_PASSED", "PASSTHROUGH_PASSED"}:
            failure_owner = decision.findings[0].owner if decision.findings else "diagram_quality_judge"
    except (DiagramCapabilityError, ProviderError) as exc:
        failure_owner = _next_owner(trace)
        code = exc.code if isinstance(exc, ProviderError) else str(exc).split(":", 1)[0]
        finding = DiagramFinding(code, "HARD", failure_owner, None, None, str(exc), {})
        decision = DiagramDecision(page_id, "PASS", "NOT_REACHED", "CAPABILITY_FAILED", (finding,))
        _materialize_failure_passthrough(source_snapshot, candidate_pdf, run_dir, code)
        write_json(run_dir / "reports" / "quality_decision.json", decision)
        trace.append({"state": "CAPABILITY_FAILED", "owner": failure_owner})
    except Exception as exc:
        failure_owner = _next_owner(trace)
        finding = DiagramFinding(type(exc).__name__, "HARD", failure_owner, None, None, str(exc), {})
        decision = DiagramDecision(page_id, "FAIL", "NOT_REACHED", "PROCESS_FAILED", (finding,))
        _materialize_failure_passthrough(source_snapshot, candidate_pdf, run_dir, type(exc).__name__)
        write_json(run_dir / "reports" / "quality_decision.json", decision)
        trace.append({"state": "PROCESS_FAILED", "owner": failure_owner})

    return _finish_run(source_pdf, source_hash, run_dir, page_id, candidate_pdf, mode, decision, trace, counts, failure_owner)


def _decision(page_id: str, findings: tuple[DiagramFinding, ...], *, passthrough: bool = False) -> DiagramDecision:
    process = tuple(item for item in findings if item.code in _PROCESS_FAILURE_CODES)
    capability = tuple(item for item in findings if item.code in _CAPABILITY_FAILURE_CODES)
    product = tuple(item for item in findings if item not in process and item not in capability)
    if process:
        return DiagramDecision(page_id, "FAIL", "NOT_REACHED", "PROCESS_FAILED", findings)
    if capability:
        return DiagramDecision(page_id, "PASS", "NOT_REACHED", "CAPABILITY_FAILED", findings)
    if product:
        return DiagramDecision(page_id, "PASS", "FAIL", "QUALITY_FAILED", findings)
    terminal = "PASSTHROUGH_PASSED" if passthrough else "PAGE_PASSED"
    return DiagramDecision(page_id, "PASS", "PASS", terminal, ())


def _missing_required_literals(request, bundle) -> dict[str, list[str]]:
    translated = {item.container_id: item.translated_text for item in bundle.translations}
    return {
        unit.container_id: [literal for literal in unit.required_literals if literal not in translated[unit.container_id]]
        for unit in request.units
        if any(literal not in translated[unit.container_id] for literal in unit.required_literals)
    }


def _target_language_mismatches(request, bundle, source_language: str, target_language: str) -> dict[str, dict[str, int]]:
    source_by_id = {item.container_id: item.source_text for item in request.units}
    result = {}
    for item in bundle.translations:
        source_latin = sum(char.isascii() and char.isalpha() for char in source_by_id[item.container_id])
        source_cjk = sum("\u3400" <= char <= "\u9fff" for char in source_by_id[item.container_id])
        target_latin = sum(char.isascii() and char.isalpha() for char in item.translated_text)
        target_cjk = sum("\u3400" <= char <= "\u9fff" for char in item.translated_text)
        english_to_chinese_echo = (
            source_language.casefold().startswith("en")
            and target_language.casefold().startswith("zh")
            and source_latin >= 8
            and target_latin >= 8
            and target_cjk == 0
        )
        chinese_to_english_echo = (
            source_language.casefold().startswith("zh")
            and target_language.casefold().startswith("en")
            and source_cjk >= 4
            and target_cjk >= 4
            and target_latin < 4
        )
        if english_to_chinese_echo or chinese_to_english_echo:
            result[item.container_id] = {
                "source_latin_count": source_latin,
                "source_cjk_count": source_cjk,
                "target_latin_count": target_latin,
                "target_cjk_count": target_cjk,
            }
    return result


def _translation_integrity_issues(request, bundle) -> dict[str, dict[str, object]]:
    source_by_id = {item.container_id: item.source_text for item in request.units}
    source_language = request.source_language.casefold()
    target_language = request.target_language.casefold()
    issues = {}
    for item in bundle.translations:
        source = source_by_id[item.container_id]
        source_latin = sum(char.isascii() and char.isalpha() for char in source)
        source_cjk = sum("\u3400" <= char <= "\u9fff" for char in source)
        target_latin = sum(char.isascii() and char.isalpha() for char in item.translated_text)
        target_cjk = sum("\u3400" <= char <= "\u9fff" for char in item.translated_text)
        if source_language.startswith("zh") and target_language.startswith("en"):
            suspicious = source_cjk >= 20 and target_latin < source_cjk * 1.2
            ratio = target_latin / source_cjk if source_cjk else 0.0
        elif source_language.startswith("en") and target_language.startswith("zh"):
            suspicious = source_latin >= 40 and target_cjk < source_latin * 0.08
            ratio = target_cjk / source_latin if source_latin else 0.0
        else:
            suspicious = False
            ratio = 1.0
        if suspicious:
            issues[item.container_id] = {
                "source_latin_count": source_latin,
                "source_cjk_count": source_cjk,
                "target_latin_count": target_latin,
                "target_cjk_count": target_cjk,
                "target_to_source_script_ratio": round(ratio, 4),
            }
    return issues


def _repair_incomplete_translations(request, bundle, provider):
    initial_issues = _translation_integrity_issues(request, bundle)
    report = {
        "attempted": False,
        "segmented_attempted": False,
        "initial_issue_container_ids": list(initial_issues),
        "repaired_container_ids": [],
        "remaining_issue_container_ids": list(initial_issues),
        "provider_error": None,
    }
    if not initial_issues or getattr(provider, "provider_name", "") != "qwen":
        return bundle, report

    repair_units = tuple(unit for unit in request.units if unit.container_id in initial_issues)
    repair_request = PageTranslationRequest(
        request_id=f"{request.request_id}-integrity-repair",
        page_id=request.page_id,
        source_language=request.source_language,
        target_language=request.target_language,
        units=repair_units,
    )
    report["attempted"] = True
    try:
        repair_bundle = provider.translate(repair_request)
        repair_bundle.validate_against(repair_request)
    except ProviderError as exc:
        report["provider_error"] = exc.code
        return bundle, report

    replacements = {item.container_id: item for item in repair_bundle.translations}
    merged = PageTranslationBundle(
        request_id=request.request_id,
        page_id=request.page_id,
        provider=bundle.provider,
        model=bundle.model,
        translations=tuple(replacements.get(item.container_id, item) for item in bundle.translations),
        provider_request_id=",".join(
            value
            for value in (bundle.provider_request_id, repair_bundle.provider_request_id)
            if value
        ) or None,
        latency_ms=sum(value for value in (bundle.latency_ms, repair_bundle.latency_ms) if value is not None) or None,
        response_sha256=None,
    )
    remaining = _translation_integrity_issues(request, merged)
    if remaining:
        source_by_id = {item.container_id: item for item in request.units}
        segmented_replacements = {}
        for repair_index, container_id in enumerate(remaining):
            segmented_units = _segmented_repair_units(source_by_id[container_id])
            if len(segmented_units) < 2:
                continue
            report["segmented_attempted"] = True
            segmented_request = PageTranslationRequest(
                request_id=f"{request.request_id}-segmented-repair-{repair_index:02d}",
                page_id=request.page_id,
                source_language=request.source_language,
                target_language=request.target_language,
                units=segmented_units,
            )
            try:
                segmented_bundle = provider.translate(segmented_request)
                segmented_bundle.validate_against(segmented_request)
            except ProviderError as exc:
                report["provider_error"] = exc.code
                continue
            segmented_replacements[container_id] = TranslationResult(
                container_id,
                " ".join(item.translated_text.strip() for item in segmented_bundle.translations),
            )
        if segmented_replacements:
            merged = PageTranslationBundle(
                request_id=request.request_id,
                page_id=request.page_id,
                provider=merged.provider,
                model=merged.model,
                translations=tuple(segmented_replacements.get(item.container_id, item) for item in merged.translations),
                provider_request_id=merged.provider_request_id,
                latency_ms=merged.latency_ms,
                response_sha256=None,
            )
            remaining = _translation_integrity_issues(request, merged)
    report["repaired_container_ids"] = sorted(set(initial_issues) - set(remaining))
    report["remaining_issue_container_ids"] = list(remaining)
    return merged, report


def _segmented_repair_units(unit: TranslationUnit) -> tuple[TranslationUnit, ...]:
    parts = [part.strip() for part in re.split(r"(?<=[。！？；;.!?])\s*|\n+", unit.source_text) if part.strip()]
    if len(parts) < 2 and len(unit.source_text) >= 30:
        parts = [part.strip() for part in re.split(r"(?<=[，,])\s*", unit.source_text) if part.strip()]
    if len(parts) < 2:
        return ()
    return tuple(
        TranslationUnit(
            container_id=f"{unit.container_id}/segment-{index:02d}",
            source_text=part,
            reading_order=index,
            required_literals=tuple(literal for literal in unit.required_literals if literal in part),
        )
        for index, part in enumerate(parts)
    )


def _next_owner(trace: list[dict[str, str]]) -> str:
    return {
        "SAMPLE_READY": "shared_pdf_kernel",
        "FACTS_READY": "diagram_template_builder",
        "TEMPLATE_READY": "translation_provider",
        "TRANSLATION_READY": "diagram_layout_planner",
        "PATCH_READY": "diagram_pdf_renderer",
        "CANDIDATE_READY": "diagram_quality_judge",
    }.get(trace[-1]["state"] if trace else "", "p14_orchestrator")


def _finish_run(source_pdf, source_hash, run_dir, page_id, candidate_pdf, mode, decision, trace, counts, failure_owner=None):
    if sha256_file(source_pdf) != source_hash:
        raise RuntimeError("upstream_sample_changed_during_run")
    write_json(run_dir / "reports" / "state_trace.json", trace)
    _write_gate_failures(run_dir, decision)
    result = P14RunResult(
        page_id=page_id,
        run_dir=str(run_dir),
        candidate_pdf=str(candidate_pdf) if candidate_pdf and candidate_pdf.is_file() else None,
        mode=mode,
        process_verdict=decision.process_verdict,
        product_verdict=decision.product_verdict,
        terminal_state=decision.terminal_state,
        failure_owner=failure_owner,
        node_count=counts["nodes"],
        connector_count=counts["connectors"],
        container_count=counts["containers"],
        protected_object_count=counts["protected"],
    )
    write_json(run_dir / "reports" / "run_result.json", result)
    return result


def _materialize_failure_passthrough(source_snapshot: Path, candidate_pdf: Path, run_dir: Path, reason: str) -> None:
    if not candidate_pdf.is_file():
        shutil.copy2(source_snapshot, candidate_pdf)
    write_json(
        run_dir / "reports" / "failure_fallback.json",
        {
            "strategy": "SOURCE_PASSTHROUGH_ON_FAILURE",
            "reason": reason,
            "source_sha256": sha256_file(source_snapshot),
            "candidate_sha256": sha256_file(candidate_pdf),
            "byte_identical": sha256_file(source_snapshot) == sha256_file(candidate_pdf),
        },
    )


def _write_gate_failures(run_dir: Path, decision: DiagramDecision) -> None:
    fallback_path = run_dir / "reports" / "failure_fallback.json"
    render_path = run_dir / "reports" / "render_evidence.json"
    if fallback_path.is_file():
        candidate_kind = "SOURCE_PASSTHROUGH_ON_FAILURE"
    elif render_path.is_file():
        candidate_kind = str(json.loads(render_path.read_text(encoding="utf-8")).get("candidate_kind") or "FULL_TRANSLATION_CANDIDATE")
    else:
        candidate_kind = "FULL_TRANSLATION_CANDIDATE"
    failures = [
        {
            "code": finding.code,
            "severity": finding.severity,
            "owner": finding.owner,
            "node_id": finding.node_id,
            "container_id": finding.container_id,
            "message": finding.message,
            "evidence": finding.evidence,
            "repair_disposition": _repair_disposition(finding.code),
        }
        for finding in decision.findings
    ]
    write_json(
        run_dir / "reports" / "gate_failures.json",
        {
            "schema_version": "p14-gate-failures/v1",
            "terminal_state": decision.terminal_state,
            "candidate_kind": candidate_kind,
            "candidate_pdf": "output/candidate.pdf",
            "requires_user_judgment": bool(failures),
            "failures": failures,
        },
    )


def _repair_disposition(code: str) -> str:
    if code in {"DIAGRAM_TEXT_OWNER_COLLISION", "DIAGRAM_CONNECTOR_TEXT_COLLISION", "DIAGRAM_TRANSLATION_MISSING"}:
        return "TIGHT_GLYPH_BBOX_REPAIR_APPLIED_REVIEW_IF_REMAINING"
    if code in {"DIAGRAM_NODE_TEXT_UNFIT", "DIAGRAM_LOCAL_TEXT_UNFIT", "FONT_GLYPH_MISSING"}:
        return "UNFIT_OWNER_PRESERVED_FROM_SOURCE_IN_PARTIAL_REVIEW_CANDIDATE"
    if code == "TRANSLATION_REQUIRED_LITERAL_MISSING":
        return "BEST_EFFORT_CANDIDATE_RENDERED_REQUIRES_USER_JUDGMENT"
    if code == "TRANSLATION_TARGET_LANGUAGE_MISMATCH":
        return "SOURCE_TEXT_RECOVERY_APPLIED_RETRY_TRANSLATION_AND_REVIEW"
    return "SOURCE_PASSTHROUGH_OR_UNRESOLVED_REQUIRES_USER_JUDGMENT"
