from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from page_toolbox_puncture.contracts import PageTranslationRequest, TranslationUnit, write_json
from page_toolbox_puncture.sample_snapshot import sha256_file
from page_toolbox_puncture.translation import ProviderError, TranslationProvider
from shared_pdf_kernel.facts import extract_page_facts
from shared_pdf_kernel.render import render_contact_sheet, render_page

from .layout_planner import plan_cover_layout
from .models import CoverDecision, CoverFinding, CoverTemplate
from .renderer import render_cover_candidate
from .template_builder import CoverCapabilityError, build_cover_template


@dataclass(frozen=True)
class P9RunResult:
    page_id: str
    run_dir: str
    candidate_pdf: str | None
    process_verdict: str
    product_verdict: str
    terminal_state: str
    failure_owner: str | None


_PROCESS_FAILURE_CODES = {
    "COVER_LOCKED_OBJECT_CHANGED",
    "COVER_PROTECTED_TEXT_CHANGED",
    "COVER_OUTSIDE_ALLOWED_REGION_CHANGED",
}
_REQUIRED_LITERAL = re.compile(r"(?<!\d)\d+(?:[.,:/-]\d+)*(?!\d)")


def build_cover_translation_request(
    template: CoverTemplate,
    source_language: str,
    target_language: str,
) -> PageTranslationRequest:
    if template.visual_only:
        raise CoverCapabilityError("COVER_VISUAL_ONLY_HAS_NO_TRANSLATION_REQUEST")
    units = tuple(
        TranslationUnit(
            container.container_id,
            container.source_text,
            container.reading_order,
            tuple(dict.fromkeys(_REQUIRED_LITERAL.findall(container.source_text))),
        )
        for container in template.containers
        if container.translatable and _requires_translation(container.source_text, target_language)
    )
    if not units:
        raise CoverCapabilityError("COVER_NO_TRANSLATION_REQUIRED")
    return PageTranslationRequest(
        f"p9-{template.page_id}-{source_language}-{target_language}",
        template.page_id,
        source_language,
        target_language,
        units,
    )


