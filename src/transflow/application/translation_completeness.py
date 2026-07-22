"""建立统一 SemanticUnitMap，并执行翻译完整性与定向重译门禁。"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from transflow.domain.completeness import (
    SEMANTIC_MAP_SCHEMA_V2,
    CompletenessCheckpoint,
    CompletenessDisposition,
    CompletenessError,
    CompletenessErrorCode,
    CompletenessStatus,
    KeepSourceReason,
    SemanticUnit,
    SemanticUnitDecision,
    SemanticUnitDisposition,
    SemanticUnitMap,
    TranslationCandidate,
    TranslationCompletenessDecision,
    bundle_content_hash,
)
from transflow.domain.errors import DomainContractError, ErrorCode, PortCallError
from transflow.domain.text_inventory import InventoryDisposition, PageTextInventory
from transflow.domain.translation import (
    TranslatedUnit,
    TranslationBatch,
    TranslationBundle,
    TranslationUnit,
)
from transflow.pdf_kernel.facts import ExtractedPageFacts, KernelTextFact, RectTuple
from transflow.pdf_kernel.text_inventory import freeze_page_text_inventory
from transflow.ports.translation import TranslationPort
from transflow.toolboxes.contracts import PageTemplate

LOGGER = logging.getLogger("transflow.application.translation_completeness")
APPLICATION_ROOT = Path(__file__).resolve().parent.parent
PAGE_NUMBER_PATTERN = re.compile(r"^\s*(?:[ivxlcdm]+|\d+)(?:\s*/\s*\d+)?\s*$", re.IGNORECASE)
URL_EMAIL_PATTERN = re.compile(r"(?:https?://\S+|www\.\S+|[\w.+-]+@[\w.-]+\.\w+)", re.IGNORECASE)
NUMERIC_PATTERN = re.compile(r"^[\s\d,.:;()%+\-/$€£¥]+$")
CURRENCY_SCALE_PATTERN = re.compile(r"^[$€£¥]\s*(?:k|m|mn|b|bn|t)$", re.IGNORECASE)
CODE_PATTERN = re.compile(r"^(?=.*[A-Z0-9])[A-Z0-9][A-Z0-9._/\-]{1,31}$")
REQUIRED_LITERAL_PATTERN = re.compile(
    r"(?:https?://\S+|www\.\S+|[\w.+-]+@[\w.-]+\.\w+|"
    r"(?:[$€£¥]\s*)?\d[\d,]*(?:\.\d+)?%?|\b[A-Z]{2,}(?:[-_/][A-Z0-9]+)*\b)"
)
PLACEHOLDER_PATTERN = re.compile(
    r"(?:\[\s*(?:待翻译|占位|placeholder)\s*\]|\{\{.+?\}\}|\bTODO\b|\?{3,})",
    re.IGNORECASE,
)
ERROR_ECHO_PATTERN = re.compile(
    r"^\s*(?:error|exception|timeout|translation failed|翻译失败|调用失败)\s*[:：]",
    re.IGNORECASE,
)
LATIN_WORD_PATTERN = re.compile(r"\b[A-Za-z]{2,}\b")


class CompletenessCheckpointPort(Protocol):
    """描述页级翻译完整性安全点的最小持久化能力。"""

    def load(self, page_no: int) -> CompletenessCheckpoint | None:
        """读取并验证指定页面的完整性安全点。"""

        ...

    def commit(self, page_no: int, checkpoint: CompletenessCheckpoint) -> None:
        """原子提交一个已经闭合的完整性安全点。"""

        ...


@dataclass(frozen=True, slots=True)
class _NativeTextRecord:
    """统一 Kernel block/span 的最小原生文字视图。"""

    object_id: str
    text: str
    bbox: RectTuple


@dataclass(frozen=True, slots=True)
class CompletenessGateResult:
    """聚合 map、完整 Bundle、裁决、实际请求批次与恢复信息。"""

    semantic_map: SemanticUnitMap
    bundle: TranslationBundle | None
    decision: TranslationCompletenessDecision
    request_batches: tuple[TranslationBatch, ...]
    provider_bundles: tuple[TranslationBundle, ...] = ()
    resumed: bool = False

    @property
    def retry_count(self) -> int:
        """返回定向重译次数，不计首次请求。"""

        return max(0, len(self.request_batches) - 1)

    def checkpoint(self) -> CompletenessCheckpoint:
        """把 PASS 结果提升为可原子保存的安全点。"""

        return CompletenessCheckpoint(self.semantic_map, self.bundle, self.decision)


def extract_required_literals(source_text: str) -> tuple[str, ...]:
    """按出现顺序提取数字、单位、代码、邮箱和网址等必保留字面量。"""

    literals: list[str] = []
    words = LATIN_WORD_PATTERN.findall(source_text)
    all_caps_heading = (
        bool(words)
        and not re.search(r"[a-z]", source_text)
        and all(word.isupper() for word in words)
    )
    prefix = re.match(r"^\s*((?:\d+|[A-Za-z])[.)])\s+", source_text)
    if prefix is not None:
        literals.append(prefix.group(1))
    for match in REQUIRED_LITERAL_PATTERN.finditer(source_text):
        literal = match.group(0).strip()
        if all_caps_heading and literal.isalpha() and literal.isupper():
            continue
        literals.append(literal)
    return tuple(dict.fromkeys(literals))


def _keep_source_reason(text: str) -> KeepSourceReason | None:
    """只对机械可证明的文本返回显式保留原因。"""

    stripped = text.strip()
    if PAGE_NUMBER_PATTERN.fullmatch(stripped):
        return KeepSourceReason.PAGE_NUMBER
    if URL_EMAIL_PATTERN.fullmatch(stripped):
        return KeepSourceReason.URL_OR_EMAIL
    if CURRENCY_SCALE_PATTERN.fullmatch(stripped) or NUMERIC_PATTERN.fullmatch(stripped) or (
        stripped and not any(character.isalpha() for character in stripped)
    ):
        return KeepSourceReason.NUMERIC_OR_SYMBOLIC_LITERAL
    if CODE_PATTERN.fullmatch(stripped):
        return KeepSourceReason.CODE_OR_ACRONYM
    if re.search(r"[\u4e00-\u9fff]", stripped) and not re.search(r"[A-Za-z]{3,}", stripped):
        return KeepSourceReason.ALREADY_TARGET_LANGUAGE
    return None


def _requested_unit_keep_source_reason(text: str) -> KeepSourceReason | None:
    """复用 Kernel 的机械预授权，禁止叶把已冻结处置改回 TRANSLATE。"""

    return _keep_source_reason(text)


def _canonical_records(
    template: PageTemplate,
    facts: ExtractedPageFacts,
) -> tuple[_NativeTextRecord, ...]:
    """选择与当前叶 object_id 层级一致的原生文字分母。"""

    spans = {
        item.object_id: _NativeTextRecord(item.object_id, item.text, item.bbox)
        for item in facts.text_spans
        if item.text.strip()
    }
    blocks = {
        item.object_id: _NativeTextRecord(item.object_id, item.text, item.bbox)
        for item in facts.objects
        if item.kind == "text" and not item.protected and item.text.strip()
    }
    owned = set(template.object_ids)
    if owned and owned <= set(spans):
        source = spans
    elif owned and owned <= set(blocks):
        source = blocks
    elif spans:
        source = spans
    else:
        source = blocks
    # Kernel 枚举已稳定；这里按几何和 object_id 再排序以消除 Provider/字典顺序影响。
    return tuple(
        sorted(
            source.values(),
            key=lambda item: (
                round(item.bbox[1], 4),
                round(item.bbox[0], 4),
                item.object_id,
            ),
        )
    )


def _projected_inventory_ids(
    record: _NativeTextRecord,
    facts: ExtractedPageFacts,
    inventory_by_id: Mapping[str, object],
) -> tuple[str, ...]:
    """把 span 或 block 机械投影到独立 Inventory 的不重叠文字对象。"""

    if record.object_id in inventory_by_id:
        return (record.object_id,)
    block = next(
        (
            item
            for item in facts.objects
            if item.object_id == record.object_id
            and item.kind == "text"
            and not item.protected
        ),
        None,
    )
    if block is None:
        return ()
    spans_by_block: dict[int, list[KernelTextFact]] = {}
    for item in facts.text_spans:
        if item.object_id in inventory_by_id:
            spans_by_block.setdefault(item.block_index, []).append(item)
    exact_groups: list[tuple[float, int, tuple[str, ...]]] = []
    normalized_block_text = " ".join(block.text.split())
    for block_index, items in spans_by_block.items():
        ordered = tuple(sorted(items, key=lambda item: (item.line_index, item.span_index)))
        lines: dict[int, list[KernelTextFact]] = {}
        for item in ordered:
            lines.setdefault(item.line_index, []).append(item)
        projected_text = " ".join(
            "".join(item.text for item in line_items).strip()
            for _, line_items in sorted(lines.items())
        )
        if " ".join(projected_text.split()) != normalized_block_text:
            continue
        projected_bbox = (
            min(item.bbox[0] for item in ordered),
            min(item.bbox[1] for item in ordered),
            max(item.bbox[2] for item in ordered),
            max(item.bbox[3] for item in ordered),
        )
        distance = sum(
            abs(left - right)
            for left, right in zip(block.bbox, projected_bbox, strict=True)
        )
        exact_groups.append(
            (distance, block_index, tuple(item.object_id for item in ordered))
        )
    if exact_groups:
        return min(exact_groups, key=lambda item: (item[0], item[1]))[2]
    return tuple(
        item.object_id
        for item in facts.text_spans
        if item.object_id in inventory_by_id
        and block.bbox[0] <= (item.bbox[0] + item.bbox[2]) / 2 <= block.bbox[2]
        and block.bbox[1] <= (item.bbox[1] + item.bbox[3]) / 2 <= block.bbox[3]
    )


def build_semantic_unit_map(
    template: PageTemplate,
    batch: TranslationBatch | None,
    facts: ExtractedPageFacts,
    inventory: PageTextInventory | None = None,
) -> SemanticUnitMap:
    """把独立文字清单映射为唯一 owner/翻译单元，并执行双向覆盖门禁。"""

    LOGGER.info(
        "调用 SemanticUnitMap 构建，意图=冻结翻译分母 page_no=%s owner=%s",
        template.context.page_no,
        template.owner,
    )
    frozen_inventory = inventory or freeze_page_text_inventory(facts)
    if (
        frozen_inventory.page_no != template.context.page_no
        or frozen_inventory.page_identity != facts.page_identity
        or frozen_inventory.kernel_facts_hash != facts.kernel_facts_hash
    ):
        raise DomainContractError(
            ErrorCode.INVALID_IDENTITY, "PageTextInventory 页面或 Kernel 身份漂移"
        )
    native_records = {
        item.object_id: _NativeTextRecord(item.object_id, item.text, item.bbox)
        for item in facts.text_spans
        if item.text.strip()
    }
    native_records.update(
        {
            item.object_id: _NativeTextRecord(item.object_id, item.text, item.bbox)
            for item in facts.objects
            if item.kind == "text" and not item.protected and item.text.strip()
        }
    )
    # 清单冻结最细分母；SemanticUnitMap 可按叶声明投影为等价 block/span 层级，
    # 但正文仍只从不可变 Kernel 事实读取，绝不从 Provider 回填。
    records = _canonical_records(template, facts)
    record_by_id = {item.object_id: item for item in records}
    batch_units = batch.units if batch is not None else ()
    object_by_unit: dict[str, str] = {}
    canonical_ids = set(record_by_id)
    if len(template.object_ids) == len(batch_units):
        object_by_unit = {
            unit.unit_id: object_id
            for unit, object_id in zip(batch_units, template.object_ids, strict=True)
            if object_id in canonical_ids
        }
    # 旧叶可能声明 block 身份而 Kernel 清单采用 span 身份；只允许按真实原文精确对齐，
    # 不允许为批次凭空生成 inventory 外对象。
    unused = [
        item.object_id for item in records if item.object_id not in object_by_unit.values()
    ]
    for unit in batch_units:
        if unit.unit_id in object_by_unit:
            continue
        matched = next(
            (
                object_id
                for object_id in unused
                if record_by_id[object_id].text.strip() == unit.source_text.strip()
            ),
            None,
        )
        if matched is not None:
            object_by_unit[unit.unit_id] = matched
            unused.remove(matched)
    unit_by_object: dict[str, TranslationUnit] = {}
    for batch_unit in batch_units:
        owned_object_id = object_by_unit.get(batch_unit.unit_id)
        if owned_object_id is not None:
            unit_by_object[owned_object_id] = batch_unit
    inventory_by_id = {item.object_id: item for item in frozen_inventory.items}
    entries: list[SemanticUnit] = []
    claimed_inventory_ids: set[str] = set()
    included_batch_ids: set[str] = set()
    for record in records:
        owned_unit = unit_by_object.get(record.object_id)
        if owned_unit is None:
            continue
        projected_ids = _projected_inventory_ids(record, facts, inventory_by_id)
        if not projected_ids:
            raise DomainContractError(
                ErrorCode.INVALID_CONTRACT,
                "翻译单元无法投影到 PageTextInventory",
            )
        included_batch_ids.add(owned_unit.unit_id)
        if record.bbox[3] <= facts.page.height_points * 0.08:
            owner = "shared.margin.header"
        elif record.bbox[1] >= facts.page.height_points * 0.92:
            owner = "shared.margin.footer"
        else:
            owner = template.owner
        reason = _requested_unit_keep_source_reason(owned_unit.source_text)
        if reason is not None and any(
            inventory_by_id[object_id].disposition is not InventoryDisposition.KEEP_SOURCE
            for object_id in projected_ids
        ):
            reason = None
        disposition = (
            SemanticUnitDisposition.KEEP_SOURCE
            if reason is not None
            else SemanticUnitDisposition.TRANSLATE
        )
        if disposition is SemanticUnitDisposition.KEEP_SOURCE:
            source_object_ids = projected_ids
        else:
            source_object_ids = tuple(
                object_id
                for object_id in projected_ids
                if inventory_by_id[object_id].disposition is InventoryDisposition.TRANSLATE
            )
            if not source_object_ids:
                raise DomainContractError(
                    ErrorCode.INVALID_CONTRACT,
                    "要求翻译的单元没有 Kernel 预授权的 TRANSLATE 对象",
                )
        claimed_inventory_ids.update(source_object_ids)
        entries.append(
            SemanticUnit(
                unit_id=owned_unit.unit_id,
                object_id=record.object_id,
                container_id=owned_unit.region_id,
                owner=owner,
                ordinal=len(entries),
                source_text=owned_unit.source_text,
                source_hash=hashlib.sha256(
                    owned_unit.source_text.encode("utf-8")
                ).hexdigest(),
                required_literals=extract_required_literals(owned_unit.source_text),
                disposition=disposition,
                keep_source_reason=reason,
                source_object_ids=source_object_ids,
            )
        )
    for unit in batch_units:
        if unit.unit_id not in included_batch_ids:
            raise DomainContractError(
                ErrorCode.INVALID_CONTRACT,
                f"翻译请求包含 Inventory 无法追溯的单元 unit_id={unit.unit_id}",
            )
    top_margin = facts.page.height_points * 0.08
    bottom_margin = facts.page.height_points * 0.92
    for item in frozen_inventory.items:
        if item.object_id in claimed_inventory_ids:
            continue
        native_record = native_records.get(item.object_id)
        if native_record is None:
            raise DomainContractError(
                ErrorCode.INVALID_CONTRACT,
                "PageTextInventory 对象无法回溯到 Kernel 文字事实",
            )
        if native_record.bbox[3] <= top_margin:
            owner = "shared.margin.header"
        elif native_record.bbox[1] >= bottom_margin:
            owner = "shared.margin.footer"
        else:
            owner = template.owner
        reason = (
            KeepSourceReason(item.keep_source_reason)
            if item.keep_source_reason is not None
            else None
        )
        if item.disposition is InventoryDisposition.KEEP_SOURCE:
            disposition = SemanticUnitDisposition.KEEP_SOURCE
        elif item.disposition is InventoryDisposition.PROTECTED:
            disposition = SemanticUnitDisposition.PROTECTED
        elif item.disposition is InventoryDisposition.UNSUPPORTED:
            disposition = SemanticUnitDisposition.UNSUPPORTED
        else:
            disposition = SemanticUnitDisposition.UNRESOLVED
        entries.append(
            SemanticUnit(
                unit_id=hashlib.sha256(
                    f"{facts.page_identity}\0{item.object_id}\0semantic".encode("ascii")
                ).hexdigest(),
                object_id=item.object_id,
                container_id=f"{owner}-p{template.context.page_no:04d}",
                owner=owner,
                ordinal=len(entries),
                source_text=native_record.text,
                source_hash=item.source_hash,
                required_literals=extract_required_literals(native_record.text),
                disposition=disposition,
                keep_source_reason=reason,
                source_object_ids=(item.object_id,),
                disposition_reason=item.disposition_reason,
            )
        )
    bbox_by_id = {item.object_id: item.bbox for item in native_records.values()}
    entries.sort(
        key=lambda entry: (
            min(bbox_by_id[object_id][1] for object_id in entry.source_object_ids),
            min(bbox_by_id[object_id][0] for object_id in entry.source_object_ids),
            entry.unit_id,
        )
    )
    ordered_entries = tuple(
        replace(entry, ordinal=ordinal) for ordinal, entry in enumerate(entries)
    )
    semantic_map = SemanticUnitMap(
        map_id=f"semantic-{template.template_id}",
        page_no=template.context.page_no,
        source_hash=template.context.source_hash,
        entries=ordered_entries,
        schema_version=SEMANTIC_MAP_SCHEMA_V2,
    )
    validate_inventory_coverage(frozen_inventory, semantic_map, facts)
    return semantic_map


def validate_inventory_coverage(
    inventory: PageTextInventory,
    semantic_map: SemanticUnitMap,
    facts: ExtractedPageFacts | None = None,
) -> None:
    """核对精细 Inventory 与等价 block/span 投影的文字覆盖及预授权处置。"""

    LOGGER.info(
        "调用文字覆盖门禁，意图=阻止漏对象或事后 KEEP_SOURCE page_no=%s",
        inventory.page_no,
    )
    inventory_by_id = {item.object_id: item for item in inventory.items}
    if semantic_map.schema_version == SEMANTIC_MAP_SCHEMA_V2:
        covered_ids = tuple(
            object_id
            for mapped in semantic_map.entries
            for object_id in mapped.source_object_ids
        )
        if set(covered_ids) != set(inventory_by_id):
            missing = sorted(set(inventory_by_id) - set(covered_ids))
            added = sorted(set(covered_ids) - set(inventory_by_id))
            raise DomainContractError(
                ErrorCode.INVALID_CONTRACT,
                "PageTextInventory/SemanticUnitMap v2 双向覆盖失败 "
                f"missing={missing} added={added}",
            )
        for mapped in semantic_map.entries:
            for object_id in mapped.source_object_ids:
                inventory_item = inventory_by_id[object_id]
                allowed = {
                    InventoryDisposition.TRANSLATE: {
                        SemanticUnitDisposition.TRANSLATE,
                        SemanticUnitDisposition.UNRESOLVED,
                    },
                    InventoryDisposition.KEEP_SOURCE: {
                        SemanticUnitDisposition.KEEP_SOURCE,
                    },
                    InventoryDisposition.PROTECTED: {
                        SemanticUnitDisposition.PROTECTED,
                    },
                    InventoryDisposition.UNSUPPORTED: {
                        SemanticUnitDisposition.UNSUPPORTED,
                    },
                }[inventory_item.disposition]
                if mapped.disposition not in allowed:
                    raise DomainContractError(
                        ErrorCode.INVALID_CONTRACT,
                        f"文字对象处置与 Kernel 预授权不一致 object_id={object_id}",
                    )
                if (
                    mapped.object_id == object_id
                    and len(mapped.source_object_ids) == 1
                    and mapped.source_hash != inventory_item.source_hash
                ):
                    raise DomainContractError(
                        ErrorCode.INVALID_CONTRACT,
                        "文字内容哈希在映射期间变化",
                    )
        return
    map_by_id = {item.object_id: item for item in semantic_map.entries}
    exact_identity = set(inventory_by_id) == set(map_by_id)
    if not exact_identity and facts is None:
        missing = sorted(set(inventory_by_id) - set(map_by_id))
        added = sorted(set(map_by_id) - set(inventory_by_id))
        raise DomainContractError(
            ErrorCode.INVALID_CONTRACT,
            f"PageTextInventory/SemanticUnitMap 双向覆盖失败 missing={missing} added={added}",
        )
    if not exact_identity:
        assert facts is not None
        text_blocks = tuple(
            item
            for item in facts.objects
            if item.kind == "text" and not item.protected and item.text.strip()
        )
        block_by_id = {item.object_id: item for item in text_blocks}
        covered: set[str] = set()
        for mapped in semantic_map.entries:
            projected_ids: tuple[str, ...]
            if mapped.object_id in inventory_by_id:
                projected_ids = (mapped.object_id,)
            elif mapped.object_id in block_by_id:
                block = block_by_id[mapped.object_id]
                projected_ids = tuple(
                    item.object_id
                    for item in facts.text_spans
                    if item.text.strip()
                    and block.bbox[0] <= (item.bbox[0] + item.bbox[2]) / 2 <= block.bbox[2]
                    and block.bbox[1] <= (item.bbox[1] + item.bbox[3]) / 2 <= block.bbox[3]
                )
                if (
                    not projected_ids
                    or _normalized(mapped.source_text) != _normalized(block.text)
                ):
                    raise DomainContractError(
                        ErrorCode.INVALID_CONTRACT,
                        "SemanticUnit block 无法机械投影到 Inventory span",
                    )
            else:
                raise DomainContractError(
                    ErrorCode.INVALID_CONTRACT,
                    "SemanticUnit 使用了 Inventory 无法追溯的文字对象",
                )
            if set(projected_ids) & covered:
                raise DomainContractError(ErrorCode.INVALID_CONTRACT, "文字对象被重复归属")
            if (
                mapped.disposition is SemanticUnitDisposition.KEEP_SOURCE
                and any(
                    inventory_by_id[object_id].disposition
                    is not InventoryDisposition.KEEP_SOURCE
                    for object_id in projected_ids
                )
            ):
                raise DomainContractError(
                    ErrorCode.INVALID_CONTRACT,
                    "聚合文字块未经全部 Kernel span 预授权却事后 KEEP_SOURCE",
                )
            covered.update(projected_ids)
        if covered != set(inventory_by_id):
            raise DomainContractError(
                ErrorCode.INVALID_CONTRACT,
                "PageTextInventory/SemanticUnitMap 层级投影文字覆盖失败",
            )
        return
    for object_id, item in inventory_by_id.items():
        if object_id not in map_by_id:
            continue
        mapped = map_by_id[object_id]
        if mapped.source_hash != item.source_hash:
            raise DomainContractError(ErrorCode.INVALID_CONTRACT, "文字内容哈希在映射期间变化")
        if (
            mapped.disposition is SemanticUnitDisposition.KEEP_SOURCE
            and item.disposition is not InventoryDisposition.KEEP_SOURCE
        ):
            raise DomainContractError(
                ErrorCode.INVALID_CONTRACT,
                f"对象未经 Kernel 预授权却事后 KEEP_SOURCE object_id={object_id}",
            )


def _normalized(value: str) -> str:
    """折叠空白并统一大小写，供源文复制和字面量比较。"""

    return " ".join(value.split()).casefold()


def _restore_mechanical_list_prefix(unit: SemanticUnit, translated_text: str) -> str:
    """在门禁内恢复叶已声明的机械列表编号，不把该动作留到布局后。"""

    source = unit.source_text.lstrip()
    candidate = translated_text.lstrip()
    for literal in unit.required_literals:
        if (
            re.fullmatch(r"(?:\d+|[A-Za-z])[.)]", literal)
            and source.startswith(literal)
            and not candidate.startswith(literal)
        ):
            return f"{literal} {candidate}"
    return translated_text


def _candidate_content_errors(
    unit: SemanticUnit,
    translated_text: str,
) -> tuple[CompletenessError, ...]:
    """检查一个真实候选的内容完整性，不使用简单“必须不同”作为唯一规则。"""

    errors: list[CompletenessError] = []
    stripped = translated_text.strip()
    if not stripped:
        errors.append(
            CompletenessError(
                CompletenessErrorCode.EMPTY_TRANSLATION,
                unit.unit_id,
                "译文为空",
            )
        )
        return tuple(errors)
    if PLACEHOLDER_PATTERN.search(stripped):
        errors.append(
            CompletenessError(
                CompletenessErrorCode.PLACEHOLDER,
                unit.unit_id,
                "译文含占位内容",
            )
        )
    if ERROR_ECHO_PATTERN.search(stripped):
        errors.append(
            CompletenessError(
                CompletenessErrorCode.ERROR_ECHO,
                unit.unit_id,
                "译文回显异常文本",
            )
        )
    if _normalized(stripped) == _normalized(unit.source_text):
        errors.append(
            CompletenessError(
                CompletenessErrorCode.UNJUSTIFIED_SOURCE_COPY,
                unit.unit_id,
                "要求翻译的单元无理由照抄源文",
            )
        )
    missing_literals = tuple(
        literal
        for literal in unit.required_literals
        if _normalized(literal) not in _normalized(stripped)
    )
    if missing_literals:
        errors.append(
            CompletenessError(
                CompletenessErrorCode.REQUIRED_LITERAL_BROKEN,
                unit.unit_id,
                "必保留字面量缺失",
            )
        )
    source_words = set(LATIN_WORD_PATTERN.findall(unit.source_text.casefold()))
    candidate_words = set(LATIN_WORD_PATTERN.findall(stripped.casefold()))
    required_words = {
        word
        for literal in unit.required_literals
        for word in LATIN_WORD_PATTERN.findall(literal.casefold())
    }
    residual = (source_words & candidate_words) - required_words
    has_target_text = bool(re.search(r"[\u4e00-\u9fff]", stripped))
    if len(source_words) >= 3 and residual == source_words and not has_target_text:
        errors.append(
            CompletenessError(
                CompletenessErrorCode.SOURCE_LANGUAGE_RESIDUAL,
                unit.unit_id,
                "译文仍由源语言主体构成",
            )
        )
    return tuple(errors)


def adjudicate_translation_candidates(
    semantic_map: SemanticUnitMap,
    candidates: tuple[TranslationCandidate, ...],
) -> TranslationCompletenessDecision:
    """对完整 map 复判缺失、重复、新增、内容和显式 KEEP_SOURCE。"""

    LOGGER.info(
        "调用翻译完整性裁决，意图=阻止不完整译文进入布局 map_hash=%s",
        semantic_map.map_hash,
    )
    errors: list[CompletenessError] = []
    by_id: dict[str, list[TranslationCandidate]] = {}
    for candidate in candidates:
        by_id.setdefault(candidate.unit_id, []).append(candidate)
    known_ids = {item.unit_id for item in semantic_map.entries}
    expected_ids = set(semantic_map.translated_unit_ids)
    for unit_id, rows in by_id.items():
        if unit_id not in expected_ids:
            errors.append(
                CompletenessError(
                    CompletenessErrorCode.EXTRA_UNIT,
                    unit_id,
                    "候选包含未请求或 KEEP_SOURCE 单元",
                )
            )
        if len(rows) > 1:
            errors.append(
                CompletenessError(
                    CompletenessErrorCode.DUPLICATE_UNIT,
                    unit_id,
                    "候选重复返回同一 unit_id",
                )
            )
    dispositions: list[SemanticUnitDecision] = []
    for unit in semantic_map.entries:
        if unit.disposition is SemanticUnitDisposition.KEEP_SOURCE:
            dispositions.append(
                SemanticUnitDecision(
                    unit.unit_id,
                    CompletenessDisposition.KEEP_SOURCE,
                    unit.keep_source_reason,
                )
            )
            continue
        if unit.disposition is SemanticUnitDisposition.PROTECTED:
            dispositions.append(
                SemanticUnitDecision(unit.unit_id, CompletenessDisposition.PROTECTED)
            )
            continue
        if unit.disposition is SemanticUnitDisposition.UNSUPPORTED:
            errors.append(
                CompletenessError(
                    CompletenessErrorCode.UNSUPPORTED_UNIT,
                    unit.unit_id,
                    f"当前能力不支持该文字对象:{unit.disposition_reason}",
                )
            )
            dispositions.append(
                SemanticUnitDecision(unit.unit_id, CompletenessDisposition.FAILED)
            )
            continue
        if unit.disposition is SemanticUnitDisposition.UNRESOLVED:
            errors.append(
                CompletenessError(
                    CompletenessErrorCode.UNRESOLVED_UNIT,
                    unit.unit_id,
                    "语义单元尚未建立 owner/处置",
                )
            )
            dispositions.append(SemanticUnitDecision(unit.unit_id, CompletenessDisposition.FAILED))
            continue
        rows = by_id.get(unit.unit_id, [])
        if not rows:
            errors.append(
                CompletenessError(
                    CompletenessErrorCode.MISSING_UNIT,
                    unit.unit_id,
                    "候选缺少预期 unit_id",
                )
            )
            dispositions.append(SemanticUnitDecision(unit.unit_id, CompletenessDisposition.FAILED))
            continue
        content_errors = _candidate_content_errors(unit, rows[0].translated_text)
        errors.extend(content_errors)
        dispositions.append(
            SemanticUnitDecision(
                unit.unit_id,
                (
                    CompletenessDisposition.FAILED
                    if content_errors or len(rows) > 1
                    else CompletenessDisposition.TRANSLATED
                ),
            )
        )
    # unknown ID 已在上方登记；known_ids 仅用于保证未来 map 变体仍走封闭集合。
    if any(candidate.unit_id not in known_ids for candidate in candidates):
        LOGGER.warning("完整性候选含未知身份，意图=保持 FAIL 而不猜测映射")
    status = CompletenessStatus.FAIL if errors else CompletenessStatus.PASS
    provisional_hash = (
        hashlib.sha256(
            b"\0".join(f"{item.unit_id}\0{item.translated_text}".encode() for item in candidates)
        ).hexdigest()
        if status is CompletenessStatus.PASS and candidates
        else None
    )
    return TranslationCompletenessDecision(
        semantic_map.map_hash,
        status,
        provisional_hash,
        tuple(dispositions),
        tuple(errors),
    )


def _port_failure_decision(
    semantic_map: SemanticUnitMap,
    detail: str,
) -> TranslationCompletenessDecision:
    """把 TranslationPort 结构化失败映射为完整 map 的 FAIL 裁决。"""

    failed_ids = (
        semantic_map.translated_unit_ids
        or semantic_map.unsupported_unit_ids
        or semantic_map.unresolved_unit_ids
        or (f"page-{semantic_map.page_no}",)
    )
    dispositions = tuple(
        SemanticUnitDecision(
            item.unit_id,
            CompletenessDisposition.KEEP_SOURCE
            if item.disposition is SemanticUnitDisposition.KEEP_SOURCE
            else (
                CompletenessDisposition.PROTECTED
                if item.disposition is SemanticUnitDisposition.PROTECTED
                else CompletenessDisposition.FAILED
            ),
            (
                item.keep_source_reason
                if item.disposition is SemanticUnitDisposition.KEEP_SOURCE
                else None
            ),
        )
        for item in semantic_map.entries
    )
    return TranslationCompletenessDecision(
        semantic_map.map_hash,
        CompletenessStatus.FAIL,
        None,
        dispositions,
        tuple(
            CompletenessError(CompletenessErrorCode.PORT_FAILURE, unit_id, detail)
            for unit_id in failed_ids
        ),
    )


class TranslationCompletenessGate:
    """在布局前统一执行 map、定向重译、全量复判与安全点恢复。"""

    def __init__(self, maximum_targeted_retries: int = 1) -> None:
        """冻结每页定向重译硬上限。"""

        if maximum_targeted_retries < 0:
            raise ValueError("maximum_targeted_retries 不得为负")
        self._maximum_targeted_retries = maximum_targeted_retries

    def execute(
        self,
        semantic_map: SemanticUnitMap,
        batch: TranslationBatch | None,
        translation: TranslationPort,
        checkpoint_port: CompletenessCheckpointPort | None = None,
    ) -> CompletenessGateResult:
        """优先恢复有效安全点，否则只重译失败 unit 并对全 map 复判。"""

        LOGGER.info(
            "调用完整性门禁，意图=在布局前验证或恢复译文 page_no=%s",
            semantic_map.page_no,
        )
        if checkpoint_port is not None:
            restored = checkpoint_port.load(semantic_map.page_no)
            if restored is not None:
                if restored.semantic_map.map_hash != semantic_map.map_hash:
                    raise DomainContractError(
                        ErrorCode.CHECKPOINT_INCOMPATIBLE,
                        "完整性安全点 map 已漂移",
                    )
                return CompletenessGateResult(
                    semantic_map,
                    restored.bundle,
                    restored.decision,
                    (),
                    (),
                    True,
                )
        if semantic_map.unresolved_unit_ids or semantic_map.unsupported_unit_ids:
            decision = adjudicate_translation_candidates(semantic_map, ())
            return CompletenessGateResult(semantic_map, None, decision, ())
        if batch is None:
            decision = adjudicate_translation_candidates(semantic_map, ())
            result = CompletenessGateResult(semantic_map, None, decision, ())
            if decision.status is CompletenessStatus.PASS and checkpoint_port is not None:
                checkpoint_port.commit(semantic_map.page_no, result.checkpoint())
            return result
        map_by_id = {item.unit_id: item for item in semantic_map.entries}
        translatable_units = tuple(
            unit
            for unit in batch.units
            if map_by_id[unit.unit_id].disposition is SemanticUnitDisposition.TRANSLATE
        )
        merged: dict[str, str] = {}
        request_batches: list[TranslationBatch] = []
        provider_bundles: list[TranslationBundle] = []
        if translatable_units:
            first_batch = TranslationBatch(
                batch.batch_id,
                batch.source_language,
                batch.target_language,
                translatable_units,
            )
            request_batches.append(first_batch)
            try:
                first_bundle = translation.translate(first_batch)
            except (DomainContractError, PortCallError, TimeoutError) as error:
                error_code = getattr(
                    getattr(error, "code", None),
                    "value",
                    type(error).__name__,
                )
                decision = _port_failure_decision(
                    semantic_map,
                    f"TranslationPort 失败:{error_code}",
                )
                return CompletenessGateResult(
                    semantic_map,
                    None,
                    decision,
                    tuple(request_batches),
                    tuple(provider_bundles),
                )
            provider_bundles.append(first_bundle)
            merged.update(
                {
                    item.unit_id: (
                        _restore_mechanical_list_prefix(
                            map_by_id[item.unit_id],
                            item.translated_text,
                        )
                        if item.unit_id in map_by_id
                        else item.translated_text
                    )
                    for item in first_bundle.units
                }
            )
        candidates = tuple(
            TranslationCandidate(unit_id, merged[unit_id])
            for unit_id in semantic_map.translated_unit_ids
            if unit_id in merged
        )
        decision = adjudicate_translation_candidates(semantic_map, candidates)
        for retry_number in range(1, self._maximum_targeted_retries + 1):
            if decision.status is CompletenessStatus.PASS:
                break
            retry_ids = tuple(
                dict.fromkeys(
                    error.unit_id
                    for error in decision.errors
                    if error.code
                    in {
                        CompletenessErrorCode.EMPTY_TRANSLATION,
                        CompletenessErrorCode.PLACEHOLDER,
                        CompletenessErrorCode.ERROR_ECHO,
                        CompletenessErrorCode.UNJUSTIFIED_SOURCE_COPY,
                        CompletenessErrorCode.REQUIRED_LITERAL_BROKEN,
                        CompletenessErrorCode.SOURCE_LANGUAGE_RESIDUAL,
                    }
                )
            )
            retry_units = tuple(unit for unit in translatable_units if unit.unit_id in retry_ids)
            if not retry_units:
                break
            retry_batch = TranslationBatch(
                f"{batch.batch_id}-retry-{retry_number}",
                batch.source_language,
                batch.target_language,
                retry_units,
            )
            request_batches.append(retry_batch)
            LOGGER.info(
                "调用定向重译，意图=只修复失败 unit retry=%s unit_count=%s",
                retry_number,
                len(retry_units),
            )
            try:
                retry_bundle = translation.translate(retry_batch)
            except (DomainContractError, PortCallError, TimeoutError) as error:
                error_code = getattr(
                    getattr(error, "code", None),
                    "value",
                    type(error).__name__,
                )
                decision = _port_failure_decision(
                    semantic_map,
                    f"定向重译失败:{error_code}",
                )
                break
            provider_bundles.append(retry_bundle)
            merged.update(
                {
                    item.unit_id: (
                        _restore_mechanical_list_prefix(
                            map_by_id[item.unit_id],
                            item.translated_text,
                        )
                        if item.unit_id in map_by_id
                        else item.translated_text
                    )
                    for item in retry_bundle.units
                }
            )
            candidates = tuple(
                TranslationCandidate(unit_id, merged[unit_id])
                for unit_id in semantic_map.translated_unit_ids
                if unit_id in merged
            )
            decision = adjudicate_translation_candidates(semantic_map, candidates)
        if decision.status is not CompletenessStatus.PASS:
            return CompletenessGateResult(
                semantic_map,
                None,
                decision,
                tuple(request_batches),
                tuple(provider_bundles),
            )
        full_units = tuple(
            TranslatedUnit(
                unit.unit_id,
                (
                    unit.source_text
                    if map_by_id[unit.unit_id].disposition is SemanticUnitDisposition.KEEP_SOURCE
                    else merged[unit.unit_id]
                ),
            )
            for unit in batch.units
        )
        bundle = TranslationBundle.from_batch(batch, full_units)
        decision = decision.with_bundle_hash(bundle_content_hash(bundle))
        result = CompletenessGateResult(
            semantic_map,
            bundle,
            decision,
            tuple(request_batches),
            tuple(provider_bundles),
        )
        if checkpoint_port is not None:
            checkpoint_port.commit(semantic_map.page_no, result.checkpoint())
        return result


def main() -> int:
    """记录完整性门禁必须位于 TranslationPort 与布局之间。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    LOGGER.info("TranslationCompletenessGate 示例，意图=只让 PASS Bundle 进入布局")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
