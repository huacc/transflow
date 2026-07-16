from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from page_toolbox_puncture.contracts import PageTranslationRequest, TranslationUnit, write_json
from page_toolbox_puncture.sample_snapshot import sha256_file
from page_toolbox_puncture.translation import ProviderError, TranslationProvider
from shared_pdf_kernel.facts import extract_page_facts

from .layout_planner import plan_visual_anchored_layout
from .models import VisualAnchoredDecision, VisualAnchoredFinding, VisualAnchoredTemplate
from .renderer import render_visual_anchored_candidate
from .template_builder import VisualAnchoredCapabilityError, build_visual_anchored_template


@dataclass(frozen=True)
class P12RunResult:
    page_id: str
    run_dir: str
    candidate_pdf: str | None
    process_verdict: str
    product_verdict: str
    terminal_state: str
    failure_owner: str | None
    slot_count: int
    container_count: int
    protected_object_count: int


_PROCESS_FAILURE_CODES = {
    "VISUAL_LOCKED_OBJECT_CHANGED",
    "VISUAL_PROTECTED_TEXT_CHANGED",
    "VISUAL_OUTSIDE_SLOT_CHANGED",
}
_CAPABILITY_FAILURE_CODES = {
    "FONT_NOT_EMBEDDED",
    "FONT_GLYPH_MISSING",
    "VISUAL_CONTRAST_LOW",
    "VISUAL_SLOT_OVERFLOW",
}


def build_visual_anchored_translation_request(
    template: VisualAnchoredTemplate,
    source_language: str,
    target_language: str,
) -> PageTranslationRequest:
    units = tuple(
        TranslationUnit(
            container_id=container.container_id,
            source_text=container.source_text,
            reading_order=container.reading_order,
            required_literals=container.required_literals,
        )
        for container in template.containers
        if _requires_translation(container.source_text, target_language)
        and not _preserved_inline_acronym(container, template, target_language)
    )
    if not units:
        raise VisualAnchoredCapabilityError("VISUAL_ANCHORED_NO_TRANSLATION_REQUIRED")
    return PageTranslationRequest(
        request_id=f"p12-{template.page_id}-{source_language}-{target_language}",
        page_id=template.page_id,
        source_language=source_language,
        target_language=target_language,
        units=units,
    )


