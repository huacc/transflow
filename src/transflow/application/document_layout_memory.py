"""从完整 PageFacts/Route 屏障构建一次不可变 DocumentLayoutMemory。"""

from __future__ import annotations

import hashlib
import json
import logging
import statistics
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from transflow.domain.common import content_sha256
from transflow.domain.errors import DomainContractError, ErrorCode
from transflow.domain.layout_memory import (
    DocumentLayoutMemory,
    DocumentLayoutMemoryIdentity,
    LayoutFactKind,
    LayoutFactProvenance,
    LayoutRoleProfile,
    PageFactsRef,
    SharedRegionProfile,
    SourceLayoutBaseline,
    TargetLayoutPolicy,
)
from transflow.pdf_kernel.facts import ExtractedPageFacts, KernelTextFact

LOGGER = logging.getLogger("transflow.application.document_layout_memory")
APPLICATION_ROOT = Path(__file__).resolve().parent.parent


class LayoutMemoryBuildStatus(StrEnum):
    """表示全页屏障尚未闭合或已经形成可冻结记忆。"""

    NOT_READY = "NOT_READY"
    READY = "READY"


@dataclass(frozen=True, slots=True)
class LayoutMemoryPolicyConfig:
    """承载由统一资源文件读取的目标布局和聚合阈值。"""

    fallback_font_ids: tuple[str, ...]
    font_scale_range: tuple[float, float]
    line_spacing_range: tuple[float, float]
    paragraph_spacing_range: tuple[float, float]
    wrap_mode: str
    glyph_coverage_required: bool
    title_font_ratio: float
    shared_edge_ratio: float
    shared_min_pages: int
    low_confidence_threshold: float

    @classmethod
    def load(cls, path: Path) -> LayoutMemoryPolicyConfig:
        """从调用方注入的仓库相对资源路径读取唯一策略配置。"""

        LOGGER.info("调用布局策略读取，意图=集中加载 P9A 配置 path=%s", path.name)
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.pop("schema_version", None) != "transflow.layout-memory-policy/v1":
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "布局策略 Schema 不受支持")
        return cls(
            fallback_font_ids=tuple(payload["fallback_font_ids"]),
            font_scale_range=tuple(payload["font_scale_range"]),
            line_spacing_range=tuple(payload["line_spacing_range"]),
            paragraph_spacing_range=tuple(payload["paragraph_spacing_range"]),
            wrap_mode=payload["wrap_mode"],
            glyph_coverage_required=payload["glyph_coverage_required"],
            title_font_ratio=payload["title_font_ratio"],
            shared_edge_ratio=payload["shared_edge_ratio"],
            shared_min_pages=payload["shared_min_pages"],
            low_confidence_threshold=payload["low_confidence_threshold"],
        )

    @property
    def config_hash(self) -> str:
        """计算统一布局策略的规范内容指纹。"""

        return content_sha256(self)


@dataclass(frozen=True, slots=True)
class DocumentLayoutMemoryBuildInput:
    """绑定预期页数、完整源事实、完整 Route 和兼容身份。"""

    expected_page_count: int
    page_facts: tuple[ExtractedPageFacts, ...]
    routes: tuple[tuple[int, str], ...]
    identity: DocumentLayoutMemoryIdentity
    policy: LayoutMemoryPolicyConfig


@dataclass(frozen=True, slots=True)
class DocumentLayoutMemoryBuildResult:
    """返回可追溯 NOT_READY 原因或唯一 READY 记忆。"""

    status: LayoutMemoryBuildStatus
    missing_page_numbers: tuple[int, ...]
    missing_route_numbers: tuple[int, ...]
    memory: DocumentLayoutMemory | None


