from __future__ import annotations

import hashlib
import shutil
import re
from dataclasses import dataclass, replace
from pathlib import Path

from page_toolbox_puncture.contracts import PageTranslationBundle, PageTranslationRequest, TranslationResult, TranslationUnit, write_json
from page_toolbox_puncture.sample_snapshot import sha256_file
from page_toolbox_puncture.translation import ProviderError, TranslationProvider
from shared_pdf_kernel.facts import extract_page_facts
from shared_pdf_kernel.render import render_contact_sheet, render_page

from .models import ToolboxDecision, ToolboxFinding
from .orchestrator.repair_loop import apply_deterministic_candidate_repairs, apply_deterministic_layout_repairs
from .p4_judge import judge_p4_candidate
from .p4_layout_planner import build_best_p4_plan
from .renderer import render_candidate
from .template_builder import build_p4_page_template


@dataclass(frozen=True)
class P4RunResult:
    page_id: str
    run_dir: str
    candidate_pdf: str | None
    process_verdict: str
    product_verdict: str
    terminal_state: str
    failure_owner: str | None
    selected_profile_id: str | None


def run_p4_page(
    *,
    source_pdf: Path,
    page_id: str,
    run_dir: Path,
    provider: TranslationProvider,
    font_file: str,
    source_language: str,
    target_language: str,
) -> P4RunResult:
    run_dir.mkdir(parents=True, exist_ok=False)
    for name in ("contracts", "docs", "input", "output", "previews", "reports"):
        (run_dir / name).mkdir()
    source_snapshot = run_dir / "input" / "source.pdf"
    shutil.copy2(source_pdf, source_snapshot)
    source_hash = sha256_file(source_snapshot)
    if source_hash != sha256_file(source_pdf):
        raise RuntimeError("source_snapshot_hash_mismatch")
    write_json(
        run_dir / "contracts" / "page_run_contract.json",
        {
            "schema_version": "p4-page-run/v1",
            "toolbox_key": "body.flow_text.single",
            "page_id": page_id,
            "source_language": source_language,
            "target_language": target_language,
            "source_snapshot": "input/source.pdf",
            "source_sha256": source_hash,
            "candidate_output": "output/candidate.pdf",
            "horizontal_rule": "normal flow x0 and column width invariant; only proven short lines may expand",
            "vertical_rule": "reflow, paragraph gaps, line height and font size may change within page and footer bounds",
            "source_is_immutable": True,
        },
    )
    (run_dir / "docs" / "README.md").write_text(
        f"# {page_id} P4 页级运行包\n\n"
        "原文在 `input/source.pdf`，候选在 `output/candidate.pdf`，并排图在 `previews/comparison.png`，修复轨迹和产品结论在 `reports/`。\n",
        encoding="utf-8",
    )
    trace: list[dict[str, str]] = [{"state": "P4_PACKAGE_READY", "owner": "p4_orchestrator"}]
    failure_owner: str | None = None
    candidate_pdf: Path | None = None
    selected_profile_id: str | None = None
    try:
        facts = extract_page_facts(source_snapshot, page_id=page_id)
        write_json(run_dir / "input" / "page_facts.json", facts)
        trace.append({"state": "P4_FACTS_READY", "owner": "shared_pdf_kernel"})
        template = build_p4_page_template(facts)
        write_json(run_dir / "input" / "page_template.json", template)
        trace.append({"state": "P4_TEMPLATE_READY", "owner": "page_template_builder"})

        units = tuple(TranslationUnit(item.container_id, item.source_text, item.reading_order) for item in template.containers)
        request = PageTranslationRequest(f"p4-{page_id}-{source_language}-{target_language}", page_id, source_language, target_language, units)
        write_json(run_dir / "input" / "translation_request.json", request)
        translation = provider.translate(request)
        translation.validate_against(request)
        # 先保留模型原始结构化返回，便于审计定向重试前后究竟改了哪个容器。
        write_json(run_dir / "output" / "translation_bundle.raw.json", translation)
        translation, translation_retries = _canonicalize_with_targeted_retry(
            request=request,
            translation=translation,
            template=template,
            provider=provider,
        )
        write_json(run_dir / "reports" / "translation_retry_trace.json", translation_retries)
        if translation_retries:
            trace.append({"state": "P4_TRANSLATION_RETRIED", "owner": "translation_provider"})
        write_json(run_dir / "output" / "translation_bundle.json", translation)
        trace.append({"state": "P4_TRANSLATION_READY", "owner": "translation_provider"})

        plan, attempts = build_best_p4_plan(
            facts=facts,
            template=template,
            translations=translation,
            source_language=source_language,
            target_language=target_language,
            font_file=font_file,
        )
        write_json(run_dir / "reports" / "repair_trace.json", attempts)
        trace.append({"state": "P4_REPAIRING" if len(attempts) > 1 else "P4_LAYOUT_READY", "owner": "p4_layout_planner"})
        if plan is None or not attempts[-1].fit:
            findings = attempts[-1].findings if attempts else (ToolboxFinding("P4_LAYOUT_UNAVAILABLE", "HARD", "p4_layout_planner", None, "未生成纵向布局计划"),)
            decision = ToolboxDecision(page_id, "PASS", "FAIL", "P4_PRODUCT_FAIL", findings)
        else:
            plan, deterministic_repairs = apply_deterministic_layout_repairs(plan)
            write_json(run_dir / "reports" / "deterministic_repair_trace.json", deterministic_repairs)
            for repair_index, repair in enumerate(deterministic_repairs, start=1):
                write_json(run_dir / "reports" / f"repair_rule_{repair_index:04d}.json", repair["rule_decision"])
                write_json(run_dir / "reports" / f"repair_patch_{repair_index:04d}.json", repair["repair_patch"])
                write_json(run_dir / "reports" / f"repair_patch_application_{repair_index:04d}.json", repair["application"])
                trace.append({"state": "P4_REPAIR_PATCH_APPLIED", "owner": "deterministic_repair_loop"})
            selected_profile_id = plan.profile_id
            write_json(run_dir / "output" / "layout_plan.json", plan)
            candidate_pdf = run_dir / "output" / "candidate.pdf"
            render_findings, render_evidence = render_candidate(
                source_pdf=source_snapshot,
                candidate_pdf=candidate_pdf,
                facts=facts,
                template=template,
                plan=plan,
                evidence_dir=run_dir / "previews",
            )
            # 候选生成后再处理必须依赖实际字形位置的图形问题，例如随译文移动的行内复选框。
            candidate_repairs = apply_deterministic_candidate_repairs(
                source_pdf=source_snapshot,
                candidate_pdf=candidate_pdf,
                facts=facts,
                template=template,
                plan=plan,
            )
            write_json(run_dir / "reports" / "deterministic_candidate_repair_trace.json", candidate_repairs)
            for repair_index, repair in enumerate(candidate_repairs, start=len(deterministic_repairs) + 1):
                write_json(run_dir / "reports" / f"repair_rule_{repair_index:04d}.json", repair["rule_decision"])
                write_json(run_dir / "reports" / f"repair_patch_{repair_index:04d}.json", repair["repair_patch"])
                write_json(run_dir / "reports" / f"repair_patch_application_{repair_index:04d}.json", repair["application"])
                trace.append({"state": "P4_REPAIR_PATCH_APPLIED", "owner": "deterministic_repair_loop"})
            if candidate_repairs:
                # 图形修补改变了最终候选，需要刷新预览；原文快照始终不动。
                render_page(source_snapshot, run_dir / "previews" / "source.png", page_index=facts.page_index, zoom=2.0)
                render_page(candidate_pdf, run_dir / "previews" / "candidate.png", page_index=facts.page_index, zoom=2.0)
                render_contact_sheet(source_snapshot, candidate_pdf, run_dir / "previews" / "comparison.png", page_index=facts.page_index, zoom=1.5)
            render_evidence = dict(render_evidence)
            render_evidence["candidate_pdf_sha256"] = sha256_file(candidate_pdf)
            render_evidence["deterministic_candidate_repair_count"] = len(candidate_repairs)
            render_evidence.update({"source_png": "previews/source.png", "candidate_png": "previews/candidate.png", "comparison_png": "previews/comparison.png"})
            write_json(run_dir / "reports" / "render_evidence.json", render_evidence)
            trace.append({"state": "P4_CANDIDATE_READY", "owner": "pdf_renderer"})
            decision = judge_p4_candidate(candidate_pdf=candidate_pdf, template=template, plan=plan, upstream_findings=render_findings)
        write_json(run_dir / "reports" / "quality_decision.json", decision)
        trace.append({"state": "P4_JUDGED", "owner": "p4_quality_judge"})
        trace.append({"state": decision.terminal_state, "owner": "p4_quality_judge"})
    except Exception as exc:
        failure_owner = _owner(trace)
        decision = ToolboxDecision(page_id, "FAIL", "NOT_REACHED", "P4_CAPABILITY_FAILED", (ToolboxFinding(type(exc).__name__, "HARD", failure_owner, None, str(exc)),))
        write_json(run_dir / "reports" / "quality_decision.json", decision)
        trace.append({"state": "P4_CAPABILITY_FAILED", "owner": failure_owner})

    write_json(run_dir / "reports" / "state_trace.json", trace)
    result = P4RunResult(page_id, str(run_dir), str(candidate_pdf) if candidate_pdf and candidate_pdf.is_file() else None, decision.process_verdict, decision.product_verdict, decision.terminal_state, failure_owner, selected_profile_id)
    write_json(run_dir / "reports" / "run_result.json", result)
    return result


