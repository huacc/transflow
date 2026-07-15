from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from page_toolbox_puncture.contracts import PageTranslationRequest, TranslationUnit, write_json
from page_toolbox_puncture.sample_snapshot import sha256_file
from page_toolbox_puncture.translation import ProviderError, TranslationProvider
from shared_pdf_kernel.facts import extract_page_facts

from .layout_planner import plan_end_layout
from .models import EndDecision, EndFinding, EndTemplate
from .renderer import render_end_candidate, render_end_passthrough
from .template_builder import EndCapabilityError, build_end_template


@dataclass(frozen=True)
class P10RunResult:
    page_id: str
    run_dir: str
    candidate_pdf: str | None
    process_verdict: str
    product_verdict: str
    terminal_state: str
    failure_owner: str | None
    mode: str
    translated_region_count: int
    protected_object_count: int


_PROCESS_FAILURE_CODES = {
    "END_LOCKED_OBJECT_CHANGED",
    "END_PROTECTED_TEXT_CHANGED",
    "END_OUTSIDE_ALLOWED_REGION_CHANGED",
    "END_PASSTHROUGH_BYTES_CHANGED",
}
_CAPABILITY_FAILURE_CODES = {"FONT_NOT_EMBEDDED"}


def build_end_translation_request(template: EndTemplate) -> PageTranslationRequest | None:
    regions = template.translatable_regions
    if not regions:
        return None
    return PageTranslationRequest(
        request_id=f"p10-{template.page_id}-{template.source_language}-{template.target_language}",
        page_id=template.page_id,
        source_language=template.source_language,
        target_language=template.target_language,
        units=tuple(
            TranslationUnit(
                container_id=region.region_id,
                source_text=region.source_text,
                reading_order=index,
                required_literals=region.required_literals,
            )
            for index, region in enumerate(regions)
        ),
    )


