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

from .layout_planner import layout_rule_trace, materialize_translated_diagnostic_plan, plan_chart_layout
from .models import ChartDecision, ChartFinding, ChartTemplate
from .renderer import render_chart_candidate
from .template_builder import ChartCapabilityError, build_chart_template


@dataclass(frozen=True)
class P13RunResult:
    page_id: str
    run_dir: str
    candidate_pdf: str | None
    process_verdict: str
    product_verdict: str
    terminal_state: str
    failure_owner: str | None
    visual_region_count: int
    container_count: int
    requested_container_count: int
    protected_object_count: int
    passthrough: bool


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


def build_chart_translation_request(
    template: ChartTemplate,
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
        request_id=f"p13-{template.page_id}-{source_language}-{target_language}",
        page_id=template.page_id,
        source_language=source_language,
        target_language=target_language,
        units=units,
    )


def run_p13_page(
    *,
    source_pdf: Path,
    page_id: str,
    run_dir: Path,
    provider: TranslationProvider,
    font_file: str,
    bold_font_file: str | None,
    source_language: str,
    target_language: str,
) -> P13RunResult:
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
            "schema_version": "p13-body-chart-page-run/v1",
            "toolbox_key": "body.chart",
            "page_id": page_id,
            "source_language": source_language,
            "target_language": target_language,
            "source_snapshot": "input/source.pdf",
            "source_sha256": source_hash,
            "candidate_output": "output/candidate.pdf",
            "translation_rule": "only located native semantic chart text enters one page-level request",
            "immutable_rule": "images, drawings, axes, ticks, values, data labels, swatches, colors, geometry, and source PDF are immutable",
            "passthrough_rule": "pages without target-language native semantic text are copied byte-for-byte; image text is never OCR-overlaid",
            "source_is_immutable": True,
        },
    )
    (run_dir / "docs" / "README.md").write_text(
        f"# {page_id} P13 body.chart 运行包\n\n"
        "input 保存源页、机械事实、图表所有权模板和页级翻译请求；output 保存真实译文包、布局计划和候选 PDF；"
        "reports 与 previews 保存过程/产品裁决和源候选视觉证据。\n",
        encoding="utf-8",
    )

    trace: list[dict[str, str]] = [{"state": "SAMPLE_READY", "owner": "p13_orchestrator"}]
    candidate_pdf: Path | None = None
    failure_owner: str | None = None
    counts = {"regions": 0, "containers": 0, "requested": 0, "protected": 0}
    passthrough = False
    try:
        facts = extract_page_facts(source_snapshot, page_id=page_id)
        write_json(run_dir / "input" / "page_facts.json", facts)
        trace.append({"state": "FACTS_READY", "owner": "shared_pdf_kernel"})

        template = build_chart_template(facts)
        counts = {
            "regions": len(template.visual_regions),
            "containers": len(template.containers),
            "requested": 0,
            "protected": len(template.protected_object_ids),
        }
        write_json(run_dir / "input" / "page_template.json", template)
        trace.append({"state": "TEMPLATE_READY", "owner": "chart_template_builder"})

        request = build_chart_translation_request(template, source_language, target_language)
        if request is None:
            passthrough = True
            candidate_pdf = run_dir / "output" / "candidate.pdf"
            shutil.copy2(source_snapshot, candidate_pdf)
            if sha256_file(candidate_pdf) != source_hash:
                raise RuntimeError("chart_passthrough_hash_mismatch")
            write_json(
                run_dir / "reports" / "passthrough_evidence.json",
                {
                    "reason": "NO_TARGET_LANGUAGE_NATIVE_SEMANTIC_CHART_TEXT",
                    "source_sha256": source_hash,
                    "candidate_sha256": sha256_file(candidate_pdf),
                    "byte_identical": True,
                    "image_text_modified": False,
                },
            )
            render_page(source_snapshot, run_dir / "previews" / "source.png", zoom=2.0)
            render_page(candidate_pdf, run_dir / "previews" / "candidate.png", zoom=2.0)
            render_contact_sheet(source_snapshot, candidate_pdf, run_dir / "previews" / "comparison.png", zoom=1.5)
            trace.extend(
                [
                    {"state": "CANDIDATE_READY", "owner": "chart_byte_passthrough"},
                    {"state": "QUALITY_DECIDED", "owner": "chart_quality_judge"},
                    {"state": "PAGE_PASSED", "owner": "chart_quality_judge"},
                ]
            )
            decision = ChartDecision(page_id, "PASS", "PASS", "PAGE_PASSED", ())
            write_json(run_dir / "reports" / "quality_decision.json", decision)
            return _finish_run(source_pdf, source_hash, run_dir, page_id, candidate_pdf, decision, trace, counts, passthrough=True)

        counts["requested"] = len(request.units)
        write_json(run_dir / "input" / "translation_request.json", request)
        bundle = provider.translate(request)
        bundle.validate_against(request)
        write_json(run_dir / "output" / "translation_bundle.raw.json", bundle)
        validation = translation_validation(request, bundle)
        write_json(run_dir / "reports" / "translation_validation.json", validation)
        validation_findings = _translation_validation_findings(validation)
        if validation_findings:
            trace.append({"state": "TRANSLATION_REJECTED", "owner": "translation_provider"})
        else:
            write_json(run_dir / "output" / "translation_bundle.json", bundle)
            trace.append({"state": "TRANSLATION_READY", "owner": "translation_provider"})

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
        candidate_pdf = run_dir / "output" / "candidate.pdf"
        translated_unfit_container_ids = [item.container_id for item in plan.placements if not item.fit]
        render_template = template
        render_plan = plan
        diagnostic_materialization: tuple[dict[str, object], ...] = ()
        if translated_unfit_container_ids:
            render_template, render_plan, diagnostic_materialization = materialize_translated_diagnostic_plan(
                template,
                plan,
            )
            write_json(run_dir / "output" / "diagnostic_layout_plan.json", render_plan)
            write_json(run_dir / "reports" / "diagnostic_layout_trace.json", diagnostic_materialization)
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
                "diagnostic_candidate": bool(validation_findings or plan_findings),
                "translation_validation_failure_codes": [item.code for item in validation_findings],
                "translated_unfit_container_ids": translated_unfit_container_ids,
                "omitted_unfit_container_ids": [],
                "diagnostic_materialization": diagnostic_materialization,
            }
        )
        write_json(run_dir / "reports" / "render_evidence.json", render_evidence)
        trace.append({"state": "CANDIDATE_READY", "owner": "chart_pdf_renderer"})
        decision = _decision(page_id, (*validation_findings, *plan_findings, *render_findings))
        write_json(run_dir / "reports" / "quality_decision.json", decision)
        trace.extend(
            [
                {"state": "QUALITY_DECIDED", "owner": "chart_quality_judge"},
                {"state": decision.terminal_state, "owner": "chart_quality_judge"},
            ]
        )
        if decision.terminal_state != "PAGE_PASSED":
            failure_owner = decision.findings[0].owner if decision.findings else "chart_quality_judge"
    except (ChartCapabilityError, ProviderError) as exc:
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

    return _finish_run(source_pdf, source_hash, run_dir, page_id, candidate_pdf, decision, trace, counts, failure_owner, passthrough)