def run_p9_page(
    *,
    source_pdf: Path,
    page_id: str,
    run_dir: Path,
    provider: TranslationProvider,
    font_file: str,
    bold_font_file: str | None,
    source_language: str,
    target_language: str,
) -> P9RunResult:
    if not Path(font_file).is_file():
        raise CoverCapabilityError(f"font_file_missing:{font_file}")
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
            "schema_version": "p9-cover-page-run/v1",
            "toolbox_key": "cover",
            "page_id": page_id,
            "source_language": source_language,
            "target_language": target_language,
            "source_snapshot": "input/source.pdf",
            "source_sha256": source_hash,
            "candidate_output": "output/candidate.pdf",
            "editable_text_rule": "every semantic native text object is translated exactly once",
            "visual_rule": "background, images, logos, color blocks, drawings and image text are immutable",
            "anchor_rule": "left, center or right sparse-cover anchor and title hierarchy are preserved",
            "visual_only_rule": "pages without semantic native text are byte-identical passthrough and never call translation",
            "source_is_immutable": True,
        },
    )
    (run_dir / "docs" / "README.md").write_text(
        f"# {page_id} P9 cover 运行包\n\n"
        "原页快照、页面事实、封面模板和翻译请求位于 `input/`；"
        "译文、布局计划和候选页位于 `output/`；机器证据与裁决位于 `reports/`。\n",
        encoding="utf-8",
    )

    trace: list[dict[str, str]] = [{"state": "SAMPLE_READY", "owner": "p9_orchestrator"}]
    failure_owner: str | None = None
    candidate_pdf: Path | None = None
    try:
        facts = extract_page_facts(source_snapshot, page_id=page_id)
        write_json(run_dir / "input" / "page_facts.json", facts)
        trace.append({"state": "FACTS_READY", "owner": "shared_pdf_kernel"})

        template = build_cover_template(facts, source_snapshot)
        write_json(run_dir / "input" / "page_template.json", template)
        trace.append({"state": "TEMPLATE_READY", "owner": "cover_template_builder"})

        translation_required = any(
            container.translatable and _requires_translation(container.source_text, target_language)
            for container in template.containers
        )
        if template.visual_only or not translation_required:
            candidate_pdf = run_dir / "output" / "candidate.pdf"
            shutil.copyfile(source_snapshot, candidate_pdf)
            render_page(source_snapshot, run_dir / "previews" / "source.png", zoom=2.0)
            render_page(candidate_pdf, run_dir / "previews" / "candidate.png", zoom=2.0)
            render_contact_sheet(source_snapshot, candidate_pdf, run_dir / "previews" / "comparison.png", zoom=1.5)
            if sha256_file(candidate_pdf) != source_hash:
                raise RuntimeError("visual_only_passthrough_not_byte_identical")
            write_json(
                run_dir / "reports" / "render_evidence.json",
                {
                    "route": "visual_only_passthrough" if template.visual_only else "already_target_passthrough",
                    "reason": template.visual_only_reason if template.visual_only else "NO_SOURCE_SCRIPT_TRANSLATION_REQUIRED",
                    "source_pdf_sha256": source_hash,
                    "candidate_pdf_sha256": sha256_file(candidate_pdf),
                    "byte_identical": True,
                    "translation_provider_called": False,
                },
            )
            route_state = "VISUAL_ONLY_ROUTED" if template.visual_only else "ALREADY_TARGET_ROUTED"
            terminal_state = "VISUAL_ONLY_PASSED" if template.visual_only else "ALREADY_TARGET_PASSED"
            trace.append({"state": route_state, "owner": "cover_template_builder"})
            decision = CoverDecision(page_id, "PASS", "NOT_APPLICABLE", terminal_state, ())
        else:
            request = build_cover_translation_request(template, source_language, target_language)
            write_json(run_dir / "input" / "translation_request.json", request)
            bundle = provider.translate(request)
            bundle.validate_against(request)
            _validate_required_literals(request, bundle)
            write_json(run_dir / "output" / "translation_bundle.json", bundle)
            trace.append({"state": "TRANSLATION_READY", "owner": "translation_provider"})

            plan, plan_findings = plan_cover_layout(
                template,
                bundle,
                font_file=font_file,
                bold_font_file=bold_font_file,
            )
            write_json(run_dir / "output" / "layout_plan.json", plan)
            write_json(run_dir / "reports" / "layout_findings.json", plan_findings)
            trace.append({"state": "PATCH_READY", "owner": "cover_layout_planner"})
            if plan_findings:
                decision = CoverDecision(page_id, "PASS", "FAIL", "QUALITY_FAILED", plan_findings)
            else:
                candidate_pdf = run_dir / "output" / "candidate.pdf"
                render_findings, render_evidence = render_cover_candidate(
                    source_pdf=source_snapshot,
                    candidate_pdf=candidate_pdf,
                    facts=facts,
                    template=template,
                    plan=plan,
                    evidence_dir=run_dir / "previews",
                )
                write_json(run_dir / "reports" / "render_evidence.json", render_evidence)
                trace.append({"state": "CANDIDATE_READY", "owner": "cover_pdf_renderer"})
                process_findings = tuple(item for item in render_findings if item.code in _PROCESS_FAILURE_CODES)
                capability_findings = tuple(item for item in render_findings if item.code == "FONT_NOT_EMBEDDED")
                product_findings = tuple(
                    item for item in render_findings if item not in process_findings and item not in capability_findings
                )
                if process_findings:
                    decision = CoverDecision(page_id, "FAIL", "NOT_REACHED", "PROCESS_FAILED", render_findings)
                elif capability_findings:
                    decision = CoverDecision(page_id, "PASS", "NOT_REACHED", "CAPABILITY_FAILED", render_findings)
                elif product_findings:
                    decision = CoverDecision(page_id, "PASS", "FAIL", "QUALITY_FAILED", render_findings)
                else:
                    decision = CoverDecision(page_id, "PASS", "PASS", "PAGE_PASSED", ())

        write_json(run_dir / "reports" / "quality_decision.json", decision)
        trace.append({"state": "QUALITY_DECIDED", "owner": "cover_quality_judge"})
        trace.append({"state": decision.terminal_state, "owner": "cover_quality_judge"})
    except (CoverCapabilityError, ProviderError) as exc:
        failure_owner = _owner(trace)
        code = exc.code if isinstance(exc, ProviderError) else str(exc).split(":", 1)[0]
        finding = CoverFinding(code, "HARD", failure_owner, None, str(exc), {})
        decision = CoverDecision(page_id, "PASS", "NOT_REACHED", "CAPABILITY_FAILED", (finding,))
        write_json(run_dir / "reports" / "quality_decision.json", decision)
        trace.append({"state": "CAPABILITY_FAILED", "owner": failure_owner})
    except Exception as exc:
        failure_owner = _owner(trace)
        finding = CoverFinding(type(exc).__name__, "HARD", failure_owner, None, str(exc), {})
        decision = CoverDecision(page_id, "FAIL", "NOT_REACHED", "PROCESS_FAILED", (finding,))
        write_json(run_dir / "reports" / "quality_decision.json", decision)
        trace.append({"state": "PROCESS_FAILED", "owner": failure_owner})

    if sha256_file(source_pdf) != source_hash:
        raise RuntimeError("upstream_sample_changed_during_run")
    write_json(run_dir / "reports" / "state_trace.json", trace)
    result = P9RunResult(
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
        "FACTS_READY": "cover_template_builder",
        "TEMPLATE_READY": "translation_provider",
        "TRANSLATION_READY": "cover_layout_planner",
        "PATCH_READY": "cover_pdf_renderer",
        "CANDIDATE_READY": "cover_quality_judge",
    }.get(trace[-1]["state"] if trace else "", "p9_orchestrator")


def _validate_required_literals(request: PageTranslationRequest, bundle) -> None:
    translated_by_id = {item.container_id: item.translated_text for item in bundle.translations}
    missing = {
        unit.container_id: [literal for literal in unit.required_literals if literal not in translated_by_id[unit.container_id]]
        for unit in request.units
        if any(literal not in translated_by_id[unit.container_id] for literal in unit.required_literals)
    }
    if missing:
        raise ProviderError("TRANSLATION_REQUIRED_LITERAL_MISSING")


def _requires_translation(text: str, target_language: str) -> bool:
    has_cjk = bool(re.search(r"[\u3400-\u9fff]", text))
    has_latin = bool(re.search(r"[A-Za-z]", text))
    if target_language.casefold().startswith("zh"):
        return has_latin
    if target_language.casefold().startswith("en"):
        return has_cjk
    return has_cjk or has_latin