def _owner(trace: list[dict[str, str]]) -> str:
    last = trace[-1]["state"] if trace else ""
    return {
        "P4_PACKAGE_READY": "shared_pdf_kernel",
        "P4_FACTS_READY": "page_template_builder",
        "P4_TEMPLATE_READY": "translation_provider",
        "P4_TRANSLATION_READY": "p4_layout_planner",
        "P4_LAYOUT_READY": "pdf_renderer",
        "P4_REPAIRING": "p4_layout_planner",
        "P4_CANDIDATE_READY": "p4_quality_judge",
    }.get(last, "p4_orchestrator")


def _canonicalize_list_markers(request, translation, template):
    source_by_id = {unit.container_id: unit.source_text for unit in request.units}
    prefix_by_id = {container.container_id: container.preserved_prefix for container in template.containers}
    forbidden_meta = (
        "the original text has a line break",
        "i will preserve it",
        "the translation is complete",
        "原文在此处有换行",
        "翻译完成",
    )
    normalized: list[TranslationResult] = []
    long_translation_sources: dict[str, tuple[str, str]] = {}
    for item in translation.translations:
        source_count = source_by_id[item.container_id].count("•")
        text = item.translated_text.replace("\uf0b7", "•")
        prefix = prefix_by_id[item.container_id]
        if prefix:
            text = re.sub(rf"^\s*{re.escape(prefix)}\s*", "", text, count=1)
        lowered = text.casefold()
        if any(phrase in lowered for phrase in forbidden_meta):
            raise ProviderError(f"P4_TRANSLATION_META_COMMENTARY:{item.container_id}")
        if source_count:
            text = text.replace("□", "•")
            text = re.sub(r"\s*•\s*", "\n• ", text).strip()
            if text.count("•") != source_count:
                raise ProviderError(f"P4_TRANSLATION_LIST_MARKER_COUNT_MISMATCH:{item.container_id}")
        duplicate_key = re.sub(r"\s+", "", text).casefold()
        source_key = re.sub(r"\s+", "", source_by_id[item.container_id]).casefold()
        if len(duplicate_key) >= 120 and duplicate_key in long_translation_sources:
            previous_id, previous_source = long_translation_sources[duplicate_key]
            if previous_source != source_key:
                raise ProviderError(f"P4_TRANSLATION_SUSPICIOUS_DUPLICATE:{previous_id}:{item.container_id}")
        long_translation_sources[duplicate_key] = (item.container_id, source_key)
        normalized.append(TranslationResult(item.container_id, text))
    return replace(translation, translations=tuple(normalized))


