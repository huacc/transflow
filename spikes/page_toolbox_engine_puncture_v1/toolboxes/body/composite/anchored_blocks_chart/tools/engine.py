from __future__ import annotations

import shutil
from pathlib import Path

from page_toolbox_puncture.contracts import write_json
from page_toolbox_puncture.sample_snapshot import sha256_file
from page_toolbox_puncture.translation import ProviderError, TranslationProvider
from shared_pdf_kernel.facts import extract_page_facts
from toolboxes.body.chart.tools.engine import translation_validation

from .layout_planner import plan_composite_layout
from .models import CompositeDecision, CompositeFinding, P15RunResult
from .renderer import render_composite_candidate
from .template_builder import CompositeCapabilityError, build_composite_template
from .translation_request import build_translation_request


_PROCESS_CODES = {
    "CHART_DATA_VISUAL_CHANGED",
    "CHART_PROTECTED_TEXT_CHANGED",
    "CHART_OUTSIDE_TEXT_REGION_CHANGED",
    "SOURCE_SNAPSHOT_CHANGED",
}
_CAPABILITY_CODES = {
    "ANCHORED_BLOCK_TEXT_OVERFLOW",
    "CHART_TEXT_SLOT_OVERFLOW",
    "FONT_GLYPH_MISSING",
    "FONT_NOT_EMBEDDED",
    "P11_TRANSLATABLE_OWNER_NOT_FOUND",
    "P13_TRANSLATABLE_CHART_OWNER_NOT_FOUND",
}


