"""实现 candidate 与 final replay 共用的唯一 PagePatch 解释器。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import pymupdf

from transflow.domain.common import content_sha256
from transflow.domain.errors import DomainContractError, ErrorCode
from transflow.domain.pages import PageExecutionContext
from transflow.domain.toolbox import PagePatch, PatchOperation
from transflow.pdf_kernel.facts import ExtractedPageFacts
from transflow.pdf_kernel.fonts import ControlledFontRegistry
from transflow.pdf_kernel.models import PatchManifest

LOGGER = logging.getLogger("transflow.pdf_kernel.patch")
PACKAGE_ROOT = Path(__file__).resolve().parent.parent
INTERPRETER_ID = "transflow.pdf-kernel.page-patch-interpreter/v1"
PATCH_MANIFEST_VERSION = "transflow.pdf-kernel.patch-manifest/v1"
RENDER_CONFIG_HASH = content_sha256(
    {
        "candidate_scale": 2.0,
        "color_space": "RGB",
        "redaction_graphics": "preserve",
        "redaction_images": "preserve",
        "writer": "pymupdf.insert_textbox",
    }
)
DIAGNOSTIC_MIN_FONT_SIZE = 1.0


def _probe_textbox_fit(
    facts: ExtractedPageFacts,
    operation: PatchOperation,
    font_path: Path,
    font_size: float,
) -> float:
    """用空白内存页探测指定字号，避免在真实候选上试写后留下半成品。"""

    if operation.rect is None or operation.replacement_text is None:
        raise DomainContractError(ErrorCode.PATCH_OPERATION_INVALID, "Patch 操作字段不完整")
    with pymupdf.open() as probe_document:
        page = probe_document.new_page(
            width=facts.page.width_points,
            height=facts.page.height_points,
        )
        font_name = f"TFP6Probe{operation.payload_hash[:8]}"
        page.insert_font(fontname=font_name, fontfile=str(font_path))
        return float(
            page.insert_textbox(
                pymupdf.Rect(operation.rect),
                operation.replacement_text,
                fontname=font_name,
                fontsize=font_size,
                color=(0, 0, 0),
            )
        )


def _diagnostic_font_size(
    facts: ExtractedPageFacts,
    operation: PatchOperation,
    font_path: Path,
) -> float:
    """在原 owner 矩形内求可完整写入的最大字号，仅供隔离诊断候选使用。"""

    if operation.font_size is None:
        raise DomainContractError(ErrorCode.PATCH_OPERATION_INVALID, "Patch 字号缺失")
    requested = operation.font_size
    if _probe_textbox_fit(facts, operation, font_path, requested) >= 0:
        return requested
    minimum = min(requested, DIAGNOSTIC_MIN_FONT_SIZE)
    if _probe_textbox_fit(facts, operation, font_path, minimum) < 0:
        raise DomainContractError(
            ErrorCode.DIAGNOSTIC_MATERIALIZATION_FAILED,
            "诊断文字在 owner 矩形内使用最小字号仍无法完整物化",
        )
    lower = minimum
    upper = requested
    # 固定轮数二分保证相同输入得到相同字号，同时尽量保留可读性。
    for _ in range(12):
        candidate = (lower + upper) / 2
        if _probe_textbox_fit(facts, operation, font_path, candidate) >= 0:
            lower = candidate
        else:
            upper = candidate
    return lower


def _required_operation_font_size(operation: PatchOperation) -> float:
    """读取已验证操作字号，并为静态检查保留显式非空边界。"""

    if operation.font_size is None:
        raise DomainContractError(ErrorCode.PATCH_OPERATION_INVALID, "Patch 字号缺失")
    return operation.font_size


def patch_operation_hash(
    *,
    owner: str,
    target_object_ids: tuple[str, ...],
    rect: tuple[float, float, float, float],
    replacement_text: str,
    font_id: str,
    font_size: float,
) -> str:
    """计算 replace_text 声明载荷的稳定内容哈希。"""

    return content_sha256(
        {
            "font_id": font_id,
            "font_size": font_size,
            "owner": owner,
            "rect": rect,
            "replacement_text": replacement_text,
            "target_object_ids": target_object_ids,
        }
    )


def build_patch_manifest(patch: PagePatch) -> PatchManifest:
    """建立 candidate/final 共用的稳定 Patch 操作和渲染配置清单。"""

    payload = {
        "geometry_hash": patch.geometry_hash,
        "interpreter_id": INTERPRETER_ID,
        "operation_hashes": tuple(item.payload_hash for item in patch.operations),
        "operation_ids": tuple(item.operation_id for item in patch.operations),
        "owner": patch.owner,
        "page_no": patch.page_no,
        "patch_id": patch.patch_id,
        "render_config_hash": RENDER_CONFIG_HASH,
        "schema_version": PATCH_MANIFEST_VERSION,
        "source_hash": patch.source_hash,
    }
    return PatchManifest(
        schema_version=PATCH_MANIFEST_VERSION,
        interpreter_id=INTERPRETER_ID,
        patch_id=patch.patch_id,
        source_hash=patch.source_hash,
        page_no=patch.page_no,
        geometry_hash=patch.geometry_hash,
        owner=patch.owner,
        operation_ids=tuple(item.operation_id for item in patch.operations),
        operation_hashes=tuple(item.payload_hash for item in patch.operations),
        render_config_hash=RENDER_CONFIG_HASH,
        manifest_hash=content_sha256(payload),
    )


def probe_operation_fit(
    facts: ExtractedPageFacts,
    operation: PatchOperation,
    font_path: Path,
) -> float:
    """在空白内存页上预量文字容纳量，保证真实页面写入前已知是否溢出。"""

    if (
        operation.rect is None
        or operation.replacement_text is None
        or operation.font_size is None
    ):
        raise DomainContractError(ErrorCode.PATCH_OPERATION_INVALID, "Patch 操作字段不完整")
    return _probe_textbox_fit(facts, operation, font_path, operation.font_size)


@dataclass(frozen=True, slots=True)
class PatchApplicationResult:
    """记录唯一解释器执行的操作顺序、所有者与排版剩余量。"""

    interpreter_id: str
    patch_id: str
    owner: str
    operation_ids: tuple[str, ...]
    applied_count: int
    layout_remainders: tuple[float, ...]
    target_object_ids: tuple[str, ...] = ()
    patch_manifest_hash: str = ""
    render_config_hash: str = ""

    @property
    def fits(self) -> bool:
        """判断所有文本框是否均完整容纳译文。"""

        return all(value >= 0 for value in self.layout_remainders)


@dataclass(frozen=True, slots=True)
class ReplayPage:
    """描述最终文档回放所需的页面上下文、事实、Patch 和期望 owner。"""

    context: PageExecutionContext
    facts: ExtractedPageFacts
    patch: PagePatch
    expected_owner: str


class PagePatchInterpreter:
    """在任何写入前完成全部绑定、所有权、保护对象和字体校验。"""

    def __init__(self, fonts: ControlledFontRegistry) -> None:
        """绑定唯一受控字体注册表。"""

        self._fonts = fonts

    def _validate_operation(
        self,
        operation: PatchOperation,
        patch: PagePatch,
        facts: ExtractedPageFacts,
    ) -> Path:
        """校验单个 replace_text 操作并返回已验证字体路径。"""

        if operation.kind != "replace_text":
            raise DomainContractError(ErrorCode.PATCH_OPERATION_INVALID, "不支持的 Patch 操作")
        if operation.owner != patch.owner:
            raise DomainContractError(ErrorCode.PATCH_OWNER_VIOLATION, "操作 owner 与 Patch 不一致")
        if (
            not operation.target_object_ids
            or operation.rect is None
            or operation.replacement_text is None
            or operation.font_id is None
            or operation.font_size is None
        ):
            raise DomainContractError(ErrorCode.PATCH_OPERATION_INVALID, "Patch 操作字段不完整")
        if (
            patch_operation_hash(
                owner=operation.owner,
                target_object_ids=operation.target_object_ids,
                rect=operation.rect,
                replacement_text=operation.replacement_text,
                font_id=operation.font_id,
                font_size=operation.font_size,
            )
            != operation.payload_hash
        ):
            raise DomainContractError(ErrorCode.PATCH_OPERATION_INVALID, "Patch 载荷哈希不一致")
        owned = set(facts.owned_object_ids)
        protected = set(facts.protected_object_ids)
        targets = set(operation.target_object_ids)
        if not targets <= owned:
            code = (
                ErrorCode.PATCH_PROTECTED_OBJECT
                if targets & protected
                else ErrorCode.PATCH_OWNER_VIOLATION
            )
            raise DomainContractError(code, "Patch 目标不属于当前 owner 或命中保护对象")
        operation_rect = pymupdf.Rect(operation.rect)
        if not pymupdf.Rect(facts.crop_box).contains(operation_rect):
            raise DomainContractError(ErrorCode.PATCH_OPERATION_INVALID, "Patch 矩形越出 CropBox")
        for protected_region in facts.protected_regions:
            if operation_rect.intersects(pymupdf.Rect(protected_region)):
                raise DomainContractError(
                    ErrorCode.PATCH_PROTECTED_OBJECT, "Patch 矩形覆盖保护对象"
                )
        return self._fonts.resolve(operation.font_id).path

    def apply(
        self,
        document: pymupdf.Document,
        context: PageExecutionContext,
        facts: ExtractedPageFacts,
        patch: PagePatch,
        expected_owner: str,
        *,
        diagnostic: bool = False,
    ) -> PatchApplicationResult:
        """预校验全部操作后，按声明顺序对指定源页面执行受控替换。"""

        LOGGER.info(
            "调用唯一 Patch 解释器，意图=按声明顺序应用受控修改 patch_id=%s page_no=%s",
            patch.patch_id,
            patch.page_no,
        )
        patch.validate_binding(context, expected_owner)
        if (
            facts.page.source_hash != context.source_hash
            or facts.page.geometry_hash != context.geometry_hash
        ):
            raise DomainContractError(ErrorCode.PATCH_BINDING_MISMATCH, "PageFacts 与上下文不一致")
        font_paths = tuple(
            self._validate_operation(item, patch, facts) for item in patch.operations
        )
        font_sizes = tuple(
            (
                _diagnostic_font_size(facts, operation, font_path)
                if diagnostic
                else _required_operation_font_size(operation)
            )
            for operation, font_path in zip(patch.operations, font_paths, strict=True)
        )
        manifest = build_patch_manifest(patch)
        page = document[context.page_no - 1]
        remainders: list[float] = []
        for operation, font_path, font_size in zip(
            patch.operations,
            font_paths,
            font_sizes,
            strict=True,
        ):
            if (
                operation.rect is None
                or operation.replacement_text is None
                or font_size is None
            ):
                raise AssertionError("预校验后的 Patch 操作字段不应为空")
            requested_font_size = _required_operation_font_size(operation)
            if diagnostic and font_size < requested_font_size:
                LOGGER.info(
                    "调用诊断字号收敛，意图=在原 owner 内完整物化译文 "
                    "operation_id=%s requested=%s applied=%s",
                    operation.operation_id,
                    requested_font_size,
                    round(font_size, 4),
                )
            font_name = f"TFP4{operation.payload_hash[:8]}"
            rectangle = pymupdf.Rect(operation.rect)
            page.add_redact_annot(rectangle, fill=(1, 1, 1))
            page.apply_redactions(images=0, graphics=0, text=0)
            page.insert_font(fontname=font_name, fontfile=str(font_path))
            remainder = page.insert_textbox(
                rectangle,
                operation.replacement_text,
                fontname=font_name,
                fontsize=font_size,
                color=(0, 0, 0),
            )
            remainders.append(float(remainder))
        return PatchApplicationResult(
            interpreter_id=INTERPRETER_ID,
            patch_id=patch.patch_id,
            owner=patch.owner,
            operation_ids=tuple(item.operation_id for item in patch.operations),
            applied_count=len(patch.operations),
            layout_remainders=tuple(remainders),
            target_object_ids=tuple(
                object_id
                for operation in patch.operations
                for object_id in operation.target_object_ids
            ),
            patch_manifest_hash=manifest.manifest_hash,
            render_config_hash=manifest.render_config_hash,
        )

    def replay_document(
        self,
        document_path: Path,
        pages: tuple[ReplayPage, ...],
        *,
        diagnostic: bool = False,
    ) -> frozenset[int]:
        """一次打开源副本，按 1-based 页码串行回放全部批准 Patch 并增量保存。"""

        LOGGER.info(
            "调用文档 Patch 回放，意图=在源副本按原页序串行修改 page_count=%s",
            len(pages),
        )
        applied_pages: set[int] = set()
        with pymupdf.open(document_path) as document:
            for item in sorted(pages, key=lambda candidate: candidate.context.page_no):
                self.apply(
                    document,
                    item.context,
                    item.facts,
                    item.patch,
                    item.expected_owner,
                    diagnostic=diagnostic,
                )
                applied_pages.add(item.context.page_no)
            if applied_pages:
                document.saveIncr()
        return frozenset(applied_pages)


def main() -> int:
    """记录 candidate 与 final replay 必须共用本解释器。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    LOGGER.info("PagePatchInterpreter 示例，意图=禁止第二套 Patch 语义")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