class DocumentLayoutMemoryBuilder:
    """只消费 Kernel PageFacts/Route/配置，绝不依赖 Toolbox、翻译或候选页面。"""

    def __init__(self) -> None:
        """初始化无可变跨 run 状态的纯构建器。"""

        self._build_count = 0

    @property
    def build_count(self) -> int:
        """返回当前构建器实例实际产生 READY 快照的次数。"""

        return self._build_count

    def build(self, request: DocumentLayoutMemoryBuildInput) -> DocumentLayoutMemoryBuildResult:
        """等待完整屏障，按当前事实聚合角色/公共边缘并派生目标策略。"""

        LOGGER.info(
            "调用文档布局记忆构建，意图=在页面放行前闭合全页屏障 expected_pages=%s",
            request.expected_page_count,
        )
        if request.expected_page_count < 1:
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "预期页数必须为正")
        page_by_no = {item.page.page_no: item for item in request.page_facts}
        route_by_no = dict(request.routes)
        expected = set(range(1, request.expected_page_count + 1))
        missing_pages = tuple(sorted(expected - set(page_by_no)))
        missing_routes = tuple(sorted(expected - set(route_by_no)))
        if len(page_by_no) != len(request.page_facts) or len(route_by_no) != len(request.routes):
            raise DomainContractError(ErrorCode.INVALID_IDENTITY, "PageFacts 或 Route 页号重复")
        if missing_pages or missing_routes:
            return DocumentLayoutMemoryBuildResult(
                LayoutMemoryBuildStatus.NOT_READY, missing_pages, missing_routes, None
            )
        if set(page_by_no) != expected or set(route_by_no) != expected:
            raise DomainContractError(
                ErrorCode.INVALID_IDENTITY, "PageFacts/Route 含预期范围外页号"
            )
        pages = tuple(page_by_no[number] for number in sorted(page_by_no))
        if any(item.page.source_hash != request.identity.source_hash for item in pages):
            raise DomainContractError(
                ErrorCode.INVALID_IDENTITY, "PageFacts source_hash 与记忆身份不一致"
            )
        page_refs = tuple(self._page_ref(item, route_by_no[item.page.page_no]) for item in pages)
        roles = self._role_profiles(pages, request.policy)
        shared = self._shared_regions(pages, request.policy)
        memory = DocumentLayoutMemory(
            schema_version="transflow.document-layout-memory/v1",
            identity=request.identity,
            source_layout_baseline=SourceLayoutBaseline(page_refs, shared, roles),
            target_layout_policy=TargetLayoutPolicy(
                fallback_font_ids=request.policy.fallback_font_ids,
                font_scale_range=request.policy.font_scale_range,
                line_spacing_range=request.policy.line_spacing_range,
                paragraph_spacing_range=request.policy.paragraph_spacing_range,
                wrap_mode=request.policy.wrap_mode,
                glyph_coverage_required=request.policy.glyph_coverage_required,
            ),
        )
        self._build_count += 1
        LOGGER.info(
            "文档布局记忆构建完成 memory_hash=%s pages=%s roles=%s shared=%s",
            memory.memory_hash,
            len(page_refs),
            len(roles),
            len(shared),
        )
        return DocumentLayoutMemoryBuildResult(LayoutMemoryBuildStatus.READY, (), (), memory)

    @staticmethod
    def _page_ref(facts: ExtractedPageFacts, route: str) -> PageFactsRef:
        """把权威页面事实窄化为内容寻址引用。"""

        return PageFactsRef(
            page_no=facts.page.page_no,
            page_identity=facts.page_identity,
            geometry_hash=facts.page.geometry_hash,
            facts_hash=facts.kernel_facts_hash,
            route=route,
            route_hash=hashlib.sha256(route.encode("utf-8")).hexdigest(),
            media_box=facts.media_box,
            crop_box=facts.crop_box,
            rotation=facts.rotation,
            provenance=LayoutFactProvenance(
                LayoutFactKind.OBSERVED, (facts.page_identity,), 1.0, True
            ),
        )

    @classmethod
    def _role_profiles(
        cls,
        pages: tuple[ExtractedPageFacts, ...],
        policy: LayoutMemoryPolicyConfig,
    ) -> tuple[LayoutRoleProfile, ...]:
        """按当前结构事实聚合有真实跨页消费者的字体、baseline 与间距画像。"""

        all_sizes = [
            span.font_size for page in pages for span in page.text_spans if span.text.strip()
        ]
        median_size = statistics.median(all_sizes) if all_sizes else 10.0
        grouped: dict[str, list[tuple[ExtractedPageFacts, KernelTextFact]]] = {}
        for page in pages:
            for span in page.text_spans:
                if not span.text.strip():
                    continue
                role = cls._role_for_span(page, span, median_size, policy)
                grouped.setdefault(role, []).append((page, span))
        profiles = [cls._profile(role, values) for role, values in sorted(grouped.items())]
        if not profiles:
            # 无可见文字 PDF 仍形成真实视觉角色，避免伪造正文统计。
            refs = tuple(item.page_identity for item in pages)
            profiles.append(
                LayoutRoleProfile(
                    role="visual_only",
                    sample_count=len(pages),
                    font_names=("NONE",),
                    font_size_range=(0.0, 0.0),
                    baseline_gap_range=(0.0, 0.0),
                    line_gap_range=(0.0, 0.0),
                    paragraph_gap_range=(0.0, 0.0),
                    indent_range=(0.0, 0.0),
                    alignments=("none",),
                    provenance=LayoutFactProvenance(LayoutFactKind.OBSERVED, refs, 1.0, False),
                )
            )
        return tuple(profiles)

    @staticmethod
    def _role_for_span(
        page: ExtractedPageFacts,
        span: KernelTextFact,
        median_size: float,
        policy: LayoutMemoryPolicyConfig,
    ) -> str:
        """仅按字号、表格 bbox、结构位置和当前文字形态判定语义角色。"""

        center_x = (span.bbox[0] + span.bbox[2]) / 2
        center_y = (span.bbox[1] + span.bbox[3]) / 2
        if any(
            table.bbox[0] <= center_x <= table.bbox[2]
            and table.bbox[1] <= center_y <= table.bbox[3]
            for table in page.table_objects
        ):
            return "table_text"
        if span.font_size >= median_size * policy.title_font_ratio:
            return "title"
        stripped = span.text.lstrip()
        if stripped[:1] in {"•", "-", "–", "·"} or stripped[:2].rstrip(".)、").isdigit():
            return "list"
        if page.image_objects or page.drawing_objects:
            if len(stripped) <= 48:
                return "visual_label"
        return "body"

    @staticmethod
    def _profile(
        role: str,
        values: list[tuple[ExtractedPageFacts, KernelTextFact]],
    ) -> LayoutRoleProfile:
        """用可追溯的稳健范围汇总一个角色，推断间距始终保留置信度。"""

        spans = [item[1] for item in values]
        refs = tuple(sorted({item[0].page_identity for item in values}))
        sizes = sorted(item.font_size for item in spans)
        baselines = sorted((item.bbox[1], item.bbox[3]) for item in spans)
        gaps = [
            max(0.0, baselines[index][0] - baselines[index - 1][1])
            for index in range(1, len(baselines))
        ]
        gap_range = (round(min(gaps), 4), round(max(gaps), 4)) if gaps else (0.0, 0.0)
        indents = [round(item.bbox[0], 4) for item in spans]
        confidence = min(1.0, 0.55 + len(spans) / 20)
        return LayoutRoleProfile(
            role=role,
            sample_count=len(spans),
            font_names=tuple(sorted({item.font_name or "UNKNOWN" for item in spans})),
            font_size_range=(round(min(sizes), 4), round(max(sizes), 4)),
            baseline_gap_range=gap_range,
            line_gap_range=gap_range,
            paragraph_gap_range=gap_range,
            indent_range=(min(indents), max(indents)),
            alignments=("source",),
            provenance=LayoutFactProvenance(LayoutFactKind.INFERRED, refs, confidence, False),
        )

    @staticmethod
    def _shared_regions(
        pages: tuple[ExtractedPageFacts, ...],
        policy: LayoutMemoryPolicyConfig,
    ) -> tuple[SharedRegionProfile, ...]:
        """按重复文字哈希聚合公共页眉/页脚，不保存正文或页内 owner。"""

        occurrences: dict[
            tuple[str, str], list[tuple[int, tuple[float, float, float, float], str]]
        ] = {}
        for page in pages:
            height = page.page.height_points
            width = page.page.width_points
            for span in page.text_spans:
                if not span.text.strip():
                    continue
                edge = None
                if span.bbox[3] <= height * policy.shared_edge_ratio:
                    edge = "top"
                elif span.bbox[1] >= height * (1.0 - policy.shared_edge_ratio):
                    edge = "bottom"
                if edge is None:
                    continue
                digest = hashlib.sha256(" ".join(span.text.split()).encode("utf-8")).hexdigest()
                bbox = (
                    span.bbox[0] / width,
                    span.bbox[1] / height,
                    span.bbox[2] / width,
                    span.bbox[3] / height,
                )
                occurrences.setdefault((edge, digest), []).append(
                    (page.page.page_no, bbox, page.page_identity)
                )
        profiles: list[SharedRegionProfile] = []
        for (edge, digest), rows in sorted(occurrences.items()):
            page_numbers = tuple(sorted({row[0] for row in rows}))
            if len(page_numbers) < policy.shared_min_pages:
                continue
            coordinates = tuple(tuple(row[1][index] for row in rows) for index in range(4))
            bbox = (
                round(statistics.median(coordinates[0]), 6),
                round(statistics.median(coordinates[1]), 6),
                round(statistics.median(coordinates[2]), 6),
                round(statistics.median(coordinates[3]), 6),
            )
            refs = tuple(sorted({row[2] for row in rows}))
            profiles.append(
                SharedRegionProfile(
                    region_id=f"shared-{edge}-{digest[:16]}",
                    edge=edge,
                    page_numbers=page_numbers,
                    normalized_bbox=bbox,
                    content_hash=digest,
                    provenance=LayoutFactProvenance(LayoutFactKind.INFERRED, refs, 1.0, False),
                )
            )
        return tuple(profiles)


def derive_page_geometry_hash(page_facts: tuple[ExtractedPageFacts, ...]) -> str:
    """从按页号规范排序的页面几何指纹形成文档级几何身份。"""

    return content_sha256(
        tuple(
            (item.page.page_no, item.page.geometry_hash)
            for item in sorted(page_facts, key=lambda row: row.page.page_no)
        )
    )


def main() -> int:
    """从仓库资源读取策略并记录构建调用意图。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    repository_root = APPLICATION_ROOT.parent.parent
    policy = LayoutMemoryPolicyConfig.load(
        repository_root / "resources" / "manifests" / "p9a_layout_policy.json"
    )
    LOGGER.info(
        "P9A Builder 示例，意图=等待调用方提供完整 PageFacts/Route config_hash=%s",
        policy.config_hash,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
