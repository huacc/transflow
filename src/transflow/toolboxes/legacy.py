"""以 Lift-and-Wrap 方式提供旧叶单页兼容物、Adapter 和严格结果映射。"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

import pymupdf

from transflow.domain.common import canonical_json_bytes, require_non_empty, require_sha256
from transflow.domain.errors import DomainContractError, ErrorCode, PortCallError
from transflow.domain.pages import PageExecutionContext, PageOutcome
from transflow.domain.toolbox import Decision, Finding, PagePatch, ToolboxDescriptor
from transflow.domain.translation import TranslationBatch
from transflow.pdf_kernel.facts import ExtractedPageFacts
from transflow.pdf_kernel.workspace import RunWorkspace, require_under
from transflow.toolboxes.contracts import (
    PageTemplate,
    PageToolbox,
    ToolboxCandidate,
    ToolboxExecutionResult,
    ToolboxExecutionTrace,
    ToolboxJudgement,
    ToolboxLayoutPlan,
    TranslationDispatch,
)

LOGGER = logging.getLogger("transflow.toolboxes.legacy")
PACKAGE_ROOT = Path(__file__).resolve().parent.parent
LEGACY_ARTIFACT_SUFFIX = ".legacy-page.pdf"


def _sha256_file(path: Path) -> str:
    """流式计算输入或兼容物哈希，避免读取未受控的大文件到内存。"""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class LegacyCompatibilityArtifact:
    """表示 run/page 私有、可重建且永远非权威的单页兼容物。"""

    path: Path
    manifest_path: Path
    source_hash: str
    page_no: int
    content_hash: str
    authority: str = "NON_AUTHORITATIVE_REBUILDABLE"

    def __post_init__(self) -> None:
        """校验兼容物身份、页码、哈希和固定非权威标记。"""

        require_sha256(self.source_hash, "legacy.source_hash")
        require_sha256(self.content_hash, "legacy.content_hash")
        if self.page_no < 1 or self.authority != "NON_AUTHORITATIVE_REBUILDABLE":
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "legacy 兼容物权威属性无效")

    def require_authoritative_use(self, target: str) -> None:
        """始终拒绝作为 Checkpoint、最终 Artifact 或 DocumentFinalizer 输入。"""

        LOGGER.warning("拒绝 legacy 权威引用，意图=保护最终文档来源 target=%s", target)
        raise DomainContractError(
            ErrorCode.INVALID_CONTRACT,
            f"legacy 单页兼容物不得用于权威目标: {require_non_empty(target, 'target')}",
        )


class LegacyPageMaterializer:
    """在注入的 run/page 私有目录中生成或验证可重建单页 PDF。"""

    def materialize(
        self,
        source_pdf: Path,
        context: PageExecutionContext,
        workspace: RunWorkspace,
    ) -> LegacyCompatibilityArtifact:
        """核对完整源哈希后提取一页，并原子写入兼容物与 manifest。"""

        LOGGER.info(
            "调用 legacy 单页物化，意图=为旧叶建立非权威兼容输入 page_no=%s",
            context.page_no,
        )
        if _sha256_file(source_pdf) != context.source_hash:
            raise PortCallError(ErrorCode.SOURCE_CHANGED_DURING_RUN, False, "legacy 源哈希变化")
        page_root = workspace.page_root(context.page_no)
        legacy_root = require_under(page_root / "legacy", workspace.run_root)
        legacy_root.mkdir(parents=True, exist_ok=True)
        artifact_path = require_under(
            legacy_root / f"page-{context.page_no:04d}{LEGACY_ARTIFACT_SUFFIX}",
            workspace.run_root,
        )
        manifest_path = require_under(
            legacy_root / f"page-{context.page_no:04d}.legacy-manifest.json",
            workspace.run_root,
        )
        existing = self._load_existing(artifact_path, manifest_path, context)
        if existing is not None:
            return existing
        partial_path = artifact_path.with_suffix(f"{artifact_path.suffix}.partial")
        if partial_path.exists():
            partial_path.unlink()
        try:
            with pymupdf.open(source_pdf) as document:
                if context.page_no > document.page_count:
                    raise PortCallError(
                        ErrorCode.INVALID_IDENTITY,
                        False,
                        "legacy page_no 越出源 PDF",
                    )
                # select 在独立内存文档上保留目标页，不修改完整只读源文件。
                document.select([context.page_no - 1])
                document.save(partial_path, garbage=4, deflate=True)
            partial_path.replace(artifact_path)
        finally:
            if partial_path.exists():
                partial_path.unlink()
        content_hash = _sha256_file(artifact_path)
        manifest = {
            "schema_version": "transflow.legacy-page-artifact/v1",
            "authority": "NON_AUTHORITATIVE_REBUILDABLE",
            "source_hash": context.source_hash,
            "page_no": context.page_no,
            "geometry_hash": context.geometry_hash,
            "content_hash": content_hash,
            "relative_artifact": artifact_path.relative_to(workspace.run_root).as_posix(),
        }
        manifest_partial = manifest_path.with_suffix(f"{manifest_path.suffix}.partial")
        manifest_partial.write_bytes(canonical_json_bytes(manifest))
        manifest_partial.replace(manifest_path)
        return LegacyCompatibilityArtifact(
            artifact_path,
            manifest_path,
            context.source_hash,
            context.page_no,
            content_hash,
        )

    def _load_existing(
        self,
        artifact_path: Path,
        manifest_path: Path,
        context: PageExecutionContext,
    ) -> LegacyCompatibilityArtifact | None:
        """仅复用哈希、来源、页码和非权威标记完全匹配的既有兼容物。"""

        if not artifact_path.is_file() or not manifest_path.is_file():
            return None
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            content_hash = _sha256_file(artifact_path)
        except OSError, ValueError:
            return None
        expected = (
            "transflow.legacy-page-artifact/v1",
            "NON_AUTHORITATIVE_REBUILDABLE",
            context.source_hash,
            context.page_no,
            content_hash,
        )
        actual = (
            manifest.get("schema_version"),
            manifest.get("authority"),
            manifest.get("source_hash"),
            manifest.get("page_no"),
            manifest.get("content_hash"),
        )
        if actual != expected:
            return None
        return LegacyCompatibilityArtifact(
            artifact_path,
            manifest_path,
            context.source_hash,
            context.page_no,
            content_hash,
        )


class LegacyLeafPort(Protocol):
    """描述 Adapter 可包装的旧叶阶段，不允许自由字典跨越生产边界。"""

    def prepare(
        self,
        single_page_pdf: Path,
        context: PageExecutionContext,
        facts: ExtractedPageFacts,
    ) -> PageTemplate:
        """使用非权威单页兼容物执行旧叶 prepare。"""

        ...

    def build_translation_request(self, template: PageTemplate) -> TranslationBatch:
        """构造旧叶保持原顺序的翻译请求。"""

        ...

    def consume_translation_bundle(
        self,
        template: PageTemplate,
        dispatch: TranslationDispatch,
    ) -> ToolboxLayoutPlan:
        """消费已校验的翻译返回或结构化失败。"""

        ...

    def render(
        self,
        context: PageExecutionContext,
        facts: ExtractedPageFacts,
        plan: ToolboxLayoutPlan,
    ) -> ToolboxCandidate:
        """执行旧叶原有的渲染顺序。"""

        ...

    def judge(self, candidate: ToolboxCandidate) -> ToolboxJudgement:
        """执行旧叶原有的判断顺序。"""

        ...

    def repair(
        self,
        candidate: ToolboxCandidate,
        judgement: ToolboxJudgement,
    ) -> ToolboxCandidate:
        """执行旧叶原有的有界修复顺序。"""

        ...


class LegacyToolboxAdapter(PageToolbox):
    """只转换参数、登记来源并委派阶段，不改写旧叶算法顺序。"""

    def __init__(
        self,
        descriptor: ToolboxDescriptor,
        legacy_leaf: LegacyLeafPort,
        source_pdf: Path,
        workspace: RunWorkspace,
        materializer: LegacyPageMaterializer | None = None,
    ) -> None:
        """绑定单个旧叶、完整源 PDF 和当前 run 私有工作区。"""

        self._descriptor = descriptor
        self._legacy_leaf = legacy_leaf
        self._source_pdf = source_pdf
        self._workspace = workspace
        self._materializer = materializer or LegacyPageMaterializer()
        self._artifacts: dict[str, LegacyCompatibilityArtifact] = {}

    @property
    def descriptor(self) -> ToolboxDescriptor:
        """返回适配器冻结的旧叶描述符。"""

        return self._descriptor

    def prepare(
        self,
        context: PageExecutionContext,
        facts: ExtractedPageFacts,
    ) -> PageTemplate:
        """物化非权威单页后原样调用旧叶 prepare。"""

        artifact = self._materializer.materialize(self._source_pdf, context, self._workspace)
        template = self._legacy_leaf.prepare(artifact.path, context, facts)
        self._artifacts[template.template_id] = artifact
        return template

    def build_translation_request(self, template: PageTemplate) -> TranslationBatch:
        """原样调用旧叶请求构造并保持 unit 顺序。"""

        return self._legacy_leaf.build_translation_request(template)

    def consume_translation_bundle(
        self,
        template: PageTemplate,
        dispatch: TranslationDispatch,
    ) -> ToolboxLayoutPlan:
        """把结构化翻译结果交回旧叶，不暴露 TranslationPort。"""

        return self._legacy_leaf.consume_translation_bundle(template, dispatch)

    def render(
        self,
        context: PageExecutionContext,
        facts: ExtractedPageFacts,
        plan: ToolboxLayoutPlan,
    ) -> ToolboxCandidate:
        """保持旧叶渲染调用顺序。"""

        return self._legacy_leaf.render(context, facts, plan)

    def judge(self, candidate: ToolboxCandidate) -> ToolboxJudgement:
        """保持旧叶 Judge 调用顺序。"""

        return self._legacy_leaf.judge(candidate)

    def repair(
        self,
        candidate: ToolboxCandidate,
        judgement: ToolboxJudgement,
    ) -> ToolboxCandidate:
        """保持旧叶 Repair 调用顺序。"""

        return self._legacy_leaf.repair(candidate, judgement)

    def compatibility_artifact(self, template_id: str) -> LegacyCompatibilityArtifact:
        """返回指定模板的非权威来源记录，供迁移证据检查。"""

        try:
            return self._artifacts[template_id]
        except KeyError as error:
            raise DomainContractError(
                ErrorCode.INVALID_IDENTITY,
                "未知 legacy template_id",
            ) from error


class LegacyStatus(StrEnum):
    """列出结果 mapper 唯一接受的旧叶状态。"""

    ACCEPTED = "ACCEPTED"
    FALLBACK = "FALLBACK"


@dataclass(frozen=True, slots=True)
class LegacyNormalizedResult:
    """表示旧叶必须显式给出的已绑定结构化结果，禁止自由字典。"""

    status: LegacyStatus
    source_hash: str
    page_no: int
    owner: str
    patch: PagePatch | None
    findings: tuple[Finding, ...]
    verdict: Decision
    outcome: PageOutcome
    ordered_unit_ids: tuple[str, ...]


def map_legacy_result(
    value: object,
    context: PageExecutionContext,
    expected_owner: str,
) -> ToolboxExecutionResult:
    """严格归一旧叶结果；自由字典、未知状态或未绑定结果全部拒绝。"""

    LOGGER.info("调用 legacy 结果映射，意图=拒绝猜测成功 page_no=%s", context.page_no)
    if not isinstance(value, LegacyNormalizedResult):
        raise DomainContractError(ErrorCode.INVALID_CONTRACT, "legacy 结果必须是结构化合同")
    if (
        value.source_hash != context.source_hash
        or value.page_no != context.page_no
        or value.owner != expected_owner
        or value.outcome.page_no != context.page_no
    ):
        raise DomainContractError(
            ErrorCode.INVALID_IDENTITY,
            "legacy 结果未绑定当前 source/page/owner",
        )
    if value.patch is not None:
        value.patch.validate_binding(context, expected_owner)
    return ToolboxExecutionResult(
        page_no=value.page_no,
        patch=value.patch if value.status is LegacyStatus.ACCEPTED else None,
        findings=value.findings,
        verdict=value.verdict,
        outcome=value.outcome,
        trace=ToolboxExecutionTrace(
            (
                "prepare",
                "build_translation_request",
                "consume_translation_bundle",
                "render",
                "judge",
                "repair",
                "outcome",
            )
        ),
        ordered_unit_ids=value.ordered_unit_ids,
    )


def main() -> int:
    """记录旧叶兼容物只允许存在于 run/page 私有工作区。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    LOGGER.info("LegacyToolboxAdapter 示例，意图=Lift-and-Wrap 且不改写旧叶算法")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
