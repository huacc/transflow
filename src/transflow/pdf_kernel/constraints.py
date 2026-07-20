"""实现不含页面类别语义的 Patch 与候选 PDF 机械硬约束。"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

import pymupdf

from transflow.domain.pages import PageExecutionContext
from transflow.domain.toolbox import PagePatch, PatchOperation
from transflow.pdf_kernel.facts import ExtractedPageFacts, PageFactsExtractor, PageObjectFact
from transflow.pdf_kernel.fonts import ControlledFontRegistry
from transflow.pdf_kernel.models import KernelFinding, make_finding
from transflow.pdf_kernel.patch import PatchApplicationResult, probe_operation_fit
from transflow.pdf_kernel.renderer import outside_region_diff_ratio

LOGGER = logging.getLogger("transflow.pdf_kernel.constraints")
PACKAGE_ROOT = Path(__file__).resolve().parent.parent
RectTuple = tuple[float, float, float, float]


def _sha256_file(path: Path) -> str:
    """流式计算候选 PDF 哈希，供重新提取机械事实。"""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _intersection_area(left: RectTuple, right: RectTuple) -> float:
    """计算两个矩形的交集面积，空交集返回零。"""

    return max(0.0, min(left[2], right[2]) - max(left[0], right[0])) * max(
        0.0,
        min(left[3], right[3]) - max(left[1], right[1]),
    )


def _area(rectangle: RectTuple) -> float:
    """计算规范矩形面积。"""

    return max(0.0, rectangle[2] - rectangle[0]) * max(
        0.0, rectangle[3] - rectangle[1]
    )


def _new_overlap(source: RectTuple, output: RectTuple, obstacle: RectTuple) -> bool:
    """判断输出相对源区域是否新增显著遮挡。"""

    baseline = _intersection_area(source, obstacle) / max(0.001, _area(source))
    proposed = _intersection_area(output, obstacle) / max(0.001, _area(output))
    return proposed > baseline + 0.01


def _stable_findings(findings: list[KernelFinding]) -> tuple[KernelFinding, ...]:
    """按级别、代码和证据排序并删除完全重复的发现项。"""

    return tuple(sorted(set(findings), key=lambda item: (item.severity, item.code, item.evidence)))


class ConstraintChecker:
    """集中执行全部机械硬约束，不根据 Classification Route 分支。"""

    def __init__(self, fonts: ControlledFontRegistry) -> None:
        """绑定唯一受控字体注册表。"""

        self._fonts = fonts

    def check_patch(
        self,
        context: PageExecutionContext,
        facts: ExtractedPageFacts,
        patch: PagePatch,
        expected_owner: str,
    ) -> tuple[KernelFinding, ...]:
        """在任何页面写入前收集全部绑定、字体、边界、遮挡和溢出问题。"""

        LOGGER.info(
            "调用 Patch 硬约束，意图=写入前收集全部机械 Finding patch_id=%s",
            patch.patch_id,
        )
        findings: list[KernelFinding] = []
        if patch.source_hash != context.source_hash or patch.source_hash != facts.page.source_hash:
            findings.append(make_finding("SOURCE_HASH_MISMATCH", "Patch 源哈希不一致"))
        if patch.page_no != context.page_no or patch.page_no != facts.page.page_no:
            findings.append(make_finding("PAGE_IDENTITY_MISMATCH", "Patch 页码不一致"))
        if (
            patch.geometry_hash != context.geometry_hash
            or patch.geometry_hash != facts.page.geometry_hash
        ):
            findings.append(make_finding("PAGE_GEOMETRY_MISMATCH", "Patch 页面几何不一致"))
        if patch.owner != expected_owner:
            findings.append(make_finding("PATCH_OWNER_MISMATCH", "Patch owner 不一致"))
        by_id = {item.object_id: item for item in facts.objects}
        protected_ids = set(facts.protected_object_ids)
        target_ids = {
            object_id
            for operation in patch.operations
            for object_id in operation.target_object_ids
        }
        for operation in patch.operations:
            self._check_operation(
                findings,
                operation,
                facts,
                by_id,
                protected_ids,
                target_ids,
            )
        for index, left in enumerate(patch.operations):
            if left.rect is None:
                continue
            for right in patch.operations[index + 1 :]:
                if right.rect is not None and _intersection_area(left.rect, right.rect) > 0.01:
                    findings.append(
                        make_finding(
                            "WRITE_OVERLAP",
                            "两个 Patch 写入区域发生重叠",
                            left=left.operation_id,
                            right=right.operation_id,
                        )
                    )
        return _stable_findings(findings)

    def _check_operation(
        self,
        findings: list[KernelFinding],
        operation: PatchOperation,
        facts: ExtractedPageFacts,
        by_id: dict[str, PageObjectFact],
        protected_ids: set[str],
        target_ids: set[str],
    ) -> None:
        """检查一个操作并继续收集后续错误，不因首错提前返回。"""

        if operation.kind != "replace_text" or operation.rect is None:
            findings.append(make_finding("PATCH_OPERATION_INVALID", "Patch 操作类型或矩形无效"))
            return
        if not pymupdf.Rect(facts.crop_box).contains(pymupdf.Rect(operation.rect)):
            findings.append(
                make_finding(
                    "PAGE_BOUNDS_EXCEEDED",
                    "Patch 区域越出 CropBox",
                    operation_id=operation.operation_id,
                )
            )
        target_objects = []
        for object_id in operation.target_object_ids:
            target = by_id.get(object_id)
            if target is None:
                code = "PROTECTED_OBJECT" if object_id in protected_ids else "UNKNOWN_OBJECT"
                findings.append(
                    make_finding(code, "Patch 目标不可编辑", object_id=object_id)
                )
            else:
                target_objects.append(target)
        for protected_region in facts.protected_regions:
            if pymupdf.Rect(operation.rect).intersects(pymupdf.Rect(protected_region)):
                findings.append(
                    make_finding(
                        "PROTECTED_REGION_OVERLAP",
                        "Patch 区域覆盖保护对象",
                        operation_id=operation.operation_id,
                    )
                )
        for target in target_objects:
            source_bbox = target.bbox
            for other in facts.objects:
                if other.object_id in target_ids or other.protected:
                    continue
                if _new_overlap(source_bbox, operation.rect, other.bbox):
                    findings.append(
                        make_finding(
                            "NON_TARGET_TEXT_OVERLAP",
                            "Patch 新增了与非目标文字的遮挡",
                            obstacle_id=other.object_id,
                            operation_id=operation.operation_id,
                        )
                    )
        if operation.font_id is None or operation.replacement_text is None:
            findings.append(make_finding("PATCH_OPERATION_INVALID", "Patch 字体或译文缺失"))
            return
        probe = self._fonts.probe(operation.font_id, operation.replacement_text)
        if not probe.registered:
            findings.append(make_finding("FONT_NOT_REGISTERED", "Patch 字体未登记"))
            return
        if not probe.integrity_passed or not probe.loadable:
            findings.append(make_finding("FONT_INVALID", "Patch 字体文件或加载状态无效"))
            return
        if probe.missing_codepoints:
            findings.append(
                make_finding(
                    "FONT_GLYPH_MISSING",
                    "Patch 字体不覆盖全部译文字形",
                    missing=",".join(probe.missing_codepoints),
                )
            )
            return
        asset = self._fonts.resolve(operation.font_id)
        fit_code = probe_operation_fit(facts, operation, asset.path)
        if fit_code < 0:
            findings.append(
                make_finding(
                    "TEXT_FIT_OVERFLOW",
                    "译文无法装入声明矩形",
                    fit_code=round(fit_code, 4),
                    operation_id=operation.operation_id,
                )
            )

    def check_candidate(
        self,
        source_path: Path,
        candidate_path: Path,
        facts: ExtractedPageFacts,
        patch: PagePatch,
        application: PatchApplicationResult | None,
    ) -> tuple[KernelFinding, ...]:
        """重新打开真实候选 PDF，检查损坏、锁定对象、残留、字体和区域外变化。"""

        LOGGER.info(
            "调用候选硬约束，意图=重验完整 PDF 机械事实 candidate=%s",
            candidate_path.name,
        )
        findings: list[KernelFinding] = []
        try:
            with pymupdf.open(candidate_path) as document:
                if document.page_count < facts.page.page_no:
                    raise ValueError("候选缺少目标页")
                page = document[facts.page.page_no - 1]
                candidate_text = page.get_text()
                resources = {str(item[4]) for item in page.get_fonts(full=True)}
        except Exception as error:
            return (
                make_finding(
                    "CANDIDATE_UNREADABLE",
                    "候选 PDF 无法解码",
                    error=type(error).__name__,
                ),
            )
        candidate_hash = _sha256_file(candidate_path)
        candidate_facts = PageFactsExtractor().extract_page(
            candidate_path,
            candidate_hash,
            facts.page.page_no,
        )
        if candidate_facts.locked_objects_hash != facts.locked_objects_hash:
            findings.append(make_finding("LOCKED_OBJECTS_CHANGED", "锁定对象发生变化"))
        by_id = {item.object_id: item for item in facts.objects}
        for operation in patch.operations:
            expected_resource = f"TFP4{operation.payload_hash[:8]}"
            if expected_resource not in resources:
                findings.append(
                    make_finding(
                        "FONT_NOT_EMBEDDED",
                        "受控字体资源未嵌入候选 PDF",
                        resource=expected_resource,
                    )
                )
            for object_id in operation.target_object_ids:
                source_object = by_id.get(object_id)
                if source_object is None:
                    continue
                source_text = str(getattr(source_object, "text", "")).strip()
                replacement = operation.replacement_text or ""
                if source_text and source_text != replacement and source_text in candidate_text:
                    findings.append(
                        make_finding(
                            "SOURCE_TEXT_RESIDUAL",
                            "候选 PDF 仍包含被替换的源文字",
                            object_id=object_id,
                        )
                    )
        ratio = outside_region_diff_ratio(
            source_path,
            candidate_path,
            (operation.rect for operation in patch.operations if operation.rect is not None),
            page_no=facts.page.page_no,
        )
        if ratio > 0.00001:
            findings.append(
                make_finding(
                    "OUTSIDE_ALLOWED_RENDER_CHANGED",
                    "允许区域外的渲染像素发生变化",
                    ratio=round(ratio, 8),
                )
            )
        if application is None or not application.fits:
            findings.append(make_finding("TEXT_FIT_OVERFLOW", "候选排版结果未通过容纳检查"))
        return _stable_findings(findings)


def main() -> int:
    """记录 Kernel 约束只能判断机械事实。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    LOGGER.info("ConstraintChecker 示例，意图=禁止页面类别语义进入硬约束")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
