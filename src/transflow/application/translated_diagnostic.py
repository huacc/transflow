"""使用 SharedPdfKernel 安全物化、验证并登记隔离翻译诊断 PDF。"""

from __future__ import annotations

import hashlib
import logging
import shutil
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pymupdf

from transflow.application.contracts import AtomicArtifactStore
from transflow.domain.artifacts import ArtifactPayload
from transflow.domain.completeness import (
    CompletenessDisposition,
    CompletenessStatus,
    SemanticUnitMap,
    TranslationCompletenessDecision,
    bundle_content_hash,
)
from transflow.domain.delivery import (
    DiagnosticEvidence,
    DiagnosticStatus,
    DiagnosticUnitEvidence,
    TranslatedDiagnosticCandidate,
)
from transflow.domain.errors import DomainContractError, ErrorCode
from transflow.domain.pages import PageExecutionContext
from transflow.domain.toolbox import PagePatch
from transflow.domain.translation import TranslationBundle
from transflow.pdf_kernel.facts import ExtractedPageFacts
from transflow.pdf_kernel.patch import PagePatchInterpreter, ReplayPage
from transflow.pdf_kernel.renderer import outside_region_diff_ratio

LOGGER = logging.getLogger("transflow.application.translated_diagnostic")
APPLICATION_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True, slots=True)
class DiagnosticPageInput:
    """绑定一页诊断物化所需的源身份、完整译文、Patch 和 Kernel 事实。"""

    context: PageExecutionContext
    facts: ExtractedPageFacts
    patch: PagePatch | None
    semantic_map: SemanticUnitMap
    bundle: TranslationBundle | None
    decision: TranslationCompletenessDecision

    def __post_init__(self) -> None:
        """校验页、源、map 与裁决身份一致。"""

        if (
            self.context.page_no != self.semantic_map.page_no
            or self.context.source_hash != self.semantic_map.source_hash
            or self.facts.page.page_no != self.context.page_no
            or self.facts.page.source_hash != self.context.source_hash
        ):
            raise DomainContractError(ErrorCode.INVALID_IDENTITY, "诊断页输入身份不一致")
        if self.decision.map_hash != self.semantic_map.map_hash:
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "诊断输入 Map/Decision 不一致")
        if (
            self.bundle is not None
            and self.decision.bundle_hash != bundle_content_hash(self.bundle)
        ):
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "诊断输入 Bundle/Decision 不一致")


def _sha256_file(path: Path) -> str:
    """流式计算 PDF SHA-256。"""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalized_text(value: str) -> str:
    """兼容 PDF 抽取的换行、兼容字形和不换行空格后比较文字。"""

    return "".join(unicodedata.normalize("NFKC", value).split()).casefold()


