from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from page_toolbox_puncture.contracts import PageTranslationRequest, TranslationUnit, write_json
from page_toolbox_puncture.sample_snapshot import sha256_file
from page_toolbox_puncture.translation import ProviderError, TranslationProvider
from shared_pdf_kernel.facts import extract_page_facts

from .layout_planner import plan_contents_layout
from .models import ContentsDecision, ContentsFinding, ContentsTemplate
from .renderer import render_contents_candidate
from .template_builder import ContentsCapabilityError, build_contents_template


@dataclass(frozen=True)
class P8RunResult:
    page_id: str
    run_dir: str
    candidate_pdf: str | None
    process_verdict: str
    product_verdict: str
    terminal_state: str
    failure_owner: str | None


_PROCESS_FAILURE_CODES = {
    "CONTENTS_LOCKED_OBJECT_CHANGED",
    "CONTENTS_PAGE_NUMBER_CHANGED",
    "CONTENTS_PROTECTED_TEXT_CHANGED",
    "CONTENTS_OUTSIDE_ALLOWED_REGION_CHANGED",
}
_REQUIRED_LITERAL = re.compile(r"(?<!\d)\d+(?:[.,:/-]\d+)*(?!\d)")


def build_contents_translation_request(
    template: ContentsTemplate,
    source_language: str,
    target_language: str,
) -> PageTranslationRequest:
    units = tuple(
        TranslationUnit(
            container.container_id,
            container.source_text,
            container.reading_order,
            tuple(dict.fromkeys(_REQUIRED_LITERAL.findall(container.source_text))),
        )
        for container in template.containers
    )
    return PageTranslationRequest(
        f"p8-{template.page_id}-{source_language}-{target_language}",
        template.page_id,
        source_language,
        target_language,
        units,
    )


