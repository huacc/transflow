"""定义 SemanticUnitMap、翻译完整性裁决与可恢复 Checkpoint 合同。"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Any

from transflow.domain.common import (
    canonical_json_bytes,
    content_sha256,
    require_non_empty,
    require_sha256,
    require_unique,
)
from transflow.domain.errors import DomainContractError, ErrorCode
from transflow.domain.translation import TranslationBundle

LOGGER = logging.getLogger("transflow.domain.completeness")
PACKAGE_ROOT = Path(__file__).resolve().parent.parent
SEMANTIC_MAP_SCHEMA = "transflow.semantic-unit-map/v1"
SEMANTIC_MAP_SCHEMA_V2 = "transflow.semantic-unit-map/v2"
COMPLETENESS_SCHEMA = "transflow.translation-completeness/v1"
PLACEHOLDER_PATTERN = re.compile(
    r"(?:\[\s*(?:待翻译|占位|placeholder)\s*\]|\{\{.+?\}\}|\bTODO\b|\?{3,})",
    re.IGNORECASE,
)
ERROR_ECHO_PATTERN = re.compile(
    r"^\s*(?:error|exception|timeout|translation failed|翻译失败|调用失败)\s*[:：]",
    re.IGNORECASE,
)
LATIN_WORD_PATTERN = re.compile(r"\b[A-Za-z]{2,}\b")


class SemanticUnitDisposition(StrEnum):
    """表示语义单元在翻译前冻结的唯一处理方式。"""

    TRANSLATE = "TRANSLATE"
    KEEP_SOURCE = "KEEP_SOURCE"
    PROTECTED = "PROTECTED"
    UNSUPPORTED = "UNSUPPORTED"
    UNRESOLVED = "UNRESOLVED"


class KeepSourceReason(StrEnum):
    """列出允许保留源文字且可审计的机械原因。"""

    NUMERIC_OR_SYMBOLIC_LITERAL = "NUMERIC_OR_SYMBOLIC_LITERAL"
    CODE_OR_ACRONYM = "CODE_OR_ACRONYM"
    URL_OR_EMAIL = "URL_OR_EMAIL"
    ALREADY_TARGET_LANGUAGE = "ALREADY_TARGET_LANGUAGE"
    PAGE_NUMBER = "PAGE_NUMBER"
    SHARED_MARGIN_OWNER = "SHARED_MARGIN_OWNER"
    EXPLICIT_PROPER_NAME = "EXPLICIT_PROPER_NAME"


class CompletenessStatus(StrEnum):
    """表示完整性门禁的二值状态。"""

    PASS = "PASS"
    FAIL = "FAIL"


class CompletenessDisposition(StrEnum):
    """表示裁决后每个语义单元的实际唯一处置。"""

    TRANSLATED = "TRANSLATED"
    KEEP_SOURCE = "KEEP_SOURCE"
    PROTECTED = "PROTECTED"
    FAILED = "FAILED"


class CompletenessErrorCode(StrEnum):
    """列出翻译完整性失败的稳定错误目录。"""

    MISSING_UNIT = "MISSING_UNIT"
    DUPLICATE_UNIT = "DUPLICATE_UNIT"
    EXTRA_UNIT = "EXTRA_UNIT"
    EMPTY_TRANSLATION = "EMPTY_TRANSLATION"
    PLACEHOLDER = "PLACEHOLDER"
    ERROR_ECHO = "ERROR_ECHO"
    UNJUSTIFIED_SOURCE_COPY = "UNJUSTIFIED_SOURCE_COPY"
    REQUIRED_LITERAL_BROKEN = "REQUIRED_LITERAL_BROKEN"
    SOURCE_LANGUAGE_RESIDUAL = "SOURCE_LANGUAGE_RESIDUAL"
    UNSUPPORTED_UNIT = "UNSUPPORTED_UNIT"
    UNRESOLVED_UNIT = "UNRESOLVED_UNIT"
    PORT_FAILURE = "PORT_FAILURE"


@dataclass(frozen=True, slots=True)
class SemanticUnit:
    """冻结一个原生文字单元的身份、容器、owner、顺序与必保留字面量。"""

    unit_id: str
    object_id: str
    container_id: str
    owner: str
    ordinal: int
    source_text: str
    source_hash: str
    required_literals: tuple[str, ...]
    disposition: SemanticUnitDisposition
    keep_source_reason: KeepSourceReason | None = None
    source_object_ids: tuple[str, ...] = ()
    disposition_reason: str | None = None

    def __post_init__(self) -> None:
        """校验单元身份、源哈希、顺序及 KEEP_SOURCE 原因闭合。"""

        for value, name in (
            (self.unit_id, "unit_id"),
            (self.object_id, "object_id"),
            (self.container_id, "container_id"),
            (self.owner, "owner"),
            (self.source_text, "source_text"),
        ):
            require_non_empty(value, name)
        require_sha256(self.source_hash, "source_hash")
        if hashlib.sha256(self.source_text.encode("utf-8")).hexdigest() != self.source_hash:
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "SemanticUnit 源文字哈希不匹配")
        if self.ordinal < 0:
            raise DomainContractError(ErrorCode.INVALID_IDENTITY, "SemanticUnit ordinal 不得为负")
        require_unique(self.required_literals, "required_literals")
        if not self.source_object_ids:
            object.__setattr__(self, "source_object_ids", (self.object_id,))
        require_unique(self.source_object_ids, "source_object_ids")
        if any(not object_id for object_id in self.source_object_ids):
            raise DomainContractError(ErrorCode.INVALID_IDENTITY, "源文字对象身份不得为空")
        if self.disposition is SemanticUnitDisposition.KEEP_SOURCE:
            if self.keep_source_reason is None:
                raise DomainContractError(
                    ErrorCode.INVALID_CONTRACT,
                    "KEEP_SOURCE 必须携带枚举原因",
                )
        elif self.keep_source_reason is not None:
            raise DomainContractError(
                ErrorCode.INVALID_CONTRACT,
                "非 KEEP_SOURCE 单元不得携带保留原因",
            )
        if self.disposition in {
            SemanticUnitDisposition.PROTECTED,
            SemanticUnitDisposition.UNSUPPORTED,
        }:
            if not self.disposition_reason:
                raise DomainContractError(
                    ErrorCode.INVALID_CONTRACT,
                    "PROTECTED/UNSUPPORTED 单元必须携带结构化原因",
                )
        elif self.disposition_reason is not None:
            raise DomainContractError(
                ErrorCode.INVALID_CONTRACT,
                "其他语义处置不得携带能力原因",
            )

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> SemanticUnit:
        """从 JSON 对象恢复语义单元并重新验证合同。"""

        reason = payload.get("keep_source_reason")
        return cls(
            unit_id=str(payload["unit_id"]),
            object_id=str(payload["object_id"]),
            container_id=str(payload["container_id"]),
            owner=str(payload["owner"]),
            ordinal=int(payload["ordinal"]),
            source_text=str(payload["source_text"]),
            source_hash=str(payload["source_hash"]),
            required_literals=tuple(str(item) for item in payload["required_literals"]),
            disposition=SemanticUnitDisposition(payload["disposition"]),
            keep_source_reason=KeepSourceReason(reason) if reason is not None else None,
            source_object_ids=tuple(
                str(item)
                for item in payload.get("source_object_ids", (payload["object_id"],))
            ),
            disposition_reason=(
                str(payload["disposition_reason"])
                if payload.get("disposition_reason") is not None
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class SemanticUnitMap:
    """表示调用 TranslationPort 前冻结的单页完整语义分母。"""

    map_id: str
    page_no: int
    source_hash: str
    entries: tuple[SemanticUnit, ...]
    schema_version: str = SEMANTIC_MAP_SCHEMA

    def __post_init__(self) -> None:
        """校验页面身份、Schema、单元/对象唯一性与连续阅读顺序。"""

        require_non_empty(self.map_id, "map_id")
        require_sha256(self.source_hash, "source_hash")
        if self.schema_version not in {SEMANTIC_MAP_SCHEMA, SEMANTIC_MAP_SCHEMA_V2}:
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "SemanticUnitMap Schema 无效")
        if self.schema_version == SEMANTIC_MAP_SCHEMA and any(
            item.disposition
            in {SemanticUnitDisposition.PROTECTED, SemanticUnitDisposition.UNSUPPORTED}
            for item in self.entries
        ):
            raise DomainContractError(
                ErrorCode.INVALID_CONTRACT,
                "v1 SemanticUnitMap 不支持 PROTECTED/UNSUPPORTED",
            )
        if self.page_no < 1:
            raise DomainContractError(ErrorCode.INVALID_IDENTITY, "SemanticUnitMap page_no 无效")
        require_unique(tuple(item.unit_id for item in self.entries), "entries.unit_id")
        require_unique(tuple(item.object_id for item in self.entries), "entries.object_id")
        require_unique(
            tuple(
                object_id
                for item in self.entries
                for object_id in item.source_object_ids
            ),
            "entries.source_object_ids",
        )
        if tuple(item.ordinal for item in self.entries) != tuple(range(len(self.entries))):
            raise DomainContractError(
                ErrorCode.INVALID_IDENTITY,
                "SemanticUnitMap 阅读顺序必须从零连续递增",
            )

    @property
    def map_hash(self) -> str:
        """返回不包含自引用哈希字段的规范内容哈希。"""

        return content_sha256(self._core_dict())

    @property
    def translated_unit_ids(self) -> tuple[str, ...]:
        """返回必须由 TranslationPort 产生译文的有序 unit ID。"""

        return tuple(
            item.unit_id
            for item in self.entries
            if item.disposition is SemanticUnitDisposition.TRANSLATE
        )

    @property
    def unresolved_unit_ids(self) -> tuple[str, ...]:
        """返回尚未形成 owner/处置合同的有序 unit ID。"""

        return tuple(
            item.unit_id
            for item in self.entries
            if item.disposition is SemanticUnitDisposition.UNRESOLVED
        )

    @property
    def unsupported_unit_ids(self) -> tuple[str, ...]:
        """返回已明确缺少当前能力、必须阻断产品 PASS 的单元。"""

        return tuple(
            item.unit_id
            for item in self.entries
            if item.disposition is SemanticUnitDisposition.UNSUPPORTED
        )

    def _core_dict(self) -> dict[str, Any]:
        """构造用于哈希的无自引用规范对象。"""

        entries: list[dict[str, Any]] = []
        for item in self.entries:
            payload: dict[str, Any] = {
                "container_id": item.container_id,
                "disposition": item.disposition.value,
                "keep_source_reason": (
                    item.keep_source_reason.value
                    if item.keep_source_reason is not None
                    else None
                ),
                "object_id": item.object_id,
                "ordinal": item.ordinal,
                "owner": item.owner,
                "required_literals": list(item.required_literals),
                "source_hash": item.source_hash,
                "source_text": item.source_text,
                "unit_id": item.unit_id,
            }
            if self.schema_version == SEMANTIC_MAP_SCHEMA_V2:
                payload["disposition_reason"] = item.disposition_reason
                payload["source_object_ids"] = list(item.source_object_ids)
            entries.append(payload)
        return {
            "entries": entries,
            "map_id": self.map_id,
            "page_no": self.page_no,
            "schema_version": self.schema_version,
            "source_hash": self.source_hash,
        }

    def to_dict(self) -> dict[str, Any]:
        """序列化为携带可复算 map_hash 的纯 JSON 对象。"""

        payload = self._core_dict()
        payload["map_hash"] = self.map_hash
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> SemanticUnitMap:
        """从 JSON 对象恢复并验证声明哈希。"""

        restored = cls(
            map_id=str(payload["map_id"]),
            page_no=int(payload["page_no"]),
            source_hash=str(payload["source_hash"]),
            entries=tuple(SemanticUnit.from_dict(item) for item in payload["entries"]),
            schema_version=str(payload["schema_version"]),
        )
        if str(payload.get("map_hash", "")) != restored.map_hash:
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "SemanticUnitMap 哈希不匹配")
        return restored


@dataclass(frozen=True, slots=True)
class TranslationCandidate:
    """保存 Provider 返回的原始候选，允许门禁观察空串和重复身份。"""

    unit_id: str
    translated_text: str

    def __post_init__(self) -> None:
        """只校验身份和文字类型，不提前吞掉完整性错误。"""

        require_non_empty(self.unit_id, "candidate.unit_id")
        if not isinstance(self.translated_text, str):
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "候选译文必须为字符串")


@dataclass(frozen=True, slots=True)
class CompletenessError:
    """记录一个可定位到 unit 的稳定完整性失败。"""

    code: CompletenessErrorCode
    unit_id: str
    detail: str

    def __post_init__(self) -> None:
        """校验错误身份和受控说明非空。"""

        require_non_empty(self.unit_id, "error.unit_id")
        require_non_empty(self.detail, "error.detail")

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> CompletenessError:
        """从 JSON 对象恢复完整性错误。"""

        return cls(
            CompletenessErrorCode(payload["code"]),
            str(payload["unit_id"]),
            str(payload["detail"]),
        )


@dataclass(frozen=True, slots=True)
class SemanticUnitDecision:
    """记录一个语义单元在完整性裁决后的唯一处置。"""

    unit_id: str
    disposition: CompletenessDisposition
    keep_source_reason: KeepSourceReason | None = None

    def __post_init__(self) -> None:
        """校验 KEEP_SOURCE 裁决仍保留原始枚举原因。"""

        require_non_empty(self.unit_id, "decision.unit_id")
        if self.disposition is CompletenessDisposition.KEEP_SOURCE:
            if self.keep_source_reason is None:
                raise DomainContractError(ErrorCode.INVALID_CONTRACT, "保留源文裁决缺少原因")
        elif self.keep_source_reason is not None:
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "非保留裁决不得携带原因")

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> SemanticUnitDecision:
        """从 JSON 对象恢复单元裁决。"""

        reason = payload.get("keep_source_reason")
        return cls(
            str(payload["unit_id"]),
            CompletenessDisposition(payload["disposition"]),
            KeepSourceReason(reason) if reason is not None else None,
        )


@dataclass(frozen=True, slots=True)
class TranslationCompletenessDecision:
    """表示完整 map 的唯一处置、错误集合与可恢复内容哈希。"""

    map_hash: str
    status: CompletenessStatus
    bundle_hash: str | None
    dispositions: tuple[SemanticUnitDecision, ...]
    errors: tuple[CompletenessError, ...]
    schema_version: str = COMPLETENESS_SCHEMA

    def __post_init__(self) -> None:
        """校验状态、哈希、处置唯一性和 PASS/FAIL 一致性。"""

        require_sha256(self.map_hash, "map_hash")
        if self.bundle_hash is not None:
            require_sha256(self.bundle_hash, "bundle_hash")
        if self.schema_version != COMPLETENESS_SCHEMA:
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "完整性 Schema 无效")
        require_unique(tuple(item.unit_id for item in self.dispositions), "dispositions.unit_id")
        if self.status is CompletenessStatus.PASS and self.errors:
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "PASS 裁决不得携带错误")
        if self.status is CompletenessStatus.FAIL and not self.errors:
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "FAIL 裁决必须携带错误")

    @property
    def decision_hash(self) -> str:
        """返回不包含自引用字段的规范裁决哈希。"""

        return content_sha256(self._core_dict())

    def with_bundle_hash(self, bundle_hash: str) -> TranslationCompletenessDecision:
        """在最终完整 Bundle 形成后返回绑定其哈希的新裁决。"""

        if self.status is not CompletenessStatus.PASS:
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "FAIL 裁决不能绑定 Bundle")
        return replace(self, bundle_hash=require_sha256(bundle_hash, "bundle_hash"))

    def _core_dict(self) -> dict[str, Any]:
        """构造用于序列化和哈希的规范对象。"""

        return {
            "bundle_hash": self.bundle_hash,
            "dispositions": [
                {
                    "disposition": item.disposition.value,
                    "keep_source_reason": (
                        item.keep_source_reason.value
                        if item.keep_source_reason is not None
                        else None
                    ),
                    "unit_id": item.unit_id,
                }
                for item in self.dispositions
            ],
            "errors": [
                {"code": item.code.value, "detail": item.detail, "unit_id": item.unit_id}
                for item in self.errors
            ],
            "map_hash": self.map_hash,
            "schema_version": self.schema_version,
            "status": self.status.value,
        }

    def to_dict(self) -> dict[str, Any]:
        """序列化为携带可复算 decision_hash 的纯 JSON 对象。"""

        payload = self._core_dict()
        payload["decision_hash"] = self.decision_hash
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> TranslationCompletenessDecision:
        """从 JSON 对象恢复并验证声明哈希。"""

        bundle_hash = payload.get("bundle_hash")
        restored = cls(
            map_hash=str(payload["map_hash"]),
            status=CompletenessStatus(payload["status"]),
            bundle_hash=str(bundle_hash) if bundle_hash is not None else None,
            dispositions=tuple(
                SemanticUnitDecision.from_dict(item) for item in payload["dispositions"]
            ),
            errors=tuple(CompletenessError.from_dict(item) for item in payload["errors"]),
            schema_version=str(payload["schema_version"]),
        )
        if str(payload.get("decision_hash", "")) != restored.decision_hash:
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "完整性裁决哈希不匹配")
        return restored


def bundle_content_hash(bundle: TranslationBundle) -> str:
    """计算严格 TranslationBundle 的规范内容哈希。"""

    return content_sha256(bundle)


@dataclass(frozen=True, slots=True)
class CompletenessCheckpoint:
    """把 map、完整 Bundle 与 PASS 裁决绑定为可恢复安全点。"""

    semantic_map: SemanticUnitMap
    bundle: TranslationBundle | None
    decision: TranslationCompletenessDecision

    def __post_init__(self) -> None:
        """校验 map/decision/Bundle 三者哈希闭合。"""

        if self.decision.map_hash != self.semantic_map.map_hash:
            raise DomainContractError(ErrorCode.CHECKPOINT_INCOMPATIBLE, "Map/Decision 哈希不闭合")
        if self.decision.status is not CompletenessStatus.PASS:
            raise DomainContractError(
                ErrorCode.CHECKPOINT_INCOMPATIBLE,
                "只允许保存 PASS 完整性安全点",
            )
        if self.bundle is None:
            if self.decision.bundle_hash is not None:
                raise DomainContractError(
                    ErrorCode.CHECKPOINT_INCOMPATIBLE,
                    "零翻译安全点不得声明 Bundle 哈希",
                )
        elif self.decision.bundle_hash != bundle_content_hash(self.bundle):
            raise DomainContractError(
                ErrorCode.CHECKPOINT_INCOMPATIBLE,
                "Bundle/Decision 哈希不闭合",
            )

    @property
    def checkpoint_hash(self) -> str:
        """返回三份合同聚合后的规范内容哈希。"""

        return content_sha256(self._core_dict())

    def _core_dict(self) -> dict[str, Any]:
        """构造无自引用的 Checkpoint 载荷。"""

        return {
            "bundle": (
                {
                    "batch_id": self.bundle.batch_id,
                    "requested_unit_ids": list(self.bundle.requested_unit_ids),
                    "units": [
                        {"translated_text": item.translated_text, "unit_id": item.unit_id}
                        for item in self.bundle.units
                    ],
                }
                if self.bundle is not None
                else None
            ),
            "decision": self.decision.to_dict(),
            "semantic_map": self.semantic_map.to_dict(),
        }

    def to_dict(self) -> dict[str, Any]:
        """序列化为携带聚合哈希的纯 JSON 对象。"""

        payload = self._core_dict()
        payload["checkpoint_hash"] = self.checkpoint_hash
        return payload

    def to_bytes(self) -> bytes:
        """返回适合原子 Checkpoint 存储的规范 JSON 字节。"""

        return canonical_json_bytes(self.to_dict())

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> CompletenessCheckpoint:
        """从 JSON 对象恢复并验证聚合哈希。"""

        raw_bundle = payload.get("bundle")
        restored = cls(
            semantic_map=SemanticUnitMap.from_dict(payload["semantic_map"]),
            bundle=(
                TranslationBundle.from_dict(raw_bundle) if raw_bundle is not None else None
            ),
            decision=TranslationCompletenessDecision.from_dict(payload["decision"]),
        )
        if str(payload.get("checkpoint_hash", "")) != restored.checkpoint_hash:
            raise DomainContractError(ErrorCode.CHECKPOINT_INCOMPATIBLE, "完整性安全点哈希不匹配")
        return restored


def main() -> int:
    """记录完整性合同的调用意图。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    LOGGER.info("SemanticUnitMap 示例，意图=在 TranslationPort 前冻结语义分母")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