def _canonicalize_with_targeted_retry(*, request, translation, template, provider):
    """只重译发生结构性错误的单个容器，不放宽整页翻译校验。"""
    current = translation
    retry_trace: list[dict[str, object]] = []
    attempted: set[tuple[str, str]] = set()
    retryable_prefixes = (
        "P4_TRANSLATION_SUSPICIOUS_DUPLICATE:",
        "P4_TRANSLATION_META_COMMENTARY:",
        "P4_TRANSLATION_LIST_MARKER_COUNT_MISMATCH:",
    )
    # 每个请求仍只包含一个容器；重复译文冲突的两端分别重问，不能猜哪一端出错。
    while True:
        try:
            return _canonicalize_list_markers(request, current, template), tuple(retry_trace)
        except ProviderError as exc:
            if not exc.code.startswith(retryable_prefixes):
                raise
            parts = exc.code.split(":")
            target_container_ids = parts[-2:] if exc.code.startswith("P4_TRANSLATION_SUSPICIOUS_DUPLICATE:") else parts[-1:]
            for target_container_id in target_container_ids:
                attempt_key = (exc.code, target_container_id)
                # 同一冲突端已单独重问仍失败时保持硬失败，避免无限循环或放行可疑译文。
                if attempt_key in attempted:
                    raise
                attempted.add(attempt_key)
                target_unit = next(
                    (unit for unit in request.units if unit.container_id == target_container_id),
                    None,
                )
                if target_unit is None:
                    raise
                retry_index = len(retry_trace) + 1
                retry_request = PageTranslationRequest(
                    f"{request.request_id}-retry-{retry_index}-{target_container_id}",
                    request.page_id,
                    request.source_language,
                    request.target_language,
                    (target_unit,),
                )
                retry_bundle = provider.translate(retry_request)
                retry_bundle.validate_against(retry_request)
                replacement = retry_bundle.translations[0]
                merged = tuple(
                    replacement if item.container_id == target_container_id else item
                    for item in current.translations
                )
                current = PageTranslationBundle(
                    request_id=request.request_id,
                    page_id=request.page_id,
                    provider=current.provider,
                    model=current.model,
                    translations=merged,
                    provider_request_id=_join_optional(current.provider_request_id, retry_bundle.provider_request_id),
                    latency_ms=_sum_optional(current.latency_ms, retry_bundle.latency_ms),
                    response_sha256=_combine_response_hashes(current.response_sha256, retry_bundle.response_sha256),
                )
                current.validate_against(request)
                retry_trace.append(
                    {
                        "retry_index": retry_index,
                        "trigger": exc.code,
                        "target_container_id": target_container_id,
                        "retry_request_id": retry_request.request_id,
                        "provider_request_id": retry_bundle.provider_request_id,
                        "latency_ms": retry_bundle.latency_ms,
                        "response_sha256": retry_bundle.response_sha256,
                    }
                )


def _join_optional(left: str | None, right: str | None) -> str | None:
    values = [value for value in (left, right) if value]
    return ",".join(values) or None


def _sum_optional(left: int | None, right: int | None) -> int | None:
    if left is None and right is None:
        return None
    return (left or 0) + (right or 0)


def _combine_response_hashes(left: str | None, right: str | None) -> str | None:
    values = [value for value in (left, right) if value]
    if not values:
        return None
    return hashlib.sha256("".join(values).encode("ascii")).hexdigest()
