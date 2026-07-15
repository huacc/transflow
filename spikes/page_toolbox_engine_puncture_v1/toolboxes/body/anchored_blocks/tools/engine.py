from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from page_toolbox_puncture.contracts import PageTranslationRequest, TranslationUnit, write_json
from page_toolbox_puncture.sample_snapshot import sha256_file
from page_toolbox_puncture.translation import ProviderError, TranslationProvider
from shared_pdf_kernel.facts import extract_page_facts

from .layout_planner import plan_anchored_layout
from .models import AnchoredBlocksTemplate, AnchoredDecision, AnchoredFinding
from .renderer import render_anchored_candidate
from .template_builder import AnchoredBlocksCapabilityError, build_anchored_blocks_template
from .translation_guard import incomplete_translations, invented_placeholders, source_language_residue


@dataclass(frozen=True)
class P11RunResult:
    page_id: str
    run_dir: str
    candidate_pdf: str | None
    process_verdict: str
    product_verdict: str
    terminal_state: str
    failure_owner: str | None
    block_owner_count: int
    container_count: int
    protected_object_count: int


_PROCESS_FAILURE_CODES = {
    "ANCHORED_BLOCK_LOCKED_OBJECT_CHANGED",
    "ANCHORED_BLOCK_PROTECTED_TEXT_CHANGED",
    "ANCHORED_BLOCK_OUTSIDE_ALLOWED_REGION_CHANGED",
}
_CAPABILITY_FAILURE_CODES = {"FONT_NOT_EMBEDDED", "FONT_GLYPH_MISSING"}


def build_anchored_translation_request(
    template: AnchoredBlocksTemplate,
    source_language: str,
    target_language: str,
) -> PageTranslationRequest:
    return PageTranslationRequest(
        request_id=f"p11-{template.page_id}-{source_language}-{target_language}",
        page_id=template.page_id,
        source_language=source_language,
        target_language=target_language,
        units=tuple(
            TranslationUnit(
                container_id=container.container_id,
                source_text=container.source_text,
                reading_order=container.reading_order,
                required_literals=container.required_literals,
            )
            for container in template.containers
        ),
    )