def translation_validation(request, bundle) -> dict[str, object]:
    translated = {item.container_id: item.translated_text for item in bundle.translations}
    cross_container_duplicates = _cross_container_duplicate_outputs(request, translated)
    missing = {
        unit.container_id: [literal for literal in unit.required_literals if literal not in translated[unit.container_id]]
        for unit in request.units
        if any(literal not in translated[unit.container_id] for literal in unit.required_literals)
    }
    residue = {}
    if request.target_language.casefold().startswith("en"):
        residue = {
            unit.container_id: sorted(set(re.findall(r"[\u3400-\u9fff]", translated[unit.container_id])))
            for unit in request.units
            if re.search(r"[\u3400-\u9fff]", translated[unit.container_id])
        }
    elif request.target_language.casefold().startswith("zh"):
        for unit in request.units:
            semantic_text = translated[unit.container_id]
            for literal in unit.required_literals:
                semantic_text = semantic_text.replace(literal, "")
            if _retained_standalone_acronym(unit.source_text, semantic_text):
                continue
            if re.search(r"[A-Za-z]", semantic_text) and not re.search(r"[\u3400-\u9fff]", semantic_text):
                residue[unit.container_id] = sorted(set(re.findall(r"[A-Za-z]+", semantic_text)))
    placeholders = [
        unit.container_id
        for unit in request.units
        if _placeholder_output(translated[unit.container_id])
    ]
    inadequate = {
        unit.container_id: reason
        for unit in request.units
        if (
            reason := _inadequate_output(
                unit.source_text,
                translated[unit.container_id],
                request.source_language,
                request.target_language,
            )
        )
    }
    magnitude_mismatches = {}
    if request.source_language.casefold().startswith("zh") and request.target_language.casefold().startswith("en"):
        for unit in request.units:
            expected = _expected_english_magnitude_markers(unit.source_text)
            translated_text = translated[unit.container_id]
            missing_markers = [
                marker
                for marker in expected
                if not _english_magnitude_present(marker, translated_text)
            ]
            if missing_markers:
                magnitude_mismatches[unit.container_id] = missing_markers
    return {
        "status": "FAIL" if missing or residue or placeholders or inadequate or magnitude_mismatches or cross_container_duplicates else "PASS",
        "missing_required_literals": missing,
        "source_language_residue": residue,
        "placeholder_outputs": placeholders,
        "inadequate_outputs": inadequate,
        "magnitude_unit_mismatches": magnitude_mismatches,
        "cross_container_duplicate_outputs": cross_container_duplicates,
    }


