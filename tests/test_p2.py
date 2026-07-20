"""按 P2.1 至 P2.5 计划编号执行领域合同与架构边界验收。"""

from __future__ import annotations

import hashlib
import inspect
import json
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest

from scripts.build_p2_assets import build_outputs
from scripts.verify_architecture import scan_production_tree
from scripts.verify_p2 import catalog_violations, verify_ports
from transflow.domain import (
    ArtifactIntegrity,
    ArtifactPayload,
    ArtifactProduced,
    ArtifactReference,
    Capability,
    CheckpointCompatibility,
    CheckpointRecord,
    ClassificationRoute,
    ControlSignal,
    DocumentOutcome,
    DocumentResult,
    DocumentRunRequest,
    DomainContractError,
    ErrorCode,
    Fallback,
    JobControlState,
    JobSnapshot,
    ModelDecision,
    ModelDecisionRequest,
    PageExecutionContext,
    PageFacts,
    PageOutcome,
    PagePatch,
    PagePipelineState,
    PagePlan,
    PatchOperation,
    PortCallError,
    Quality,
    RepairBudget,
    TranslatedUnit,
    TranslationBatch,
    TranslationBundle,
    TranslationCoverage,
    TranslationUnit,
    build_runtime_fingerprints,
)
from transflow.domain.common import json_ready
from transflow.domain.states import (
    JOB_TRANSITIONS,
    PAGE_TRANSITIONS,
    advance_checkpoint,
    ensure_checkpoint_compatible,
    ensure_document_finalizable,
    transition_job,
    transition_page,
)
from transflow.ports import (
    ArtifactPort,
    CheckpointPort,
    JobQueuePort,
    ModelDecisionPort,
    TranslationPort,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
MUTATION_ROOT = REPO_ROOT / "tmp" / "p2_architecture_mutations"
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64


def make_request() -> DocumentRunRequest:
    """构造一份真实通过领域校验的完整 PDF 请求。"""

    return DocumentRunRequest(
        source_pdf_path="samples/report.pdf",
        source_hash=HASH_A,
        source_language="en",
        target_language="zh-CN",
        config_snapshot_hash=HASH_B,
        job_id="job-1",
        run_id="run-1",
    )


def make_batch() -> TranslationBatch:
    """构造包含两个真实文本单元的有序翻译批次。"""

    return TranslationBatch(
        batch_id="batch-1",
        source_language="en",
        target_language="zh-CN",
        units=(
            TranslationUnit("unit-1", 0, 0, "Revenue", "region-1"),
            TranslationUnit("unit-2", 0, 1, "Profit", "region-2"),
        ),
    )


def make_context() -> PageExecutionContext:
    """构造 PagePatch 绑定测试使用的页面执行上下文。"""

    return PageExecutionContext("job-1", "run-1", HASH_A, 0, HASH_B, HASH_C)


def make_patch() -> PagePatch:
    """构造与页面上下文完全匹配的声明式补丁。"""

    operation = PatchOperation("operation-1", "region-1", "REPLACE_TEXT", HASH_C)
    return PagePatch("patch-1", HASH_A, 0, HASH_B, "body.table", (operation,))


@contextmanager
def mutation_tree(name: str, files: dict[str, str]) -> Iterator[Path]:
    """在仓库 tmp 子目录创建并可靠清理一个架构突变源码树。"""

    root = (MUTATION_ROOT / name).resolve()
    root.relative_to(MUTATION_ROOT.resolve())
    if root.exists():
        shutil.rmtree(root)
    for relative_path, content in files.items():
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    try:
        yield root
    finally:
        if root.exists():
            shutil.rmtree(root)


class MemoryJobQueue:
    """提供 JobQueuePort 成功与稳定失败语义的内存实现。"""

    def __init__(self, snapshot: JobSnapshot) -> None:
        """保存待取得任务和结果集合。"""

        self.snapshot: JobSnapshot | None = snapshot
        self.results: dict[str, DocumentResult] = {}

    def acquire(self) -> JobSnapshot | None:
        """取得一次任务，再次调用返回无任务。"""

        snapshot, self.snapshot = self.snapshot, None
        return snapshot

    def read_control(self, job_id: str) -> ControlSignal:
        """返回已知任务控制信号，并拒绝未知身份。"""

        if job_id != "job-1":
            raise PortCallError(ErrorCode.PORT_UNAVAILABLE, False, "Job 不存在")
        return ControlSignal(job_id, JobControlState.RUNNING, 0)

    def publish_result(self, result: DocumentResult) -> None:
        """按 run_id 幂等保存相同结果并拒绝冲突结果。"""

        existing = self.results.get(result.run_id)
        if existing is not None and existing != result:
            raise PortCallError(ErrorCode.PORT_CONTRACT_VIOLATION, False, "结果冲突")
        self.results[result.run_id] = result


class MemoryCheckpointStore:
    """提供 CheckpointPort 乐观版本语义的内存实现。"""

    def __init__(self) -> None:
        """初始化空快照集合。"""

        self.records: dict[str, CheckpointRecord] = {}

    def load(self, run_id: str) -> CheckpointRecord | None:
        """读取指定 Run 最新快照。"""

        return self.records.get(run_id)

    def save(self, record: CheckpointRecord, expected_version: int) -> CheckpointRecord:
        """校验期望版本后保存，完全相同的重复提交保持幂等。"""

        existing = self.records.get(record.run_id)
        if existing == record:
            return record
        actual_version = existing.version if existing else 0
        if expected_version != actual_version or record.version <= actual_version:
            raise PortCallError(ErrorCode.PORT_CONTRACT_VIOLATION, True, "Checkpoint 版本冲突")
        self.records[record.run_id] = record
        return record


class MemoryArtifactStore:
    """提供 ArtifactPort 哈希校验与不可变写入语义的内存实现。"""

    def __init__(self) -> None:
        """初始化空产物集合。"""

        self.payloads: dict[str, ArtifactPayload] = {}

    def put(self, payload: ArtifactPayload) -> ArtifactReference:
        """验证真实内容哈希后幂等写入产物。"""

        actual_hash = hashlib.sha256(payload.content).hexdigest()
        if actual_hash != payload.content_hash:
            raise PortCallError(ErrorCode.PORT_CONTRACT_VIOLATION, False, "Artifact 哈希不匹配")
        existing = self.payloads.get(payload.artifact_id)
        if existing is not None and existing != payload:
            raise PortCallError(ErrorCode.PORT_CONTRACT_VIOLATION, False, "Artifact 身份冲突")
        self.payloads[payload.artifact_id] = payload
        return ArtifactReference(
            payload.artifact_id,
            payload.media_type,
            payload.content_hash,
            len(payload.content),
        )

    def get(self, artifact_id: str) -> bytes:
        """读取真实写入内容，并以稳定错误拒绝未知产物。"""

        try:
            return self.payloads[artifact_id].content
        except KeyError as error:
            raise PortCallError(ErrorCode.PORT_UNAVAILABLE, False, "Artifact 不存在") from error


class FixedTranslationAdapter:
    """提供确定性真实字符串转换的 TranslationPort 实现。"""

    def translate(self, batch: TranslationBatch) -> TranslationBundle:
        """将每个源文本转换为带目标语言前缀的非空结果。"""

        if batch.target_language == "unsupported":
            raise PortCallError(ErrorCode.PORT_UNAVAILABLE, False, "目标语言不支持")
        units = tuple(
            TranslatedUnit(unit.unit_id, f"[{batch.target_language}]{unit.source_text}")
            for unit in batch.units
        )
        return TranslationBundle.from_batch(batch, units)


class JsonHttpStyleTranslationAdapter:
    """模拟 HTTP JSON 边界序列化但执行同一 TranslationPort 合同。"""

    def translate(self, batch: TranslationBatch) -> TranslationBundle:
        """经过真实 JSON 编解码后返回严格对齐的翻译结果。"""

        encoded = json.dumps(
            [
                {
                    "translated_text": f"[{batch.target_language}]{unit.source_text}",
                    "unit_id": unit.unit_id,
                }
                for unit in batch.units
            ],
            ensure_ascii=False,
        )
        decoded = json.loads(encoded)
        units = tuple(TranslatedUnit(**item) for item in decoded)
        return TranslationBundle.from_batch(batch, units)


class FixedModelDecisionAdapter:
    """提供结构化 ModelDecisionPort 成功与失败语义的内存实现。"""

    def decide(self, request: ModelDecisionRequest) -> ModelDecision:
        """对已知判定类型生成实际结构化结果并拒绝未知类型。"""

        if request.decision_kind != "classification":
            raise PortCallError(ErrorCode.PORT_UNAVAILABLE, False, "判定类型不支持")
        return ModelDecision(
            request.decision_id,
            request.decision_kind,
            "body.table",
            request.evidence_ids,
        )


@pytest.mark.contract
def test_p2_1_t01_valid_models_round_trip_and_keep_order() -> None:
    """P2.1-T01：有效核心模型往返后字段和有序身份不变。"""

    request = make_request()
    snapshot = JobSnapshot(request, JobControlState.QUEUED, 1, 0)
    facts = PageFacts(HASH_A, 0, 595.0, 842.0, HASH_B, HASH_C)
    context = make_context()
    plan = PagePlan("body.table", "body.table", "body.table", ("unit-1", "unit-2"), 2)
    route = ClassificationRoute("body.table", 0.95, ("evidence-1", "evidence-2"))
    batch = make_batch()
    bundle = TranslationBundle.from_batch(
        batch,
        (TranslatedUnit("unit-1", "收入"), TranslatedUnit("unit-2", "利润")),
    )
    patch = make_patch()
    outcome = PageOutcome(
        0,
        PagePipelineState.FINALIZED,
        ArtifactProduced.YES,
        ArtifactIntegrity.PASS,
        TranslationCoverage.FULL,
        Capability.SUPPORTED,
        Quality.PASS,
        Fallback.NONE,
        ("finding-1",),
    )
    artifact = ArtifactReference("artifact-1", "application/pdf", HASH_A, 42)
    cases = (
        (request, DocumentRunRequest.from_dict),
        (snapshot, JobSnapshot.from_dict),
        (facts, PageFacts.from_dict),
        (context, PageExecutionContext.from_dict),
        (plan, PagePlan.from_dict),
        (route, ClassificationRoute.from_dict),
        (batch, TranslationBatch.from_dict),
        (bundle, TranslationBundle.from_dict),
        (patch, PagePatch.from_dict),
        (outcome, PageOutcome.from_dict),
        (artifact, ArtifactReference.from_dict),
    )
    for model, restore in cases:
        restored = restore(json_ready(model))
        assert restored == model
    assert batch.ordered_unit_ids == ("unit-1", "unit-2")


@pytest.mark.contract
def test_p2_1_t02_only_one_pdf_path_is_accepted() -> None:
    """P2.1-T02：单 PDF 路径通过，列表、目录形态和非 PDF 路径拒绝。"""

    assert make_request().source_pdf_path.endswith(".pdf")
    for invalid_path in (["a.pdf", "b.pdf"], "samples/annual_reports", "samples/a.docx"):
        with pytest.raises(DomainContractError, match=r"单个完整 PDF|非空字符串"):
            replace(make_request(), source_pdf_path=invalid_path)  # type: ignore[arg-type]


@pytest.mark.contract
def test_p2_1_t03_translation_bundle_rejects_all_identity_drift() -> None:
    """P2.1-T03：缺失、重复、新增、改写和重排 unit_id 均拒绝。"""

    batch = make_batch()
    invalid_results = (
        (TranslatedUnit("unit-1", "收入"),),
        (TranslatedUnit("unit-1", "收入"), TranslatedUnit("unit-1", "利润")),
        (TranslatedUnit("unit-1", "收入"), TranslatedUnit("unit-3", "利润")),
        (TranslatedUnit("unit-1", "收入"), TranslatedUnit("rewritten-2", "利润")),
        (TranslatedUnit("unit-2", "利润"), TranslatedUnit("unit-1", "收入")),
    )
    for units in invalid_results:
        with pytest.raises(DomainContractError) as captured:
            TranslationBundle.from_batch(batch, units)
        assert captured.value.code is ErrorCode.INVALID_TRANSLATION_BUNDLE


@pytest.mark.contract
def test_p2_1_t04_page_patch_rejects_every_binding_mismatch() -> None:
    """P2.1-T04：源、页码、几何或 owner 任一不匹配均拒绝。"""

    context = make_context()
    patch = make_patch()
    patch.validate_binding(context, "body.table")
    mismatches = (
        (replace(patch, source_hash=HASH_C), context, "body.table"),
        (replace(patch, page_no=1), context, "body.table"),
        (replace(patch, geometry_hash=HASH_C), context, "body.table"),
        (patch, context, "body.chart"),
    )
    for candidate, expected_context, owner in mismatches:
        with pytest.raises(DomainContractError) as captured:
            candidate.validate_binding(expected_context, owner)
        assert captured.value.code is ErrorCode.PATCH_BINDING_MISMATCH


@pytest.mark.contract
def test_p2_2_t01_all_legal_state_edges_and_duplicates_succeed() -> None:
    """P2.2-T01：Job/Page 全部合法边和幂等重复命令通过。"""

    for current, targets in JOB_TRANSITIONS.items():
        assert transition_job(current, current) is current
        for target in targets:
            assert transition_job(current, target) is target
    for current, targets in PAGE_TRANSITIONS.items():
        assert transition_page(current, current) is current
        for target in targets:
            assert transition_page(current, target) is target


@pytest.mark.contract
def test_p2_2_t02_all_illegal_edges_and_terminal_rollbacks_fail() -> None:
    """P2.2-T02：Job/Page 全部非法边及终态回退均失败。"""

    for current, targets in JOB_TRANSITIONS.items():
        for target in JobControlState:
            if target != current and target not in targets:
                with pytest.raises(DomainContractError):
                    transition_job(current, target)
    for current, targets in PAGE_TRANSITIONS.items():
        for target in PagePipelineState:
            if target != current and target not in targets:
                with pytest.raises(DomainContractError):
                    transition_page(current, target)


@pytest.mark.contract
def test_p2_2_t03_document_requires_every_page_finalized() -> None:
    """P2.2-T03：空页集合或任意未终态页都无法绕过最终化屏障。"""

    ensure_document_finalizable((PagePipelineState.FINALIZED, PagePipelineState.FINALIZED))
    for states in ((), (PagePipelineState.FINALIZED, PagePipelineState.QUALITY_DECIDED)):
        with pytest.raises(DomainContractError) as captured:
            ensure_document_finalizable(states)
        assert captured.value.code is ErrorCode.DOCUMENT_NOT_FINALIZABLE


@pytest.mark.contract
def test_p2_2_t04_checkpoint_monotonic_compatibility_and_repair_budget() -> None:
    """P2.2-T04：Checkpoint 单调/兼容及 Repair 上限均不可绕过。"""

    assert advance_checkpoint(0, 1) == 1
    for proposed in (1, 0):
        with pytest.raises(DomainContractError):
            advance_checkpoint(1, proposed)
    compatibility = CheckpointCompatibility(HASH_A, HASH_A, HASH_A, HASH_A, HASH_A)
    ensure_checkpoint_compatible(compatibility, compatibility)
    with pytest.raises(DomainContractError) as captured:
        ensure_checkpoint_compatible(compatibility, replace(compatibility, font_hash=HASH_B))
    assert captured.value.code is ErrorCode.CHECKPOINT_INCOMPATIBLE
    budget = RepairBudget(1).consume()
    with pytest.raises(DomainContractError) as captured:
        budget.consume()
    assert captured.value.code is ErrorCode.REPAIR_BUDGET_EXHAUSTED


@pytest.mark.contract
def test_p2_3_t01_memory_fakes_cover_five_ports_success_and_error() -> None:
    """P2.3-T01：五个内存实现不依赖外部库并执行真实成功/失败合同。"""

    snapshot = JobSnapshot(make_request(), JobControlState.QUEUED, 1, 0)
    queue = MemoryJobQueue(snapshot)
    assert isinstance(queue, JobQueuePort)
    assert queue.acquire() == snapshot
    assert queue.acquire() is None
    assert queue.read_control("job-1").state is JobControlState.RUNNING
    with pytest.raises(PortCallError):
        queue.read_control("missing")
    result = DocumentResult("run-1", DocumentOutcome.COMPLETED, "artifact-1")
    queue.publish_result(result)
    queue.publish_result(result)
    with pytest.raises(PortCallError):
        queue.publish_result(replace(result, final_artifact_id="artifact-2"))

    checkpoint_store = MemoryCheckpointStore()
    assert isinstance(checkpoint_store, CheckpointPort)
    compatibility = CheckpointCompatibility(HASH_A, HASH_A, HASH_A, HASH_A, HASH_A)
    checkpoint = CheckpointRecord("run-1", 1, HASH_A, b"state", compatibility)
    assert checkpoint_store.load("run-1") is None
    assert checkpoint_store.save(checkpoint, 0) == checkpoint
    assert checkpoint_store.save(checkpoint, 0) == checkpoint
    with pytest.raises(PortCallError):
        checkpoint_store.save(replace(checkpoint, version=2), 0)

    artifact_store = MemoryArtifactStore()
    assert isinstance(artifact_store, ArtifactPort)
    content = b"real artifact bytes"
    payload = ArtifactPayload(
        "artifact-1",
        "application/pdf",
        content,
        hashlib.sha256(content).hexdigest(),
    )
    reference = artifact_store.put(payload)
    assert reference.size_bytes == len(content)
    assert artifact_store.get("artifact-1") == content
    with pytest.raises(PortCallError):
        artifact_store.get("missing")

    translation = FixedTranslationAdapter()
    assert isinstance(translation, TranslationPort)
    assert translation.translate(make_batch()).units[0].translated_text == "[zh-CN]Revenue"
    with pytest.raises(PortCallError):
        translation.translate(replace(make_batch(), target_language="unsupported"))

    model = FixedModelDecisionAdapter()
    assert isinstance(model, ModelDecisionPort)
    request = ModelDecisionRequest("decision-1", "classification", "v1", ("evidence-1",))
    assert model.decide(request).result_code == "body.table"
    with pytest.raises(PortCallError):
        model.decide(replace(request, decision_kind="unknown"))


@pytest.mark.contract
def test_p2_3_t02_port_signatures_have_zero_implementation_leaks() -> None:
    """P2.3-T02：五端口签名冻结且生产包架构扫描无实现泄漏。"""

    assert verify_ports() == []
    assert scan_production_tree(REPO_ROOT / "src" / "transflow") == []


@pytest.mark.contract
def test_p2_3_t03_translation_implementations_are_swappable() -> None:
    """P2.3-T03：Fixed 与 JSON/HTTP 风格实现替换后应用合同不变。"""

    batch = make_batch()
    implementations: tuple[TranslationPort, ...] = (
        FixedTranslationAdapter(),
        JsonHttpStyleTranslationAdapter(),
    )
    results = [implementation.translate(batch) for implementation in implementations]
    assert results[0] == results[1]
    assert all(result.requested_unit_ids == batch.ordered_unit_ids for result in results)


@pytest.mark.contract
def test_p2_3_t04_public_port_set_is_exactly_registered_once() -> None:
    """P2.3-T04：公开 *Port 恰好五个且没有未登记或重复定义。"""

    import transflow.ports as ports_package

    public_ports = {
        name: value
        for name, value in vars(ports_package).items()
        if name.endswith("Port") and inspect.isclass(value)
    }
    assert set(public_ports) == {
        "ArtifactPort",
        "CheckpointPort",
        "JobQueuePort",
        "ModelDecisionPort",
        "TranslationPort",
    }
    assert len({id(value) for value in public_ports.values()}) == 5


@pytest.mark.contract
def test_p2_4_t01_every_design_route_appears_exactly_once() -> None:
    """P2.4-T01：设计台账、Taxonomy 和 Catalog 的 17 条路由一一对应。"""

    ledger = json.loads((REPO_ROOT / "docs" / "迁移" / "migration_ledger.json").read_text("utf-8"))
    taxonomy = json.loads(
        (REPO_ROOT / "resources" / "taxonomy" / "page_classification_routes_v1.json").read_text(
            "utf-8"
        )
    )
    catalog = json.loads(
        (REPO_ROOT / "resources" / "catalogs" / "page_toolbox_catalog_v1.json").read_text(
            "utf-8"
        )
    )
    taxonomy_routes = [item["route"] for item in taxonomy["routes"]]
    catalog_routes = [item["route"] for item in catalog["entries"]]
    assert taxonomy_routes == ledger["route_behavior_keys"]
    assert catalog_routes == taxonomy_routes
    assert len(catalog_routes) == len(set(catalog_routes)) == 17


@pytest.mark.contract
def test_p2_4_t02_mutating_each_resource_kind_changes_fingerprint() -> None:
    """P2.4-T02：Prompt、Schema、字体、Catalog 任一字节变化都会改变组合指纹。"""

    baseline_parts = [b"prompt", b"schema", b"font", b"catalog"]
    baseline = build_runtime_fingerprints(*baseline_parts)
    for index in range(4):
        mutated = baseline_parts.copy()
        mutated[index] += b"!"
        assert build_runtime_fingerprints(*mutated).combined_hash != baseline.combined_hash


@pytest.mark.contract
def test_p2_4_t03_asset_regeneration_is_stable_without_drift() -> None:
    """P2.4-T03：连续生成结果一致，且与磁盘冻结资源逐字节相同。"""

    first = build_outputs()
    second = build_outputs()
    assert first == second
    assert all(path.read_bytes() == content for path, content in first.items())


@pytest.mark.contract
def test_p2_4_t04_unverified_catalog_enablement_fails() -> None:
    """P2.4-T04：没有 PASS 与晋升清单的叶子一旦启用立即失败。"""

    taxonomy = json.loads(
        (REPO_ROOT / "resources" / "taxonomy" / "page_classification_routes_v1.json").read_text(
            "utf-8"
        )
    )
    catalog = json.loads(
        (REPO_ROOT / "resources" / "catalogs" / "page_toolbox_catalog_v1.json").read_text(
            "utf-8"
        )
    )
    assert catalog_violations(taxonomy, catalog) == []
    catalog["entries"][0]["enabled"] = True
    assert any(
        violation.startswith("UNVERIFIED_ENABLED:")
        for violation in catalog_violations(taxonomy, catalog)
    )


@pytest.mark.contract
def test_p2_5_t01_domain_to_pdf_engine_mutation_is_detected() -> None:
    """P2.5-T01：领域层引入 PDF 引擎的突变必被架构扫描捕获。"""

    with mutation_tree("t01_domain_pdf", {"domain/bad.py": "import pymupdf\n"}) as root:
        violations = scan_production_tree(root)
    assert any(item.code == "ILLEGAL_LAYER_IMPORT" for item in violations)
    assert any(item.code == "DOMAIN_EXTERNAL_DEPENDENCY" for item in violations)


@pytest.mark.contract
def test_p2_5_t02_spike_and_host_references_are_detected() -> None:
    """P2.5-T02：生产代码引用 spike 或宿主项目的突变均被捕获。"""

    mutations = {
        "spike": {"production/bad.py": "from spikes.demo import run\n"},
        "host": {"production/bad.py": "from merqfin.jobs import acquire\n"},
    }
    expected_codes = {"spike": "SPIKE_REFERENCE", "host": "HOST_REFERENCE"}
    for name, files in mutations.items():
        with mutation_tree(f"t02_{name}", files) as root:
            violations = scan_production_tree(root)
        assert any(item.code == expected_codes[name] for item in violations)


@pytest.mark.contract
def test_p2_5_t03_forbidden_ai_browser_and_page_merge_mutations_are_detected() -> None:
    """P2.5-T03：AI 代理、模型端点、浏览器、HTML 回填和单页拼接突变全捕获。"""

    mutations = {
        "litellm": ("import litellm\n", "LITELLM_REFERENCE"),
        "endpoint": ("MODEL_ENDPOINT = 'https://example.invalid/v1'\n", "MODEL_ENDPOINT"),
        "chrome": ("chrome = 'renderer'\n", "CHROME_REFERENCE"),
        "html": ("page.insert_htmlbox(box, text)\n", "HTML_INSERTION"),
        "page_merge": ("def merge_page_pdf():\n    pass\n", "PAGE_PDF_MERGE"),
    }
    for name, (content, expected_code) in mutations.items():
        with mutation_tree(f"t03_{name}", {"production/bad.py": content}) as root:
            violations = scan_production_tree(root)
        assert any(item.code == expected_code for item in violations)


@pytest.mark.contract
def test_p2_5_t04_legal_layer_dependencies_pass() -> None:
    """P2.5-T04：runtime、adapter、application、port 的合法单向依赖通过。"""

    files = {
        "application/service.py": "from transflow.ports import ArtifactPort\n",
        "adapters/store.py": "from transflow.ports import CheckpointPort\n",
        "runtime/main.py": (
            "from transflow.adapters import store\n"
            "from transflow.application import service\n"
        ),
        "ports/sample.py": "from transflow.domain import DocumentRunRequest\n",
        "domain/value.py": "from dataclasses import dataclass\n",
    }
    with mutation_tree("t04_legal", files) as root:
        assert scan_production_tree(root) == []