def run_p8_page(
    *,
    source_pdf: Path,
    page_id: str,
    run_dir: Path,
    provider: TranslationProvider,
    font_file: str,
    bold_font_file: str | None,
    source_language: str,
    target_language: str,
) -> P8RunResult:
    if not Path(font_file).is_file():
        raise ContentsCapabilityError(f"font_file_missing:{font_file}")
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
            "schema_version": "p8-contents-page-run/v1",
            "toolbox_key": "contents",
            "page_id": page_id,
            "source_language": source_language,
            "target_language": target_language,
            "source_snapshot": "input/source.pdf",
            "source_sha256": source_hash,
            "candidate_output": "output/candidate.pdf",
            "entry_rule": "entry count, order, hierarchy and column ownership are immutable",
            "page_number_rule": "page numbers, ranges and serial anchors are never sent for translation or redrawn",
            "layout_rule": "translated labels stay inside their source column and clear of fixed page anchors",
            "source_is_immutable": True,
        },
    )
    (run_dir / "docs" / "README.md").write_text(
        f"# {page_id} P8 contents 运行包\n\n"
        "原页快照、页面事实、目录模板和翻译请求位于 `input/`；"
        "译文、布局计划和候选页位于 `output/`；机械证据与双裁决位于 `reports/`。\n",
        encoding="utf-8",
    )
    trace: list[dict[str, str]] = [{"state": "SAMPLE_READY", "owner": "p8_orchestrator"}]
    failure_owner: str | None = None
    candidate_pdf: Path | None = None
    try:
        facts = extract_page_facts(source_snapshot, page_id=page_id)
        write_json(run_dir / "input" / "page_facts.json", facts)
        trace.append({"state": "FACTS_READY", "owner": "shared_pdf_kernel"})

        template = build_contents_template(facts)
        write_json(run_dir / "input" / "page_template.json", template)
        trace.append({"state": "TEMPLATE_READY", "owner": "contents_template_builder"})

        request = build_contents_translation_request(template, source_language, target_language)
        write_json(run_dir / "input" / "translation_request.json", request)
        bundle = provider.translate(request)
        bundle.validate_against(request)
        _validate_required_literals(request, bundle)
        write_json(run_dir / "output" / "translation_bundle.json", bundle)
        trace.append({"state": "TRANSLATION_READY", "owner": "translation_provider"})

        plan, plan_findings = plan_contents_layout(
            template,
            bundle,
            font_file=font_file,
            bold_font_file=bold_font_file,
        )
        write_json(run_dir / "output" / "layout_plan.json", plan)
        write_json(run_dir / "reports" / "layout_findings.json", plan_findings)
        trace.append({"state": "PATCH_READY", "owner": "contents_layout_planner"})
        if plan_findings:
            decision = ContentsDecision(page_id, "PASS", "FAIL", "QUALITY_FAILED", plan_findings)
        else:
            candidate_pdf = run_dir / "output" / "candidate.pdf"
            render_findings, render_evidence = render_contents_candidate(
                source_pdf=source_snapshot,
                candidate_pdf=candidate_pdf,
                facts=facts,
                template=template,
                plan=plan,
                evidence_dir=run_dir / "previews",
            )
            write_json(run_dir / "reports" / "render_evidence.json", render_evidence)
            trace.append({"state": "CANDIDATE_READY", "owner": "contents_pdf_renderer"})
            process_findings = tuple(item for item in render_findings if item.code in _PROCESS_FAILURE_CODES)
            capability_findings = tuple(item for item in render_findings if item.code == "FONT_NOT_EMBEDDED")
            product_findings = tuple(
                item for item in render_findings if item not in process_findings and item not in capability_findings
            )
            if process_findings:
                decision = ContentsDecision(page_id, "FAIL", "NOT_REACHED", "PROCESS_FAILED", render_findings)
            elif capability_findings:
                decision = ContentsDecision(page_id, "PASS", "NOT_REACHED", "CAPABILITY_FAILED", render_findings)
            elif product_findings:
                decision = ContentsDecision(page_id, "PASS", "FAIL", "QUALITY_FAILED", render_findings)
            else:
                decision = ContentsDecision(page_id, "PASS", "PASS", "PAGE_PASSED", ())
        write_json(run_dir / "reports" / "quality_decision.json", decision)
        trace.append({"state": "QUALITY_DECIDED", "owner": "contents_quality_judge"})
        trace.append({"state": decision.terminal_state, "owner": "contents_quality_judge"})
    except (ContentsCapabilityError, ProviderError) as exc:
        failure_owner = _owner(trace)
        code = exc.code if isinstance(exc, ProviderError) else str(exc).split(":", 1)[0]
        finding = ContentsFinding(code, "HARD", failure_owner, None, str(exc), {})
        decision = ContentsDecision(page_id, "PASS", "NOT_REACHED", "CAPABILITY_FAILED", (finding,))
        write_json(run_dir / "reports" / "quality_decision.json", decision)
        trace.append({"state": "CAPABILITY_FAILED", "owner": failure_owner})
    except Exception as exc:
        failure_owner = _owner(trace)
        finding = ContentsFinding(type(exc).__name__, "HARD", failure_owner, None, str(exc), {})
        decision = ContentsDecision(page_id, "FAIL", "NOT_REACHED", "PROCESS_FAILED", (finding,))
        write_json(run_dir / "reports" / "quality_decision.json", decision)
        trace.append({"state": "PROCESS_FAILED", "owner": failure_owner})

    if sha256_file(source_pdf) != source_hash:
        raise RuntimeError("upstream_sample_changed_during_run")
    write_json(run_dir / "reports" / "state_trace.json", trace)
    result = P8RunResult(
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
        "FACTS_READY": "contents_template_builder",
        "TEMPLATE_READY": "translation_provider",
        "TRANSLATION_READY": "contents_layout_planner",
        "PATCH_READY": "contents_pdf_renderer",
        "CANDIDATE_READY": "contents_quality_judge",
    }.get(trace[-1]["state"] if trace else "", "p8_orchestrator")


def _validate_required_literals(request: PageTranslationRequest, bundle) -> None:
    translated_by_id = {item.container_id: item.translated_text for item in bundle.translations}
    missing = {
        unit.container_id: [literal for literal in unit.required_literals if literal not in translated_by_id[unit.container_id]]
        for unit in request.units
        if any(literal not in translated_by_id[unit.container_id] for literal in unit.required_literals)
    }
    if missing:
        raise ProviderError("TRANSLATION_REQUIRED_LITERAL_MISSING")
