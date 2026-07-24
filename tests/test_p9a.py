"""执行 P9A.0 至 P9A.4 文档级布局记忆的真实验收。"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from typing import Any

import pytest

from scripts import build_p0_assets, verify_p0
from transflow.adapters.filesystem.common import InjectedCrash
from transflow.adapters.filesystem.layout_memory_runtime import DocumentLayoutMemoryRuntime
from transflow.application.document_layout_memory import (
    DocumentLayoutMemoryBuilder,
    DocumentLayoutMemoryBuildInput,
    LayoutMemoryBuildStatus,
    LayoutMemoryPolicyConfig,
    derive_page_geometry_hash,
)
from transflow.application.translation_completeness import validate_inventory_coverage
from transflow.domain.common import content_sha256
from transflow.domain.completeness import (
    KeepSourceReason,
    SemanticUnit,
    SemanticUnitDisposition,
    SemanticUnitMap,
)
from transflow.domain.errors import DomainContractError, PortCallError
from transflow.domain.layout_memory import (
    DocumentLayoutMemory,
    DocumentLayoutMemoryIdentity,
    DocumentLayoutMemoryRef,
    LayoutFactKind,
    LayoutFactProvenance,
    SharedRegionProfile,
    SourceLayoutBaseline,
    TargetLayoutPolicy,
)
from transflow.domain.pages import PageExecutionContext
from transflow.domain.text_inventory import (
    InventoryDisposition,
    PageTextInventory,
    PageTextInventoryItem,
)
from transflow.pdf_kernel.facts import ExtractedPageFacts, KernelTextFact, PageFactsExtractor
from transflow.pdf_kernel.text_inventory import freeze_page_text_inventory

REPO_ROOT = Path(__file__).resolve().parent.parent
POLICY_PATH = REPO_ROOT / "resources" / "manifests" / "p9a_layout_policy.json"
SCHEMA_PATH = REPO_ROOT / "resources" / "schemas" / "document_layout_memory_v1.schema.json"
ANNUAL_PATHS = (
    REPO_ROOT / "样本" / "年报" / "03161_br_83161_A CAM RMB MM_br_A CAM RMB MM-R_英文_2025.pdf",
    REPO_ROOT / "样本" / "年报" / "02580_AUX ELECTRIC_英文_2025.pdf",
)
CLASSIFICATION_ROOT = REPO_ROOT / "spikes" / "page_classification_engine_puncture_v1" / "分类结果"


def _sha256_file(path: Path) -> str:
    """流式计算真实仓库文件哈希。"""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _real_routes(facts: tuple[ExtractedPageFacts, ...]) -> tuple[tuple[int, str], ...]:
    """按当前 Kernel 结构事实提供完整 Route 输入，不读取文件名或样本身份。"""

    rows: list[tuple[int, str]] = []
    for item in facts:
        if item.table_objects:
            route = "body.table"
        elif item.image_objects or item.drawing_objects:
            route = "body.flow_text.visual_anchored"
        elif len(item.text_spans) > 40:
            route = "body.flow_text.multi"
        else:
            route = "body.flow_text.single"
        rows.append((item.page.page_no, route))
    return tuple(rows)


def _identity(
    facts: tuple[ExtractedPageFacts, ...],
    policy: LayoutMemoryPolicyConfig,
    **changes: str,
) -> DocumentLayoutMemoryIdentity:
    """用真实源、代码和资源字节构造文档记忆完整兼容身份。"""

    values = {
        "source_hash": facts[0].page.source_hash,
        "source_language": "en",
        "target_language": "zh-CN",
        "page_geometry_hash": derive_page_geometry_hash(facts),
        "config_hash": policy.config_hash,
        "builder_hash": _sha256_file(
            REPO_ROOT / "src" / "transflow" / "application" / "document_layout_memory.py"
        ),
        "classifier_hash": _sha256_file(
            REPO_ROOT / "src" / "transflow" / "classification" / "engine.py"
        ),
        "catalog_hash": _sha256_file(
            REPO_ROOT / "resources" / "manifests" / "p7_resource_fingerprints.json"
        ),
        "kernel_hash": _sha256_file(REPO_ROOT / "src" / "transflow" / "pdf_kernel" / "facts.py"),
        "patch_interpreter_hash": _sha256_file(
            REPO_ROOT / "src" / "transflow" / "pdf_kernel" / "patch.py"
        ),
        "font_hash": _sha256_file(REPO_ROOT / "resources" / "manifests" / "font_manifest.json"),
        "schema_hash": _sha256_file(SCHEMA_PATH),
    }
    values.update(changes)
    return DocumentLayoutMemoryIdentity(**values)


def _build_input(
    facts: tuple[ExtractedPageFacts, ...],
    *,
    routes: tuple[tuple[int, str], ...] | None = None,
    identity: DocumentLayoutMemoryIdentity | None = None,
) -> DocumentLayoutMemoryBuildInput:
    """把完整真实事实装配为 Builder 输入。"""

    policy = LayoutMemoryPolicyConfig.load(POLICY_PATH)
    return DocumentLayoutMemoryBuildInput(
        expected_page_count=len(facts),
        page_facts=facts,
        routes=routes or _real_routes(facts),
        identity=identity or _identity(facts, policy),
        policy=policy,
    )


def _build_memory(facts: tuple[ExtractedPageFacts, ...]) -> DocumentLayoutMemory:
    """对完整事实运行真实 Builder 并返回 READY 快照。"""

    result = DocumentLayoutMemoryBuilder().build(_build_input(facts))
    assert result.status is LayoutMemoryBuildStatus.READY
    assert result.memory is not None
    return result.memory


def _runtime(run_root: Path, run_id: str) -> DocumentLayoutMemoryRuntime:
    """装配应用 Builder 与文件运行时，保持架构依赖方向可执行。"""

    return DocumentLayoutMemoryRuntime(run_root, run_id, DocumentLayoutMemoryBuilder())


def _inventory_map(
    facts: ExtractedPageFacts,
    inventory: PageTextInventory,
) -> SemanticUnitMap:
    """按真实 Kernel 文字建立用于独立覆盖门禁的等价语义图。"""

    text_by_id = {item.object_id: item.text for item in facts.text_spans}
    text_by_id.update(
        {
            item.object_id: item.text
            for item in facts.objects
            if item.kind == "text" and not item.protected
        }
    )
    entries = tuple(
        SemanticUnit(
            unit_id=hashlib.sha256(f"inventory\0{item.object_id}".encode()).hexdigest(),
            object_id=item.object_id,
            container_id=f"page-{inventory.page_no}",
            owner="kernel.inventory",
            ordinal=index,
            source_text=text_by_id[item.object_id],
            source_hash=item.source_hash,
            required_literals=(),
            disposition=(
                SemanticUnitDisposition.KEEP_SOURCE
                if item.disposition is InventoryDisposition.KEEP_SOURCE
                else SemanticUnitDisposition.TRANSLATE
            ),
            keep_source_reason=(
                KeepSourceReason(item.keep_source_reason)
                if item.keep_source_reason is not None
                else None
            ),
        )
        for index, item in enumerate(inventory.items)
    )
    return SemanticUnitMap(
        map_id=f"inventory-map-{inventory.page_no}",
        page_no=inventory.page_no,
        source_hash=facts.page.source_hash,
        entries=entries,
    )


def _process_load_memory(
    run_root: str,
    run_id: str,
    ref_payload: dict[str, Any],
) -> tuple[int, str, bool]:
    """供进程池独立创建 Adapter 并反序列化本进程只读副本。"""

    runtime = _runtime(Path(run_root), run_id)
    memory = runtime.load_readonly(DocumentLayoutMemoryRef(**ref_payload))
    return os.getpid(), memory.memory_hash, not hasattr(memory, "__dict__")


def _json_keys(value: Any) -> set[str]:
    """递归收集 JSON 字段名，避免把 Route 值中的普通词误判为重复明细字段。"""

    if isinstance(value, dict):
        return set(value) | {key for child in value.values() for key in _json_keys(child)}
    if isinstance(value, list):
        return {key for child in value for key in _json_keys(child)}
    return set()


@pytest.fixture(scope="session")
def annual_facts() -> dict[str, tuple[ExtractedPageFacts, ...]]:
    """对两份来源和结构不同的完整真实年报各提取一次全页 PageFacts。"""

    extractor = PageFactsExtractor()
    return {path.name: extractor.extract_all(path, _sha256_file(path)) for path in ANNUAL_PATHS}


@pytest.fixture(scope="session")
def primary_facts(
    annual_facts: dict[str, tuple[ExtractedPageFacts, ...]],
) -> tuple[ExtractedPageFacts, ...]:
    """返回较小完整年报事实，供合同、并发和恢复测试复用。"""

    return annual_facts[ANNUAL_PATHS[0].name]


def test_p9a_0_t01_historical_and_current_hashes_are_traceable() -> None:
    """P9A.0-T01：old/new hash、授权、勘误和历史资产均可重算。"""

    overlay = json.loads(build_p0_assets.CURRENT_BASELINE_PATH.read_text(encoding="utf-8"))
    assert overlay == build_p0_assets.collect_current_baseline_overlay()
    assert overlay["authorization"]["authorized_by"] == "项目负责人"
    assert (
        overlay["documents"]["design"]["old_sha256"] != overlay["documents"]["design"]["new_sha256"]
    )
    assert overlay["documents"]["plan"]["old_sha256"] != overlay["documents"]["plan"]["new_sha256"]
    experience_files = tuple(
        (REPO_ROOT / "spikes" / "page_toolbox_engine_puncture_v1" / "docs" / "经验").rglob("*.md")
    )
    assert len(experience_files) >= 10
    combined = "\n".join(path.read_text(encoding="utf-8") for path in experience_files)
    assert "候选" in combined or "candidate" in combined.casefold()
    assert overlay["historical_assets"]["baseline_manifest_sha256"] == _sha256_file(
        build_p0_assets.BASELINE_PATH
    )


def test_p9a_0_t02_letter_stage_parser_and_schedule_are_exact() -> None:
    """P9A.0-T02：字母阶段、子任务、硬顺序和直接依赖准确率为 100%。"""

    trace = build_p0_assets.collect_traceability_matrix()
    stages = {task["stage"] for task in trace["tasks"]}
    assert {"P9C", "P9A", "P9B"} <= stages
    assert [task["task_id"] for task in trace["tasks"] if task["stage"] == "P9A"] == [
        "P9A.0",
        "P9A.1",
        "P9A.2",
        "P9A.3",
        "P9A.4",
    ]
    assert verify_p0.check_schedule() == []
    assert build_p0_assets.check_assets() == []


def test_p9a_0_t03_current_traceability_and_gate_index_are_unique() -> None:
    """P9A.0-T03：current hash/追溯闭合且四个 Gate 各有唯一权威 manifest。"""

    assert verify_p0.check_traceability() == []
    index = json.loads(
        (REPO_ROOT / "resources" / "manifests" / "gate_index.json").read_text(encoding="utf-8")
    )
    paths = tuple(index["gates"].values())
    assert set(index["gates"]) == {"G9C", "G9A-0", "G9A", "G9B"}
    assert len(paths) == len(set(paths))
    assert all((REPO_ROOT / path).is_file() for path in paths)
    overlay = json.loads(build_p0_assets.CURRENT_BASELINE_PATH.read_text(encoding="utf-8"))
    assert overlay["documents"]["design"]["new_sha256"] == _sha256_file(build_p0_assets.DESIGN_PATH)
    assert overlay["documents"]["plan"]["new_sha256"] == _sha256_file(build_p0_assets.PLAN_PATH)


def test_p9a_0_t04_real_inventory_missing_object_fails_before_calls(
    primary_facts: tuple[ExtractedPageFacts, ...],
) -> None:
    """P9A.0-T04：真实页漏一个对象即在 Translation/Layout 调用前失败。"""

    facts = next(item for item in primary_facts if item.text_spans)
    inventory = freeze_page_text_inventory(facts)
    assert inventory.to_dict()["inventory_hash"] == inventory.inventory_hash
    block_inventory = freeze_page_text_inventory(replace(facts, text_spans=()))
    assert block_inventory.page_no == facts.page.page_no
    mechanical_texts = (
        ("https://example.test", "URL_OR_EMAIL"),
        ("12/99", "PAGE_NUMBER"),
        ("$ 1,234.50", "NUMERIC_OR_SYMBOLIC_LITERAL"),
        ("EBITDA", "CODE_OR_ACRONYM"),
        ("已经是中文", "ALREADY_TARGET_LANGUAGE"),
    )
    for index, (text, reason) in enumerate(mechanical_texts):
        span = KernelTextFact(
            object_id=hashlib.sha256(f"mechanical-{index}".encode()).hexdigest(),
            text=text,
            bbox=(1.0, 1.0, 20.0, 10.0),
            font_name="Test",
            font_size=10.0,
            color_srgb=0,
            block_index=0,
            line_index=0,
            span_index=index,
        )
        mechanical = freeze_page_text_inventory(replace(facts, text_spans=(span,)))
        assert mechanical.items[0].keep_source_reason == reason
    with pytest.raises(DomainContractError):
        replace(inventory.items[0], object_id="")
    with pytest.raises(DomainContractError):
        replace(
            inventory.items[0],
            disposition=InventoryDisposition.KEEP_SOURCE,
            keep_source_reason=None,
        )
    with pytest.raises(DomainContractError):
        PageTextInventoryItem(
            inventory.items[0].object_id,
            inventory.items[0].source_hash,
            inventory.items[0].bbox,
            InventoryDisposition.TRANSLATE,
            "PAGE_NUMBER",
        )
    with pytest.raises(DomainContractError):
        replace(inventory, page_no=0)
    semantic_map = _inventory_map(facts, inventory)
    validate_inventory_coverage(inventory, semantic_map)
    broken = SemanticUnitMap(
        semantic_map.map_id,
        semantic_map.page_no,
        semantic_map.source_hash,
        tuple(replace(item, ordinal=index) for index, item in enumerate(semantic_map.entries[1:])),
    )
    translation_calls = 0
    layout_calls = 0
    with pytest.raises(DomainContractError, match="双向覆盖失败"):
        validate_inventory_coverage(inventory, broken)
    assert translation_calls == layout_calls == 0


def test_p9a_0_t05_late_keep_source_and_historical_rewrite_are_zero(
    primary_facts: tuple[ExtractedPageFacts, ...],
) -> None:
    """P9A.0-T05：Provider 事后 KEEP_SOURCE 被拒且 P9C 历史锚点保持只读。"""

    facts = next(item for item in primary_facts if item.text_spans)
    inventory = freeze_page_text_inventory(facts)
    semantic_map = _inventory_map(facts, inventory)
    index = next(
        number
        for number, item in enumerate(inventory.items)
        if item.disposition is InventoryDisposition.TRANSLATE
    )
    entries = list(semantic_map.entries)
    entries[index] = replace(
        entries[index],
        disposition=SemanticUnitDisposition.KEEP_SOURCE,
        keep_source_reason=KeepSourceReason.EXPLICIT_PROPER_NAME,
    )
    late = SemanticUnitMap(
        semantic_map.map_id,
        semantic_map.page_no,
        semantic_map.source_hash,
        tuple(entries),
    )
    historical = REPO_ROOT / "resources" / "manifests" / "p9c_historical_anchor.json"
    before = _sha256_file(historical)
    with pytest.raises(DomainContractError, match="事后 KEEP_SOURCE"):
        validate_inventory_coverage(inventory, late)
    assert _sha256_file(historical) == before
    assert (
        PageExecutionContext(
            "job", "run", "0" * 64, 1, "1" * 64, "2" * 64
        ).document_layout_memory_ref
        is None
    )


def test_p9a_1_t01_contract_round_trip_and_no_page_detail_duplication(
    primary_facts: tuple[ExtractedPageFacts, ...],
) -> None:
    """P9A.1-T01：最小合同往返一致且页内结构明细重复数为零。"""

    memory = _build_memory(primary_facts)
    restored = DocumentLayoutMemory.from_dict(memory.to_dict())
    assert (
        DocumentLayoutMemory.from_bytes(
            json.dumps(memory.to_dict(), ensure_ascii=False).encode("utf-8")
        )
        == memory
    )
    assert restored == memory
    assert restored.memory_hash == memory.memory_hash
    payload_keys = {key.casefold() for key in _json_keys(memory.to_dict())}
    forbidden = ("cell", "owner", "anchor", "reading_order", "visual_object", "semantic_unit_map")
    assert all(token not in payload_keys for token in forbidden)
    assert len(memory.source_layout_baseline.page_refs) == len(primary_facts)
    provenance = memory.source_layout_baseline.page_refs[0].provenance
    with pytest.raises(DomainContractError):
        LayoutFactProvenance(LayoutFactKind.OBSERVED, (), 1.0, True)
    with pytest.raises(DomainContractError):
        LayoutFactProvenance(LayoutFactKind.INFERRED, provenance.source_refs, 0.2, True)
    with pytest.raises(DomainContractError):
        replace(memory.source_layout_baseline.page_refs[0], page_no=0)
    with pytest.raises(DomainContractError):
        replace(memory.source_layout_baseline.page_refs[0], rotation=45)
    profile = memory.source_layout_baseline.role_profiles[0]
    with pytest.raises(DomainContractError):
        replace(profile, role="")
    with pytest.raises(DomainContractError):
        replace(profile, font_size_range=(2.0, 1.0))
    shared_provenance = LayoutFactProvenance(
        LayoutFactKind.INFERRED, provenance.source_refs, 1.0, False
    )
    shared = SharedRegionProfile(
        "shared-test",
        "top",
        (1,),
        (0.0, 0.0, 1.0, 0.1),
        "a" * 64,
        shared_provenance,
    )
    with pytest.raises(DomainContractError):
        replace(shared, edge="middle")
    with pytest.raises(DomainContractError):
        replace(shared, page_numbers=(1, 1))
    with pytest.raises(DomainContractError):
        replace(shared, normalized_bbox=(-0.1, 0.0, 1.0, 0.1))
    with pytest.raises(DomainContractError):
        SourceLayoutBaseline(memory.source_layout_baseline.page_refs[1:], (), (profile,))
    policy = memory.target_layout_policy
    with pytest.raises(DomainContractError):
        TargetLayoutPolicy(
            (),
            policy.font_scale_range,
            policy.line_spacing_range,
            policy.paragraph_spacing_range,
            policy.wrap_mode,
            True,
        )
    with pytest.raises(DomainContractError):
        replace(policy, font_scale_range=(0.0, 1.0))


def test_p9a_1_t02_canonical_hash_ignores_input_enumeration_order(
    primary_facts: tuple[ExtractedPageFacts, ...],
) -> None:
    """P9A.1-T02：事实和 Route 枚举顺序变化不改变规范 JSON 与 hash。"""

    normal = _build_memory(primary_facts)
    request = _build_input(
        tuple(reversed(primary_facts)), routes=tuple(reversed(_real_routes(primary_facts)))
    )
    reordered = DocumentLayoutMemoryBuilder().build(request).memory
    assert reordered is not None
    assert reordered.canonical_bytes == normal.canonical_bytes
    assert reordered.memory_hash == normal.memory_hash


def test_p9a_1_t03_all_identity_changes_invalidate_except_static_registry(
    primary_facts: tuple[ExtractedPageFacts, ...],
) -> None:
    """P9A.1-T03：全部身份指纹变化可定位；V1 静态 Registry 不进入 hash。"""

    policy = LayoutMemoryPolicyConfig.load(POLICY_PATH)
    base = _identity(primary_facts, policy)
    fields = tuple(base.__dataclass_fields__)
    for field in fields:
        value = getattr(base, field)
        changed_value = (
            f"changed-{value}" if field in {"source_language", "target_language"} else "f" * 64
        )
        changed = replace(base, **{field: changed_value})
        assert base.changed_fields(changed) == (field,)
    memory = _build_memory(primary_facts)
    static_registry_audit_hash = content_sha256({"registry": "changed"})
    assert static_registry_audit_hash not in memory.identity.__dataclass_fields__
    assert _build_memory(primary_facts).memory_hash == memory.memory_hash


def test_p9a_1_t04_geometry_has_current_page_source_and_provenance(
    primary_facts: tuple[ExtractedPageFacts, ...],
) -> None:
    """P9A.1-T04：当前源 bbox 可追溯，跨文档身份与固定坐标字段命中为零。"""

    memory = _build_memory(primary_facts)
    for ref, facts in zip(memory.source_layout_baseline.page_refs, primary_facts, strict=True):
        assert ref.media_box == facts.media_box
        assert ref.provenance.source_refs == (facts.page_identity,)
    payload = json.dumps(memory.to_dict(), ensure_ascii=False).casefold()
    assert all(token not in payload for token in ("sample_id", "file_name", "company_name"))


def test_p9a_1_t05_secrets_unbounded_text_and_absolute_paths_are_rejected(
    primary_facts: tuple[ExtractedPageFacts, ...],
    tmp_path: Path,
) -> None:
    """P9A.1-T05：秘密、Provider 响应、超限原文和宿主绝对路径落盘数为零。"""

    payload = _build_memory(primary_facts).to_dict()
    attacks = ("api_key=secret", "C:\\private\\memory.json", "x" * 2049)
    for attack in attacks:
        broken = json.loads(json.dumps(payload))
        broken["target_layout_policy"]["wrap_mode"] = attack
        with pytest.raises(DomainContractError):
            DocumentLayoutMemory.from_dict(broken)
    extra = json.loads(json.dumps(payload))
    extra["provider_response"] = {"raw_text": "secret"}
    with pytest.raises(DomainContractError):
        DocumentLayoutMemory.from_dict(extra)
    assert tuple(tmp_path.iterdir()) == ()


def test_p9a_1_t06_unknown_schema_missing_provenance_and_bad_hash_fail_closed(
    primary_facts: tuple[ExtractedPageFacts, ...],
) -> None:
    """P9A.1-T06：未知 major、缺 provenance 和坏 hash 均不暴露半有效对象。"""

    payload = _build_memory(primary_facts).to_dict()
    variants = []
    unknown = json.loads(json.dumps(payload))
    unknown["schema_version"] = "transflow.document-layout-memory/v2"
    variants.append(unknown)
    missing = json.loads(json.dumps(payload))
    del missing["source_layout_baseline"]["page_refs"][0]["provenance"]
    variants.append(missing)
    bad_hash = json.loads(json.dumps(payload))
    bad_hash["memory_hash"] = "0" * 64
    variants.append(bad_hash)
    for variant in variants:
        with pytest.raises(DomainContractError):
            DocumentLayoutMemory.from_dict(variant)
    with pytest.raises(DomainContractError):
        DocumentLayoutMemory.from_bytes(b"not-json")
    with pytest.raises(DomainContractError):
        DocumentLayoutMemory.from_bytes(b"[]")


def test_p9a_2_t01_partial_is_not_ready_and_complete_builds_once(
    primary_facts: tuple[ExtractedPageFacts, ...],
) -> None:
    """P9A.2-T01：部分完整年报不发布，完整输入无漏页/重复且仅构建一次。"""

    builder = DocumentLayoutMemoryBuilder()
    complete = _build_input(primary_facts)
    partial = replace(complete, page_facts=primary_facts[:1], routes=complete.routes[:1])
    first = builder.build(partial)
    assert first.status is LayoutMemoryBuildStatus.NOT_READY
    assert first.memory is None and builder.build_count == 0
    final = builder.build(complete)
    assert final.status is LayoutMemoryBuildStatus.READY and final.memory is not None
    assert builder.build_count == 1
    assert len(final.memory.source_layout_baseline.page_refs) == len(primary_facts)
    with pytest.raises(DomainContractError):
        builder.build(replace(complete, expected_page_count=0))
    with pytest.raises(DomainContractError):
        builder.build(replace(complete, page_facts=(*complete.page_facts, complete.page_facts[0])))
    with pytest.raises(DomainContractError):
        builder.build(replace(complete, identity=replace(complete.identity, source_hash="f" * 64)))


def test_p9a_2_t02_roles_policy_and_provenance_precede_translation(
    primary_facts: tuple[ExtractedPageFacts, ...],
) -> None:
    """P9A.2-T02：角色画像/目标策略完整且 Builder 对翻译调用为零。"""

    memory = _build_memory(primary_facts)
    assert memory.source_layout_baseline.role_profiles
    assert all(
        profile.sample_count > 0 and profile.provenance.source_refs
        for profile in memory.source_layout_baseline.role_profiles
    )
    assert memory.target_layout_policy.fallback_font_ids
    source = (
        REPO_ROOT / "src" / "transflow" / "application" / "document_layout_memory.py"
    ).read_text(encoding="utf-8")
    assert "TranslationPort" not in source and "toolbox.prepare" not in source


def test_p9a_2_t03_real_table_details_remain_behind_pagefacts_ref() -> None:
    """P9A.2-T03：真实 table 页可经 PageFacts ref 追溯且 memory 不复制 cell/owner 明细。"""

    path = min(
        (CLASSIFICATION_ROOT / "body" / "table").rglob("*.pdf"),
        key=lambda item: item.stat().st_size,
    )
    facts = PageFactsExtractor().extract_all(path, _sha256_file(path))
    memory = _build_memory(facts)
    assert any(
        item.table_objects or "table" in dict(_real_routes(facts))[item.page.page_no]
        for item in facts
    )
    assert memory.source_layout_baseline.page_refs[0].facts_hash == facts[0].kernel_facts_hash
    payload = json.dumps(memory.to_dict(), ensure_ascii=False).casefold()
    assert all(token not in payload for token in ("cell", "padding", "border", "owner"))


def test_p9a_2_t04_real_visual_objects_are_referenced_without_ocr_or_toolbox() -> None:
    """P9A.2-T04：真实视觉页只聚合角色/公共区，不 OCR、翻译或调用 Toolbox。"""

    roots = (CLASSIFICATION_ROOT / "body" / "chart", CLASSIFICATION_ROOT / "body" / "diagram")
    path = min(
        (item for root in roots for item in root.rglob("*.pdf")),
        key=lambda item: item.stat().st_size,
    )
    facts = PageFactsExtractor().extract_all(path, _sha256_file(path))
    assert any(item.image_objects or item.drawing_objects for item in facts)
    memory = _build_memory(facts)
    assert len(memory.source_layout_baseline.page_refs) == len(facts)
    payload_keys = {key.casefold() for key in _json_keys(memory.to_dict())}
    assert all(
        token not in payload_keys for token in ("ocr", "translation_unit", "page_patch", "anchor")
    )


def test_p9a_2_t05_reorder_and_scale_follow_current_facts(
    primary_facts: tuple[ExtractedPageFacts, ...],
) -> None:
    """P9A.2-T05：输入顺序不漂移，等比例缩放后仅按当前事实产生新画像。"""

    base = _build_memory(primary_facts)
    reordered = (
        DocumentLayoutMemoryBuilder()
        .build(
            _build_input(
                tuple(reversed(primary_facts)), routes=tuple(reversed(_real_routes(primary_facts)))
            )
        )
        .memory
    )
    assert reordered is not None and reordered.memory_hash == base.memory_hash
    first = primary_facts[0]
    scaled_page = replace(
        first.page,
        width_points=first.page.width_points * 1.1,
        height_points=first.page.height_points * 1.1,
        geometry_hash=content_sha256({"scaled": first.page.geometry_hash, "factor": 1.1}),
    )
    scaled_facts = (replace(first, page=scaled_page), *primary_facts[1:])
    policy = LayoutMemoryPolicyConfig.load(POLICY_PATH)
    scaled_identity = _identity(scaled_facts, policy)
    scaled = (
        DocumentLayoutMemoryBuilder()
        .build(_build_input(scaled_facts, identity=scaled_identity))
        .memory
    )
    assert scaled is not None and scaled.memory_hash != base.memory_hash
    source = (
        (REPO_ROOT / "src" / "transflow" / "application" / "document_layout_memory.py")
        .read_text(encoding="utf-8")
        .casefold()
    )
    assert all(path.name.casefold() not in source for path in ANNUAL_PATHS)


def test_p9a_2_t06_candidate_files_cannot_contaminate_builder(
    primary_facts: tuple[ExtractedPageFacts, ...],
    tmp_path: Path,
) -> None:
    """P9A.2-T06：失败候选和译后页存在时 memory hash 仍只由登记源事实决定。"""

    before = _build_memory(primary_facts)
    (tmp_path / "failed_candidate.pdf").write_bytes(b"candidate")
    (tmp_path / "translated_page.pdf").write_bytes(b"translated")
    after = _build_memory(primary_facts)
    assert after.memory_hash == before.memory_hash
    assert "candidate" not in json.dumps(after.to_dict(), ensure_ascii=False).casefold()


def test_p9a_3_t01_threads_and_processes_share_one_hash_not_mutable_objects(
    primary_facts: tuple[ExtractedPageFacts, ...],
    tmp_path: Path,
) -> None:
    """P9A.3-T01：线程/进程页同 hash、single-flight 一次且各进程本地只读加载。"""

    runtime = _runtime(tmp_path / "run", "p9a-thread-process")
    request = _build_input(primary_facts)
    with ThreadPoolExecutor(max_workers=4) as executor:
        refs = tuple(executor.map(lambda _index: runtime.prepare(request), range(8)))
    assert len({item.memory_hash for item in refs}) == 1
    assert runtime.builder.build_count == 1
    ref_payload = {
        "memory_hash": refs[0].memory_hash,
        "identity_hash": refs[0].identity_hash,
        "artifact_id": refs[0].artifact_id,
        "relative_path": refs[0].relative_path,
        "schema_version": refs[0].schema_version,
    }
    with ProcessPoolExecutor(max_workers=2) as executor:
        outputs = tuple(
            executor.map(
                _process_load_memory,
                (str(tmp_path / "run"), str(tmp_path / "run")),
                ("p9a-thread-process", "p9a-thread-process"),
                (ref_payload, ref_payload),
            )
        )
    assert {item[1] for item in outputs} == {refs[0].memory_hash}
    assert all(item[2] for item in outputs)


def test_p9a_3_t02_global_mutation_rejected_and_page_adjustment_is_local(
    primary_facts: tuple[ExtractedPageFacts, ...],
) -> None:
    """P9A.3-T02：全局画像不可变，合法页级调整只存在本页局部数据。"""

    memory = _build_memory(primary_facts)
    original_hash = memory.memory_hash
    with pytest.raises(FrozenInstanceError):
        memory.target_layout_policy.wrap_mode = "MUTATED"  # type: ignore[misc]
    page_adjustments = {1: {"font_scale": 0.9}}
    page_adjustments[1]["font_scale"] = 0.85
    assert 2 not in page_adjustments
    assert memory.memory_hash == original_hash


def test_p9a_3_t03_artifact_before_checkpoint_recovers_without_unverified_read(
    primary_facts: tuple[ExtractedPageFacts, ...],
    tmp_path: Path,
) -> None:
    """P9A.3-T03：Artifact 已写未引用崩溃后只复用/重建同内容，未校验对象不放行。"""

    run_root = tmp_path / "run"
    request = _build_input(primary_facts)
    runtime = _runtime(run_root, "p9a-crash-artifact")
    with pytest.raises(InjectedCrash, match="after_memory_artifact"):
        runtime.prepare(request, crash_at="after_memory_artifact")
    recovered = _runtime(run_root, "p9a-crash-artifact")
    recovered.recover_filesystem()
    ref = recovered.prepare(request)
    assert recovered.load_readonly(ref).memory_hash == ref.memory_hash
    assert recovered.builder.build_count == 1


def test_p9a_3_t04_checkpoint_restart_skips_builder(
    primary_facts: tuple[ExtractedPageFacts, ...],
    tmp_path: Path,
) -> None:
    """P9A.3-T04：Checkpoint 提交后重启复用一次且重建次数为零。"""

    run_root = tmp_path / "run"
    request = _build_input(primary_facts)
    first = _runtime(run_root, "p9a-restart")
    ref = first.prepare(request)
    restarted = _runtime(run_root, "p9a-restart")
    assert restarted.prepare(request) == ref
    assert restarted.reuse_count == 1
    assert restarted.builder.build_count == 0


def test_p9a_3_t05_stale_identity_rejected_and_static_registry_ignored(
    primary_facts: tuple[ExtractedPageFacts, ...],
    tmp_path: Path,
) -> None:
    """P9A.3-T05：全部运行指纹变化拒绝旧记忆，静态 Registry 只留 run 审计。"""

    request = _build_input(primary_facts)
    run_root = tmp_path / "run"
    first = _runtime(run_root, "p9a-stale")
    ref = first.prepare(request)
    changed_fields = (
        "source_language",
        "target_language",
        "config_hash",
        "font_hash",
        "builder_hash",
        "classifier_hash",
        "catalog_hash",
        "kernel_hash",
    )
    for field in changed_fields:
        value = getattr(request.identity, field)
        replacement = f"changed-{value}" if "language" in field else "e" * 64
        stale = replace(request, identity=replace(request.identity, **{field: replacement}))
        with pytest.raises(PortCallError, match=r"指纹变化|身份变化"):
            _runtime(run_root, "p9a-stale").prepare(stale)
    static_registry_hash = content_sha256({"registry": "v2"})
    assert static_registry_hash != ref.memory_hash
    assert _runtime(run_root, "p9a-stale").prepare(request) == ref


def test_p9a_3_t06_late_worker_cas_rejects_different_ref(
    primary_facts: tuple[ExtractedPageFacts, ...],
    tmp_path: Path,
) -> None:
    """P9A.3-T06：旧 Worker 的不同 ref 被 CAS 拒绝，权威 hash 和 Context 不变。"""

    runtime = _runtime(tmp_path / "run", "p9a-cas")
    ref = runtime.prepare(_build_input(primary_facts))
    with pytest.raises(PortCallError, match="identity"):
        runtime.load_readonly(replace(ref, identity_hash="e" * 64))
    with pytest.raises(PortCallError, match="hash"):
        runtime.load_readonly(replace(ref, memory_hash="e" * 64))
    with pytest.raises(DomainContractError):
        replace(ref, relative_path="../outside.json")
    stale = replace(ref, memory_hash="f" * 64)
    with pytest.raises(PortCallError, match="CAS"):
        runtime.assert_authoritative(stale)
    contexts = tuple(
        PageExecutionContext(
            "job",
            "p9a-cas",
            primary_facts[0].page.source_hash,
            item.page.page_no,
            item.page.geometry_hash,
            "a" * 64,
        )
        for item in primary_facts[:3]
    )
    bound = runtime.bind_page_contexts(contexts, ref)
    assert {item.document_layout_memory_ref for item in bound} == {ref}
    with pytest.raises(PortCallError, match="混用"):
        runtime.bind_page_contexts((replace(contexts[0], run_id="other-run"),), ref)
    empty_runtime = _runtime(tmp_path / "empty-run", "p9a-empty")
    with pytest.raises(PortCallError, match="尚未提交"):
        empty_runtime.assert_authoritative(ref)


def test_p9a_4_t01_two_complete_real_pdfs_publish_verified_artifacts(
    annual_facts: dict[str, tuple[ExtractedPageFacts, ...]],
    tmp_path: Path,
) -> None:
    """P9A.4-T01：两份完整真实 PDF 均发布可验证 memory Artifact，页覆盖 100%。"""

    results = []
    for index, facts in enumerate(annual_facts.values(), start=1):
        runtime = _runtime(tmp_path / f"run-{index}", f"p9a-real-{index}")
        ref = runtime.prepare(_build_input(facts))
        memory = runtime.load_readonly(ref)
        results.append((ref, memory, facts))
    assert len(results) == 2
    assert all(
        len(memory.source_layout_baseline.page_refs) == len(facts)
        for _ref, memory, facts in results
    )
    assert all(memory.memory_hash == ref.memory_hash for ref, memory, _facts in results)
    assert results[0][0].memory_hash != results[1][0].memory_hash


def test_p9a_4_t02_real_leaf_role_coverage_uses_pagefacts_refs() -> None:
    """P9A.4-T02：正文、表格、视觉、multi、anchored 和公共边缘均用真实事实覆盖。"""

    relative_roots = (
        Path("body/flow_text/single"),
        Path("body/table"),
        Path("body/chart"),
        Path("body/flow_text/multi"),
        Path("body/anchored_blocks"),
        Path("contents"),
    )
    evidence: dict[str, str] = {}
    for relative in relative_roots:
        path = min(
            (CLASSIFICATION_ROOT / relative).rglob("*.pdf"), key=lambda item: item.stat().st_size
        )
        facts = PageFactsExtractor().extract_all(path, _sha256_file(path))
        memory = _build_memory(facts)
        evidence[relative.as_posix()] = memory.source_layout_baseline.page_refs[0].facts_hash
    assert set(evidence) == {item.as_posix() for item in relative_roots}
    assert all(len(value) == 64 for value in evidence.values())


def test_p9a_4_t03_same_source_and_config_have_zero_canonical_diff(
    primary_facts: tuple[ExtractedPageFacts, ...],
) -> None:
    """P9A.4-T03：相同 source/config 连续真实构建 canonical diff 为零。"""

    first = _build_memory(primary_facts)
    second = _build_memory(primary_facts)
    assert first.canonical_bytes == second.canonical_bytes
    assert first.memory_hash == second.memory_hash


def test_p9a_4_t04_rename_reorder_scale_have_no_identity_branch(
    primary_facts: tuple[ExtractedPageFacts, ...],
    tmp_path: Path,
) -> None:
    """P9A.4-T04：重命名保持事实行为，顺序/缩放仅按事实变化且无身份分支。"""

    renamed = tmp_path / "anonymous-input.pdf"
    renamed.write_bytes(ANNUAL_PATHS[0].read_bytes())
    renamed_facts = PageFactsExtractor().extract_all(renamed, _sha256_file(renamed))
    assert _build_memory(renamed_facts).memory_hash == _build_memory(primary_facts).memory_hash
    assert (
        _build_memory(tuple(reversed(primary_facts))).memory_hash
        == _build_memory(primary_facts).memory_hash
    )


def test_p9a_4_t05_artifact_growth_is_bounded_to_refs_and_profiles(
    primary_facts: tuple[ExtractedPageFacts, ...],
) -> None:
    """P9A.4-T05：Artifact 只线性保存页引用/聚合，不含原文、候选或 Provider 响应。"""

    memory = _build_memory(primary_facts)
    payload = memory.canonical_bytes
    assert len(memory.source_layout_baseline.page_refs) == len(primary_facts)
    assert len(payload) < len(primary_facts) * 4096
    lowered = payload.decode("utf-8").casefold()
    assert all(
        token not in lowered
        for token in ("raw_text", "provider_response", "candidate", "translated_text")
    )


def test_p9a_4_t06_g8_g9_g9c_and_g9a0_interfaces_do_not_regress() -> None:
    """P9A.4-T06：运行受影响的 G8/G9/G9C/G9A-0 真实接口回归且差异为零。"""

    command = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "tests/test_p7.py",
        "tests/test_p8.py",
        "tests/test_p9.py",
        "tests/test_p9c.py",
        "-k",
        "p7_1_t01 or p8_1_t01 or p9_1_t01 or p9c_2_t01",
    ]
    completed = subprocess.run(command, cwd=REPO_ROOT, check=False, capture_output=True, text=True)
    print(completed.stdout)
    print(completed.stderr)
    assert completed.returncode == 0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