def _cross_container_duplicate_outputs(request, translated: dict[str, str]) -> dict[str, list[str]]:
    by_output: dict[str, list[str]] = {}
    source_by_id = {unit.container_id: re.sub(r"\s+", "", unit.source_text).casefold() for unit in request.units}
    for unit in request.units:
        text = translated[unit.container_id]
        if request.target_language.casefold().startswith("en"):
            long_output = len(re.findall(r"[A-Za-z]+", text)) >= 12
        elif request.target_language.casefold().startswith("zh"):
            long_output = len(re.findall(r"[\u3400-\u9fff]", text)) >= 20
        else:
            long_output = len(text) >= 80
        if long_output:
            by_output.setdefault(re.sub(r"\s+", " ", text).strip().casefold(), []).append(unit.container_id)

    duplicates: dict[str, list[str]] = {}
    for container_ids in by_output.values():
        if len(container_ids) < 2 or len({source_by_id[container_id] for container_id in container_ids}) < 2:
            continue
        for container_id in container_ids:
            duplicates[container_id] = [other for other in container_ids if other != container_id]
    return duplicates


def _expected_english_magnitude_markers(source_text: str) -> tuple[str, ...]:
    expected: list[str] = []
    unit_suffix = r"(?:元|户|戶|人|吨|噸|平方米|平米|件|次|股|份|个|個|家|公里|千瓦|度)?"
    for source_unit, marker in (("万", "ten-thousand"), ("萬", "ten-thousand"), ("亿", "hundred-million"), ("億", "hundred-million")):
        standalone_unit = rf"(?<![百千]){source_unit}"
        if re.search(rf"(?:\d[\d,.]*\s*{standalone_unit}|{standalone_unit}{unit_suffix})", source_text):
            expected.append(marker)
    return tuple(dict.fromkeys(expected))