def run_p10_page(
    *,
    source_pdf: Path,
    page_id: str,
    run_dir: Path,
    provider: TranslationProvider | None,
    font_file: str,
    bold_font_file: str | None,
    source_language: str,
    target_language: str,
) -> P10RunResult:
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
            "schema_version": "p10-end-page-run/v1",
            "toolbox_key": "end",
            "page_id": page_id,
            "source_language": source_language,
            "target_language": target_language,
            "source_snapshot": "input/source.pdf",
            "source_sha256": source_hash,
            "candidate_output": "output/candidate.pdf",
            "visual_rule": "page geometry, backgrounds, logos, QR codes, images and vector decorations are immutable",
            "identifier_rule": "brand identifiers, links, email and already-target-language contact text are not redrawn",
            "translation_rule": "only native semantic end-page text is translated; required literals must survive exactly",
            "passthrough_rule": "pages without translatable native text are copied byte-for-byte",
            "source_is_immutable": True,
        },
    )
    (run_dir / "docs" / "README.md").write_text(
        f"# {page_id} P10 end 运行包\n\n"
        "原页快照、页面事实、结束页模板和翻译请求位于 `input/`；"
        "译文、布局计划和候选页位于 `output/`；机械证据与双裁决位于 `reports/`。\n",
        encoding="utf-8",
    )

    trace: list[dict[str, str]] = [{"state": "SAMPLE_READY", "owner": "p10_orchestrator"}]
    candidate_pdf: Path | None = None
    failure_owner: str | None = None
    mode = "unknown"
    translated_region_count = 0
    protected_object_count = 0
    try:
        facts = extract_page_facts(source_snapshot, page_id=page_id)
        write_json(run_dir / "input" / "page_facts.json", facts)
        trace.append({"state": "FACTS_READY", "owner": "shared_pdf_kernel"})

        template = build_end_template(facts, source_language, target_language)
        translated_region_count = len(template.translatable_regions)
        protected_object_count = len(template.protected_object_ids)
        write_json(run_dir / "input" / "page_template.json", template)
        trace.append({"state": "TEMPLATE_READY", "owner": "end_template_builder"})

        candidate_pdf = run_dir / "output" / "candidate.pdf"
        if template.passthrough:
            mode = "passthrough"
            write_json(
                run_dir / "output" / "translation_status.json",
                {
                    "status": "SKIPPED",
                    "reason": "NO_TRANSLATABLE_NATIVE_END_TEXT",
                    "native_text_region_count": len(template.regions),
                    "protected_object_count": len(template.protected_object_ids),
                },
            )
            trace.append({"state": "TRANSLATION_SKIPPED", "owner": "end_template_builder"})
            render_findings, render_evidence = render_end_passthrough(
                source_pdf=source_snapshot,
                candidate_pdf=candidate_pdf,
                facts=facts,
                template=template,
                evidence_dir=run_dir / "previews",
            )
        else:
            mode = "translated"
            if provider is None:
                raise EndCapabilityError("END_TRANSLATION_PROVIDER_REQUIRED")
            if not Path(font_file).is_file():
                raise EndCapabilityError(f"END_FONT_FILE_MISSING:{font_file}")
            request = build_end_translation_request(template)
            if request is None:
                raise RuntimeError("end_translation_request_unexpectedly_empty")
            write_json(run_dir / "input" / "translation_request.json", request)
            bundle = provider.translate(request)
            bundle.validate_against(request)
            _validate_required_literals(request, bundle)
            write_json(run_dir / "output" / "translation_bundle.json", bundle)
            trace.append({"state": "TRANSLATION_READY", "owner": "translation_provider"})

            plan, plan_findings = plan_end_layout(
                template,
                bundle,
                font_file=font_file,
                bold_font_file=bold_font_file,
            )
            write_json(run_dir / "output" / "layout_plan.json", plan)
            write_json(run_dir / "reports" / "layout_findings.json", plan_findings)
            trace.append({"state": "PATCH_READY", "owner": "end_layout_planner"})
            if plan_findings:
                candidate_pdf = None
                decision = EndDecision(page_id, "PASS", "FAIL", "QUALITY_FAILED", plan_findings)
                write_json(run_dir / "reports" / "quality_decision.json", decision)
                trace.append({"state": "QUALITY_DECIDED", "owner": "end_quality_judge"})
                trace.append({"state": decision.terminal_state, "owner": "end_quality_judge"})
                return _finish_run(
                    source_pdf=source_pdf,
                    source_hash=source_hash,
                    run_dir=run_dir,
                    page_id=page_id,
                    candidate_pdf=None,
                    decision=decision,
                    trace=trace,
                    mode=mode,
                    translated_region_count=translated_region_count,
                    protected_object_count=protected_object_count,
                )
            render_findings, render_evidence = render_end_candidate(
                source_pdf=source_snapshot,
                candidate_pdf=candidate_pdf,
                facts=facts,
                template=template,
                plan=plan,
                evidence_dir=run_dir / "previews",
            )

        write_json(run_dir / "reports" / "render_evidence.json", render_evidence)
        trace.append({"state": "CANDIDATE_READY", "owner": "end_pdf_renderer"})
        decision = _decision(page_id, render_findings)
        write_json(run_dir / "reports" / "quality_decision.json", decision)
        trace.append({"state": "QUALITY_DECIDED", "owner": "end_quality_judge"})
        trace.append({"state": decision.terminal_state, "owner": "end_quality_judge"})
        if decision.terminal_state != "PAGE_PASSED":
            failure_owner = decision.findings[0].owner if decision.findings else "end_quality_judge"
    except (EndCapabilityError, ProviderError) as exc:
        failure_owner = _next_owner(trace)
        code = exc.code if isinstance(exc, ProviderError) else str(exc).split(":", 1)[0]
        finding = EndFinding(code, "HARD", failure_owner, None, str(exc), {})
        decision = EndDecision(page_id, "PASS", "NOT_REACHED", "CAPABILITY_FAILED", (finding,))
        candidate_pdf = None
        write_json(run_dir / "reports" / "quality_decision.json", decision)
        trace.append({"state": "CAPABILITY_FAILED", "owner": failure_owner})
    except Exception as exc:
        failure_owner = _next_owner(trace)
        finding = EndFinding(type(exc).__name__, "HARD", failure_owner, None, str(exc), {})
        decision = EndDecision(page_id, "FAIL", "NOT_REACHED", "PROCESS_FAILED", (finding,))
        candidate_pdf = None
        write_json(run_dir / "reports" / "quality_decision.json", decision)
        trace.append({"state": "PROCESS_FAILED", "owner": failure_owner})

    return _finish_run(
        source_pdf=source_pdf,
        source_hash=source_hash,
        run_dir=run_dir,
        page_id=page_id,
        candidate_pdf=candidate_pdf,
        decision=decision,
        trace=trace,
        mode=mode,
        translated_region_count=translated_region_count,
        protected_object_count=protected_object_count,
        failure_owner=failure_owner,
    )