def run_p15_page(
    *,
    source_pdf: Path,
    page_id: str,
    run_dir: Path,
    provider: TranslationProvider,
    font_file: str,
    bold_font_file: str | None,
    source_language: str,
    target_language: str,
    semantic_evaluation: bool,
) -> P15RunResult:
    run_dir.mkdir(parents=True, exist_ok=False)
    for name in ("contracts", "docs", "input", "output", "previews", "reports"):
        (run_dir / name).mkdir()
    source_snapshot = run_dir / "input" / "source.pdf"
    candidate_pdf = run_dir / "output" / "candidate.pdf"
    shutil.copy2(source_pdf, source_snapshot)
    source_hash = sha256_file(source_pdf)
    if sha256_file(source_snapshot) != source_hash:
        raise RuntimeError("source_snapshot_hash_mismatch")
    write_json(
        run_dir / "contracts" / "page_run_contract.json",
        {
            "schema_version": "p15-body-composite-anchored-blocks-chart-page-run/v1",
            "toolbox_key": "body.composite.anchored_blocks_chart",
            "page_id": page_id,
            "source_language": source_language,
            "target_language": target_language,
            "source_snapshot": "input/source.pdf",
            "candidate_output": "output/candidate.pdf",
            "ownership_rule": "each native text object has exactly one anchored, chart, shared, or protected owner",
            "translation_rule": "one page-level request; return values are sliced by immutable prefixed container id",
            "render_rule": "one composite redaction-and-write pass; no sequential leaf rewrite",
            "failure_rule": "any P11/P13 child capability failure fails the page; every delivered candidate must materialize all returned translations",
            "semantic_evaluation": semantic_evaluation,
            "source_sha256": source_hash,
            "source_is_immutable": True,
        },
    )
    (run_dir / "docs" / "README.md").write_text(
        f"# {page_id} P15 运行包\n\n"
        "input 保存不可变源页、事实、所有权模板和统一翻译请求；output 保存翻译包、布局计划与候选 PDF；"
        "reports 保存过程、能力和产品三层裁决；previews 保存源页与候选页视觉对照。\n",
        encoding="utf-8",
    )

    trace: list[dict[str, str]] = [{"state": "SAMPLE_READY", "owner": "p15_orchestrator"}]
    failure_owner: str | None = None
    decision: CompositeDecision
    findings: list[CompositeFinding] = []
    try:
        if not Path(font_file).is_file():
            raise CompositeCapabilityError(f"FONT_FILE_MISSING:{font_file}")
        facts = extract_page_facts(source_snapshot, page_id=page_id)
        write_json(run_dir / "input" / "page_facts.json", facts)
        trace.append({"state": "FACTS_READY", "owner": "shared_pdf_kernel"})

        template = build_composite_template(
            source_snapshot,
            facts,
            target_language=target_language,
        )
        findings.extend(_missing_required_owner_findings(template))
        write_json(run_dir / "input" / "page_template.json", template)
        write_json(
            run_dir / "reports" / "ownership_audit.json",
            {
                "status": "FAIL" if _missing_required_owner_findings(template) else "PASS",
                "object_count": len(template.ownerships),
                "unique_object_count": len({item.object_id for item in template.ownerships}),
                "owner_counts": {
                    owner: sum(1 for item in template.ownerships if item.owner == owner)
                    for owner in ("anchored", "chart", "shared", "protected")
                },
                "container_counts": {
                    owner: sum(1 for item in template.containers if item.owner == owner)
                    for owner in ("anchored", "chart", "shared")
                },
                "ownerships": template.ownerships,
            },
        )
        trace.append({"state": "TEMPLATE_READY", "owner": "composite_template_builder"})

        request = build_translation_request(
            template,
            source_language=source_language,
            target_language=target_language,
        )
        write_json(run_dir / "input" / "translation_request.json", request)
        bundle = provider.translate(request)
        bundle.validate_against(request)
        write_json(run_dir / "output" / "translation_bundle.json", bundle)
        provider_audit = getattr(provider, "last_audit", {})
        if provider_audit:
            write_json(run_dir / "reports" / "translation_retry.json", provider_audit)
        validation = (
            _filter_confirmed_proper_names(
                translation_validation(request, bundle),
                provider_audit,
            )
            if semantic_evaluation
            else {
                "status": "SKIPPED_MECHANICAL_ONLY",
                "reason": "fixed output exercises ownership, layout, rendering, and gates; it is not product translation evidence",
            }
        )
        write_json(run_dir / "reports" / "translation_validation.json", validation)
        findings.extend(_translation_findings(validation))
        trace.append({"state": "TRANSLATION_READY", "owner": "translation_provider"})

        plan, layout_findings, rule_trace = plan_composite_layout(
            template,
            bundle,
            font_file=font_file,
            bold_font_file=bold_font_file,
        )
        findings.extend(layout_findings)
        write_json(run_dir / "output" / "layout_plan.json", plan)
        write_json(run_dir / "reports" / "layout_findings.json", layout_findings)
        write_json(run_dir / "reports" / "layout_rule_trace.json", rule_trace)
        trace.append({"state": "PATCH_READY", "owner": "composite_layout_planner"})

        render_findings, render_evidence = render_composite_candidate(
            source_pdf=source_snapshot,
            candidate_pdf=candidate_pdf,
            facts=facts,
            template=template,
            plan=plan,
            evidence_dir=run_dir / "previews",
        )
        findings.extend(render_findings)
        write_json(run_dir / "reports" / "render_evidence.json", render_evidence)
        trace.append({"state": "CANDIDATE_READY", "owner": "composite_pdf_renderer"})
        decision = _decide(page_id, tuple(_deduplicate(findings)), semantic_evaluation)
    except (CompositeCapabilityError, ProviderError) as exc:
        failure_owner = _next_owner(trace)
        code = exc.code if isinstance(exc, ProviderError) else str(exc).split(":", 1)[0]
        findings.append(
            CompositeFinding(
                code,
                "HARD",
                failure_owner,
                None,
                None,
                str(exc),
                {},
            )
        )
        write_json(
            run_dir / "reports" / "candidate_artifact.json",
            {
                "status": "NOT_PRODUCED",
                "reason": "real translation did not complete; untranslated source-copy candidates are forbidden",
                "failure_code": code,
                "delivery_eligible": False,
            },
        )
        decision = CompositeDecision(
            page_id,
            "PASS",
            "NOT_REACHED",
            "CAPABILITY_FAILED",
            tuple(_deduplicate(findings)),
        )
    except Exception as exc:
        failure_owner = _next_owner(trace)
        findings.append(
            CompositeFinding(
                type(exc).__name__,
                "HARD",
                failure_owner,
                None,
                None,
                str(exc),
                {},
            )
        )
        if not candidate_pdf.is_file():
            write_json(
                run_dir / "reports" / "candidate_artifact.json",
                {
                    "status": "NOT_PRODUCED",
                    "reason": "translated candidate rendering did not complete; untranslated source-copy candidates are forbidden",
                    "failure_code": type(exc).__name__,
                    "delivery_eligible": False,
                },
            )
        decision = CompositeDecision(
            page_id,
            "FAIL",
            "NOT_REACHED",
            "PROCESS_FAILED",
            tuple(_deduplicate(findings)),
        )

    if sha256_file(source_pdf) != source_hash:
        decision = CompositeDecision(
            page_id,
            "FAIL",
            "NOT_REACHED",
            "PROCESS_FAILED",
            (*decision.findings, CompositeFinding(
                "SOURCE_SNAPSHOT_CHANGED",
                "HARD",
                "p15_orchestrator",
                None,
                None,
                "上游样本在运行期间发生变化。",
                {},
            )),
        )
    trace.append({"state": "QUALITY_DECIDED", "owner": "composite_quality_judge"})
    trace.append({"state": decision.terminal_state, "owner": "composite_quality_judge"})
    write_json(run_dir / "reports" / "quality_decision.json", decision)
    write_json(run_dir / "reports" / "state_trace.json", trace)
    result = P15RunResult(
        page_id=page_id,
        run_dir=str(run_dir),
        candidate_pdf=str(candidate_pdf),
        process_verdict=decision.process_verdict,
        product_verdict=decision.product_verdict,
        terminal_state=decision.terminal_state,
        provider=provider.provider_name,
        failure_owner=failure_owner,
    )
    write_json(run_dir / "reports" / "run_result.json", result)
    return result