def _english_magnitude_present(marker: str, translated_text: str) -> bool:
    patterns = {
        "ten-thousand": r"(?:\bten[-\s]thousand\b|(?<!\d)10[ ,]?000(?!\d))",
        "hundred-million": r"(?:\bhundred[-\s]million\b|(?<!\d)100[ -]?million\b)",
    }
    return bool(re.search(patterns[marker], translated_text, re.IGNORECASE))


def _retained_standalone_acronym(source_text: str, translated_text: str) -> bool:
    source = re.fullmatch(r"([A-Z]{2,6})s?", source_text.strip())
    target = re.fullmatch(r"([A-Z]{2,6})s?", translated_text.strip())
    return bool(source and target and source.group(1) == target.group(1))


def _translation_validation_findings(validation: dict[str, object]) -> tuple[ChartFinding, ...]:
    findings: list[ChartFinding] = []
    checks = (
        ("TRANSLATION_REQUIRED_LITERAL_MISSING", "missing_required_literals"),
        ("TRANSLATION_SOURCE_LANGUAGE_RESIDUE", "source_language_residue"),
        ("TRANSLATION_MAGNITUDE_UNIT_MISMATCH", "magnitude_unit_mismatches"),
        ("TRANSLATION_PLACEHOLDER_OUTPUT", "placeholder_outputs"),
        ("TRANSLATION_INADEQUATE_OUTPUT", "inadequate_outputs"),
        ("TRANSLATION_CROSS_CONTAINER_DUPLICATE", "cross_container_duplicate_outputs"),
    )
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
                "Translation contract validation failed; candidate PDF is diagnostic only.",
                {"details": details},
            )
        )
    return tuple(findings)


def _placeholder_output(text: str) -> bool:
    compact = re.sub(r"\s+", "", text)
    if not compact:
        return True
    if re.search(r"\[\[P13_KEEP_\d+\]\]", compact):
        return True
    placeholder_count = sum(character in "?？□�" for character in compact)
    return placeholder_count >= 2 and placeholder_count / len(compact) >= 0.5