def run_p12_page(
    *,
    source_pdf: Path,
    page_id: str,
    run_dir: Path,
    provider: TranslationProvider,
    font_file: str,
    bold_font_file: str | None,
    source_language: str,
    target_language: str,
) -> P12RunResult:
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
            "schema_version": "p12-visual-anchored-page-run/v1",
            "toolbox_key": "body.flow_text.visual_anchored",
            "page_id": page_id,
            "source_language": source_language,
            "target_language": target_language,
            "source_snapshot": "input/source.pdf",
            "source_sha256": source_hash,
            "candidate_output": "output/candidate.pdf",
            "ownership_rule": "every editable native text unit binds one and only one VisualTextSlot",
            "translation_rule": "image pixels never enter the request; returned IDs must preserve slot order",
            "layout_rule": "wrap, leading, and bounded font reduction occur only inside the original slot",
            "immutable_rule": "page geometry, images, panels, colors, bounds, z-order, protected text, and source PDF are immutable",
            "source_is_immutable": True,
        },
    )
    (run_dir / "docs" / "README.md").write_text(
        f"# {page_id} P12 visual_anchored 运行包\n\n"
        "`input/` 保存源页、事实、VisualTextSlot 模板和翻译请求；`output/` 保存真实译文包、槽内布局计划和候选页；"
        "`reports/` 与 `previews/` 保存双裁决和源候选渲染证据。\n",
        encoding="utf-8",
    )

    trace: list[dict[str, str]] = [{"state": "SAMPLE_READY", "owner": "p12_orchestrator"}]
    candidate_pdf: Path | None = None
    failure_owner: str | None = None
    counts = {"slots": 0, "containers": 0, "protected": 0}
    try:
        facts = extract_page_facts(source_snapshot, page_id=page_id)
        write_json(run_dir / "input" / "page_facts.json", facts)
        trace.append({"state": "FACTS_READY", "owner": "shared_pdf_kernel"})

        template = build_visual_anchored_template(facts, source_snapshot)
        counts = {
            "slots": len(template.visual_slots),
            "containers": len(template.containers),
            "protected": len(template.protected_object_ids),
        }
        write_json(run_dir / "input" / "page_template.json", template)
        trace.append({"state": "TEMPLATE_READY", "owner": "visual_anchored_template_builder"})

        request = build_visual_anchored_translation_request(template, source_language, target_language)
        write_json(run_dir / "input" / "translation_request.json", request)
        bundle = provider.translate(request)
        bundle.validate_against(request)
        write_json(run_dir / "output" / "translation_bundle.raw.json", bundle)
        validation = translation_validation(request, bundle)
        write_json(
            run_dir / "reports" / "translation_validation.json",
            validation,
        )
        if validation["missing_required_literals"]:
            raise ProviderError("TRANSLATION_REQUIRED_LITERAL_MISSING")
        if validation["source_language_residue"]:
            raise ProviderError("TRANSLATION_SOURCE_LANGUAGE_RESIDUE")
        if validation["structurally_incomplete_translations"]:
            raise ProviderError("TRANSLATION_STRUCTURALLY_INCOMPLETE")
        write_json(run_dir / "output" / "translation_bundle.json", bundle)
        trace.append({"state": "TRANSLATION_READY", "owner": "translation_provider"})

        plan, plan_findings = plan_visual_anchored_layout(
            template,
            bundle,
            font_file=font_file,
            bold_font_file=bold_font_file,
        )
        write_json(run_dir / "output" / "layout_plan.json", plan)
        write_json(run_dir / "reports" / "layout_findings.json", plan_findings)
        trace.append({"state": "PATCH_READY", "owner": "visual_anchored_layout_planner"})
        if plan_findings:
            decision = _decision(page_id, plan_findings)
            write_json(run_dir / "reports" / "quality_decision.json", decision)
            trace.extend(
                [
                    {"state": "QUALITY_DECIDED", "owner": "visual_anchored_quality_judge"},
                    {"state": decision.terminal_state, "owner": "visual_anchored_quality_judge"},
                ]
            )
            return _finish_run(source_pdf, source_hash, run_dir, page_id, None, decision, trace, counts)

        candidate_pdf = run_dir / "output" / "candidate.pdf"
        render_findings, render_evidence = render_visual_anchored_candidate(
            source_pdf=source_snapshot,
            candidate_pdf=candidate_pdf,
            facts=facts,
            template=template,
            plan=plan,
            evidence_dir=run_dir / "previews",
        )
        write_json(run_dir / "reports" / "render_evidence.json", render_evidence)
        trace.append({"state": "CANDIDATE_READY", "owner": "visual_anchored_pdf_renderer"})
        decision = _decision(page_id, render_findings)
        write_json(run_dir / "reports" / "quality_decision.json", decision)
        trace.extend(
            [
                {"state": "QUALITY_DECIDED", "owner": "visual_anchored_quality_judge"},
                {"state": decision.terminal_state, "owner": "visual_anchored_quality_judge"},
            ]
        )
        if decision.terminal_state != "PAGE_PASSED":
            failure_owner = decision.findings[0].owner if decision.findings else "visual_anchored_quality_judge"
    except (VisualAnchoredCapabilityError, ProviderError) as exc:
        failure_owner = _next_owner(trace)
        code = exc.code if isinstance(exc, ProviderError) else str(exc).split(":", 1)[0]
        finding = VisualAnchoredFinding(code, "HARD", failure_owner, None, None, str(exc), {})
        decision = VisualAnchoredDecision(page_id, "PASS", "NOT_REACHED", "CAPABILITY_FAILED", (finding,))
        candidate_pdf = None
        write_json(run_dir / "reports" / "quality_decision.json", decision)
        trace.append({"state": "CAPABILITY_FAILED", "owner": failure_owner})
    except Exception as exc:
        failure_owner = _next_owner(trace)
        finding = VisualAnchoredFinding(type(exc).__name__, "HARD", failure_owner, None, None, str(exc), {})
        decision = VisualAnchoredDecision(page_id, "FAIL", "NOT_REACHED", "PROCESS_FAILED", (finding,))
        candidate_pdf = None
        write_json(run_dir / "reports" / "quality_decision.json", decision)
        trace.append({"state": "PROCESS_FAILED", "owner": failure_owner})

    return _finish_run(source_pdf, source_hash, run_dir, page_id, candidate_pdf, decision, trace, counts, failure_owner)