def _translation_findings(validation: dict[str, object]) -> tuple[CompositeFinding, ...]:
    if validation.get("status") != "FAIL":
        return ()
    checks = (
        ("TRANSLATION_REQUIRED_LITERAL_MISSING", "missing_required_literals"),
        ("TRANSLATION_SOURCE_LANGUAGE_RESIDUE", "source_language_residue"),
        ("TRANSLATION_PLACEHOLDER_OUTPUT", "placeholder_outputs"),
        ("TRANSLATION_INADEQUATE_OUTPUT", "inadequate_outputs"),
        ("TRANSLATION_MAGNITUDE_UNIT_MISMATCH", "magnitude_unit_mismatches"),
    )
    return tuple(
        CompositeFinding(
            code,
            "HARD",
            "translation_provider",
            None,
            None,
            "统一页级翻译包未通过语义合同校验。",
            {"details": validation.get(key)},
        )
        for code, key in checks
        if validation.get(key)
    )


def _missing_required_owner_findings(template) -> tuple[CompositeFinding, ...]:
    missing = []
    if not any(item.owner == "anchored" for item in template.containers):
        missing.append(("P11_TRANSLATABLE_OWNER_NOT_FOUND", "anchored"))
    if not any(item.owner == "chart" for item in template.containers):
        missing.append(("P13_TRANSLATABLE_CHART_OWNER_NOT_FOUND", "chart"))
    return tuple(
        CompositeFinding(
            code,
            "HARD",
            "composite_template_builder",
            owner,
            None,
            f"{owner} owner 缺失；其余明确 owner 仍继续翻译并生成诊断候选。",
            {"translated_diagnostic_required": True},
        )
        for code, owner in missing
    )


def _filter_confirmed_proper_names(
    validation: dict[str, object],
    provider_audit: dict[str, object],
) -> dict[str, object]:
    confirmed = {
        str(container_id)
        for container_id in provider_audit.get("confirmed_proper_name_ids", ())
    }
    if not confirmed:
        return validation
    filtered = dict(validation)
    residue = validation.get("source_language_residue")
    if isinstance(residue, dict):
        filtered["source_language_residue"] = {
            container_id: values
            for container_id, values in residue.items()
            if str(container_id) not in confirmed
        }
    filtered["confirmed_proper_name_ids"] = sorted(confirmed)
    filtered["status"] = (
        "FAIL"
        if any(
            filtered.get(key)
            for key in (
                "missing_required_literals",
                "source_language_residue",
                "placeholder_outputs",
                "inadequate_outputs",
                "magnitude_unit_mismatches",
            )
        )
        else "PASS"
    )
    return filtered


def _decide(
    page_id: str,
    findings: tuple[CompositeFinding, ...],
    semantic_evaluation: bool,
) -> CompositeDecision:
    hard = tuple(item for item in findings if item.severity == "HARD")
    if any(item.code in _PROCESS_CODES for item in hard):
        return CompositeDecision(page_id, "FAIL", "NOT_REACHED", "PROCESS_FAILED", findings)
    if any(item.code in _CAPABILITY_CODES for item in hard):
        return CompositeDecision(page_id, "PASS", "NOT_REACHED", "CAPABILITY_FAILED", findings)
    if hard:
        return CompositeDecision(page_id, "PASS", "FAIL", "QUALITY_FAILED", findings)
    if not semantic_evaluation:
        return CompositeDecision(page_id, "PASS", "NOT_EVALUATED", "MECHANICAL_PASSED", findings)
    return CompositeDecision(page_id, "PASS", "PASS", "PAGE_PASSED", findings)


def _next_owner(trace: list[dict[str, str]]) -> str:
    return {
        "SAMPLE_READY": "shared_pdf_kernel",
        "FACTS_READY": "composite_template_builder",
        "TEMPLATE_READY": "translation_provider",
        "TRANSLATION_READY": "composite_layout_planner",
        "PATCH_READY": "composite_pdf_renderer",
        "CANDIDATE_READY": "composite_quality_judge",
    }.get(trace[-1]["state"] if trace else "", "p15_orchestrator")


def _deduplicate(findings: list[CompositeFinding] | tuple[CompositeFinding, ...]):
    result = []
    seen = set()
    for item in findings:
        key = (item.code, item.container_id, item.message)
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result
