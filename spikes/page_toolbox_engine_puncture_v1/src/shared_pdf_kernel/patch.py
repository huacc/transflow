from __future__ import annotations

from pathlib import Path

import fitz

from page_toolbox_puncture.contracts import ContainerWrite, PageFacts, PagePatch
from page_toolbox_puncture.sample_snapshot import sha256_file

from .constraints import invariant_findings, preflight_patch
from .facts import extract_page_facts
from .fonts import embedded_font_resources, missing_embedded_resources
from .models import ConstraintFinding, PatchApplicationResult
from .render import outside_region_diff_ratio
from .workspace import require_under


def apply_page_patch(*, workspace_root: Path, source_pdf: Path, candidate_pdf: Path, facts: PageFacts, patch: PagePatch) -> PatchApplicationResult:
    source_pdf = require_under(source_pdf, workspace_root, must_exist=True)
    candidate_pdf = require_under(candidate_pdf, workspace_root)
    source_hash_before = sha256_file(source_pdf)
    findings = list(preflight_patch(facts, patch))
    write_evidence: list[dict[str, object]] = []

    by_id = {item.object_id: item for item in facts.text_objects}
    if not findings:
        for write in patch.writes:
            fit_code = _probe_write(facts, by_id[write.container_id].color_srgb, write)
            write_evidence.append({"container_id": write.container_id, "fit_return_code": round(float(fit_code), 4)})
            if fit_code < 0:
                findings.append(ConstraintFinding("TEXT_FIT_OVERFLOW", "HARD", "译文无法装入声明的输出 bbox", {"container_id": write.container_id, "fit_return_code": fit_code}))

    if findings:
        return _result("REJECTED", None, findings, write_evidence, facts, None, None, ())

    candidate_pdf.parent.mkdir(parents=True, exist_ok=True)
    temporary = candidate_pdf.with_suffix(candidate_pdf.suffix + ".tmp")
    with fitz.open(source_pdf) as document:
        page = document[patch.page_index]
        for write in patch.writes:
            source = by_id[write.container_id]
            page.add_redact_annot(fitz.Rect(source.bbox), fill=None)
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE, graphics=fitz.PDF_REDACT_LINE_ART_NONE, text=fitz.PDF_REDACT_TEXT_REMOVE)
        for write in patch.writes:
            source = by_id[write.container_id]
            color = _srgb_to_pdf(source.color_srgb)
            result = page.insert_textbox(
                fitz.Rect(write.output_bbox),
                write.translated_text,
                fontname=write.font_resource,
                fontfile=write.font_file,
                fontsize=write.font_size,
                lineheight=write.line_height,
                color=color,
                overlay=True,
            )
            if result < 0:
                raise RuntimeError("probe_and_render_fit_disagreed")
        document.save(temporary, garbage=4, deflate=True)
    temporary.replace(candidate_pdf)

    if sha256_file(source_pdf) != source_hash_before:
        raise RuntimeError("source_pdf_changed_during_patch")
    with fitz.open(candidate_pdf) as reopened:
        if reopened.page_count < patch.page_index + 1:
            raise RuntimeError("candidate_pdf_cannot_be_reopened")

    candidate_facts = extract_page_facts(candidate_pdf, page_index=patch.page_index, page_id=facts.page_id)
    findings.extend(invariant_findings(facts, candidate_facts))
    diff_ratio = outside_region_diff_ratio(source_pdf, candidate_pdf, [write.allowed_bbox for write in patch.writes], page_index=patch.page_index)
    if diff_ratio > 0.00001:
        findings.append(ConstraintFinding("OUTSIDE_ALLOWED_RENDER_CHANGED", "HARD", "允许区域外的渲染像素发生变化", {"changed_pixel_ratio": diff_ratio}))
    resources = embedded_font_resources(candidate_pdf, patch.page_index)
    missing_resources = missing_embedded_resources(candidate_pdf, {write.font_resource for write in patch.writes}, patch.page_index)
    if missing_resources:
        findings.append(ConstraintFinding("FONT_NOT_EMBEDDED", "HARD", "目标字体资源未嵌入候选 PDF", {"resources": missing_resources}))
    status = "APPLIED" if not findings else "REJECTED"
    return _result(status, candidate_pdf, findings, write_evidence, facts, candidate_facts, diff_ratio, resources)


def _probe_write(facts: PageFacts, color_srgb: int, write: ContainerWrite) -> float:
    with fitz.open() as probe_document:
        page = probe_document.new_page(width=facts.width, height=facts.height)
        return float(
            page.insert_textbox(
                fitz.Rect(write.output_bbox),
                write.translated_text,
                fontname=write.font_resource,
                fontfile=write.font_file,
                fontsize=write.font_size,
                lineheight=write.line_height,
                color=_srgb_to_pdf(color_srgb),
            )
        )


def _srgb_to_pdf(color: int) -> tuple[float, float, float]:
    return (((color >> 16) & 255) / 255.0, ((color >> 8) & 255) / 255.0, (color & 255) / 255.0)


def _result(status: str, candidate: Path | None, findings: list[ConstraintFinding], evidence: list[dict[str, object]], source: PageFacts, candidate_facts: PageFacts | None, diff_ratio: float | None, resources: tuple[str, ...]) -> PatchApplicationResult:
    return PatchApplicationResult(
        status=status,
        candidate_pdf=str(candidate) if candidate else None,
        candidate_sha256=sha256_file(candidate) if candidate and candidate.exists() else None,
        findings=tuple(findings),
        write_evidence=tuple(evidence),
        source_locked_objects_sha256=source.locked_objects_sha256 or "",
        candidate_locked_objects_sha256=candidate_facts.locked_objects_sha256 if candidate_facts else None,
        outside_allowed_changed_pixel_ratio=diff_ratio,
        embedded_font_resources=resources,
    )