def run_p11_page(
    *,
    source_pdf: Path,
    page_id: str,
    run_dir: Path,
    provider: TranslationProvider,
    font_file: str,
    bold_font_file: str | None,
    source_language: str,
    target_language: str,
) -> P11RunResult:
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
            "schema_version": "p11-anchored-blocks-page-run/v1",
            "toolbox_key": "body.anchored_blocks",
            "page_id": page_id,
            "source_language": source_language,
            "target_language": target_language,
            "source_snapshot": "input/source.pdf",
            "source_sha256": source_hash,
            "candidate_output": "output/candidate.pdf",
            "ownership_rule": "every native text object belongs to exactly one block owner, container, or protected set",
            "translation_rule": "container IDs encode block owner; writes cannot leave that owner",
            "repair_rule": "only the target container is re-fit; every accepted profile is re-judged globally",
            "immutable_rule": "images, drawings, colors, block boundaries, protected text, and source PDF are immutable",
            "source_is_immutable": True,
        },
    )
    (run_dir / "docs" / "README.md").write_text(
        f"# {page_id} P11 anchored_blocks 运行包\n\n"
        "`input/` 保存源页、事实、BlockOwner 模板和翻译请求；`output/` 保存译文、局部布局计划和候选页；"
        "`reports/` 与 `previews/` 保存双裁决和源候选渲染证据。\n",
        encoding="utf-8",
    )

    trace: list[dict[str, str]] = [{"state": "SAMPLE_READY", "owner": "p11_orchestrator"}]
    candidate_pdf: Path | None = None
    failure_owner: str | None = None
    counts = {"blocks": 0, "containers": 0, "protected": 0}
    try:
        facts = extract_page_facts(source_snapshot, page_id=page_id)
        write_json(run_dir / "input" / "page_facts.json", facts)
        trace.append({"state": "FACTS_READY", "owner": "shared_pdf_kernel"})

        template = build_anchored_blocks_template(facts, source_snapshot)
        counts = {
            "blocks": len(template.block_owners),
            "containers": len(template.containers),
            "protected": len(template.protected_object_ids),
        }
        write_json(run_dir / "input" / "page_template.json", template)
        trace.append({"state": "TEMPLATE_READY", "owner": "anchored_blocks_template_builder"})

        request = build_anchored_translation_request(template, source_language, target_language)
        write_json(run_dir / "input" / "translation_request.json", request)
        bundle = provider.translate(request)
        bundle.validate_against(request)
        provider_audit = getattr(provider, "last_audit", {})
        if hasattr(provider, "last_audit"):
            write_json(run_dir / "reports" / "translation_retry.json", provider_audit)
        write_json(run_dir / "output" / "translation_bundle.raw.json", bundle)
        missing_literals = _missing_required_literals(request, bundle)
        source_residue = source_language_residue(request, bundle)
        confirmed_proper_names = set(provider_audit.get("confirmed_proper_name_ids", ()))
        source_residue = {
            container_id: values
            for container_id, values in source_residue.items()
            if container_id not in confirmed_proper_names
        }
        placeholders = invented_placeholders(request, bundle)
        incomplete = incomplete_translations(request, bundle)
        write_json(
            run_dir / "reports" / "translation_validation.json",
            {
                "status": "FAIL" if missing_literals or source_residue or placeholders or incomplete else "PASS",
                "missing_required_literals": missing_literals,
                "source_language_residue": source_residue,
                "invented_placeholders": placeholders,
                "incomplete_translations": incomplete,
                "confirmed_proper_name_ids": sorted(confirmed_proper_names),
            },
        )
        if missing_literals:
            raise ProviderError("TRANSLATION_REQUIRED_LITERAL_MISSING")
        if source_residue:
            raise ProviderError("TRANSLATION_SOURCE_LANGUAGE_RESIDUE")
        if placeholders:
            raise ProviderError("TRANSLATION_INVENTED_PLACEHOLDER")
        if incomplete:
            raise ProviderError("TRANSLATION_INCOMPLETE")
        write_json(run_dir / "output" / "translation_bundle.json", bundle)
        trace.append({"state": "TRANSLATION_READY", "owner": "translation_provider"})

        plan, plan_findings = plan_anchored_layout(
            template,
            bundle,
            font_file=font_file,
            bold_font_file=bold_font_file,
        )
        write_json(run_dir / "output" / "layout_plan.json", plan)
        write_json(run_dir / "reports" / "layout_findings.json", plan_findings)
        trace.append({"state": "PATCH_READY", "owner": "anchored_blocks_layout_planner"})
        if plan_findings:
            decision = _decision(page_id, plan_findings)
            write_json(run_dir / "reports" / "quality_decision.json", decision)
            trace.extend(
                [
                    {"state": "QUALITY_DECIDED", "owner": "anchored_blocks_quality_judge"},
                    {"state": decision.terminal_state, "owner": "anchored_blocks_quality_judge"},
                ]
            )
            return _finish_run(source_pdf, source_hash, run_dir, page_id, None, decision, trace, counts)

        candidate_pdf = run_dir / "output" / "candidate.pdf"
        render_findings, render_evidence = render_anchored_candidate(
            source_pdf=source_snapshot,
            candidate_pdf=candidate_pdf,
            facts=facts,
            template=template,
            plan=plan,
            evidence_dir=run_dir / "previews",
        )
        write_json(run_dir / "reports" / "render_evidence.json", render_evidence)
        trace.append({"state": "CANDIDATE_READY", "owner": "anchored_blocks_pdf_renderer"})
        decision = _decision(page_id, render_findings)
        write_json(run_dir / "reports" / "quality_decision.json", decision)
        trace.extend(
            [
                {"state": "QUALITY_DECIDED", "owner": "anchored_blocks_quality_judge"},
                {"state": decision.terminal_state, "owner": "anchored_blocks_quality_judge"},
            ]
        )
        if decision.terminal_state != "PAGE_PASSED":
            failure_owner = decision.findings[0].owner if decision.findings else "anchored_blocks_quality_judge"
    except (AnchoredBlocksCapabilityError, ProviderError) as exc:
        failure_owner = _next_owner(trace)
        code = exc.code if isinstance(exc, ProviderError) else str(exc).split(":", 1)[0]
        finding = AnchoredFinding(code, "HARD", failure_owner, None, None, str(exc), {})
        decision = AnchoredDecision(page_id, "PASS", "NOT_REACHED", "CAPABILITY_FAILED", (finding,))
        candidate_pdf = None
        write_json(run_dir / "reports" / "quality_decision.json", decision)
        trace.append({"state": "CAPABILITY_FAILED", "owner": failure_owner})
    except Exception as exc:
        failure_owner = _next_owner(trace)
        finding = AnchoredFinding(type(exc).__name__, "HARD", failure_owner, None, None, str(exc), {})
        decision = AnchoredDecision(page_id, "FAIL", "NOT_REACHED", "PROCESS_FAILED", (finding,))
        candidate_pdf = None
        write_json(run_dir / "reports" / "quality_decision.json", decision)
        trace.append({"state": "PROCESS_FAILED", "owner": failure_owner})

    return _finish_run(source_pdf, source_hash, run_dir, page_id, candidate_pdf, decision, trace, counts, failure_owner)