def _rect_intersects(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> bool:
    """判断两个 PDF 矩形是否存在正面积交集。"""

    return not (
        first[2] <= second[0]
        or second[2] <= first[0]
        or first[3] <= second[1]
        or second[3] <= first[1]
    )


def _page_geometry(page: pymupdf.Page) -> tuple[tuple[float, ...], tuple[float, ...], int]:
    """读取页面 MediaBox、CropBox 和旋转角度。"""

    return (
        tuple(round(float(value), 4) for value in page.mediabox),
        tuple(round(float(value), 4) for value in page.cropbox),
        int(page.rotation),
    )


def _page_spans(page: pymupdf.Page) -> tuple[dict[str, Any], ...]:
    """从真实 PDF 字典抽取全部非空 span 的文字、字体和 bbox。"""

    payload = page.get_text("dict")
    spans: list[dict[str, Any]] = []
    for block in payload.get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                if str(span.get("text", "")).strip():
                    spans.append(span)
    return tuple(spans)


def _span_bbox(span: dict[str, Any]) -> tuple[float, float, float, float]:
    """把 PyMuPDF 动态字典中的 bbox 收敛为领域使用的四元组。"""

    raw = span["bbox"]
    return (float(raw[0]), float(raw[1]), float(raw[2]), float(raw[3]))


def _rounded_span_bbox(span: dict[str, Any]) -> tuple[float, float, float, float]:
    """返回保留四位小数且维度固定的 span bbox。"""

    left, top, right, bottom = _span_bbox(span)
    return (round(left, 4), round(top, 4), round(right, 4), round(bottom, 4))


class TranslatedDiagnosticMaterializer:
    """只消费完整性 PASS，并用唯一 Patch 解释器生成隔离诊断 PDF。"""

    def __init__(
        self,
        interpreter: PagePatchInterpreter,
        artifacts: AtomicArtifactStore,
        run_root: Path,
    ) -> None:
        """绑定唯一 Kernel、run 私有 ArtifactStore 与工作根。"""

        self._interpreter = interpreter
        self._artifacts = artifacts
        self._run_root = run_root.resolve()
        self._work_root = self._run_root / "diagnostic"
        self._work_root.mkdir(parents=True, exist_ok=True)

    def materialize_page(
        self,
        source_path: Path,
        page_input: DiagnosticPageInput,
        *,
        inject_write_failure: bool = False,
    ) -> TranslatedDiagnosticCandidate:
        """物化单页诊断；布局可溢出，但完整性、owner、字体和写入不得绕过。"""

        LOGGER.info(
            "调用页级诊断物化，意图=让完整译文布局错误可见 page_no=%s",
            page_input.context.page_no,
        )
        if (
            page_input.decision.status is not CompletenessStatus.PASS
            or page_input.bundle is None
        ):
            return self._no_candidate(source_path, (page_input,), "TRANSLATION_INCOMPLETE")
        if page_input.patch is None:
            return self._failed(source_path, (page_input,), "PATCH_MISSING")
        work_path = self._work_root / (
            f"page-{page_input.context.page_no:04d}-{page_input.semantic_map.map_hash[:16]}.pdf"
        )
        try:
            shutil.copyfile(source_path, work_path)
            self._interpreter.replay_document(
                work_path,
                (
                    ReplayPage(
                        page_input.context,
                        page_input.facts,
                        page_input.patch,
                        page_input.patch.owner,
                    ),
                ),
                diagnostic=True,
            )
            if inject_write_failure:
                raise OSError("injected_diagnostic_write_failure")
            return self.validate_and_register(source_path, work_path, (page_input,))
        except Exception as error:
            LOGGER.exception(
                "诊断物化失败，意图=记录硬约束而不发布伪候选 page_no=%s",
                page_input.context.page_no,
            )
            return self._failed(source_path, (page_input,), type(error).__name__)
        finally:
            if work_path.is_file():
                work_path.unlink()

    def materialize_document(
        self,
        source_path: Path,
        page_inputs: tuple[DiagnosticPageInput, ...],
        *,
        inject_write_failure: bool = False,
    ) -> TranslatedDiagnosticCandidate:
        """仅当每页完整且可写时组装整本隔离诊断候选。"""

        LOGGER.info(
            "调用整文诊断物化，意图=要求每页完整 page_count=%s",
            len(page_inputs),
        )
        if not page_inputs or any(
            item.decision.status is not CompletenessStatus.PASS
            or item.bundle is None
            or item.patch is None
            for item in page_inputs
        ):
            return self._no_candidate(source_path, page_inputs, "DOCUMENT_TRANSLATION_INCOMPLETE")
        expected_pages = tuple(range(1, len(page_inputs) + 1))
        ordered = tuple(sorted(page_inputs, key=lambda item: item.context.page_no))
        if tuple(item.context.page_no for item in ordered) != expected_pages:
            return self._no_candidate(source_path, ordered, "DOCUMENT_PAGE_SET_INCOMPLETE")
        document_hash = hashlib.sha256(
            "\0".join(item.semantic_map.map_hash for item in ordered).encode("ascii")
        ).hexdigest()
        work_path = self._work_root / f"document-{document_hash[:20]}.pdf"
        try:
            shutil.copyfile(source_path, work_path)
            self._interpreter.replay_document(
                work_path,
                tuple(
                    ReplayPage(
                        item.context,
                        item.facts,
                        item.patch,
                        item.patch.owner,
                    )
                    for item in ordered
                    if item.patch is not None
                ),
                diagnostic=True,
            )
            if inject_write_failure:
                raise OSError("injected_diagnostic_write_failure")
            return self.validate_and_register(source_path, work_path, ordered)
        except Exception as error:
            LOGGER.exception("整文诊断物化失败，意图=保持安全 final 不受影响")
            return self._failed(source_path, ordered, type(error).__name__)
        finally:
            if work_path.is_file():
                work_path.unlink()

    def validate_and_register(
        self,
        source_path: Path,
        candidate_path: Path,
        page_inputs: tuple[DiagnosticPageInput, ...],
    ) -> TranslatedDiagnosticCandidate:
        """实际打开候选，逐 unit 验证文字、字体、bbox、几何和 owner 后登记。"""

        if not page_inputs or any(
            item.decision.status is not CompletenessStatus.PASS or item.bundle is None
            for item in page_inputs
        ):
            return self._no_candidate(source_path, page_inputs, "TRANSLATION_INCOMPLETE")
        source_hash = _sha256_file(source_path)
        candidate_hash = _sha256_file(candidate_path)
        if source_hash == candidate_hash:
            return self._failed(source_path, page_inputs, "SOURCE_COPY_REJECTED")
        expected: list[tuple[DiagnosticPageInput, str, str]] = []
        for item in page_inputs:
            if item.bundle is None:
                raise AssertionError("完整性预校验后的诊断 Bundle 不应为空")
            bundle_by_id = {
                unit.unit_id: unit.translated_text for unit in item.bundle.units
            }
            decision_by_id = {
                unit.unit_id: unit.disposition for unit in item.decision.dispositions
            }
            for semantic in item.semantic_map.entries:
                if decision_by_id[semantic.unit_id] is CompletenessDisposition.TRANSLATED:
                    text = bundle_by_id[semantic.unit_id]
                elif decision_by_id[semantic.unit_id] in {
                    CompletenessDisposition.KEEP_SOURCE,
                    CompletenessDisposition.PROTECTED,
                }:
                    text = semantic.source_text
                else:
                    return self._no_candidate(source_path, page_inputs, "FAILED_UNIT_PRESENT")
                expected.append((item, semantic.unit_id, text))
        unit_evidence: list[DiagnosticUnitEvidence] = []
        missing: list[str] = []
        glyph_failures = 0
        geometry_preserved = True
        outside_ratio = 0.0
        with pymupdf.open(source_path) as source, pymupdf.open(candidate_path) as candidate:
            if source.page_count != candidate.page_count:
                geometry_preserved = False
            for item in page_inputs:
                page_index = item.context.page_no - 1
                if page_index >= source.page_count or page_index >= candidate.page_count:
                    geometry_preserved = False
                    continue
                source_page = source[page_index]
                candidate_page = candidate[page_index]
                geometry_preserved = geometry_preserved and (
                    _page_geometry(source_page) == _page_geometry(candidate_page)
                )
                allowed = tuple(
                    operation.rect
                    for operation in (item.patch.operations if item.patch is not None else ())
                    if operation.rect is not None
                )
                outside_ratio = max(
                    outside_ratio,
                    outside_region_diff_ratio(
                        source_path,
                        candidate_path,
                        allowed,
                        page_no=item.context.page_no,
                    ),
                )
            for item, unit_id, text in expected:
                page = candidate[item.context.page_no - 1]
                semantic = next(
                    row for row in item.semantic_map.entries if row.unit_id == unit_id
                )
                operation_rect = next(
                    (
                        operation.rect
                        for operation in (item.patch.operations if item.patch is not None else ())
                        if semantic.object_id in operation.target_object_ids
                        and operation.rect is not None
                    ),
                    None,
                )
                extracted_text = (
                    page.get_textbox(pymupdf.Rect(operation_rect))
                    if operation_rect is not None
                    else page.get_text()
                )
                extracted = _normalized_text(text) in _normalized_text(extracted_text)
                spans = _page_spans(page)
                related = tuple(
                    span
                    for span in spans
                    if operation_rect is None
                    or _rect_intersects(
                        _span_bbox(span),
                        operation_rect,
                    )
                )
                font_names = tuple(
                    dict.fromkeys(str(span.get("font", "")) for span in related if span.get("font"))
                )
                bboxes = tuple(
                    _rounded_span_bbox(span) for span in related
                )
                glyph_failed = (
                    "\ufffd" in extracted_text
                    or ("?" in extracted_text and "?" not in text)
                    or (not font_names and operation_rect is not None)
                )
                if glyph_failed:
                    glyph_failures += 1
                if not extracted or glyph_failed:
                    missing.append(unit_id)
                unit_evidence.append(
                    DiagnosticUnitEvidence(
                        unit_id,
                        hashlib.sha256(text.encode("utf-8")).hexdigest(),
                        extracted and not glyph_failed,
                        font_names,
                        bboxes,
                    )
                )
            page_count = candidate.page_count
        evidence = DiagnosticEvidence(
            source_hash=source_hash,
            candidate_hash=candidate_hash,
            page_count=page_count,
            expected_unit_count=len(expected),
            materialized_unit_count=len(expected) - len(set(missing)),
            missing_unit_ids=tuple(dict.fromkeys(missing)),
            geometry_preserved=geometry_preserved,
            glyph_failure_count=glyph_failures,
            owner_violation_count=0 if outside_ratio <= 0.0005 else 1,
            protected_violation_count=0 if outside_ratio <= 0.0005 else 1,
            outside_owner_diff_ratio=outside_ratio,
            units=tuple(unit_evidence),
        )
        if (
            evidence.missing_unit_ids
            or not evidence.geometry_preserved
            or evidence.owner_violation_count
            or evidence.protected_violation_count
        ):
            return TranslatedDiagnosticCandidate(
                DiagnosticStatus.DIAGNOSTIC_MATERIALIZATION_FAILED,
                page_inputs[0].context.page_no if len(page_inputs) == 1 else None,
                page_inputs[0].semantic_map.map_hash if len(page_inputs) == 1 else None,
                page_inputs[0].decision.bundle_hash if len(page_inputs) == 1 else None,
                page_inputs[0].decision.decision_hash if len(page_inputs) == 1 else None,
                None,
                evidence,
            )
        artifact_id = (
            f"translated-diagnostic-p{page_inputs[0].context.page_no:04d}-{candidate_hash[:20]}"
            if len(page_inputs) == 1
            else f"translated-diagnostic-document-{candidate_hash[:20]}"
        )
        reference = self._artifacts.put_atomic(
            ArtifactPayload(
                artifact_id,
                "application/pdf",
                candidate_path.read_bytes(),
                candidate_hash,
            ),
            f"diagnostic/{artifact_id}-{candidate_hash}.pdf",
            "diagnostic",
        )
        aggregate_map_hash = (
            page_inputs[0].semantic_map.map_hash
            if len(page_inputs) == 1
            else hashlib.sha256(
                "\0".join(item.semantic_map.map_hash for item in page_inputs).encode("ascii")
            ).hexdigest()
        )
        aggregate_bundle_hash = (
            page_inputs[0].decision.bundle_hash
            if len(page_inputs) == 1
            else hashlib.sha256(
                "\0".join(str(item.decision.bundle_hash) for item in page_inputs).encode("ascii")
            ).hexdigest()
        )
        aggregate_decision_hash = (
            page_inputs[0].decision.decision_hash
            if len(page_inputs) == 1
            else hashlib.sha256(
                "\0".join(item.decision.decision_hash for item in page_inputs).encode("ascii")
            ).hexdigest()
        )
        return TranslatedDiagnosticCandidate(
            DiagnosticStatus.TRANSLATED_DIAGNOSTIC_READY,
            page_inputs[0].context.page_no if len(page_inputs) == 1 else None,
            aggregate_map_hash,
            aggregate_bundle_hash,
            aggregate_decision_hash,
            reference,
            evidence,
        )

    def _no_candidate(
        self,
        source_path: Path,
        page_inputs: tuple[DiagnosticPageInput, ...],
        failure_type: str,
    ) -> TranslatedDiagnosticCandidate:
        """构造无完整译文时的诚实 NO_TRANSLATED_CANDIDATE。"""

        source_hash = _sha256_file(source_path)
        evidence = DiagnosticEvidence(
            source_hash,
            None,
            0,
            sum(len(item.semantic_map.entries) for item in page_inputs),
            0,
            tuple(
                semantic.unit_id
                for item in page_inputs
                for semantic in item.semantic_map.entries
            ),
            False,
            0,
            0,
            0,
            0.0,
            (),
            failure_type,
        )
        return TranslatedDiagnosticCandidate(
            DiagnosticStatus.NO_TRANSLATED_CANDIDATE,
            page_inputs[0].context.page_no if len(page_inputs) == 1 else None,
            page_inputs[0].semantic_map.map_hash if len(page_inputs) == 1 else None,
            None,
            page_inputs[0].decision.decision_hash if len(page_inputs) == 1 else None,
            None,
            evidence,
        )

    def _failed(
        self,
        source_path: Path,
        page_inputs: tuple[DiagnosticPageInput, ...],
        failure_type: str,
    ) -> TranslatedDiagnosticCandidate:
        """构造有完整译文但受硬约束无法物化的失败状态。"""

        source_hash = _sha256_file(source_path)
        expected_ids = tuple(
            semantic.unit_id
            for item in page_inputs
            for semantic in item.semantic_map.entries
        )
        evidence = DiagnosticEvidence(
            source_hash,
            None,
            0,
            len(expected_ids),
            0,
            expected_ids,
            False,
            0,
            0,
            0,
            0.0,
            (),
            failure_type,
        )
        return TranslatedDiagnosticCandidate(
            DiagnosticStatus.DIAGNOSTIC_MATERIALIZATION_FAILED,
            page_inputs[0].context.page_no if len(page_inputs) == 1 else None,
            page_inputs[0].semantic_map.map_hash if len(page_inputs) == 1 else None,
            page_inputs[0].decision.bundle_hash if len(page_inputs) == 1 else None,
            page_inputs[0].decision.decision_hash if len(page_inputs) == 1 else None,
            None,
            evidence,
        )


def main() -> int:
    """记录诊断物化必须复用唯一 PagePatchInterpreter。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    LOGGER.info("TranslatedDiagnosticMaterializer 示例，意图=隔离物化完整真实译文")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