def _decision(page_id: str, findings: tuple[VisualAnchoredFinding, ...]) -> VisualAnchoredDecision:
    process = tuple(item for item in findings if item.code in _PROCESS_FAILURE_CODES)
    capability = tuple(item for item in findings if item.code in _CAPABILITY_FAILURE_CODES)
    product = tuple(item for item in findings if item not in process and item not in capability)
    if process:
        return VisualAnchoredDecision(page_id, "FAIL", "NOT_REACHED", "PROCESS_FAILED", findings)
    if capability:
        return VisualAnchoredDecision(page_id, "PASS", "NOT_REACHED", "CAPABILITY_FAILED", findings)
    if product:
        return VisualAnchoredDecision(page_id, "PASS", "FAIL", "QUALITY_FAILED", findings)
    return VisualAnchoredDecision(page_id, "PASS", "PASS", "PAGE_PASSED", ())


def translation_validation(request, bundle) -> dict[str, object]:
    missing_literals = _missing_required_literals(request, bundle)
    source_residue = _source_language_residue(request, bundle)
    incomplete_translations = _structurally_incomplete_translations(request, bundle)
    return {
        "status": "FAIL" if missing_literals or source_residue or incomplete_translations else "PASS",
        "missing_required_literals": missing_literals,
        "source_language_residue": source_residue,
        "structurally_incomplete_translations": incomplete_translations,
    }


def _missing_required_literals(request, bundle) -> dict[str, list[str]]:
    translated = {item.container_id: item.translated_text for item in bundle.translations}
    return {
        unit.container_id: [literal for literal in unit.required_literals if literal not in translated[unit.container_id]]
        for unit in request.units
        if any(literal not in translated[unit.container_id] for literal in unit.required_literals)
    }


def _source_language_residue(request, bundle) -> dict[str, list[str]]:
    if not request.target_language.casefold().startswith("en"):
        return {}
    translated = {item.container_id: item.translated_text for item in bundle.translations}
    return {
        unit.container_id: sorted(set(re.findall(r"[\u3400-\u9fff]", translated[unit.container_id])))
        for unit in request.units
        if re.search(r"[\u3400-\u9fff]", translated[unit.container_id])
    }


def _structurally_incomplete_translations(request, bundle) -> list[str]:
    unit_by_id = {unit.container_id: unit for unit in request.units}
    incomplete = []
    for item in bundle.translations:
        unit = unit_by_id[item.container_id]
        source = unit.source_text
        text = item.translated_text.strip()
        if not text or _has_unbalanced_delimiters(text):
            incomplete.append(item.container_id)
            continue
        if request.target_language.casefold().startswith("en"):
            dangling = re.search(r"\b(?:a|an|the|of|to|and|or|for|with|in|on|by|as)\s*$", text, flags=re.IGNORECASE)
            terminal = dangling.group(0).strip() if dangling else ""
            required_zone_marker = terminal == "A" and "A" in unit.required_literals
            if dangling and not required_zone_marker and not _source_allows_terminal_conjunction(source, terminal):
                incomplete.append(item.container_id)
                continue
        source_compact = re.sub(r"\s+", "", source)
        target_compact = re.sub(r"\s+", "", text)
        if (
            len(source_compact) >= 24
            and re.search(r"[。！？；.!?;]$", source_compact)
            and len(target_compact) < len(source_compact) * 0.15
            and not re.search(r"[。！？；.!?;:：)）\]】}\"'”’]$", target_compact)
        ):
            incomplete.append(item.container_id)
    return incomplete


