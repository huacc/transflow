"""按 P9C.1～P9C.4 验收历史纠偏、完整性、诊断双轨与真实回归。"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pymupdf
import pytest

from tests.migration.p9_qwen_translation_adapter import (
    MigrationQwenTranslationAdapter,
    migration_translation_environment_ready,
)
from transflow.adapters.ai.fixed import FixedTranslationAdapter
from transflow.adapters.filesystem.artifact_store import SharedFilesystemArtifactAdapter
from transflow.adapters.filesystem.completeness_checkpoint import (
    FilesystemCompletenessCheckpointAdapter,
)
from transflow.application.contracts import EnumeratedPage
from transflow.application.document_coordinator import DocumentCoordinator
from transflow.application.route_capability import (
    RouteCapabilityEvidence,
    RouteCapabilityGuard,
    audit_status_counts,
    load_classification_audit,
)
from transflow.application.toolbox_page_coordinator import (
    ToolboxPageCoordinator,
    ToolboxPageWork,
)
from transflow.application.translated_diagnostic import (
    DiagnosticPageInput,
    TranslatedDiagnosticMaterializer,
)
from transflow.application.translation_completeness import (
    TranslationCompletenessGate,
    adjudicate_translation_candidates,
    build_semantic_unit_map,
)
from transflow.domain.artifacts import ArtifactPayload
from transflow.domain.classification import ClassificationRoute
from transflow.domain.completeness import (
    CompletenessDisposition,
    CompletenessStatus,
    KeepSourceReason,
    SemanticUnit,
    SemanticUnitDisposition,
    SemanticUnitMap,
    TranslationCandidate,
    TranslationCompletenessDecision,
)
from transflow.domain.delivery import (
    DiagnosticStatus,
    FinalDeliveryArtifact,
    ReleaseArtifactGuard,
    ReleaseSurface,
    TranslatedDiagnosticCandidate,
)
from transflow.domain.errors import DomainContractError, ErrorCode, PortCallError
from transflow.domain.jobs import DocumentRunRequest
from transflow.domain.pages import PageOutcome
from transflow.domain.result_axes import (
    EngineeringClosure,
    ProductAcceptance,
    ResultScope,
    ThreeAxisResult,
    aggregate_results,
    project_page_result,
)
from transflow.domain.states import (
    ArtifactIntegrity,
    ArtifactProduced,
    Capability,
    Fallback,
    PagePipelineState,
    Quality,
    TranslationCoverage,
)
from transflow.domain.translation import (
    TranslatedUnit,
    TranslationBatch,
    TranslationBundle,
    TranslationUnit,
)
from transflow.pdf_kernel import ControlledFontRegistry, PageFactsExtractor
from transflow.pdf_kernel.patch import PagePatchInterpreter, ReplayPage, patch_operation_hash
from transflow.pdf_kernel.relations import (
    probe_data_bindings,
    probe_low_contrast,
    validate_data_binding,
)
from transflow.ports.translation import TranslationPort
from transflow.toolboxes.contracts import (
    PageToolbox,
    TranslationDispatch,
)
from transflow.toolboxes.leaves import (
    AnchoredBlocksToolbox,
    ContentsToolbox,
    CoverToolbox,
    EndToolbox,
    MultiFlowTextToolbox,
    SingleFlowTextToolbox,
    TableToolbox,
)
from transflow.toolboxes.leaves.ordinary_policy import load_p9_ordinary_leaf_policy
from transflow.toolboxes.leaves.policy import load_p8_toolbox_policy

REPO_ROOT = Path(__file__).resolve().parent.parent
LEDGER_PATH = REPO_ROOT / "resources" / "evidence" / "p9c" / "p9c_corrective_ledger.v1.json"
AUDIT_ROOT = (
    REPO_ROOT
    / "spikes"
    / "page_classification_engine_puncture_v1"
    / "reports"
    / "deep_audits"
    / "current_classification_20260711"
)
AUDIT_JSONL = AUDIT_ROOT / "page_audit.jsonl"
SAMPLE_ROOT = REPO_ROOT / "spikes" / "page_classification_engine_puncture_v1" / "分类结果"
P8_POLICY = REPO_ROOT / "resources" / "manifests" / "p8_toolbox_policy.json"
P9_POLICY = REPO_ROOT / "resources" / "manifests" / "p9_ordinary_leaf_policy.json"
FONT_MANIFEST = REPO_ROOT / "resources" / "manifests" / "font_manifest.json"
FONT_ID = "noto-sans-cjk-sc-regular"
P9_SUMMARY = REPO_ROOT / "output" / "pdf" / "P9_real_samples" / "P9_real_samples_summary.json"
SIX_ROUTES = (
    "cover",
    "contents",
    "end",
    "body.flow_text.multi",
    "body.table",
    "body.anchored_blocks",
)


def _sha256_file(path: Path) -> str:
    """流式计算测试输入和产物的真实 SHA-256。"""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _enumerate_page(path: Path, run_id: str, page_no: int = 1) -> EnumeratedPage:
    """通过生产 DocumentCoordinator 从真实 PDF 枚举指定页面。"""

    request = DocumentRunRequest(
        source_pdf_path=str(path.resolve()),
        source_hash=_sha256_file(path),
        source_language="en",
        target_language="zh-CN",
        config_snapshot_hash="a" * 64,
        job_id=f"job-{run_id}",
        run_id=run_id,
    )
    pages = DocumentCoordinator(PageFactsExtractor()).enumerate_pages(request)
    return pages[page_no - 1]


def _font_path() -> Path:
    """从受控字体 manifest 解析实际字体文件。"""

    return ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT).resolve(FONT_ID).path


def _toolbox(route: str) -> PageToolbox:
    """按 P8/P9 集中策略构造当前生产叶。"""

    font = _font_path()
    if route == "body.flow_text.single":
        return SingleFlowTextToolbox(load_p8_toolbox_policy(P8_POLICY), font)
    factories: dict[str, type[Any]] = {
        "cover": CoverToolbox,
        "contents": ContentsToolbox,
        "end": EndToolbox,
        "body.flow_text.multi": MultiFlowTextToolbox,
        "body.table": TableToolbox,
        "body.anchored_blocks": AnchoredBlocksToolbox,
    }
    return factories[route](load_p9_ordinary_leaf_policy(P9_POLICY), font)


def _semantic_entry(
    unit_id: str,
    ordinal: int,
    source_text: str,
    *,
    disposition: SemanticUnitDisposition = SemanticUnitDisposition.TRANSLATE,
    reason: KeepSourceReason | None = None,
    required_literals: tuple[str, ...] = (),
    owner: str = "body.flow_text.single",
) -> SemanticUnit:
    """构造一个字段完整且可复算的语义单元。"""

    return SemanticUnit(
        unit_id=unit_id,
        object_id=f"object-{unit_id}",
        container_id=f"container-{unit_id}",
        owner=owner,
        ordinal=ordinal,
        source_text=source_text,
        source_hash=hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
        required_literals=required_literals,
        disposition=disposition,
        keep_source_reason=reason,
    )


def _manual_map(entries: tuple[SemanticUnit, ...], page_no: int = 1) -> SemanticUnitMap:
    """构造用于合同故障注入的稳定 SemanticUnitMap。"""

    return SemanticUnitMap("semantic-contract-fixture", page_no, "b" * 64, entries)


def _publish_source_final(
    artifacts: SharedFilesystemArtifactAdapter,
    source_path: Path,
    artifact_id: str,
) -> FinalDeliveryArtifact:
    """把完整源 PDF 作为安全 final 发布，并诚实标记源文透传。"""

    content = source_path.read_bytes()
    content_hash = hashlib.sha256(content).hexdigest()
    reference = artifacts.put_atomic(
        ArtifactPayload(artifact_id, "application/pdf", content, content_hash),
        f"final/{artifact_id}-{content_hash}.pdf",
        "final",
    )
    artifacts.publish_final(reference)
    return FinalDeliveryArtifact(reference, True)


def _probe_diagnostic_materialization(
    source_path: Path,
    page: EnumeratedPage,
    toolbox: PageToolbox,
    template: Any,
    batch: TranslationBatch,
    semantic_map: SemanticUnitMap,
    probe_root: Path,
) -> bool:
    """用可控制完整输入筛选安全物化结构；该探针不作为真实千问结果证据。"""

    entry_by_id = {item.unit_id: item for item in semantic_map.entries}
    translations = {
        unit_id: " ".join(("诊断译文", *entry_by_id[unit_id].required_literals))
        for unit_id in semantic_map.translated_unit_ids
    }
    gate = TranslationCompletenessGate(maximum_targeted_retries=0).execute(
        semantic_map,
        batch,
        FixedTranslationAdapter(translations),
    )
    if gate.bundle is None:
        return False
    plan = toolbox.consume_translation_bundle(
        template,
        TranslationDispatch(batch=batch, bundle=gate.bundle),
    )
    artifacts = SharedFilesystemArtifactAdapter(probe_root, "run-p9c-probe")
    candidate = TranslatedDiagnosticMaterializer(
        PagePatchInterpreter(ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT)),
        artifacts,
        probe_root,
    ).materialize_page(
        source_path,
        DiagnosticPageInput(
            page.context,
            page.facts,
            plan.patch,
            semantic_map,
            gate.bundle,
            gate.decision,
        ),
    )
    return candidate.status is DiagnosticStatus.TRANSLATED_DIAGNOSTIC_READY


@dataclass(frozen=True, slots=True)
class _RealLeafEvidence:
    """聚合一条真实 Qwen 叶回归的输入、合同、诊断、final 与三轴结论。"""

    route: str
    source_path: Path
    page: EnumeratedPage
    page_input: DiagnosticPageInput
    provider_bundle_count: int
    diagnostic: TranslatedDiagnosticCandidate
    final: FinalDeliveryArtifact
    axes: ThreeAxisResult
    artifacts: SharedFilesystemArtifactAdapter


@pytest.fixture(scope="module")
def real_qwen_leaf_evidence(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[_RealLeafEvidence, ...]:
    """对 P9 六叶各执行一张真实分类页和真实千问迁移适配器。"""

    assert migration_translation_environment_ready(), "P9C 真实验收缺少迁移千问环境变量"
    summary = json.loads(P9_SUMMARY.read_text(encoding="utf-8"))
    preferred: dict[str, Path] = {}
    for item in summary["candidate_results"]:
        route = str(item["route"])
        if route in SIX_ROUTES and route not in preferred:
            preferred[route] = REPO_ROOT / str(item["relative_path"])
    assert tuple(route for route in SIX_ROUTES if route in preferred) == SIX_ROUTES
    root = tmp_path_factory.mktemp("p9c-real-qwen")
    adapter = MigrationQwenTranslationAdapter(timeout_seconds=180.0, chunk_size=48)
    evidence: list[_RealLeafEvidence] = []
    diagnostic_probe_selected = False
    for ordinal, route in enumerate(SIX_ROUTES, start=1):
        route_root = SAMPLE_ROOT.joinpath(*route.split("."))
        candidates = tuple(
            dict.fromkeys((preferred[route], *sorted(route_root.glob("*.pdf"))))
        )
        selected_run: tuple[
            Path,
            EnumeratedPage,
            PageToolbox,
            Any,
            TranslationBatch,
            SemanticUnitMap,
        ] | None = None
        fallback_run: tuple[
            Path,
            EnumeratedPage,
            PageToolbox,
            Any,
            TranslationBatch,
            SemanticUnitMap,
        ] | None = None
        for candidate_no, candidate in enumerate(candidates, start=1):
            candidate_page = _enumerate_page(
                candidate,
                f"p9c-real-{ordinal:02d}-{candidate_no:04d}",
            )
            candidate_toolbox = _toolbox(route)
            candidate_template = candidate_toolbox.prepare(
                candidate_page.context,
                candidate_page.facts,
            )
            candidate_batch = candidate_toolbox.build_translation_request(
                candidate_template
            )
            candidate_map = build_semantic_unit_map(
                candidate_template,
                candidate_batch,
                candidate_page.facts,
            )
            # 只按统一 map 覆盖事实选样，不按文件名、公司或页码绑定行为。
            if (
                candidate_batch is not None
                and candidate_map.translated_unit_ids
                and not candidate_map.unresolved_unit_ids
            ):
                current_run = (
                    candidate,
                    candidate_page,
                    candidate_toolbox,
                    candidate_template,
                    candidate_batch,
                    candidate_map,
                )
                if fallback_run is None:
                    fallback_run = current_run
                if diagnostic_probe_selected:
                    selected_run = current_run
                    break
                probe_root = root / (
                    f"probe-{ordinal:02d}-{candidate_no:04d}-{route.replace('.', '-')}"
                )
                if not _probe_diagnostic_materialization(
                    candidate,
                    candidate_page,
                    candidate_toolbox,
                    candidate_template,
                    candidate_batch,
                    candidate_map,
                    probe_root,
                ):
                    continue
                diagnostic_probe_selected = True
                selected_run = current_run
                break
        if selected_run is None:
            selected_run = fallback_run
        assert selected_run is not None, route
        source_path, page, toolbox, template, batch, semantic_map = selected_run
        gate = TranslationCompletenessGate(maximum_targeted_retries=1).execute(
            semantic_map,
            batch,
            adapter,
        )
        assert gate.provider_bundles, route
        patch = None
        if gate.bundle is not None:
            plan = toolbox.consume_translation_bundle(
                template,
                TranslationDispatch(batch=batch, bundle=gate.bundle),
            )
            patch = plan.patch
        run_root = root / f"run-{ordinal:02d}-{route.replace('.', '-')}"
        artifacts = SharedFilesystemArtifactAdapter(
            run_root,
            f"run-p9c-real-{ordinal:02d}",
        )
        final = _publish_source_final(artifacts, source_path, f"final-p9c-real-{ordinal:02d}")
        page_input = DiagnosticPageInput(
            page.context,
            page.facts,
            patch,
            semantic_map,
            gate.bundle,
            gate.decision,
        )
        diagnostic = TranslatedDiagnosticMaterializer(
            PagePatchInterpreter(ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT)),
            artifacts,
            run_root,
        ).materialize_page(source_path, page_input)
        outcome = PageOutcome(
            page_no=1,
            state=PagePipelineState.FINALIZED,
            artifact_produced=ArtifactProduced.YES,
            integrity=ArtifactIntegrity.PASS,
            translation_coverage=(
                TranslationCoverage.FULL
                if gate.decision.status is CompletenessStatus.PASS
                else TranslationCoverage.NONE
            ),
            capability=Capability.PARTIAL,
            quality=Quality.FAIL,
            fallback=Fallback.PAGE_PASSTHROUGH,
            finding_codes=("P9C_DIAGNOSTIC_QUALITY_FAIL",),
        )
        axes = project_page_result(
            f"{route}-p1",
            outcome,
            final_available=True,
            completeness=gate.decision,
            diagnostic=diagnostic,
        )
        evidence.append(
            _RealLeafEvidence(
                route,
                source_path,
                page,
                page_input,
                len(gate.provider_bundles),
                diagnostic,
                final,
                axes,
                artifacts,
            )
        )
    assert diagnostic_probe_selected
    return tuple(evidence)


class _TimeoutPort:
    """通过稳定 AI_TIMEOUT 故障注入验证无完整译文出口。"""

    def translate(self, batch: TranslationBatch) -> TranslationBundle:
        """对真实 Batch 返回结构化超时，而非伪造译文。"""

        raise PortCallError(ErrorCode.AI_TIMEOUT, True, f"timeout:{batch.batch_id}")


class _InvalidContractPort:
    """通过合同异常验证非法 Provider 响应不会进入布局。"""

    def translate(self, batch: TranslationBatch) -> TranslationBundle:
        """显式报告 Provider 合同违例。"""

        raise DomainContractError(ErrorCode.INVALID_TRANSLATION_BUNDLE, batch.batch_id)


class _PlaceholderPort:
    """返回身份有效但内容为占位符的真实 Bundle，供深层门禁拒绝。"""

    def translate(self, batch: TranslationBatch) -> TranslationBundle:
        """严格按 Batch 身份返回可构造但不可通过完整性的内容。"""

        return TranslationBundle.from_batch(
            batch,
            tuple(TranslatedUnit(item.unit_id, "[待翻译]") for item in batch.units),
        )


class _ExplodingPort:
    """若恢复后发生重复翻译则立即让测试失败。"""

    def translate(self, batch: TranslationBatch) -> TranslationBundle:
        """禁止恢复路径再次调用 TranslationPort。"""

        raise AssertionError(f"恢复后不应重复翻译:{batch.batch_id}")


class _LayoutCountingToolbox:
    """代理真实叶并统计四个布局阶段是否被完整性门禁调用。"""

    def __init__(self, wrapped: PageToolbox) -> None:
        """绑定真实叶并初始化布局调用计数。"""

        self._wrapped = wrapped
        self.layout_calls = 0

    @property
    def descriptor(self) -> Any:
        """返回真实叶描述符。"""

        return self._wrapped.descriptor

    def prepare(self, context: Any, facts: Any) -> Any:
        """转发 prepare。"""

        return self._wrapped.prepare(context, facts)

    def build_translation_request(self, template: Any) -> Any:
        """转发 Batch 构建。"""

        return self._wrapped.build_translation_request(template)

    def consume_translation_bundle(self, template: Any, dispatch: Any) -> Any:
        """统计并转发布局计划构建。"""

        self.layout_calls += 1
        return self._wrapped.consume_translation_bundle(template, dispatch)

    def render(self, context: Any, facts: Any, plan: Any) -> Any:
        """统计并转发渲染。"""

        self.layout_calls += 1
        return self._wrapped.render(context, facts, plan)

    def judge(self, candidate: Any) -> Any:
        """统计并转发裁决。"""

        self.layout_calls += 1
        return self._wrapped.judge(candidate)

    def repair(self, candidate: Any, judgement: Any) -> Any:
        """统计并转发修复。"""

        self.layout_calls += 1
        return self._wrapped.repair(candidate, judgement)


class _FirstFailureRealQwenPort:
    """包装真实千问，仅在首次响应注入一个可定位占位符。"""

    def __init__(self) -> None:
        """初始化真实迁移 Adapter 和请求身份记录。"""

        self._real = MigrationQwenTranslationAdapter(timeout_seconds=180.0, chunk_size=48)
        self.requested_ids: list[tuple[str, ...]] = []

    def translate(self, batch: TranslationBatch) -> TranslationBundle:
        """取得真实模型结果，首次只替换最后一个 unit 以触发定向重译。"""

        self.requested_ids.append(batch.ordered_unit_ids)
        bundle = self._real.translate(batch)
        if len(self.requested_ids) != 1:
            return bundle
        units = list(bundle.units)
        units[-1] = TranslatedUnit(units[-1].unit_id, "[待翻译]")
        return TranslationBundle.from_batch(batch, tuple(units))


@pytest.mark.contract
def test_p9c_1_t01_corrective_inventory_is_complete_unique_and_rehashable() -> None:
    """P9C.1-T01：指定来源零缺失、零重复且 SHA-256 可重算率为 100%。"""

    ledger = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
    inventory = ledger["source_inventory"]
    paths = [str(item["path"]) for item in inventory]
    assert len(paths) == len(set(paths))
    assert sum(item["evidence_type"] == "historical_report" for item in inventory) == 5
    assert sum(item["evidence_type"] == "toolbox_experience" for item in inventory) == 10
    assert sum(item["evidence_type"] == "p9_real_sample_chain" for item in inventory) == 4
    assert all((REPO_ROOT / item["path"]).is_file() for item in inventory)
    assert all(
        _sha256_file(REPO_ROOT / item["path"]) == item["current_sha256"]
        for item in inventory
    )


@pytest.mark.migration
def test_p9c_1_t02_current_classification_audit_recomputes_457_scope() -> None:
    """P9C.1-T02：从逐页深审计重算 457=424+30+3，且不冒充原 709 页。"""

    rows = load_classification_audit(AUDIT_JSONL)
    counts = audit_status_counts(rows)
    ledger = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
    fact = ledger["historical_facts"]["classification_current_audit"]
    assert len(rows) == 457
    assert counts == {"CORRECT": 424, "ERROR": 30, "AMBIGUOUS": 3}
    assert fact["total"] == 457 and fact["original_scope_total"] == 709
    assert fact["removed_original_table_pages"] == 252


@pytest.mark.migration
def test_p9c_1_t03_p5_and_p9_historical_facts_are_parsed_without_regating() -> None:
    """P9C.1-T03：22 页 P5 与 12 个真实 Qwen 候选历史事实一致且不重验旧 Gate。"""

    ledger = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
    facts = ledger["historical_facts"]
    assert facts["p5_migration_baseline"] == {
        "anonymous_page_count": 22,
        "historical_gate_reexecuted": False,
    }
    qwen = facts["p9_real_qwen"]
    assert (qwen["candidate_count"], qwen["accepted"], qwen["fallback"]) == (12, 1, 11)
    assert qwen["source_passthrough_leaf_count"] == 6
    assert qwen["historical_gate_reexecuted"] is False


@pytest.mark.contract
def test_p9c_1_t04_all_historical_contract_conflicts_have_precedence() -> None:
    """P9C.1-T04：候选、完整性、路由、字形绑定和三轴冲突均有采用合同与生效 Gate。"""

    ledger = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
    register = ledger["contradiction_register"]
    assert {item["id"] for item in register} == {
        "candidate-semantics",
        "translation-completeness",
        "classification-route-mismatch",
        "glyph-data-binding",
        "engineering-vs-product-pass",
    }
    assert all(item["adopted_contract"] and item["reason"] for item in register)
    assert all(item["effective_gate"] == "G9C" for item in register)


@pytest.mark.contract
def test_p9c_1_t05_impact_matrix_has_five_categories_and_forward_owners() -> None:
    """P9C.1-T05：五类问题全部具备 owner、阶段、最低测试和禁止旁路。"""

    ledger = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
    matrix = ledger["impact_matrix"]
    assert {item["category"] for item in matrix} == {
        "CONTRACT_GAP",
        "QUALITY_GAP",
        "CLASSIFICATION_GAP",
        "EVIDENCE_GAP",
        "IMPLEMENTATION_DEFECT",
    }
    assert all(
        item["forward_owner"]
        and item["affected_stages"]
        and item["minimum_tests"]
        and item["prohibited_bypass"]
        for item in matrix
    )


@pytest.mark.regression
def test_p9c_1_t06_historical_sources_match_stage_start_hashes() -> None:
    """P9C.1-T06：P5–P9 历史来源变化数和伪重验数均为零。"""

    ledger = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
    changed = [
        item["path"]
        for item in ledger["source_inventory"]
        if _sha256_file(REPO_ROOT / item["path"]) != item["anchor_sha256"]
    ]
    assert changed == []
    assert ledger["historical_change_count"] == 0
    assert ledger["historical_gate_reexecution_count"] == 0


@pytest.mark.contract
def test_p9c_2_t01_semantic_map_and_decision_round_trip_all_unit_kinds() -> None:
    """P9C.2-T01：正文、列表、cell、anchored、标签和 KEEP_SOURCE 往返保持 100%。"""

    entries = (
        _semantic_entry("body", 0, "Annual performance"),
        _semantic_entry("list", 1, "1. Revenue", required_literals=("1.",)),
        _semantic_entry("cell", 2, "Revenue 100", required_literals=("100",), owner="body.table"),
        _semantic_entry("anchor", 3, "Business segment", owner="body.anchored_blocks"),
        _semantic_entry("label", 4, "Legend 20%", required_literals=("20%",), owner="body.chart"),
        _semantic_entry(
            "code",
            5,
            "FY2024",
            disposition=SemanticUnitDisposition.KEEP_SOURCE,
            reason=KeepSourceReason.CODE_OR_ACRONYM,
        ),
    )
    semantic_map = _manual_map(entries)
    candidates = (
        TranslationCandidate("body", "年度表现"),
        TranslationCandidate("list", "1. 收入"),
        TranslationCandidate("cell", "收入 100"),
        TranslationCandidate("anchor", "业务分部"),
        TranslationCandidate("label", "图例 20%"),
    )
    decision = adjudicate_translation_candidates(semantic_map, candidates)
    restored_map = SemanticUnitMap.from_dict(semantic_map.to_dict())
    restored_decision = TranslationCompletenessDecision.from_dict(decision.to_dict())
    assert restored_map == semantic_map and restored_map.map_hash == semantic_map.map_hash
    assert restored_decision == decision and decision.status is CompletenessStatus.PASS


@pytest.mark.migration
def test_p9c_2_t02_real_single_multi_table_anchor_maps_cover_native_text() -> None:
    """P9C.2-T02：四类真实分类页原生文字唯一归属，图片内部文字不进入 map。"""

    specs = (
        ("body.flow_text.single", SAMPLE_ROOT / "body" / "flow_text" / "single"),
        ("body.flow_text.multi", SAMPLE_ROOT / "body" / "flow_text" / "multi"),
        ("body.table", SAMPLE_ROOT / "body" / "table"),
        ("body.anchored_blocks", SAMPLE_ROOT / "body" / "anchored_blocks"),
    )
    maps: list[SemanticUnitMap] = []
    for ordinal, (route, root) in enumerate(specs, start=1):
        semantic_map: SemanticUnitMap | None = None
        for candidate_no, path in enumerate(sorted(root.glob("*.pdf")), start=1):
            page = _enumerate_page(path, f"p9c-map-{ordinal:02d}-{candidate_no:04d}")
            toolbox = _toolbox(route)
            template = toolbox.prepare(page.context, page.facts)
            batch = toolbox.build_translation_request(template)
            candidate_map = build_semantic_unit_map(template, batch, page.facts)
            # 样本选择只依赖冻结后的结构覆盖事实，不依赖文件名、公司或页码身份。
            if not candidate_map.unresolved_unit_ids:
                semantic_map = candidate_map
                break
        assert semantic_map is not None
        assert semantic_map.entries
        assert not semantic_map.unresolved_unit_ids
        assert len({item.object_id for item in semantic_map.entries}) == len(semantic_map.entries)
        image_ids = {item.object_id for item in page.facts.image_objects}
        assert image_ids.isdisjoint(item.object_id for item in semantic_map.entries)
        maps.append(semantic_map)
    assert len(maps) == 4


@pytest.mark.fault_injection
def test_p9c_2_t03_invalid_bundle_content_never_enters_layout_or_full() -> None:
    """P9C.2-T03：九类不完整响应全部 FAIL，正常布局和误报 FULL 均为零。"""

    source = "Revenue increased substantially by 10% in FY2024."
    semantic_map = _manual_map(
        (
            _semantic_entry(
                "u1",
                0,
                source,
                required_literals=("10%", "FY2024"),
            ),
        )
    )
    valid = TranslationCandidate("u1", "收入在 FY2024 增长 10%")
    invalid_sets = (
        (),
        (valid, valid),
        (valid, TranslationCandidate("extra", "额外内容")),
        (TranslationCandidate("u1", ""),),
        (TranslationCandidate("u1", "[待翻译] 10% FY2024"),),
        (TranslationCandidate("u1", "Error: timeout 10% FY2024"),),
        (TranslationCandidate("u1", source),),
        (TranslationCandidate("u1", "收入增长"),),
        (TranslationCandidate("u1", f"{source} Today"),),
    )
    decisions = tuple(
        adjudicate_translation_candidates(semantic_map, candidates)
        for candidates in invalid_sets
    )
    assert all(item.status is CompletenessStatus.FAIL for item in decisions)
    sample = sorted((SAMPLE_ROOT / "body" / "flow_text" / "single").glob("*.pdf"))[0]
    page = _enumerate_page(sample, "p9c-invalid-layout")
    counting = _LayoutCountingToolbox(_toolbox("body.flow_text.single"))
    result = ToolboxPageCoordinator(_PlaceholderPort()).execute(
        ToolboxPageWork(page.context, page.facts, counting)
    )
    assert counting.layout_calls == 0
    assert result.outcome.translation_coverage is TranslationCoverage.NONE


@pytest.mark.contract
def test_p9c_2_t04_keep_source_reasons_and_required_literals_are_auditable() -> None:
    """P9C.2-T04：数字、代码、缩写、专名可显式保留，required literal 仍强校验。"""

    entries = (
        _semantic_entry(
            "num",
            0,
            "100%",
            disposition=SemanticUnitDisposition.KEEP_SOURCE,
            reason=KeepSourceReason.NUMERIC_OR_SYMBOLIC_LITERAL,
        ),
        _semantic_entry(
            "code",
            1,
            "A-101",
            disposition=SemanticUnitDisposition.KEEP_SOURCE,
            reason=KeepSourceReason.CODE_OR_ACRONYM,
        ),
        _semantic_entry(
            "acro",
            2,
            "EBITDA",
            disposition=SemanticUnitDisposition.KEEP_SOURCE,
            reason=KeepSourceReason.CODE_OR_ACRONYM,
        ),
        _semantic_entry(
            "name",
            3,
            "MerqFin",
            disposition=SemanticUnitDisposition.KEEP_SOURCE,
            reason=KeepSourceReason.EXPLICIT_PROPER_NAME,
        ),
        _semantic_entry("translated", 4, "Revenue USD 100", required_literals=("USD", "100")),
    )
    decision = adjudicate_translation_candidates(
        _manual_map(entries),
        (TranslationCandidate("translated", "收入 USD 100"),),
    )
    assert decision.status is CompletenessStatus.PASS
    assert sum(
        item.disposition is CompletenessDisposition.KEEP_SOURCE
        for item in decision.dispositions
    ) == 4
    with pytest.raises(DomainContractError):
        _semantic_entry(
            "invalid",
            0,
            "ABC",
            disposition=SemanticUnitDisposition.KEEP_SOURCE,
        )


@pytest.mark.migration
def test_p9c_2_t05_real_qwen_targeted_retry_only_resends_failed_units() -> None:
    """P9C.2-T05：真实千问首次单元失败后仅重发失败 ID，并对完整 map 复判。"""

    assert migration_translation_environment_ready(), "P9C 定向重译缺少迁移千问环境变量"
    units = (
        TranslationUnit("retry-1", 1, 0, "Revenue increased by 10%.", "region-1"),
        TranslationUnit("retry-2", 1, 1, "Operating profit rose.", "region-2"),
    )
    batch = TranslationBatch("batch-targeted-retry", "en", "zh-CN", units)
    semantic_map = _manual_map(
        (
            _semantic_entry("retry-1", 0, units[0].source_text, required_literals=("10%",)),
            _semantic_entry("retry-2", 1, units[1].source_text),
        )
    )
    port = _FirstFailureRealQwenPort()
    result = TranslationCompletenessGate(maximum_targeted_retries=1).execute(
        semantic_map,
        batch,
        port,
    )
    assert result.decision.status is CompletenessStatus.PASS
    assert result.bundle is not None and len(result.provider_bundles) == 2
    assert port.requested_ids[0] == ("retry-1", "retry-2")
    assert port.requested_ids[1] == ("retry-2",)
    assert result.decision.bundle_hash == hashlib.sha256(
        json.dumps(
            {
                "batch_id": result.bundle.batch_id,
                "requested_unit_ids": list(result.bundle.requested_unit_ids),
                "units": [
                    {"unit_id": item.unit_id, "translated_text": item.translated_text}
                    for item in result.bundle.units
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


@pytest.mark.fault_injection
def test_p9c_2_t06_completeness_checkpoint_recovers_without_retranslation(
    tmp_path: Path,
) -> None:
    """P9C.2-T06：map/Bundle/Decision 哈希闭合，恢复后不重译且 FAIL 不进布局。"""

    unit = TranslationUnit("checkpoint-u1", 1, 0, "Revenue", "region-1")
    batch = TranslationBatch("batch-checkpoint", "en", "zh-CN", (unit,))
    semantic_map = _manual_map((_semantic_entry("checkpoint-u1", 0, "Revenue"),))
    store = FilesystemCompletenessCheckpointAdapter(tmp_path / "run")
    first = TranslationCompletenessGate().execute(
        semantic_map,
        batch,
        FixedTranslationAdapter({"checkpoint-u1": "收入"}),
        store,
    )
    restored = TranslationCompletenessGate().execute(
        semantic_map,
        batch,
        _ExplodingPort(),
        store,
    )
    assert first.decision.status is CompletenessStatus.PASS
    assert restored.resumed and restored.request_batches == ()
    assert restored.checkpoint().checkpoint_hash == first.checkpoint().checkpoint_hash
    failing = TranslationCompletenessGate().execute(
        semantic_map,
        batch,
        _PlaceholderPort(),
    )
    assert failing.decision.status is CompletenessStatus.FAIL and failing.bundle is None


@pytest.mark.migration
def test_p9c_3_t01_real_qwen_quality_fail_has_safe_final_and_diagnostic(
    real_qwen_leaf_evidence: tuple[_RealLeafEvidence, ...],
) -> None:
    """P9C.3-T01：真实千问完整译文在 Quality FAIL 下同时保留安全 final 与可见诊断。"""

    ready = next(
        item
        for item in real_qwen_leaf_evidence
        if item.diagnostic.status is DiagnosticStatus.TRANSLATED_DIAGNOSTIC_READY
    )
    assert ready.provider_bundle_count >= 1
    assert ready.final.artifact.label == "final" and ready.final.source_passthrough
    assert ready.diagnostic.artifact is not None
    assert ready.diagnostic.artifact.content_hash != _sha256_file(ready.source_path)
    assert ready.diagnostic.evidence.materialized_unit_count > 0


@pytest.mark.fault_injection
def test_p9c_3_t02_provider_failures_have_final_but_no_diagnostic(tmp_path: Path) -> None:
    """P9C.3-T02：超时、非法合同和最终缺 unit 均为 NO_TRANSLATED_CANDIDATE。"""

    source = SAMPLE_ROOT / "end" / "S2P0120.pdf"
    statuses: list[DiagnosticStatus] = []
    ports: tuple[TranslationPort, ...] = (
        _TimeoutPort(),
        _InvalidContractPort(),
        _PlaceholderPort(),
    )
    for ordinal, port in enumerate(ports):
        page = _enumerate_page(source, f"p9c-no-candidate-{ordinal}")
        toolbox = _toolbox("end")
        template = toolbox.prepare(page.context, page.facts)
        batch = toolbox.build_translation_request(template)
        assert batch is not None
        semantic_map = build_semantic_unit_map(template, batch, page.facts)
        gate = TranslationCompletenessGate(maximum_targeted_retries=0).execute(
            semantic_map,
            batch,
            port,
        )
        run_root = tmp_path / f"run-{ordinal}"
        artifacts = SharedFilesystemArtifactAdapter(run_root, f"run-p9c-no-{ordinal}")
        final = _publish_source_final(artifacts, source, f"final-p9c-no-{ordinal}")
        candidate = TranslatedDiagnosticMaterializer(
            PagePatchInterpreter(ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT)),
            artifacts,
            run_root,
        ).materialize_page(
            source,
            DiagnosticPageInput(page.context, page.facts, None, semantic_map, None, gate.decision),
        )
        assert final.artifact.content_hash == _sha256_file(source)
        assert candidate.artifact is None
        statuses.append(candidate.status)
    assert statuses == [DiagnosticStatus.NO_TRANSLATED_CANDIDATE] * 3


@pytest.mark.fault_injection
def test_p9c_3_t03_source_partial_and_placeholder_candidates_are_rejected(
    real_qwen_leaf_evidence: tuple[_RealLeafEvidence, ...],
    tmp_path: Path,
) -> None:
    """P9C.3-T03：源副本、局部替换和占位 PDF 均不能登记为诊断候选。"""

    ready = next(
        item
        for item in real_qwen_leaf_evidence
        if item.diagnostic.status is DiagnosticStatus.TRANSLATED_DIAGNOSTIC_READY
        and item.page_input.patch is not None
        and len(item.page_input.patch.operations) > 1
    )
    ready_patch = ready.page_input.patch
    assert ready_patch is not None
    materializer = TranslatedDiagnosticMaterializer(
        PagePatchInterpreter(ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT)),
        ready.artifacts,
        tmp_path / "validation",
    )
    source_copy = materializer.validate_and_register(
        ready.source_path,
        ready.source_path,
        (ready.page_input,),
    )
    partial_path = tmp_path / "partial.pdf"
    shutil.copyfile(ready.source_path, partial_path)
    partial_patch = replace(
        ready_patch,
        operations=(ready_patch.operations[0],),
    )
    PagePatchInterpreter(ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT)).replay_document(
        partial_path,
        (
            ReplayPage(
                ready.page.context,
                ready.page.facts,
                partial_patch,
                partial_patch.owner,
            ),
        ),
    )
    partial = materializer.validate_and_register(
        ready.source_path,
        partial_path,
        (ready.page_input,),
    )
    placeholder_path = tmp_path / "placeholder.pdf"
    shutil.copyfile(ready.source_path, placeholder_path)
    with pymupdf.open(placeholder_path) as document:
        document[0].insert_text((20, 20), "[PLACEHOLDER]", fontsize=8)
        document.saveIncr()
    placeholder = materializer.validate_and_register(
        ready.source_path,
        placeholder_path,
        (ready.page_input,),
    )
    assert all(
        item.status is DiagnosticStatus.DIAGNOSTIC_MATERIALIZATION_FAILED
        and item.artifact is None
        for item in (source_copy, partial, placeholder)
    )


@pytest.mark.integration
def test_p9c_3_t04_diagnostic_units_fonts_bboxes_and_geometry_are_real(
    real_qwen_leaf_evidence: tuple[_RealLeafEvidence, ...],
) -> None:
    """P9C.3-T04：真实诊断逐 unit 可提取，字体/bbox 和页框旋转证据完整。"""

    ready = next(
        item
        for item in real_qwen_leaf_evidence
        if item.diagnostic.status is DiagnosticStatus.TRANSLATED_DIAGNOSTIC_READY
    )
    evidence = ready.diagnostic.evidence
    assert evidence.expected_unit_count == evidence.materialized_unit_count
    assert evidence.missing_unit_ids == ()
    assert evidence.geometry_preserved
    assert evidence.owner_violation_count == evidence.protected_violation_count == 0
    assert all(item.extracted for item in evidence.units)
    assert all(item.font_names and item.bboxes for item in evidence.units)


@pytest.mark.contract
def test_p9c_3_t05_diagnostic_is_rejected_by_all_release_surfaces(
    real_qwen_leaf_evidence: tuple[_RealLeafEvidence, ...],
) -> None:
    """P9C.3-T05：diagnostic 进入 Patch/preview/download/final/target 的次数均为零。"""

    ready = next(
        item
        for item in real_qwen_leaf_evidence
        if item.diagnostic.status is DiagnosticStatus.TRANSLATED_DIAGNOSTIC_READY
    )
    reference = ready.diagnostic.artifact
    assert reference is not None
    for surface in ReleaseSurface:
        with pytest.raises(DomainContractError) as captured:
            ReleaseArtifactGuard.assert_allowed(reference, surface)
        assert captured.value.code is ErrorCode.DIAGNOSTIC_RELEASE_FORBIDDEN
    with pytest.raises(DomainContractError):
        ready.artifacts.publish_final(reference)
    assert ready.artifacts.published_final() == ready.final.artifact


def _create_two_page_pdf(path: Path) -> Path:
    """创建含两页真实原生英文文字的整本 PDF 输入。"""

    document = pymupdf.open()
    for index, text in enumerate(("Revenue increased 10%", "Operating profit 20"), start=1):
        page = document.new_page(width=420, height=600)
        page.insert_textbox(
            pymupdf.Rect(60, 120, 360, 220),
            f"{index}. {text}",
            fontname="helv",
            fontsize=12,
        )
    document.save(path)
    document.close()
    return path


@pytest.mark.integration
def test_p9c_3_t06_document_diagnostic_requires_every_page_complete(tmp_path: Path) -> None:
    """P9C.3-T06：全页完整可组装整本诊断，缺一页时只保留安全完整 final。"""

    source = _create_two_page_pdf(tmp_path / "two-page.pdf")
    request = DocumentRunRequest(
        source_pdf_path=str(source.resolve()),
        source_hash=_sha256_file(source),
        source_language="en",
        target_language="zh-CN",
        config_snapshot_hash="c" * 64,
        job_id="job-p9c-document",
        run_id="run-p9c-document",
    )
    pages = DocumentCoordinator(PageFactsExtractor()).enumerate_pages(request)
    page_inputs: list[DiagnosticPageInput] = []
    for page in pages:
        toolbox = _toolbox("body.flow_text.single")
        template = toolbox.prepare(page.context, page.facts)
        batch = toolbox.build_translation_request(template)
        assert batch is not None
        mapping = {
            unit.unit_id: (
                "1. 收入增长 10%"
                if page.context.page_no == 1
                else "2. 营业利润 20"
            )
            for unit in batch.units
        }
        semantic_map = build_semantic_unit_map(template, batch, page.facts)
        gate = TranslationCompletenessGate().execute(
            semantic_map,
            batch,
            FixedTranslationAdapter(mapping),
        )
        assert gate.bundle is not None
        plan = toolbox.consume_translation_bundle(
            template,
            TranslationDispatch(batch=batch, bundle=gate.bundle),
        )
        page_inputs.append(
            DiagnosticPageInput(
                page.context,
                page.facts,
                plan.patch,
                semantic_map,
                gate.bundle,
                gate.decision,
            )
        )
    run_root = tmp_path / "run"
    artifacts = SharedFilesystemArtifactAdapter(run_root, "run-p9c-document")
    final = _publish_source_final(artifacts, source, "final-p9c-document")
    materializer = TranslatedDiagnosticMaterializer(
        PagePatchInterpreter(ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT)),
        artifacts,
        run_root,
    )
    complete = materializer.materialize_document(source, tuple(page_inputs))
    failed_decision = adjudicate_translation_candidates(page_inputs[1].semantic_map, ())
    incomplete_inputs = (
        page_inputs[0],
        DiagnosticPageInput(
            page_inputs[1].context,
            page_inputs[1].facts,
            None,
            page_inputs[1].semantic_map,
            None,
            failed_decision,
        ),
    )
    incomplete = materializer.materialize_document(source, incomplete_inputs)
    assert complete.status is DiagnosticStatus.TRANSLATED_DIAGNOSTIC_READY
    assert incomplete.status is DiagnosticStatus.NO_TRANSLATED_CANDIDATE
    assert final.artifact.content_hash == _sha256_file(source)
    with pymupdf.open(source) as document:
        assert document.page_count == 2


@pytest.mark.fault_injection
def test_p9c_3_t07_font_owner_and_write_failures_do_not_publish_candidates(
    real_qwen_leaf_evidence: tuple[_RealLeafEvidence, ...],
    tmp_path: Path,
) -> None:
    """P9C.3-T07：字体、owner/protected 和写入故障均记录失败且不影响安全 final。"""

    ready = next(
        item
        for item in real_qwen_leaf_evidence
        if item.diagnostic.status is DiagnosticStatus.TRANSLATED_DIAGNOSTIC_READY
        and item.page_input.patch is not None
    )
    ready_patch = ready.page_input.patch
    assert ready_patch is not None
    operation = ready_patch.operations[0]
    operation_rect = operation.rect
    operation_text = operation.replacement_text
    operation_font_size = operation.font_size
    assert operation_rect is not None
    assert operation_text is not None
    assert operation_font_size is not None
    missing_font_operation = replace(
        operation,
        font_id="unregistered-font",
        payload_hash=patch_operation_hash(
            owner=str(operation.owner),
            target_object_ids=operation.target_object_ids,
            rect=operation_rect,
            replacement_text=operation_text,
            font_id="unregistered-font",
            font_size=operation_font_size,
        ),
    )
    missing_font_patch = replace(
        ready_patch,
        operations=(missing_font_operation, *ready_patch.operations[1:]),
    )
    foreign_operation = replace(
        operation,
        target_object_ids=("foreign-protected-object",),
        payload_hash=patch_operation_hash(
            owner=str(operation.owner),
            target_object_ids=("foreign-protected-object",),
            rect=operation_rect,
            replacement_text=operation_text,
            font_id=str(operation.font_id),
            font_size=operation_font_size,
        ),
    )
    foreign_patch = replace(
        ready_patch,
        operations=(foreign_operation, *ready_patch.operations[1:]),
    )
    materializer = TranslatedDiagnosticMaterializer(
        PagePatchInterpreter(ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT)),
        ready.artifacts,
        tmp_path / "faults",
    )
    font_failed = materializer.materialize_page(
        ready.source_path,
        replace(ready.page_input, patch=missing_font_patch),
    )
    owner_failed = materializer.materialize_page(
        ready.source_path,
        replace(ready.page_input, patch=foreign_patch),
    )
    write_failed = materializer.materialize_page(
        ready.source_path,
        ready.page_input,
        inject_write_failure=True,
    )
    assert all(
        item.status is DiagnosticStatus.DIAGNOSTIC_MATERIALIZATION_FAILED
        and item.artifact is None
        for item in (font_failed, owner_failed, write_failed)
    )
    assert ready.artifacts.published_final() == ready.final.artifact


@pytest.mark.migration
def test_p9c_4_t01_full_457_audit_ingestion_keeps_p5_baseline_separate() -> None:
    """P9C.4-T01：457 条导入覆盖 100%，与 P5 的 22 页并列而不互相覆盖。"""

    rows = load_classification_audit(AUDIT_JSONL)
    counts = audit_status_counts(rows)
    ledger = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
    assert len(rows) == 457 and counts == {"CORRECT": 424, "ERROR": 30, "AMBIGUOUS": 3}
    assert ledger["historical_facts"]["p5_migration_baseline"]["anonymous_page_count"] == 22


@pytest.mark.migration
def test_p9c_4_t02_real_misroutes_fallback_without_runtime_route_mutation() -> None:
    """P9C.4-T02：真实错路由/歧义页形成 ROUTE_CAPABILITY_MISMATCH 且无禁止旁路。"""

    rows = {row["sample_id"]: row for row in load_classification_audit(AUDIT_JSONL)}
    sample_ids = ("S2P0060", "S2P0608", "S2P0446", "S2P0213")
    guard = RouteCapabilityGuard()
    findings = []
    for ordinal, sample_id in enumerate(sample_ids, start=1):
        row = rows[sample_id]
        route_parts = str(row["current_leaf"]).split("/")
        path = SAMPLE_ROOT.joinpath(*route_parts, f"{sample_id}.pdf")
        page = _enumerate_page(path, f"p9c-mismatch-{ordinal}")
        current_route = str(row["current_leaf"]).replace("/", ".")
        entries = tuple(
            _semantic_entry(
                f"{sample_id}-{index}",
                index,
                text.text,
                owner=current_route,
            )
            for index, text in enumerate(page.facts.text_spans)
            if text.text.strip()
        )
        semantic_map = SemanticUnitMap(
            f"semantic-{sample_id}",
            1,
            page.context.source_hash,
            entries,
        )
        current_leaf = str(row["current_leaf"])
        required = row["suggested_leaf"] or next(
            candidate
            for candidate in row["candidate_leaves"]
            if candidate != current_leaf
        )
        evidence = RouteCapabilityEvidence(
            f"audit-{sample_id}",
            str(required).replace("/", "."),
            str(row["reason_code"]),
            str(row["audit_status"]),
        )
        route_evidence_id = f"classification-{sample_id}"
        classification_route = ClassificationRoute(
            current_route,
            0.75,
            (route_evidence_id,),
        )
        finding = guard.evaluate(
            current_route,
            page.facts,
            semantic_map,
            evidence,
            classification_route,
        )
        assert finding is not None and finding.code == ErrorCode.ROUTE_CAPABILITY_MISMATCH.value
        assert finding.route_evidence_ids == (route_evidence_id,)
        assert finding.failure_stage == "SEMANTIC_UNIT_MAP"
        assert guard.fallback_outcome(finding).fallback is Fallback.PAGE_PASSTHROUGH
        findings.append(finding)
    assert len(findings) == 4
    assert guard.forbidden_operation_counts == {
        "catalog_writes": 0,
        "cross_leaf_private_calls": 0,
        "route_writes": 0,
    }


@pytest.mark.migration
def test_p9c_4_t03_six_real_qwen_leaves_have_bundle_decision_final_and_axes(
    real_qwen_leaf_evidence: tuple[_RealLeafEvidence, ...],
) -> None:
    """P9C.4-T03：六叶真实千问均有 Provider Bundle、Decision、final 与三轴结论。"""

    assert {item.route for item in real_qwen_leaf_evidence} == set(SIX_ROUTES)
    assert all(item.provider_bundle_count >= 1 for item in real_qwen_leaf_evidence)
    assert all(item.page_input.decision.decision_hash for item in real_qwen_leaf_evidence)
    assert all(item.final.artifact.label == "final" for item in real_qwen_leaf_evidence)
    assert all(
        item.axes.engineering_closure is EngineeringClosure.PASS
        for item in real_qwen_leaf_evidence
    )
    assert all(
        item.axes.product_acceptance is not ProductAcceptance.PASS
        for item in real_qwen_leaf_evidence
    )
    document = aggregate_results(
        ResultScope.DOCUMENT,
        "p9c-six-leaf-document",
        tuple(item.axes for item in real_qwen_leaf_evidence),
    )
    stage = aggregate_results(ResultScope.STAGE, "G9C", (document,))
    assert stage.engineering_closure is EngineeringClosure.PASS
    assert stage.product_acceptance is ProductAcceptance.FAIL


@pytest.mark.integration
def test_p9c_4_t04_actual_cjk_latin_rotation_and_controlled_font_probes(
    tmp_path: Path,
) -> None:
    """P9C.4-T04：CJK、Latin、旋转和受控回退均有实际字体/bbox/提取证据。"""

    path = tmp_path / "glyph-probes.pdf"
    font_path = _font_path()
    document = pymupdf.open()
    latin = document.new_page(width=420, height=600)
    latin.insert_text((60, 120), "Revenue 2024", fontname="helv", fontsize=12)
    cjk = document.new_page(width=420, height=600)
    cjk.insert_font(fontname="TFControlled", fontfile=str(font_path))
    cjk.insert_text((60, 120), "中文，标点。", fontname="TFControlled", fontsize=12)
    rotated = document.new_page(width=420, height=600)
    rotated.insert_text((120, 300), "ROTATED", fontname="helv", fontsize=12, rotate=90)
    fallback = document.new_page(width=420, height=600)
    fallback.insert_font(fontname="TFFallback", fontfile=str(font_path))
    fallback.insert_text((60, 120), "Latin 与中文 fallback", fontname="TFFallback", fontsize=12)
    document.save(path)
    document.close()
    facts = PageFactsExtractor().extract_all(path, _sha256_file(path))
    evidence = {
        "latin": facts[0].text_spans,
        "cjk": facts[1].text_spans,
        "rotated": facts[2].text_spans,
        "controlled_fallback": facts[3].text_spans,
    }
    probe = ControlledFontRegistry(FONT_MANIFEST, REPO_ROOT).probe(
        FONT_ID,
        "中文，标点。Revenue",
    )
    assert all(
        rows and all(row.font_name and row.bbox for row in rows)
        for rows in evidence.values()
    )
    assert facts[2].text_spans[0].bbox[3] - facts[2].text_spans[0].bbox[1] > 20
    assert probe.registered and probe.integrity_passed and probe.loadable
    assert probe.missing_codepoints == ()


@pytest.mark.integration
def test_p9c_4_t05_actual_data_binding_and_low_contrast_are_hard_gated(
    tmp_path: Path,
) -> None:
    """P9C.4-T05：图例/数字/单位错绑被拒绝，低对比度保持源颜色并诚实降级。"""

    path = tmp_path / "relations.pdf"
    document = pymupdf.open()
    page = document.new_page(width=500, height=500)
    page.draw_rect(pymupdf.Rect(40, 80, 180, 220), color=(0, 0, 0), fill=(0.8, 0.9, 1))
    page.draw_rect(pymupdf.Rect(300, 80, 440, 220), color=(0, 0, 0), fill=(1, 0.9, 0.8))
    page.insert_text((60, 120), "Revenue 100%", fontsize=12)
    page.insert_text((320, 120), "Cost 50%", fontsize=12)
    page.insert_text((60, 300), "Low contrast 20%", fontsize=12, color=(0.85, 0.85, 0.85))
    document.save(path)
    document.close()
    source_hash = _sha256_file(path)
    facts = PageFactsExtractor().extract_all(path, source_hash)[0]
    bindings = probe_data_bindings(facts)
    assert len(bindings) >= 2
    assert all(
        validate_data_binding(facts, item.text_object_id, item.visual_object_id) == item
        for item in bindings
    )
    wrong_visual = next(
        visual.object_id
        for visual in facts.drawing_objects
        if visual.object_id != bindings[0].visual_object_id
    )
    with pytest.raises(DomainContractError) as captured:
        validate_data_binding(facts, bindings[0].text_object_id, wrong_visual)
    assert captured.value.code is ErrorCode.DATA_BINDING_INVALID
    low_contrast = probe_low_contrast(facts)
    assert low_contrast and all(
        item.action == "PRESERVE_SOURCE_AND_DEGRADE" for item in low_contrast
    )
    assert _sha256_file(path) == source_hash


@pytest.mark.regression
def test_p9c_4_t06_g8_g9_mixed_and_corrective_contracts_have_no_drift() -> None:
    """P9C.4-T06：重跑 G8/G9 mixed 与资源审计，历史 hash 和后续接口无退化。"""

    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "tests/test_p8.py",
            "tests/test_p9.py",
            "-k",
            "p8_5 or p9_7_t05",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        check=False,
    )
    assert process.returncode == 0, process.stdout + process.stderr
    check = subprocess.run(
        [sys.executable, "-m", "scripts.build_p9c_assets", "--check"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    assert check.returncode == 0 and "historical_change_count=0" in check.stdout
    fields = ToolboxPageCoordinator.__init__.__annotations__
    assert "completeness_gate" in fields and "route_guard" in fields
