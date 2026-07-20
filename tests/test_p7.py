"""按 P7.1～P7.5 验收 Toolbox 生产合同与逐叶迁移骨架。"""

from __future__ import annotations

import hashlib
import json
import shutil
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

import pymupdf
import pytest

from scripts import verify_p7
from transflow.adapters.ai.fixed import DeterministicTranslationAdapter
from transflow.application.document_finalizer import DocumentFinalizer
from transflow.application.toolbox_page_coordinator import (
    ToolboxPageCoordinator,
    ToolboxPageWork,
    TranslationCompatibilityRecorder,
)
from transflow.domain.errors import DomainContractError, ErrorCode, PortCallError
from transflow.domain.jobs import DocumentRunRequest
from transflow.domain.pages import PageExecutionContext
from transflow.domain.states import (
    CheckpointCompatibility,
    Fallback,
    ensure_checkpoint_compatible,
)
from transflow.domain.toolbox import (
    Decision,
    DecisionDisposition,
    Finding,
    PagePatch,
    PatchOperation,
    ToolboxDescriptor,
)
from transflow.domain.translation import (
    TranslatedUnit,
    TranslationBatch,
    TranslationBundle,
    TranslationUnit,
)
from transflow.pdf_kernel.facts import ExtractedPageFacts, PageFactsExtractor
from transflow.pdf_kernel.workspace import RunWorkspace, WorkspaceAllocator
from transflow.toolboxes.catalog import (
    ToolboxCatalog,
    ToolboxCatalogEntry,
    catalog_entry_fingerprint,
    load_toolbox_catalog,
)
from transflow.toolboxes.contracts import (
    TOOLBOX_CONTRACT_VERSION,
    PageTemplate,
    ToolboxCandidate,
    ToolboxJudgement,
    ToolboxLayoutPlan,
    TranslationDispatch,
    normalized_page_outcome,
)
from transflow.toolboxes.leaf_gate import (
    COMPONENT_HASH_FIELDS,
    LeafGateConclusion,
    LeafGateEvaluator,
    LeafMigrationEvidence,
    evidence_is_current,
    validate_catalog_publication,
)
from transflow.toolboxes.legacy import (
    LegacyCompatibilityArtifact,
    LegacyNormalizedResult,
    LegacyPageMaterializer,
    LegacyStatus,
    LegacyToolboxAdapter,
    map_legacy_result,
)
from transflow.toolboxes.margin import (
    MarginObservation,
    MarginPolicy,
    MarginRegionProcessor,
    load_margin_policy,
    validate_owner_assignments,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
CATALOG_PATH = REPO_ROOT / "resources" / "catalogs" / "page_toolbox_catalog_v2.json"
MARGIN_POLICY_PATH = REPO_ROOT / "resources" / "manifests" / "p7_margin_policy.json"
LEAF_STATE_PATH = REPO_ROOT / "docs" / "迁移" / "p7_leaf_initial_state.json"
HASH_A = "a" * 64
HASH_B = "b" * 64


def sha256_file(path: Path) -> str:
    """流式计算测试 PDF 或资源文件的真实 SHA-256。"""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class P7PdfFixture:
    """聚合 P7 合同测试使用的真实三页 PDF、事实、上下文和私有工作区。"""

    source: Path
    source_hash: str
    facts: tuple[ExtractedPageFacts, ...]
    contexts: tuple[PageExecutionContext, ...]
    workspace: RunWorkspace


@pytest.fixture
def p7_pdf(tmp_path: Path) -> P7PdfFixture:
    """生成真实三页 PDF 并通过 SharedPdfKernel 提取稳定事实。"""

    source = tmp_path / "inputs" / "p7-source.pdf"
    source.parent.mkdir(parents=True, exist_ok=True)
    with pymupdf.open() as document:
        for page_no in range(1, 4):
            page = document.new_page(width=420, height=600)
            page.insert_textbox(
                pymupdf.Rect(40, 60, 380, 150),
                f"P7 source page {page_no}",
                fontsize=12,
                fontname="helv",
            )
        document.save(source)
    source_hash = sha256_file(source)
    facts = PageFactsExtractor().extract_all(source, source_hash)
    contexts = tuple(
        PageExecutionContext(
            job_id="job-p7",
            run_id="run-p7",
            source_hash=source_hash,
            page_no=item.page.page_no,
            geometry_hash=item.page.geometry_hash,
            config_snapshot_hash=HASH_A,
        )
        for item in facts
    )
    workspace = WorkspaceAllocator(tmp_path / "runs").allocate("job-p7", "run-p7")
    return P7PdfFixture(source, source_hash, facts, contexts, workspace)


class ProbeToolbox:
    """实现 P7 测试用的完整六阶段叶，所有输出均为真实领域 DTO。"""

    def __init__(self, route: str = "body.flow_text.single", toolbox_id: str = "dummy") -> None:
        """冻结 Route/Toolbox 身份并初始化阶段记录。"""

        self._descriptor = ToolboxDescriptor(
            toolbox_id,
            route,
            TOOLBOX_CONTRACT_VERSION,
            route,
        )
        self.calls: list[str] = []
        self._facts: dict[str, ExtractedPageFacts] = {}

    @property
    def descriptor(self) -> ToolboxDescriptor:
        """返回测试叶稳定描述符。"""

        return self._descriptor

    def prepare(
        self,
        context: PageExecutionContext,
        facts: ExtractedPageFacts,
    ) -> PageTemplate:
        """由真实页面事实建立模板并记录阶段。"""

        self.calls.append("prepare")
        template_id = f"template-p{context.page_no:04d}"
        self._facts[template_id] = facts
        return PageTemplate(
            template_id,
            context,
            facts.page.facts_hash,
            self.descriptor.owner,
            facts.owned_object_ids,
        )

    def build_translation_request(self, template: PageTemplate) -> TranslationBatch:
        """构造一个稳定 TranslationUnit 的真实批次。"""

        self.calls.append("build_translation_request")
        unit_id = hashlib.sha256(f"{template.template_id}\0unit".encode()).hexdigest()
        unit = TranslationUnit(
            unit_id,
            template.context.page_no,
            0,
            f"source-{template.context.page_no}",
            f"region-{template.context.page_no}",
        )
        return TranslationBatch(
            f"batch-{template.context.run_id}-p{template.context.page_no:04d}",
            "en",
            "zh-CN",
            (unit,),
        )

    def consume_translation_bundle(
        self,
        template: PageTemplate,
        dispatch: TranslationDispatch,
    ) -> ToolboxLayoutPlan:
        """将真实 Bundle 转成绑定 Patch；错误则形成确定 fallback 计划。"""

        self.calls.append("consume_translation_bundle")
        if dispatch.failure is not None:
            finding = Finding(
                f"finding-translation-p{template.context.page_no}",
                dispatch.failure.code,
                "HARD",
                (dispatch.batch.batch_id,),
            )
            return ToolboxLayoutPlan(
                f"plan-p{template.context.page_no}",
                self.descriptor.route,
                None,
                (finding,),
                True,
            )
        assert dispatch.bundle is not None
        facts = self._facts[template.template_id]
        text_object = next(item for item in facts.objects if not item.protected and item.text)
        translated_text = dispatch.bundle.units[0].translated_text
        operation = PatchOperation(
            operation_id=f"operation-p{template.context.page_no}",
            region_id=f"region-{template.context.page_no}",
            kind="replace_text",
            payload_hash=hashlib.sha256(translated_text.encode("utf-8")).hexdigest(),
            owner=self.descriptor.owner,
            target_object_ids=(text_object.object_id,),
            rect=text_object.bbox,
            replacement_text=translated_text,
            font_id="noto-sans-cjk-sc-regular",
            font_size=10.0,
        )
        patch = PagePatch(
            patch_id=f"patch-p{template.context.page_no}",
            source_hash=template.context.source_hash,
            page_no=template.context.page_no,
            geometry_hash=template.context.geometry_hash,
            owner=self.descriptor.owner,
            operations=(operation,),
        )
        return ToolboxLayoutPlan(
            f"plan-p{template.context.page_no}",
            self.descriptor.route,
            patch,
            (),
        )

    def render(
        self,
        context: PageExecutionContext,
        facts: ExtractedPageFacts,
        plan: ToolboxLayoutPlan,
    ) -> ToolboxCandidate:
        """用计划和事实哈希生成真实稳定候选指纹。"""

        self.calls.append("render")
        fingerprint = hashlib.sha256(
            f"{plan.plan_id}\0{facts.kernel_facts_hash}".encode("ascii")
        ).hexdigest()
        return ToolboxCandidate(f"candidate-p{context.page_no}", plan, fingerprint)

    def judge(self, candidate: ToolboxCandidate) -> ToolboxJudgement:
        """根据 fallback 标记生成 ACCEPT 或 FALLBACK 裁决。"""

        self.calls.append("judge")
        disposition = (
            DecisionDisposition.FALLBACK
            if candidate.plan.fallback_requested
            else DecisionDisposition.ACCEPT
        )
        return ToolboxJudgement(
            (),
            Decision(
                f"decision-{candidate.candidate_id}",
                disposition,
                (),
                disposition.value,
            ),
        )

    def repair(
        self,
        candidate: ToolboxCandidate,
        judgement: ToolboxJudgement,
    ) -> ToolboxCandidate:
        """无修复需求时原样返回，仍记录固定第六阶段。"""

        self.calls.append("repair")
        return candidate


class LegacyProbeLeaf:
    """用真实单页兼容输入委派 ProbeToolbox，模拟不改算法顺序的旧叶。"""

    def __init__(self) -> None:
        """建立委派叶并记录实际接收的单页路径。"""

        self.delegate = ProbeToolbox()
        self.single_page_path: Path | None = None

    @property
    def calls(self) -> list[str]:
        """返回委派叶的阶段记录。"""

        return self.delegate.calls

    def prepare(
        self,
        single_page_pdf: Path,
        context: PageExecutionContext,
        facts: ExtractedPageFacts,
    ) -> PageTemplate:
        """真实打开兼容 PDF 确认只有一页后执行旧叶 prepare。"""

        with pymupdf.open(single_page_pdf) as document:
            assert document.page_count == 1
        self.single_page_path = single_page_pdf
        return self.delegate.prepare(context, facts)

    def build_translation_request(self, template: PageTemplate) -> TranslationBatch:
        """委派请求构造。"""

        return self.delegate.build_translation_request(template)

    def consume_translation_bundle(
        self,
        template: PageTemplate,
        dispatch: TranslationDispatch,
    ) -> ToolboxLayoutPlan:
        """委派 Bundle 消费。"""

        return self.delegate.consume_translation_bundle(template, dispatch)

    def render(
        self,
        context: PageExecutionContext,
        facts: ExtractedPageFacts,
        plan: ToolboxLayoutPlan,
    ) -> ToolboxCandidate:
        """委派候选渲染。"""

        return self.delegate.render(context, facts, plan)

    def judge(self, candidate: ToolboxCandidate) -> ToolboxJudgement:
        """委派候选裁决。"""

        return self.delegate.judge(candidate)

    def repair(
        self,
        candidate: ToolboxCandidate,
        judgement: ToolboxJudgement,
    ) -> ToolboxCandidate:
        """委派有界修复。"""

        return self.delegate.repair(candidate, judgement)


class InvalidBundleAdapter:
    """按指定模式返回身份错误，用于验证 PageCoordinator 前置拒绝。"""

    def __init__(self, mode: str) -> None:
        """保存缺失、重复或新增三种故障模式。"""

        self.mode = mode
        self.calls = 0

    def translate(self, batch: TranslationBatch) -> TranslationBundle:
        """返回可构造但不绑定原批次的 Bundle，或触发重复身份合同错误。"""

        self.calls += 1
        if self.mode == "duplicate":
            return TranslationBundle(
                batch.batch_id,
                (batch.units[0].unit_id,),
                (
                    TranslatedUnit(batch.units[0].unit_id, "译文一"),
                    TranslatedUnit(batch.units[0].unit_id, "译文二"),
                ),
            )
        unexpected_id = f"{self.mode}-unit"
        return TranslationBundle(
            f"{batch.batch_id}-{self.mode}",
            (unexpected_id,),
            (TranslatedUnit(unexpected_id, "不匹配译文"),),
        )


class TimeoutTranslationAdapter:
    """抛出结构化真实端口超时，验证叶不得自行重试。"""

    def __init__(self) -> None:
        """初始化真实调用计数。"""

        self.calls = 0

    def translate(self, batch: TranslationBatch) -> TranslationBundle:
        """每次调用都抛出非重试端口超时。"""

        self.calls += 1
        raise PortCallError(ErrorCode.AI_TIMEOUT, False, f"timeout:{batch.batch_id}")


class SlowDeterministicAdapter:
    """故意按页引入不同延迟，再委派真实确定性翻译实现。"""

    def translate(self, batch: TranslationBatch) -> TranslationBundle:
        """让页码更小的请求更慢，以制造响应乱序。"""

        page_no = batch.units[0].page_no
        time.sleep((4 - page_no) * 0.01)
        return DeterministicTranslationAdapter().translate(batch)


def make_enabled_entry(
    *,
    route: str = "body.flow_text.single",
    toolbox_key: str = "dummy",
    version: str = "1.0.0",
    attestation_hash: str = HASH_B,
) -> ToolboxCatalogEntry:
    """构造带匹配生产身份和证明哈希的 enabled Catalog 项。"""

    return ToolboxCatalogEntry(
        route=route,
        toolbox_key=toolbox_key,
        toolbox_version=version,
        fingerprint=catalog_entry_fingerprint(
            route,
            toolbox_key,
            version,
            TOOLBOX_CONTRACT_VERSION,
        ),
        contract_version=TOOLBOX_CONTRACT_VERSION,
        evidence_state="PASS_ENABLE",
        evidence_attestation_hash=attestation_hash,
        enabled=True,
        fallback="PAGE_PASSTHROUGH",
    )


def run_direct_legacy(
    leaf: LegacyProbeLeaf,
    artifact: LegacyCompatibilityArtifact,
    context: PageExecutionContext,
    facts: ExtractedPageFacts,
) -> tuple[PagePatch | None, Decision, Any]:
    """按迁移前旧叶顺序直接执行，并返回可与 wrapper 比较的结构结果。"""

    template = leaf.prepare(artifact.path, context, facts)
    batch = leaf.build_translation_request(template)
    bundle = DeterministicTranslationAdapter().translate(batch)
    plan = leaf.consume_translation_bundle(template, TranslationDispatch(batch, bundle=bundle))
    candidate = leaf.render(context, facts, plan)
    judgement = leaf.judge(candidate)
    repaired = leaf.repair(candidate, judgement)
    outcome = normalized_page_outcome(
        context.page_no,
        accepted=True,
        translated=True,
        finding_codes=(),
    )
    return repaired.plan.patch, judgement.decision, outcome


def margin_observation(
    page_no: int,
    object_id: str,
    text: str,
    y0: float,
    route: str,
    *,
    kind: str = "text",
    semantic_hint: str = "unknown",
    page_width: float = 600,
    page_height: float = 800,
) -> MarginObservation:
    """构造不含文件名的真实几何边缘观测。"""

    return MarginObservation(
        page_no,
        object_id,
        kind,
        (40, y0, 300, y0 + 20),
        text,
        page_width,
        page_height,
        route,
        semantic_hint,
    )


def leaf_evidence(**overrides: Any) -> LeafMigrationEvidence:
    """构造字段齐全且哈希可复算的 dummy 叶真实证据 DTO。"""

    payload: dict[str, Any] = {
        "schema_version": "transflow.leaf-migration-evidence/v1",
        "route": "body.flow_text.single",
        "source_path": "spikes/page_toolbox_engine_puncture_v1/toolboxes/body/flow_text/single",
        "source_hash": HASH_A,
        "original_state": "PASS",
        "target_toolbox_key": "dummy",
        "target_version": "1.0.0",
        "allowed_changes": ["CONTRACT_ADAPTER_ONLY"],
        "migration_differences": ["PROVIDER_CALL_MOVED_TO_COORDINATOR"],
        "fixture_refs": ["tests/fixtures/p7-dummy.pdf"],
        "gold_refs": ["tests/fixtures/p7-dummy-gold.json"],
        "threshold_refs": ["resources/manifests/p7-margin-policy.json"],
        "fallback": "PAGE_PASSTHROUGH",
        "limitations": ["P7_DUMMY_ONLY"],
        "owner": "body.flow_text.single",
        "contract_passed": True,
        "equivalence_passed": True,
        "blind_passed": True,
        "anti_overfit_passed": True,
        "failure_passed": True,
        "document_e2e_passed": True,
        "fallback_has_page_outcome": True,
        "fallback_has_complete_pdf": True,
        "new_evidence": True,
        "code_hash": "1" * 64,
        "schema_hash": "2" * 64,
        "catalog_hash": "3" * 64,
        "font_hash": "4" * 64,
        "threshold_hash": "5" * 64,
        "evidence_hash": "",
    }
    payload.update(overrides)
    payload["evidence_hash"] = ""
    return LeafMigrationEvidence.from_dict(payload)


@pytest.mark.contract
def test_p7_1_t01_fake_leaf_closes_all_six_stages(p7_pdf: P7PdfFixture) -> None:
    """P7.1-T01：fake 叶逐阶段执行，schema 闭合、顺序固定且形成 PageOutcome。"""

    toolbox = ProbeToolbox()
    result = ToolboxPageCoordinator(DeterministicTranslationAdapter()).execute(
        ToolboxPageWork(p7_pdf.contexts[0], p7_pdf.facts[0], toolbox)
    )
    assert toolbox.calls == [
        "prepare",
        "build_translation_request",
        "consume_translation_bundle",
        "render",
        "judge",
        "repair",
    ]
    assert result.trace.stages[-1] == "outcome"
    assert result.patch is not None and result.outcome.fallback is Fallback.NONE


@pytest.mark.integration
def test_p7_1_t02_legacy_single_page_is_private_traced_and_rebuildable(
    p7_pdf: P7PdfFixture,
) -> None:
    """P7.1-T02：legacy 单页仅在 run/page，manifest 可追踪且删除后可重建。"""

    materializer = LegacyPageMaterializer()
    artifact = materializer.materialize(p7_pdf.source, p7_pdf.contexts[0], p7_pdf.workspace)
    assert artifact.path.is_relative_to(p7_pdf.workspace.page_root(1))
    manifest = json.loads(artifact.manifest_path.read_text(encoding="utf-8"))
    assert manifest["authority"] == "NON_AUTHORITATIVE_REBUILDABLE"
    artifact.path.unlink()
    rebuilt = materializer.materialize(p7_pdf.source, p7_pdf.contexts[0], p7_pdf.workspace)
    with pymupdf.open(rebuilt.path) as document:
        assert document.page_count == 1
    assert rebuilt.path == artifact.path and rebuilt.content_hash == sha256_file(rebuilt.path)


@pytest.mark.contract
def test_p7_1_t03_legacy_mapper_rejects_free_unknown_and_unbound_results(
    p7_pdf: P7PdfFixture,
) -> None:
    """P7.1-T03：自由 dict、未知状态和未绑定 source/page 结果均拒绝。"""

    context = p7_pdf.contexts[0]
    with pytest.raises(DomainContractError):
        map_legacy_result({"status": "ACCEPTED"}, context, "body.flow_text.single")
    with pytest.raises(ValueError):
        LegacyStatus("UNKNOWN")
    unbound = LegacyNormalizedResult(
        LegacyStatus.ACCEPTED,
        HASH_B,
        context.page_no,
        "body.flow_text.single",
        None,
        (),
        Decision("decision-unbound", DecisionDisposition.ACCEPT, (), "ACCEPT"),
        normalized_page_outcome(
            context.page_no,
            accepted=True,
            translated=True,
            finding_codes=(),
        ),
        (),
    )
    with pytest.raises(DomainContractError):
        map_legacy_result(unbound, context, "body.flow_text.single")


@pytest.mark.contract
def test_p7_1_t04_legacy_artifact_has_zero_authoritative_references(
    p7_pdf: P7PdfFixture,
) -> None:
    """P7.1-T04：DocumentFinalizer、最终 Artifact、Checkpoint 均拒绝临时单页。"""

    artifact = LegacyPageMaterializer().materialize(
        p7_pdf.source,
        p7_pdf.contexts[0],
        p7_pdf.workspace,
    )
    rejected = 0
    for target in ("final_artifact", "checkpoint"):
        with pytest.raises(DomainContractError):
            artifact.require_authoritative_use(target)
        rejected += 1
    request = DocumentRunRequest(
        str(artifact.path),
        artifact.content_hash,
        "en",
        "zh-CN",
        HASH_A,
        "job-p7",
        "run-p7-legacy-final",
    )
    finalizer = DocumentFinalizer(cast(Any, None), cast(Any, None), p7_pdf.workspace.run_root)
    with pytest.raises(DomainContractError):
        finalizer.preflight(request)
    rejected += 1
    assert rejected == 3 and artifact.authority == "NON_AUTHORITATIVE_REBUILDABLE"


@pytest.mark.contract
def test_p7_1_t05_toolbox_dependency_mutations_are_detected(tmp_path: Path) -> None:
    """P7.1-T05：DB/API/lease/MerqFin/Provider 直接依赖突变全部被架构扫描捕获。"""

    mutations = {
        "database.py": "import sqlalchemy\ndatabase = object()\n",
        "api.py": "import fastapi\n",
        "lease.py": "lease_client = object()\n",
        "host.py": "import merqfin\n",
        "provider.py": "provider_client = object()\n",
    }
    for name, content in mutations.items():
        root = tmp_path / name.removesuffix(".py")
        root.mkdir()
        (root / name).write_text(content, encoding="utf-8")
        assert verify_p7.scan_toolbox_tree(root)


@pytest.mark.migration
def test_p7_1_t06_legacy_wrapper_preserves_algorithm_order_and_result(
    p7_pdf: P7PdfFixture,
) -> None:
    """P7.1-T06：wrapper 前后只有单页来源适配差异，阶段顺序和结构结果一致。"""

    materializer = LegacyPageMaterializer()
    artifact = materializer.materialize(p7_pdf.source, p7_pdf.contexts[0], p7_pdf.workspace)
    direct_leaf = LegacyProbeLeaf()
    direct = run_direct_legacy(direct_leaf, artifact, p7_pdf.contexts[0], p7_pdf.facts[0])
    wrapped_leaf = LegacyProbeLeaf()
    wrapped = LegacyToolboxAdapter(
        ProbeToolbox().descriptor,
        wrapped_leaf,
        p7_pdf.source,
        p7_pdf.workspace,
        materializer,
    )
    result = ToolboxPageCoordinator(DeterministicTranslationAdapter()).execute(
        ToolboxPageWork(p7_pdf.contexts[0], p7_pdf.facts[0], wrapped)
    )
    assert wrapped_leaf.calls == direct_leaf.calls
    assert (result.patch, result.verdict, result.outcome) == direct


@pytest.mark.migration
def test_p7_2_t01_compatibility_recorder_preserves_request_consume_order(
    p7_pdf: P7PdfFixture,
) -> None:
    """P7.2-T01：兼容记录器保持 unit/request/consume 稳定顺序。"""

    recorder = TranslationCompatibilityRecorder()
    result = ToolboxPageCoordinator(DeterministicTranslationAdapter(), recorder).execute(
        ToolboxPageWork(p7_pdf.contexts[0], p7_pdf.facts[0], ProbeToolbox())
    )
    events = recorder.snapshot()
    assert tuple(item.event for item in events) == ("request", "consume")
    assert events[0].unit_ids == events[1].unit_ids == result.ordered_unit_ids


@pytest.mark.migration
def test_p7_2_t02_fixed_bundle_embedded_and_split_results_are_equivalent(
    p7_pdf: P7PdfFixture,
) -> None:
    """P7.2-T02：同一确定 Bundle 的内嵌与拆分路径结构差异为零。"""

    direct_leaf = LegacyProbeLeaf()
    artifact = LegacyPageMaterializer().materialize(
        p7_pdf.source,
        p7_pdf.contexts[0],
        p7_pdf.workspace,
    )
    direct = run_direct_legacy(direct_leaf, artifact, p7_pdf.contexts[0], p7_pdf.facts[0])
    split = ToolboxPageCoordinator(DeterministicTranslationAdapter()).execute(
        ToolboxPageWork(p7_pdf.contexts[0], p7_pdf.facts[0], ProbeToolbox())
    )
    assert (split.patch, split.verdict, split.outcome) == direct


@pytest.mark.contract
def test_p7_2_t03_invalid_bundle_ids_are_rejected_before_leaf(
    p7_pdf: P7PdfFixture,
) -> None:
    """P7.2-T03：缺失、重复、新增 unit ID 在交叶前转为结构化失败并安全降级。"""

    for mode in ("missing", "duplicate", "added"):
        toolbox = ProbeToolbox()
        result = ToolboxPageCoordinator(InvalidBundleAdapter(mode)).execute(
            ToolboxPageWork(p7_pdf.contexts[0], p7_pdf.facts[0], toolbox)
        )
        assert result.outcome.fallback is Fallback.PAGE_PASSTHROUGH
        assert result.patch is None


@pytest.mark.contract
def test_p7_2_t04_direct_http_retry_and_concurrency_mutations_fail(tmp_path: Path) -> None:
    """P7.2-T04：Toolbox 直接 HTTP、Provider、重试和并发突变均使 Gate 失败。"""

    mutations = {
        "http.py": "import httpx\n",
        "provider.py": "provider_client = object()\n",
        "retry.py": "def run():\n    return retry()\n",
        "concurrency.py": "from concurrent.futures import ThreadPoolExecutor\n",
    }
    for name, content in mutations.items():
        root = tmp_path / name.removesuffix(".py")
        root.mkdir()
        (root / name).write_text(content, encoding="utf-8")
        assert verify_p7.scan_toolbox_tree(root)


@pytest.mark.integration
def test_p7_2_t05_concurrent_translation_results_are_reordered_by_page_and_unit(
    p7_pdf: P7PdfFixture,
) -> None:
    """P7.2-T05：响应乱序后按 page/unit ID 归位且零串页。"""

    work = tuple(
        ToolboxPageWork(context, facts, ProbeToolbox())
        for context, facts in zip(p7_pdf.contexts, p7_pdf.facts, strict=True)
    )
    results = ToolboxPageCoordinator(SlowDeterministicAdapter()).execute_many(work, 3)
    assert tuple(item.page_no for item in results) == (1, 2, 3)
    assert all(len(item.ordered_unit_ids) == 1 for item in results)
    assert len({item.ordered_unit_ids[0] for item in results}) == 3


@pytest.mark.fault_injection
def test_p7_2_t06_translation_timeout_falls_back_without_leaf_retry(
    p7_pdf: P7PdfFixture,
) -> None:
    """P7.2-T06：TranslationPort 超时形成结构化 fallback，叶不再次调用。"""

    adapter = TimeoutTranslationAdapter()
    result = ToolboxPageCoordinator(adapter).execute(
        ToolboxPageWork(p7_pdf.contexts[0], p7_pdf.facts[0], ProbeToolbox())
    )
    assert adapter.calls == 1
    assert result.outcome.fallback is Fallback.PAGE_PASSTHROUGH
    assert result.findings[0].code == ErrorCode.AI_TIMEOUT.value


@pytest.mark.contract
def test_p7_3_t01_all_design_routes_resolve_uniquely() -> None:
    """P7.3-T01：§8.2 全 Route 均返回唯一 enabled Toolbox 或确定 fallback。"""

    catalog = load_toolbox_catalog(CATALOG_PATH)
    report = catalog.validate_startup()
    resolutions = tuple(catalog.resolve_enabled(item.route, 1) for item in catalog.entries)
    assert report.ready is True
    assert len(resolutions) == len(catalog.entries) == 17
    assert all(item.toolbox is None and item.outcome is not None for item in resolutions)


@pytest.mark.contract
def test_p7_3_t02_all_catalog_failure_modes_have_page_outcomes() -> None:
    """P7.3-T02：未注册、禁用、版本/指纹不匹配、初始化失败均有定义出口。"""

    disabled_catalog = load_toolbox_catalog(CATALOG_PATH)
    unregistered = disabled_catalog.resolve_enabled("unknown.route", 1)
    disabled = disabled_catalog.resolve_enabled("cover", 1)
    entry = make_enabled_entry()
    factory = {"dummy": lambda: ProbeToolbox()}
    enabled_catalog = ToolboxCatalog((entry,), HASH_A, factory)
    version = enabled_catalog.resolve_enabled(entry.route, 1, expected_version="0.9.0")
    fingerprint = enabled_catalog.resolve_enabled(
        entry.route,
        1,
        expected_fingerprint=HASH_A,
    )
    failed_catalog = ToolboxCatalog(
        (entry,),
        HASH_A,
        {"dummy": lambda: (_ for _ in ()).throw(RuntimeError("init"))},
    )
    initialization = failed_catalog.resolve_enabled(entry.route, 1)
    codes = {
        cast(Finding, item.finding).code
        for item in (unregistered, disabled, version, fingerprint, initialization)
    }
    assert codes == {
        "TOOLBOX_UNREGISTERED",
        "TOOLBOX_DISABLED",
        "TOOLBOX_VERSION_MISMATCH",
        "TOOLBOX_FINGERPRINT_MISMATCH",
        "TOOLBOX_INITIALIZATION_FAILED",
    }


@pytest.mark.contract
def test_p7_3_t03_duplicate_binding_or_missing_fallback_blocks_readiness() -> None:
    """P7.3-T03：重复 enabled 或无 fallback 令 readiness=false 且构造/claim 数为零。"""

    calls = 0

    def factory() -> ProbeToolbox:
        """记录是否发生 Toolbox 构造。"""

        nonlocal calls
        calls += 1
        return ProbeToolbox()

    entry = make_enabled_entry()
    report = ToolboxCatalog((entry, entry), HASH_A, {"dummy": factory}).validate_startup()
    assert report.ready is False and calls == 0
    with pytest.raises(DomainContractError):
        replace(entry, fallback="")


@pytest.mark.contract
def test_p7_3_t04_catalog_file_mutation_is_detected_without_changing_old_snapshot(
    tmp_path: Path,
) -> None:
    """P7.3-T04：运行中修改 Catalog 被拒绝，旧 run 的不可变快照不受影响。"""

    copied = tmp_path / "catalog.json"
    shutil.copyfile(CATALOG_PATH, copied)
    catalog = load_toolbox_catalog(copied)
    before = catalog.resolve_enabled("cover", 1)
    payload = json.loads(copied.read_text(encoding="utf-8"))
    payload["entries"][0]["disabled_reason"] = "MUTATED"
    copied.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(DomainContractError):
        catalog.assert_source_unchanged()
    after = catalog.resolve_enabled("cover", 1)
    assert before == after and catalog.entries[0].disabled_reason != "MUTATED"


@pytest.mark.contract
def test_p7_3_t05_dynamic_discovery_and_model_selection_mutations_fail(tmp_path: Path) -> None:
    """P7.3-T05：目录扫描、entry point 和模型工具选择实现均被架构 Gate 捕获。"""

    mutations = {
        "scan.py": "from pathlib import Path\nfiles = Path('.').rglob('*.py')\n",
        "entry.py": "from importlib.metadata import entry_points\nitems = entry_points()\n",
        "model.py": "model_selector = object()\n",
    }
    for name, content in mutations.items():
        root = tmp_path / name.removesuffix(".py")
        root.mkdir()
        (root / name).write_text(content, encoding="utf-8")
        assert verify_p7.scan_toolbox_tree(root)


@pytest.mark.contract
def test_p7_3_t06_catalog_drift_rejects_old_checkpoint() -> None:
    """P7.3-T06：Catalog version/fingerprint 变化后旧 checkpoint 被识别为不兼容。"""

    stored = CheckpointCompatibility(HASH_A, HASH_A, HASH_A, HASH_A, HASH_A)
    current = replace(stored, toolbox_catalog_hash=HASH_B)
    with pytest.raises(DomainContractError) as captured:
        ensure_checkpoint_compatible(stored, current)
    assert captured.value.code is ErrorCode.CHECKPOINT_INCOMPATIBLE


@pytest.mark.contract
def test_p7_4_t01_repeated_semantic_margins_receive_one_shared_owner() -> None:
    """P7.4-T01：跨多数页语义页眉形成唯一 owner，位置和阅读顺序证据完整。"""

    processor = MarginRegionProcessor(load_margin_policy(MARGIN_POLICY_PATH))
    observations = (
        margin_observation(1, "h1", "Annual Report", 30, "cover"),
        margin_observation(2, "h2", "Annual Report", 30, "body.flow_text.single"),
        margin_observation(3, "h3", "Annual Report", 30, "body.flow_text.single"),
    )
    result = processor.process(observations, 3)
    assert len(result.shared_regions) == 3
    assert {owner for _, owner in result.owner_by_object} == {"shared.margin.header"}
    assert tuple(item.ordinal for item in result.evidence) == (0, 0, 0)
    assert len(result.evidence_hash) == 64


@pytest.mark.contract
def test_p7_4_t02_page_numbers_logos_and_decorations_are_protected() -> None:
    """P7.4-T02：纯页码、Logo、装饰线全部 protected 且零 TranslationUnit/Patch。"""

    observations = (
        margin_observation(1, "page-number", "1 / 3", 760, "cover"),
        margin_observation(1, "logo", "", 20, "cover", kind="logo"),
        margin_observation(1, "line", "", 50, "cover", kind="decoration"),
    )
    result = MarginRegionProcessor(load_margin_policy(MARGIN_POLICY_PATH)).process(
        observations,
        1,
    )
    assert set(result.protected_object_ids) == {"page-number", "logo", "line"}
    assert result.shared_regions == () and result.owner_by_object == ()


@pytest.mark.contract
def test_p7_4_t03_edge_body_table_note_and_chart_label_are_handed_back() -> None:
    """P7.4-T03：靠边正文、表注和图标签不被 margin owner 领取。"""

    hints = ("body", "table_note", "chart_label")
    observations = tuple(
        margin_observation(
            index,
            f"semantic-{index}",
            "Repeated edge text",
            30,
            "body.table" if index == 2 else "body.chart",
            semantic_hint=hint,
        )
        for index, hint in enumerate(hints, start=1)
    )
    result = MarginRegionProcessor(load_margin_policy(MARGIN_POLICY_PATH)).process(
        observations,
        3,
    )
    assert set(result.handback_object_ids) == {"semantic-1", "semantic-2", "semantic-3"}
    assert result.shared_regions == ()


@pytest.mark.contract
def test_p7_4_t04_insufficient_repetition_is_uncertain_and_handed_back() -> None:
    """P7.4-T04：只出现一次或跨叶证据不足的边缘文本明确 handback。"""

    observations = (
        margin_observation(1, "once", "Unique header", 30, "cover"),
        margin_observation(2, "same-route-1", "Same route", 30, "cover"),
        margin_observation(3, "same-route-2", "Same route", 30, "cover"),
    )
    result = MarginRegionProcessor(load_margin_policy(MARGIN_POLICY_PATH)).process(
        observations,
        3,
    )
    assert set(result.handback_object_ids) == {"once", "same-route-1", "same-route-2"}


@pytest.mark.contract
def test_p7_4_t05_decision_follows_geometry_and_style_not_sample_identity() -> None:
    """P7.4-T05：页尺寸、边距和页码样式改变按结构判定，不读取文件名。"""

    policy = MarginPolicy(0.14, 0.86, 0.5, 2, 2)
    observations = (
        margin_observation(1, "large-1", "Header 2026", 30, "cover", page_height=1000),
        margin_observation(
            2,
            "large-2",
            "Header 2027",
            30,
            "body.flow_text.single",
            page_height=1000,
        ),
        margin_observation(3, "small", "Header 2028", 30, "cover", page_height=300),
        margin_observation(3, "roman", "iv", 270, "cover", page_height=300),
    )
    result = MarginRegionProcessor(policy).process(observations, 3)
    assert {item[0] for item in result.owner_by_object} == {"large-1", "large-2"}
    assert "small" in result.handback_object_ids and "roman" in result.protected_object_ids


@pytest.mark.contract
def test_p7_4_t06_margin_and_body_owner_conflict_is_rejected() -> None:
    """P7.4-T06：margin 与 body 重复 owner 在计划校验中拒绝并可走 fallback。"""

    with pytest.raises(DomainContractError):
        validate_owner_assignments(
            (("object-1", "shared.margin.header"),),
            (("object-1", "body.flow_text.single"),),
        )


@pytest.mark.contract
def test_p7_5_t01_complete_leaf_evidence_enables_matching_catalog_entry() -> None:
    """P7.5-T01：证据齐全且阈值达标的 dummy 叶 PASS_ENABLE 并匹配 Catalog。"""

    evidence = leaf_evidence()
    attestation = LeafGateEvaluator().evaluate(evidence)
    entry = make_enabled_entry(attestation_hash=attestation.attestation_hash)
    validate_catalog_publication(entry, attestation)
    assert attestation.conclusion is LeafGateConclusion.PASS_ENABLE


@pytest.mark.contract
def test_p7_5_t02_missing_hard_evidence_stays_disabled_with_fallback() -> None:
    """P7.5-T02：缺独立盲测但 fallback 可靠时保持 disabled。"""

    attestation = LeafGateEvaluator().evaluate(leaf_evidence(blind_passed=False))
    assert attestation.conclusion is LeafGateConclusion.PASS_DISABLED_WITH_FALLBACK


@pytest.mark.contract
def test_p7_5_t03_incomplete_fallback_fails_leaf_and_stage_gate() -> None:
    """P7.5-T03：fallback 无 PageOutcome 或完整 PDF 时 FAIL 且不能被其他叶掩盖。"""

    evaluator = LeafGateEvaluator()
    failed = leaf_evidence(
        blind_passed=False,
        fallback_has_complete_pdf=False,
    )
    batch = evaluator.evaluate_all((leaf_evidence(), failed))
    assert batch.attestations[1].conclusion is LeafGateConclusion.FAIL
    assert batch.stage_passed is False


@pytest.mark.migration
def test_p7_5_t04_nonblind_unevaluated_and_failed_states_are_not_upgraded() -> None:
    """P7.5-T04：三类原状态未经新证据不得升级，导入清单也全部 upgrade=false。"""

    evaluator = LeafGateEvaluator()
    for state in ("PASS_NON_BLIND", "NOT_EVALUATED", "FAIL"):
        conclusion = evaluator.evaluate(
            leaf_evidence(original_state=state, new_evidence=False)
        ).conclusion
        assert conclusion is LeafGateConclusion.PASS_DISABLED_WITH_FALLBACK
    imported = json.loads(LEAF_STATE_PATH.read_text(encoding="utf-8"))
    assert all(item["upgrade_performed"] is False for item in imported["leaves"])


@pytest.mark.contract
def test_p7_5_t05_tampered_catalog_enable_or_evidence_hash_is_rejected() -> None:
    """P7.5-T05：Catalog enabled/证明哈希与叶结论不一致时发布校验拒绝。"""

    attestation = LeafGateEvaluator().evaluate(leaf_evidence())
    wrong_hash_entry = make_enabled_entry(attestation_hash="e" * 64)
    with pytest.raises(DomainContractError):
        validate_catalog_publication(wrong_hash_entry, attestation)
    disabled_entry = replace(
        make_enabled_entry(attestation_hash=attestation.attestation_hash),
        enabled=False,
        disabled_reason="TAMPERED",
    )
    with pytest.raises(DomainContractError):
        validate_catalog_publication(disabled_entry, attestation)


@pytest.mark.contract
def test_p7_5_t06_component_changes_expire_old_leaf_evidence() -> None:
    """P7.5-T06：代码、Schema、Catalog、字体或阈值变化均使旧证据过期。"""

    evidence = leaf_evidence()
    attestation = LeafGateEvaluator().evaluate(evidence)
    current = {field_name: getattr(evidence, field_name) for field_name in COMPONENT_HASH_FIELDS}
    assert evidence_is_current(attestation, current)
    for field_name in COMPONENT_HASH_FIELDS:
        changed = dict(current)
        changed[field_name] = "f" * 64
        assert not evidence_is_current(attestation, changed)
