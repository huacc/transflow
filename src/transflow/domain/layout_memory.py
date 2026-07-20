"""定义 P9A 文档级布局记忆、严格序列化、哈希和失效合同。"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from transflow.domain.common import (
    canonical_json_bytes,
    content_sha256,
    require_sha256,
    require_unique,
)
from transflow.domain.errors import DomainContractError, ErrorCode

LOGGER = logging.getLogger("transflow.domain.layout_memory")
DOMAIN_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_VERSION = "transflow.document-layout-memory/v1"
ABSOLUTE_PATH = re.compile(r"(?i)(?:^|[^A-Za-z0-9_])[A-Z]:[\\/]")
SECRET_PATTERN = re.compile(r"(?i)(?:api[_-]?key|authorization|bearer\s+|token\s*[:=])")
FORBIDDEN_KEYS = frozenset(
    {
        "api_key",
        "company_name",
        "file_name",
        "full_text",
        "provider_response",
        "raw_text",
        "sample_id",
        "semantic_unit_map",
        "translated_text",
    }
)


class LayoutFactKind(StrEnum):
    """区分 PDF 可直接读取的事实与聚合推断。"""

    OBSERVED = "observed"
    INFERRED = "inferred"


@dataclass(frozen=True, slots=True)
class LayoutFactProvenance:
    """记录布局事实的来源引用、事实类型、置信度和约束强度。"""

    kind: LayoutFactKind
    source_refs: tuple[str, ...]
    confidence: float
    hard_constraint: bool

    def __post_init__(self) -> None:
        """校验来源存在、置信区间有效且低置信推断不能升级为硬约束。"""

        require_unique(self.source_refs, "provenance.source_refs")
        if not self.source_refs or not 0.0 <= self.confidence <= 1.0:
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "布局事实来源或置信度无效")
        if self.kind is LayoutFactKind.INFERRED and self.confidence < 0.8 and self.hard_constraint:
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "低置信推断不得成为硬约束")


@dataclass(frozen=True, slots=True)
class PageFactsRef:
    """只引用一页权威 PageFacts/Route，不复制页内结构事实。"""

    page_no: int
    page_identity: str
    geometry_hash: str
    facts_hash: str
    route: str
    route_hash: str
    media_box: tuple[float, float, float, float]
    crop_box: tuple[float, float, float, float]
    rotation: int
    provenance: LayoutFactProvenance

    def __post_init__(self) -> None:
        """校验 1-based 页身份、哈希、几何和旋转。"""

        if self.page_no < 1 or not self.route:
            raise DomainContractError(ErrorCode.INVALID_IDENTITY, "PageFactsRef 页面或 Route 无效")
        for field_name in ("page_identity", "geometry_hash", "facts_hash", "route_hash"):
            require_sha256(getattr(self, field_name), field_name)
        if self.rotation not in {0, 90, 180, 270}:
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "页面旋转无效")


@dataclass(frozen=True, slots=True)
class LayoutRoleProfile:
    """保存有跨页消费者的语义角色字体与间距聚合，不保存原文。"""

    role: str
    sample_count: int
    font_names: tuple[str, ...]
    font_size_range: tuple[float, float]
    baseline_gap_range: tuple[float, float]
    line_gap_range: tuple[float, float]
    paragraph_gap_range: tuple[float, float]
    indent_range: tuple[float, float]
    alignments: tuple[str, ...]
    provenance: LayoutFactProvenance

    def __post_init__(self) -> None:
        """校验角色、样本数、唯一枚举和全部受控数值区间。"""

        if not self.role or self.sample_count < 1:
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "角色画像缺少角色或样本")
        require_unique(self.font_names, "font_names")
        require_unique(self.alignments, "alignments")
        for value in (
            self.font_size_range,
            self.baseline_gap_range,
            self.line_gap_range,
            self.paragraph_gap_range,
            self.indent_range,
        ):
            if value[0] > value[1]:
                raise DomainContractError(ErrorCode.INVALID_CONTRACT, "角色画像区间上下界倒置")


@dataclass(frozen=True, slots=True)
class SharedRegionProfile:
    """保存跨页公共边缘聚合和来源页引用，不复制页内 owner/anchor 明细。"""

    region_id: str
    edge: str
    page_numbers: tuple[int, ...]
    normalized_bbox: tuple[float, float, float, float]
    content_hash: str
    provenance: LayoutFactProvenance

    def __post_init__(self) -> None:
        """校验公共区域身份、页集合、归一化坐标和内容指纹。"""

        if not self.region_id or self.edge not in {"top", "bottom", "protected"}:
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "公共区域身份或边缘无效")
        if not self.page_numbers or tuple(sorted(set(self.page_numbers))) != self.page_numbers:
            raise DomainContractError(ErrorCode.INVALID_IDENTITY, "公共区域页号必须唯一有序")
        require_sha256(self.content_hash, "content_hash")
        if any(value < 0.0 or value > 1.0 for value in self.normalized_bbox):
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "公共区域坐标必须归一化")


@dataclass(frozen=True, slots=True)
class SourceLayoutBaseline:
    """组合完整页引用、公共区域和角色聚合，作为源布局唯一文档级快照。"""

    page_refs: tuple[PageFactsRef, ...]
    shared_regions: tuple[SharedRegionProfile, ...]
    role_profiles: tuple[LayoutRoleProfile, ...]

    def __post_init__(self) -> None:
        """校验全页连续、各类身份唯一并固定规范顺序。"""

        page_numbers = tuple(item.page_no for item in self.page_refs)
        if page_numbers != tuple(range(1, len(self.page_refs) + 1)):
            raise DomainContractError(ErrorCode.INVALID_IDENTITY, "文档页引用必须完整、连续、有序")
        require_unique(tuple(item.region_id for item in self.shared_regions), "shared_regions")
        require_unique(tuple(item.role for item in self.role_profiles), "role_profiles")


@dataclass(frozen=True, slots=True)
class TargetLayoutPolicy:
    """声明翻译前即可确定的目标语言字体与布局调整边界。"""

    fallback_font_ids: tuple[str, ...]
    font_scale_range: tuple[float, float]
    line_spacing_range: tuple[float, float]
    paragraph_spacing_range: tuple[float, float]
    wrap_mode: str
    glyph_coverage_required: bool

    def __post_init__(self) -> None:
        """校验字体身份唯一、策略非空和受控调整区间。"""

        require_unique(self.fallback_font_ids, "fallback_font_ids")
        if not self.fallback_font_ids or not self.wrap_mode:
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "目标布局策略不完整")
        for value in (self.font_scale_range, self.line_spacing_range, self.paragraph_spacing_range):
            if value[0] <= 0 or value[0] > value[1]:
                raise DomainContractError(ErrorCode.INVALID_CONTRACT, "目标布局调整区间无效")


@dataclass(frozen=True, slots=True)
class DocumentLayoutMemoryIdentity:
    """绑定所有会改变文档记忆的输入指纹；静态 Repair Registry 明确不在其中。"""

    source_hash: str
    source_language: str
    target_language: str
    page_geometry_hash: str
    config_hash: str
    builder_hash: str
    classifier_hash: str
    catalog_hash: str
    kernel_hash: str
    patch_interpreter_hash: str
    font_hash: str
    schema_hash: str

    def __post_init__(self) -> None:
        """校验语言对非空以及所有兼容性指纹均为 SHA-256。"""

        if not self.source_language or not self.target_language:
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "源/目标语言不得为空")
        hash_fields = (
            "source_hash",
            "page_geometry_hash",
            "config_hash",
            "builder_hash",
            "classifier_hash",
            "catalog_hash",
            "kernel_hash",
            "patch_interpreter_hash",
            "font_hash",
            "schema_hash",
        )
        for field_name in hash_fields:
            require_sha256(getattr(self, field_name), field_name)

    @property
    def identity_hash(self) -> str:
        """计算兼容身份指纹，供 Checkpoint 恢复和 CAS 使用。"""

        return content_sha256(self)

    def changed_fields(self, other: DocumentLayoutMemoryIdentity) -> tuple[str, ...]:
        """列出拒绝旧记忆复用的全部变化字段。"""

        return tuple(
            name
            for name in self.__dataclass_fields__
            if getattr(self, name) != getattr(other, name)
        )


@dataclass(frozen=True, slots=True)
class DocumentLayoutMemory:
    """表示全页屏障后一次构建、冻结只读的文档级布局记忆。"""

    schema_version: str
    identity: DocumentLayoutMemoryIdentity
    source_layout_baseline: SourceLayoutBaseline
    target_layout_policy: TargetLayoutPolicy

    def __post_init__(self) -> None:
        """拒绝未知 Schema、源身份不一致和任何敏感/无界内容。"""

        if self.schema_version != SCHEMA_VERSION:
            raise DomainContractError(
                ErrorCode.INVALID_CONTRACT, "未知 DocumentLayoutMemory major Schema"
            )
        if any(
            item.provenance.source_refs[0] != item.page_identity
            for item in self.source_layout_baseline.page_refs
        ):
            raise DomainContractError(
                ErrorCode.INVALID_IDENTITY, "PageFactsRef provenance 与页面身份不一致"
            )
        _validate_safe_payload(self._payload())

    def _payload(self) -> dict[str, Any]:
        """生成不含派生 memory_hash 的规范负载。"""

        return _encode(self)

    @property
    def canonical_bytes(self) -> bytes:
        """返回跨枚举顺序、线程和进程稳定的规范 JSON 字节。"""

        return canonical_json_bytes(self._payload())

    @property
    def memory_hash(self) -> str:
        """计算内容寻址 memory hash。"""

        return hashlib.sha256(self.canonical_bytes).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        """输出携带派生 hash 的严格持久化字典。"""

        return {**self._payload(), "memory_hash": self.memory_hash}

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> DocumentLayoutMemory:
        """严格加载持久化对象并重新校验 Schema、安全边界和内容哈希。"""

        expected_keys = {
            "schema_version",
            "identity",
            "source_layout_baseline",
            "target_layout_policy",
            "memory_hash",
        }
        if set(payload) != expected_keys:
            raise DomainContractError(
                ErrorCode.INVALID_CONTRACT, "DocumentLayoutMemory 字段集合漂移"
            )
        _validate_safe_payload(payload)
        try:
            identity = DocumentLayoutMemoryIdentity(**payload["identity"])
            baseline_payload = payload["source_layout_baseline"]
            page_refs = tuple(_page_ref_from_dict(item) for item in baseline_payload["page_refs"])
            shared = tuple(
                _shared_region_from_dict(item) for item in baseline_payload["shared_regions"]
            )
            roles = tuple(
                _role_profile_from_dict(item) for item in baseline_payload["role_profiles"]
            )
            memory = cls(
                schema_version=payload["schema_version"],
                identity=identity,
                source_layout_baseline=SourceLayoutBaseline(page_refs, shared, roles),
                target_layout_policy=TargetLayoutPolicy(
                    fallback_font_ids=tuple(payload["target_layout_policy"]["fallback_font_ids"]),
                    font_scale_range=tuple(payload["target_layout_policy"]["font_scale_range"]),
                    line_spacing_range=tuple(payload["target_layout_policy"]["line_spacing_range"]),
                    paragraph_spacing_range=tuple(
                        payload["target_layout_policy"]["paragraph_spacing_range"]
                    ),
                    wrap_mode=payload["target_layout_policy"]["wrap_mode"],
                    glyph_coverage_required=payload["target_layout_policy"][
                        "glyph_coverage_required"
                    ],
                ),
            )
        except (KeyError, TypeError, ValueError) as error:
            if isinstance(error, DomainContractError):
                raise
            raise DomainContractError(
                ErrorCode.INVALID_CONTRACT, "DocumentLayoutMemory 结构无效"
            ) from error
        if memory.memory_hash != payload["memory_hash"]:
            raise DomainContractError(ErrorCode.ARTIFACT_INTEGRITY_FAILED, "memory_hash 不匹配")
        return memory

    @classmethod
    def from_bytes(cls, content: bytes) -> DocumentLayoutMemory:
        """从 UTF-8 JSON 字节加载并拒绝坏编码或非对象根。"""

        try:
            payload = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise DomainContractError(
                ErrorCode.INVALID_CONTRACT, "DocumentLayoutMemory JSON 无效"
            ) from error
        if not isinstance(payload, dict):
            raise DomainContractError(
                ErrorCode.INVALID_CONTRACT, "DocumentLayoutMemory 根必须是对象"
            )
        return cls.from_dict(payload)


@dataclass(frozen=True, slots=True)
class DocumentLayoutMemoryRef:
    """表示 Context/Checkpoint 唯一允许传递的不可变内容寻址引用。"""

    memory_hash: str
    identity_hash: str
    artifact_id: str
    relative_path: str
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        """校验内容身份、Artifact 身份和 run 相对路径。"""

        require_sha256(self.memory_hash, "memory_hash")
        require_sha256(self.identity_hash, "identity_hash")
        if not self.artifact_id or not self.relative_path or Path(self.relative_path).is_absolute():
            raise DomainContractError(ErrorCode.INVALID_IDENTITY, "文档记忆引用身份或路径无效")
        if ABSOLUTE_PATH.search(self.relative_path) or ".." in Path(self.relative_path).parts:
            raise DomainContractError(
                ErrorCode.PATH_OUTSIDE_ALLOWED_ROOT, "文档记忆引用必须是受控相对路径"
            )


def _provenance_from_dict(payload: dict[str, Any]) -> LayoutFactProvenance:
    """从严格字典恢复事实来源。"""

    return LayoutFactProvenance(
        kind=LayoutFactKind(payload["kind"]),
        source_refs=tuple(payload["source_refs"]),
        confidence=payload["confidence"],
        hard_constraint=payload["hard_constraint"],
    )


def _page_ref_from_dict(payload: dict[str, Any]) -> PageFactsRef:
    """从严格字典恢复 PageFactsRef。"""

    return PageFactsRef(
        **{
            key: value
            for key, value in payload.items()
            if key not in {"media_box", "crop_box", "provenance"}
        },
        media_box=tuple(payload["media_box"]),
        crop_box=tuple(payload["crop_box"]),
        provenance=_provenance_from_dict(payload["provenance"]),
    )


def _shared_region_from_dict(payload: dict[str, Any]) -> SharedRegionProfile:
    """从严格字典恢复公共区域聚合。"""

    return SharedRegionProfile(
        region_id=payload["region_id"],
        edge=payload["edge"],
        page_numbers=tuple(payload["page_numbers"]),
        normalized_bbox=tuple(payload["normalized_bbox"]),
        content_hash=payload["content_hash"],
        provenance=_provenance_from_dict(payload["provenance"]),
    )


def _role_profile_from_dict(payload: dict[str, Any]) -> LayoutRoleProfile:
    """从严格字典恢复角色聚合。"""

    return LayoutRoleProfile(
        role=payload["role"],
        sample_count=payload["sample_count"],
        font_names=tuple(payload["font_names"]),
        font_size_range=tuple(payload["font_size_range"]),
        baseline_gap_range=tuple(payload["baseline_gap_range"]),
        line_gap_range=tuple(payload["line_gap_range"]),
        paragraph_gap_range=tuple(payload["paragraph_gap_range"]),
        indent_range=tuple(payload["indent_range"]),
        alignments=tuple(payload["alignments"]),
        provenance=_provenance_from_dict(payload["provenance"]),
    )


def _encode(value: Any) -> Any:
    """递归编码领域对象，并对无序语义集合使用稳定顺序。"""

    from transflow.domain.common import json_ready

    payload = json_ready(value)
    if isinstance(value, SourceLayoutBaseline):
        payload["shared_regions"] = sorted(
            payload["shared_regions"], key=lambda item: item["region_id"]
        )
        payload["role_profiles"] = sorted(payload["role_profiles"], key=lambda item: item["role"])
    return payload


def _validate_safe_payload(payload: Any, *, key: str = "") -> None:
    """递归拒绝秘密、Provider 响应、无界正文、样本身份和宿主绝对路径。"""

    if key.casefold() in FORBIDDEN_KEYS:
        raise DomainContractError(ErrorCode.INVALID_CONTRACT, f"禁止字段进入文档记忆: {key}")
    if isinstance(payload, dict):
        for child_key, value in payload.items():
            _validate_safe_payload(value, key=str(child_key))
        return
    if isinstance(payload, list | tuple):
        for value in payload:
            _validate_safe_payload(value, key=key)
        return
    if isinstance(payload, str):
        if len(payload) > 2048 or ABSOLUTE_PATH.search(payload) or SECRET_PATTERN.search(payload):
            raise DomainContractError(
                ErrorCode.INVALID_CONTRACT, "文档记忆含秘密、无界内容或绝对路径"
            )


def main() -> int:
    """记录文档记忆必须由完整事实构建器创建。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    LOGGER.info("DocumentLayoutMemory 示例，意图=由 Builder 注入完整 PageFacts/Route")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