def _decision(page_id: str, findings: tuple[EndFinding, ...]) -> EndDecision:
    process = tuple(finding for finding in findings if finding.code in _PROCESS_FAILURE_CODES)
    capability = tuple(finding for finding in findings if finding.code in _CAPABILITY_FAILURE_CODES)
    product = tuple(finding for finding in findings if finding not in process and finding not in capability)
    if process:
        return EndDecision(page_id, "FAIL", "NOT_REACHED", "PROCESS_FAILED", findings)
    if capability:
        return EndDecision(page_id, "PASS", "NOT_REACHED", "CAPABILITY_FAILED", findings)
    if product:
        return EndDecision(page_id, "PASS", "FAIL", "QUALITY_FAILED", findings)
    return EndDecision(page_id, "PASS", "PASS", "PAGE_PASSED", ())


def _finish_run(
    *,
    source_pdf: Path,
    source_hash: str,
    run_dir: Path,
    page_id: str,
    candidate_pdf: Path | None,
    decision: EndDecision,
    trace: list[dict[str, str]],
    mode: str,
    translated_region_count: int,
    protected_object_count: int,
    failure_owner: str | None = None,
) -> P10RunResult:
    if sha256_file(source_pdf) != source_hash:
        raise RuntimeError("upstream_sample_changed_during_run")
    write_json(run_dir / "reports" / "state_trace.json", trace)
    result = P10RunResult(
        page_id=page_id,
        run_dir=str(run_dir),
        candidate_pdf=str(candidate_pdf) if candidate_pdf and candidate_pdf.is_file() else None,
        process_verdict=decision.process_verdict,
        product_verdict=decision.product_verdict,
        terminal_state=decision.terminal_state,
        failure_owner=failure_owner,
        mode=mode,
        translated_region_count=translated_region_count,
        protected_object_count=protected_object_count,
    )
    write_json(run_dir / "reports" / "run_result.json", result)
    return result


def _next_owner(trace: list[dict[str, str]]) -> str:
    return {
        "SAMPLE_READY": "shared_pdf_kernel",
        "FACTS_READY": "end_template_builder",
        "TEMPLATE_READY": "translation_provider",
        "TRANSLATION_READY": "end_layout_planner",
        "PATCH_READY": "end_pdf_renderer",
        "TRANSLATION_SKIPPED": "end_pdf_renderer",
        "CANDIDATE_READY": "end_quality_judge",
    }.get(trace[-1]["state"] if trace else "", "p10_orchestrator")


def _validate_required_literals(request: PageTranslationRequest, bundle) -> None:
    translated_by_id = {item.container_id: item.translated_text for item in bundle.translations}
    missing = {
        unit.container_id: [literal for literal in unit.required_literals if literal not in translated_by_id[unit.container_id]]
        for unit in request.units
        if any(literal not in translated_by_id[unit.container_id] for literal in unit.required_literals)
    }
    if missing:
        raise ProviderError("TRANSLATION_REQUIRED_LITERAL_MISSING")