def _decision(page_id: str, findings: tuple[AnchoredFinding, ...]) -> AnchoredDecision:
    process = tuple(item for item in findings if item.code in _PROCESS_FAILURE_CODES)
    capability = tuple(item for item in findings if item.code in _CAPABILITY_FAILURE_CODES)
    product = tuple(item for item in findings if item not in process and item not in capability)
    if process:
        return AnchoredDecision(page_id, "FAIL", "NOT_REACHED", "PROCESS_FAILED", findings)
    if capability:
        return AnchoredDecision(page_id, "PASS", "NOT_REACHED", "CAPABILITY_FAILED", findings)
    if product:
        return AnchoredDecision(page_id, "PASS", "FAIL", "QUALITY_FAILED", findings)
    return AnchoredDecision(page_id, "PASS", "PASS", "PAGE_PASSED", ())


def _missing_required_literals(request, bundle) -> dict[str, list[str]]:
    translated = {item.container_id: item.translated_text for item in bundle.translations}
    return {
        unit.container_id: [literal for literal in unit.required_literals if literal not in translated[unit.container_id]]
        for unit in request.units
        if any(literal not in translated[unit.container_id] for literal in unit.required_literals)
    }


def _next_owner(trace: list[dict[str, str]]) -> str:
    return {
        "SAMPLE_READY": "shared_pdf_kernel",
        "FACTS_READY": "anchored_blocks_template_builder",
        "TEMPLATE_READY": "translation_provider",
        "TRANSLATION_READY": "anchored_blocks_layout_planner",
        "PATCH_READY": "anchored_blocks_pdf_renderer",
        "CANDIDATE_READY": "anchored_blocks_quality_judge",
    }.get(trace[-1]["state"] if trace else "", "p11_orchestrator")


def _finish_run(source_pdf, source_hash, run_dir, page_id, candidate_pdf, decision, trace, counts, failure_owner=None):
    if sha256_file(source_pdf) != source_hash:
        raise RuntimeError("upstream_sample_changed_during_run")
    write_json(run_dir / "reports" / "state_trace.json", trace)
    result = P11RunResult(
        page_id,
        str(run_dir),
        str(candidate_pdf) if candidate_pdf and candidate_pdf.is_file() else None,
        decision.process_verdict,
        decision.product_verdict,
        decision.terminal_state,
        failure_owner,
        counts["blocks"],
        counts["containers"],
        counts["protected"],
    )
    write_json(run_dir / "reports" / "run_result.json", result)
    return result