def _inadequate_output(source: str, translated: str, source_language: str, target_language: str) -> dict[str, int | str] | None:
    if source_language.casefold().startswith("zh") and target_language.casefold().startswith("en"):
        source_count = len(re.findall(r"[\u3400-\u9fff]", source))
        if source_count < 20:
            return None
        target_count = len(re.findall(r"[A-Za-z]+", translated))
        minimum = max(3, (source_count + 7) // 8)
    elif source_language.casefold().startswith("en") and target_language.casefold().startswith("zh"):
        source_count = len(re.findall(r"[A-Za-z]+", source))
        if source_count < 12:
            return None
        target_count = len(re.findall(r"[\u3400-\u9fff]", translated))
        minimum = max(3, (source_count + 2) // 3)
    else:
        return None
    if target_count >= minimum:
        return None
    return {"reason": "SUSPICIOUSLY_SHORT", "source_semantic_count": source_count, "target_semantic_count": target_count, "minimum": minimum}


def _decision(page_id: str, findings: tuple[ChartFinding, ...]) -> ChartDecision:
    process = tuple(item for item in findings if item.code in _PROCESS_FAILURE_CODES)
    capability = tuple(item for item in findings if item.code in _CAPABILITY_FAILURE_CODES)
    product = tuple(item for item in findings if item not in process and item not in capability)
    if process:
        return ChartDecision(page_id, "FAIL", "NOT_REACHED", "PROCESS_FAILED", findings)
    if capability:
        return ChartDecision(page_id, "PASS", "NOT_REACHED", "CAPABILITY_FAILED", findings)
    if product:
        return ChartDecision(page_id, "PASS", "FAIL", "QUALITY_FAILED", findings)
    return ChartDecision(page_id, "PASS", "PASS", "PAGE_PASSED", ())


def _requires_translation(text: str, target_language: str) -> bool:
    has_cjk = bool(re.search(r"[\u3400-\u9fff]", text))
    has_latin = bool(re.search(r"[A-Za-z]", text))
    if target_language.casefold().startswith("zh"):
        return has_latin and not has_cjk
    if target_language.casefold().startswith("en"):
        return has_cjk
    return has_cjk or has_latin


def _next_owner(trace: list[dict[str, str]]) -> str:
    return {
        "SAMPLE_READY": "shared_pdf_kernel",
        "FACTS_READY": "chart_template_builder",
        "TEMPLATE_READY": "translation_provider",
        "TRANSLATION_REJECTED": "chart_layout_planner",
        "TRANSLATION_READY": "chart_layout_planner",
        "PATCH_READY": "chart_pdf_renderer",
        "CANDIDATE_READY": "chart_quality_judge",
    }.get(trace[-1]["state"] if trace else "", "p13_orchestrator")


def _finish_run(source_pdf, source_hash, run_dir, page_id, candidate_pdf, decision, trace, counts, failure_owner=None, passthrough=False):
    if sha256_file(source_pdf) != source_hash:
        raise RuntimeError("upstream_sample_changed_during_run")
    if candidate_pdf is None or not candidate_pdf.is_file():
        candidate_pdf = _copy_source_diagnostic_candidate(
            run_dir,
            source_hash,
            reason="TERMINAL_FAILURE_BEFORE_RENDER",
            failure_codes=[item.code for item in decision.findings],
        )
    write_json(run_dir / "reports" / "state_trace.json", trace)
    result = P13RunResult(
        page_id=page_id,
        run_dir=str(run_dir),
        candidate_pdf=str(candidate_pdf) if candidate_pdf and candidate_pdf.is_file() else None,
        process_verdict=decision.process_verdict,
        product_verdict=decision.product_verdict,
        terminal_state=decision.terminal_state,
        failure_owner=failure_owner,
        visual_region_count=counts["regions"],
        container_count=counts["containers"],
        requested_container_count=counts["requested"],
        protected_object_count=counts["protected"],
        passthrough=bool(passthrough),
    )
    write_json(run_dir / "reports" / "run_result.json", result)
    return result


def _copy_source_diagnostic_candidate(
    run_dir: Path,
    source_hash: str,
    *,
    reason: str,
    failure_codes: list[str],
    omitted_container_ids: list[str] | None = None,
) -> Path:
    source_pdf = run_dir / "input" / "source.pdf"
    candidate_pdf = run_dir / "output" / "candidate.pdf"
    temporary = candidate_pdf.with_suffix(candidate_pdf.suffix + ".tmp")
    shutil.copy2(source_pdf, temporary)
    temporary.replace(candidate_pdf)
    if sha256_file(candidate_pdf) != source_hash:
        raise RuntimeError("diagnostic_candidate_hash_mismatch")
    evidence: dict[str, object] = {
        "artifact_kind": "SOURCE_COPY_DIAGNOSTIC_FALLBACK",
        "diagnostic_candidate": True,
        "reason": reason,
        "failure_codes": failure_codes,
        "omitted_unfit_container_ids": omitted_container_ids or [],
        "source_sha256": source_hash,
        "candidate_sha256": sha256_file(candidate_pdf),
        "byte_identical": True,
    }
    try:
        render_page(source_pdf, run_dir / "previews" / "source.png", zoom=2.0)
        render_page(candidate_pdf, run_dir / "previews" / "candidate.png", zoom=2.0)
        render_contact_sheet(source_pdf, candidate_pdf, run_dir / "previews" / "comparison.png", zoom=1.5)
    except Exception as exc:
        evidence["preview_error"] = f"{type(exc).__name__}: {exc}"
    write_json(run_dir / "reports" / "candidate_artifact.json", evidence)
    return candidate_pdf