def _has_unbalanced_delimiters(text: str) -> bool:
    return any(text.count(left) != text.count(right) for left, right in (("(", ")"), ("[", "]"), ("{", "}"), ("「", "」"), ("“", "”")))


def _source_allows_terminal_conjunction(source: str, terminal: str) -> bool:
    return terminal.casefold() in {"and", "or"} and bool(re.search(r"(?:[；;]\s*)?(?:以及|及|和|或)\s*$", source))


def _requires_translation(text: str, target_language: str) -> bool:
    has_cjk = bool(re.search(r"[\u3400-\u9fff]", text))
    has_latin = bool(re.search(r"[A-Za-z]", text))
    if target_language.casefold().startswith("zh"):
        if has_cjk and _latin_is_inline_identifiers(text):
            return False
        return has_latin
    if target_language.casefold().startswith("en"):
        return has_cjk
    return has_cjk or has_latin


def _latin_is_inline_identifiers(text: str) -> bool:
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9+./-]*", text)
    return bool(tokens) and all(
        re.fullmatch(r"[A-Z][A-Z0-9+./-]{0,15}", token)
        for token in tokens
    )


def _preserved_inline_acronym(container, template: VisualAnchoredTemplate, target_language: str) -> bool:
    if not target_language.casefold().startswith("zh"):
        return False
    if not re.fullmatch(r"[A-Z][A-Z0-9+./-]{1,15}", container.source_text.strip()):
        return False
    for other in template.containers:
        if other.container_id == container.container_id or not re.search(r"[\u3400-\u9fff]", other.source_text):
            continue
        overlap = max(
            0.0,
            min(container.source_bbox[3], other.source_bbox[3]) - max(container.source_bbox[1], other.source_bbox[1]),
        )
        height = min(
            container.source_bbox[3] - container.source_bbox[1],
            other.source_bbox[3] - other.source_bbox[1],
        )
        gap = max(
            0.0,
            container.source_bbox[0] - other.source_bbox[2],
            other.source_bbox[0] - container.source_bbox[2],
        )
        if overlap >= height * 0.5 and gap <= max(container.font_size, other.font_size) * 1.5:
            return True
    return False


def _next_owner(trace: list[dict[str, str]]) -> str:
    return {
        "SAMPLE_READY": "shared_pdf_kernel",
        "FACTS_READY": "visual_anchored_template_builder",
        "TEMPLATE_READY": "translation_provider",
        "TRANSLATION_READY": "visual_anchored_layout_planner",
        "PATCH_READY": "visual_anchored_pdf_renderer",
        "CANDIDATE_READY": "visual_anchored_quality_judge",
    }.get(trace[-1]["state"] if trace else "", "p12_orchestrator")


def _finish_run(source_pdf, source_hash, run_dir, page_id, candidate_pdf, decision, trace, counts, failure_owner=None):
    if sha256_file(source_pdf) != source_hash:
        raise RuntimeError("upstream_sample_changed_during_run")
    write_json(run_dir / "reports" / "state_trace.json", trace)
    result = P12RunResult(
        page_id=page_id,
        run_dir=str(run_dir),
        candidate_pdf=str(candidate_pdf) if candidate_pdf and candidate_pdf.is_file() else None,
        process_verdict=decision.process_verdict,
        product_verdict=decision.product_verdict,
        terminal_state=decision.terminal_state,
        failure_owner=failure_owner,
        slot_count=counts["slots"],
        container_count=counts["containers"],
        protected_object_count=counts["protected"],
    )
    write_json(run_dir / "reports" / "run_result.json", result)
    return result
