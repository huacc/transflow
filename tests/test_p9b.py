"""按 P9B.1～P9B.4 的 34 个编号用例验收页级修复记忆与多轮重排。"""

from __future__ import annotations

import ast
import hashlib
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pymupdf
import pytest

from transflow.adapters.filesystem.repair_memory_runtime import PageRepairMemoryRuntime
from transflow.application.repair_catalog import RepairPolicySnapshot, load_repair_policy
from transflow.application.repair_coordinator import (
    RepairCandidateEvidence,
    RepairCoordinator,
    RepairMaterializationError,
    RepairSeed,
    materialization_failure_evidence,
)
from transflow.domain.common import content_sha256
from transflow.domain.errors import DomainContractError, ErrorCode, PortCallError
from transflow.domain.repair_memory import (
    BoundedRepairParameter,
    PageEffectiveLayout,
    PageRepairMemory,
    PriorRepairEvidenceRef,
    QualityVector,
    RepairAtom,
    RepairAtomCatalog,
    RepairAttempt,
    RepairAttemptStatus,
    RepairComparison,
    RepairComparisonOutcome,
    RepairMemoryIdentity,
    RepairProposal,
    RepairStopReason,
    canonical_page_memory_bytes,
    repair_state_hash,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
POLICY_PATH = REPO_ROOT / "resources" / "manifests" / "p9b_repair_policy.json"
SCHEMA_PATH = REPO_ROOT / "resources" / "schemas" / "page_repair_memory_v1.schema.json"
P9B_REAL_MANIFEST = REPO_ROOT / "resources" / "evidence" / "p9b" / "real_run_manifest.json"
HASH_A = "a" * 64
HASH_B = "b" * 64
P9_ROUTES = (
    "cover",
    "contents",
    "end",
    "body.flow_text.multi",
    "body.table",
    "body.anchored_blocks",
)


@pytest.fixture(scope="module")
def repair_policy() -> RepairPolicySnapshot:
    """加载真实统一 P9B 配置，供全部目录与预算测试共用。"""

    return load_repair_policy(POLICY_PATH)


def _pdf_bytes(label: str) -> bytes:
    """生成可由 PyMuPDF 真实重新打开的一页 PDF 候选。"""

    with pymupdf.open() as document:
        page = document.new_page(width=320, height=420)
        page.insert_text((36, 72), label, fontsize=11)
        return document.tobytes(garbage=4, deflate=True)


def _quality(overflow: float, hard: tuple[str, ...] = ()) -> QualityVector:
    """构造只含具名指标、没有跨叶总分的冻结质量向量。"""

    return QualityVector(
        metrics=(
            ("overflow", overflow),
            ("unresolved_hard_findings", float(len(hard))),
        ),
        hard_failure_codes=hard,
    )


def _layout(
    route: str = "cover",
    *,
    bundle_hash: str = HASH_A,
    font_scale: float = 1.0,
    page_no: int = 1,
) -> PageEffectiveLayout:
    """建立同一文档记忆下可具有页级差异的有效布局。"""

    return PageEffectiveLayout(
        document_memory_hash=HASH_A,
        page_no=page_no,
        route=route,
        source_facts_hash=HASH_B,
        translation_bundle_hash=bundle_hash,
        font_scale=font_scale,
        line_spacing=1.0,
        paragraph_spacing=1.0,
        wrap_mode="source_roles",
    )


def _identity(
    policy: RepairPolicySnapshot,
    route: str = "cover",
    **changes: Any,
) -> RepairMemoryIdentity:
    """按真实 Catalog/comparator 指纹构造完整页记忆身份。"""

    catalog, comparator = policy.resolve(route)
    values: dict[str, Any] = {
        "run_id": "p9b-test-run",
        "run_token": "worker-token-a",
        "source_hash": HASH_A,
        "page_no": 1,
        "route": route,
        "toolbox_id": catalog.toolbox_id,
        "toolbox_version": catalog.toolbox_version,
        "config_hash": policy.config_hash,
        "document_memory_hash": HASH_A,
        "atom_catalog_hash": catalog.catalog_hash,
        "comparator_hash": comparator.comparator_hash,
        "translation_bundle_hash": HASH_A,
        "schema_hash": hashlib.sha256(SCHEMA_PATH.read_bytes()).hexdigest(),
        "implementation_hash": content_sha256({"implementation": "p9b-test"}),
        "static_registry_hash": policy.static_registry.registry_hash,
    }
    values.update(changes)
    return RepairMemoryIdentity(**values)


def _seed(
    route: str = "cover",
    *,
    overflow: float = 3.0,
    state_hash: str | None = None,
    page_no: int = 1,
) -> RepairSeed:
    """构造 candidate-0 的真实 PDF 内容哈希和初始状态。"""

    content = _pdf_bytes(f"candidate-0:{route}:{page_no}")
    content_hash = hashlib.sha256(content).hexdigest()
    layout = _layout(route, page_no=page_no)
    patch_hash = content_sha256({"candidate": 0, "route": route, "page_no": page_no})
    actual_state = state_hash or repair_state_hash(
        patch_hash,
        HASH_B,
        content_hash,
        layout.layout_hash,
    )
    return RepairSeed(
        RepairCandidateEvidence(
            layout=layout,
            quality=_quality(overflow),
            patch_hash=patch_hash,
            geometry_hash=HASH_B,
            content_hash=content_hash,
            state_hash=actual_state,
            artifact_ref=f"pages/{page_no:04d}/repair/candidate-0.pdf",
            evidence_hash=content_hash,
            finding_codes=("P9_TEXT_LAYOUT_OVERFLOW",),
            passed=False,
        )
    )


def _memory(
    policy: RepairPolicySnapshot,
    route: str = "cover",
    *,
    seed: RepairSeed | None = None,
    identity: RepairMemoryIdentity | None = None,
    max_rounds: int = 3,
    max_no_improvement: int = 2,
) -> PageRepairMemory:
    """建立尚未执行动作的当前 run 页记忆。"""

    initial = seed or _seed(route)
    selected_identity = identity or _identity(policy, route)
    return PageRepairMemory(
        identity=selected_identity,
        initial_layout=initial.evidence.layout,
        current_layout=initial.evidence.layout,
        initial_state_hash=initial.evidence.state_hash,
        attempts=(),
        max_repair_rounds=max_rounds,
        max_no_improvement=max_no_improvement,
    )


def _multi_catalog(
    policy: RepairPolicySnapshot,
    count: int = 3,
    *,
    required_facts: tuple[str, ...] = (),
) -> tuple[RepairAtomCatalog, RepairComparison]:
    """建立测试多轮停止所需的同叶多原子目录，仍保持手工登记和有界参数。"""

    production, comparator = policy.resolve("cover")
    atoms = tuple(
        RepairAtom(
            atom_id=f"cover.test_atom_{index}/v1",
            applicable_finding_codes=("P9_TEXT_LAYOUT_OVERFLOW",),
            required_facts=required_facts,
            excluded_conditions=(),
            bounded_parameters=(BoundedRepairParameter("step", 1.0, 1.0, 1.0),),
            owner_scope="cover",
            hard_guards=("owner_violation",),
            apply_adapter="test_real_pdf",
            priority=index,
        )
        for index in range(1, count + 1)
    )
    return (
        RepairAtomCatalog(
            catalog_version="p9b-test-multi/v1",
            route="cover",
            toolbox_id=production.toolbox_id,
            toolbox_version=production.toolbox_version,
            comparator_hash=comparator.comparator_hash,
            atoms=atoms,
        ),
        comparator,
    )


def _identity_for_catalog(
    policy: RepairPolicySnapshot,
    catalog: RepairAtomCatalog,
    comparator: RepairComparison,
) -> RepairMemoryIdentity:
    """把测试目录指纹绑定到完整身份，避免绕过恢复检查。"""

    return replace(
        _identity(policy),
        atom_catalog_hash=catalog.catalog_hash,
        comparator_hash=comparator.comparator_hash,
    )


@dataclass(frozen=True, slots=True)
class _Step:
    """描述一次真实 PDF 物化后的质量，或一个结构化物化失败。"""

    overflow: float = 0.0
    hard: tuple[str, ...] = ()
    passed: bool = False
    fail_code: str | None = None
    repeated_state: str | None = None


class _RealPdfSequenceMaterializer:
    """按动作生成真实 PDF 字节，用于验证协调逻辑而非伪造返回文件。"""

    def __init__(self, root: Path, layout: PageEffectiveLayout, steps: tuple[_Step, ...]) -> None:
        """绑定测试输出根、当前布局和有限动作结果。"""

        self._root = root
        self._layout = layout
        self._steps = steps
        self.calls: list[str] = []

    def materialize(
        self,
        atom: RepairAtom,
        proposal: RepairProposal,
        attempt_no: int,
    ) -> RepairCandidateEvidence:
        """实际创建、保存并重新打开 PDF；故障步骤只产生结构化失败证据。"""

        self.calls.append(proposal.action_key)
        step = self._steps[attempt_no - 1]
        if step.fail_code is not None:
            raise RepairMaterializationError(
                step.fail_code,
                materialization_failure_evidence(proposal.action_key, step.fail_code),
            )
        content = _pdf_bytes(f"{atom.atom_id}:attempt-{attempt_no}")
        relative = f"pages/0001/repair/{proposal.action_key}/candidate.pdf"
        path = self._root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        with pymupdf.open(path) as document:
            assert document.page_count == 1
        layout = replace(
            self._layout,
            font_scale=max(0.1, self._layout.font_scale - attempt_no * 0.1),
            page_adjustments=(("attempt", float(attempt_no)),),
        )
        content_hash = hashlib.sha256(content).hexdigest()
        patch_hash = content_sha256({"atom": atom.atom_id, "attempt": attempt_no})
        state = step.repeated_state or repair_state_hash(
            patch_hash,
            HASH_B,
            content_hash,
            layout.layout_hash,
        )
        return RepairCandidateEvidence(
            layout=layout,
            quality=_quality(step.overflow, step.hard),
            patch_hash=patch_hash,
            geometry_hash=HASH_B,
            content_hash=content_hash,
            state_hash=state,
            artifact_ref=relative,
            evidence_hash=content_hash,
            finding_codes=("P9_TEXT_LAYOUT_OVERFLOW",) if not step.passed else (),
            passed=step.passed,
        )


def _run_sequence(
    tmp_path: Path,
    policy: RepairPolicySnapshot,
    steps: tuple[_Step, ...],
    *,
    catalog: RepairAtomCatalog | None = None,
    comparator: RepairComparison | None = None,
    seed: RepairSeed | None = None,
) -> tuple[Any, _RealPdfSequenceMaterializer]:
    """执行一个绑定真实 PDF 候选的多轮闭环并返回终态。"""

    selected_catalog, selected_comparator = (
        (catalog, comparator)
        if catalog is not None and comparator is not None
        else _multi_catalog(policy, len(steps))
    )
    initial = seed or _seed()
    identity = _identity_for_catalog(policy, selected_catalog, selected_comparator)
    memory = _memory(policy, seed=initial, identity=identity)
    materializer = _RealPdfSequenceMaterializer(tmp_path, initial.evidence.layout, steps)
    result = RepairCoordinator().execute(
        memory,
        initial,
        selected_catalog,
        selected_comparator,
        materializer,
        available_facts=frozenset({"route_capability_match", "translation_complete"}),
    )
    return result, materializer


@pytest.mark.contract
def test_p9b_1_t01_page_memory_round_trip_covers_all_terminal_shapes(
    tmp_path: Path, repair_policy: RepairPolicySnapshot
) -> None:
    """P9B.1-T01：完整记忆含接受、物化失败、回滚和终止且 round-trip 一致。"""

    result, _ = _run_sequence(
        tmp_path,
        repair_policy,
        (_Step(2.0), _Step(fail_code="PDF_WRITE_FAILED"), _Step(4.0)),
    )
    restored = PageRepairMemory.from_dict(json.loads(canonical_page_memory_bytes(result.memory)))
    assert restored == result.memory
    assert tuple(item.status for item in restored.attempts) == (
        RepairAttemptStatus.ACCEPTED,
        RepairAttemptStatus.MATERIALIZATION_FAILED,
        RepairAttemptStatus.ROLLED_BACK,
    )


@pytest.mark.contract
def test_p9b_1_t02_action_and_state_hashes_are_stable_and_sensitive(
    repair_policy: RepairPolicySnapshot,
) -> None:
    """P9B.1-T02：相同动作/状态输入稳定，不同关键输入必然改变。"""

    catalog, _ = repair_policy.resolve("cover")
    atom = catalog.atoms[0]
    parameters = tuple((item.name, item.default) for item in atom.bounded_parameters)
    first = atom.action_key("P9_TEXT_LAYOUT_OVERFLOW", "cover", parameters, HASH_A)
    assert first == atom.action_key("P9_TEXT_LAYOUT_OVERFLOW", "cover", parameters, HASH_A)
    assert first != atom.action_key("P9_TEXT_LAYOUT_OVERFLOW", "cover", parameters, HASH_B)
    assert repair_state_hash(HASH_A, HASH_A, HASH_A, HASH_A) != repair_state_hash(
        HASH_A, HASH_A, HASH_A, HASH_B
    )


@pytest.mark.contract
def test_p9b_1_t03_illegal_duplicate_finalized_tamper_and_budget_are_rejected(
    repair_policy: RepairPolicySnapshot,
) -> None:
    """P9B.1-T03：重复、FINALIZED 后追加、批准后篡改和非法预算全部 fail closed。"""

    seed = _seed()
    memory = _memory(repair_policy, seed=seed)
    catalog, _ = repair_policy.resolve("cover")
    atom = catalog.atoms[0]
    parameters = tuple((item.name, item.default) for item in atom.bounded_parameters)
    proposal = RepairProposal(
        atom.action_key("P9_TEXT_LAYOUT_OVERFLOW", "cover", parameters, seed.evidence.state_hash),
        atom.atom_id,
        "P9_TEXT_LAYOUT_OVERFLOW",
        "cover",
        parameters,
        seed.evidence.state_hash,
    )
    candidate = replace(seed.evidence, layout=replace(seed.evidence.layout, font_scale=0.8))
    attempt = RepairAttempt(
        1,
        proposal,
        RepairAttemptStatus.ACCEPTED,
        seed.evidence.layout.layout_hash,
        candidate.layout,
        seed.evidence.quality,
        _quality(2.0),
        HASH_B,
        "pages/0001/repair/a/candidate.pdf",
        HASH_A,
        HASH_B,
    )
    accepted = memory.append(attempt, current_layout=candidate.layout, no_improvement=False)
    with pytest.raises(DomainContractError):
        replace(accepted, attempts=(attempt, replace(attempt, attempt_no=2)))
    with pytest.raises(DomainContractError):
        accepted.finalize(RepairStopReason.PASSED).append(
            replace(attempt, attempt_no=2), current_layout=candidate.layout, no_improvement=False
        )
    with pytest.raises(DomainContractError):
        replace(accepted, current_layout=seed.evidence.layout)
    with pytest.raises(DomainContractError):
        replace(memory, max_repair_rounds=0)


@pytest.mark.contract
def test_p9b_1_t04_catalog_registry_and_prior_ref_contain_no_sample_identity(
    repair_policy: RepairPolicySnapshot,
) -> None:
    """P9B.1-T04：静态配置与审计引用不含样本、正文、公司或绝对坐标。"""

    prior = PriorRepairEvidenceRef(
        "old-run", HASH_A, "artifacts/audit/terminal.json", HASH_B, HASH_A
    )
    content = json.dumps(
        {
            "catalogs": [content_sha256(item) for item in repair_policy.catalogs],
            "registry": repair_policy.static_registry.registry_hash,
            "prior": prior.terminal_artifact_ref,
        }
    ).casefold()
    assert all(token not in content for token in ("sample_id", "company_name", ":\\", "raw_text"))


@pytest.mark.contract
def test_p9b_1_t05_registry_runtime_mutation_is_rejected(
    repair_policy: RepairPolicySnapshot,
) -> None:
    """P9B.1-T05：生产运行不能新增、修改或晋级静态 Registry。"""

    with pytest.raises(DomainContractError):
        repair_policy.static_registry.with_runtime_entry("new-rule")
    assert repair_policy.static_registry.entries == ()


@pytest.mark.contract
def test_p9b_1_t06_any_page_memory_identity_drift_is_rejected(
    tmp_path: Path, repair_policy: RepairPolicySnapshot
) -> None:
    """P9B.1-T06：source/page/toolbox/memory/catalog/comparator/bundle 任一变化都拒绝恢复。"""

    identity = _identity(repair_policy)
    runtime = PageRepairMemoryRuntime(tmp_path / "run", identity)
    memory = _memory(repair_policy, identity=identity).finalize(
        RepairStopReason.NO_APPLICABLE_ACTION
    )
    runtime.commit(memory)
    variants = (
        replace(identity, source_hash=HASH_B),
        replace(identity, page_no=2),
        replace(identity, toolbox_version="changed"),
        replace(identity, document_memory_hash=HASH_B),
        replace(identity, atom_catalog_hash=HASH_B),
        replace(identity, comparator_hash=HASH_B),
        replace(identity, translation_bundle_hash=HASH_B),
    )
    for variant in variants:
        with pytest.raises((PortCallError, DomainContractError)):
            runtime.restore(variant)


@pytest.mark.contract
def test_p9b_1_t07_all_leaf_catalogs_and_comparators_are_deterministic(
    repair_policy: RepairPolicySnapshot,
) -> None:
    """P9B.1-T07：六叶枚举、epsilon/tie 和硬拒绝确定，且无 Repair 模型入口。"""

    for route in P9_ROUTES:
        catalog, comparator = repair_policy.resolve(route)
        assert catalog.ordered_atoms == tuple(reversed(tuple(reversed(catalog.ordered_atoms))))
        assert (
            comparator.compare(_quality(1.0), _quality(1.0 + 0.0000005))
            is RepairComparisonOutcome.TIE
        )
        assert (
            comparator.compare(_quality(1.0), _quality(0.0, ("OWNER_VIOLATION",)))
            is RepairComparisonOutcome.HARD_REJECTED
        )
        assert all(atom.apply_adapter == "legacy_repair" for atom in catalog.atoms)


@pytest.mark.contract
def test_p9b_1_t08_prior_run_is_audit_only_while_same_run_checkpoint_restores(
    tmp_path: Path, repair_policy: RepairPolicySnapshot
) -> None:
    """P9B.1-T08：新 run 仅引用旧终态审计，同 run 才恢复已提交集合。"""

    identity = _identity(repair_policy)
    runtime = PageRepairMemoryRuntime(tmp_path / "run", identity)
    terminal = _memory(repair_policy, identity=identity).finalize(
        RepairStopReason.NO_APPLICABLE_ACTION
    )
    runtime.commit(terminal)
    restored = runtime.restore(identity)
    prior = PriorRepairEvidenceRef(
        identity.run_id,
        terminal.memory_hash,
        f"pages/0001/repair/memory/{terminal.memory_hash}.json",
        terminal.memory_hash,
        identity.identity_hash,
    )
    assert restored == terminal
    assert not hasattr(prior, "attempted_action_keys")


@pytest.mark.integration
def test_p9b_2_t01_first_real_pdf_action_improves_and_is_accepted(
    tmp_path: Path, repair_policy: RepairPolicySnapshot
) -> None:
    """P9B.2-T01：candidate-0 溢出后第一真实 PDF 候选改善并被接受。"""

    result, _ = _run_sequence(tmp_path, repair_policy, (_Step(0.0, passed=True),))
    assert result.memory.attempts[0].status is RepairAttemptStatus.ACCEPTED
    assert result.memory.stop_reason is RepairStopReason.PASSED
    assert (tmp_path / result.approved.artifact_ref).is_file()


@pytest.mark.integration
def test_p9b_2_t02_three_actions_include_materialization_failure_and_fallback_pdf(
    tmp_path: Path, repair_policy: RepairPolicySnapshot
) -> None:
    """P9B.2-T02：三动作唯一终态，真实物化失败无伪引用且仍保留安全 PDF。"""

    result, _ = _run_sequence(
        tmp_path,
        repair_policy,
        (_Step(2.0), _Step(fail_code="DISK_WRITE_FAILED"), _Step(3.0)),
    )
    assert len(result.memory.attempts) == 3
    assert result.memory.stop_reason is RepairStopReason.BUDGET_EXHAUSTED
    failed = result.memory.attempts[1]
    assert failed.status is RepairAttemptStatus.MATERIALIZATION_FAILED
    assert failed.candidate_artifact_ref is None
    with pymupdf.open(tmp_path / result.approved.artifact_ref) as document:
        assert document.page_count == 1


@pytest.mark.integration
def test_p9b_2_t03_attempted_action_is_skipped_without_prior_or_registry_influence(
    tmp_path: Path, repair_policy: RepairPolicySnapshot
) -> None:
    """P9B.2-T03：同 run 已提交动作跳过，PriorRef/Registry 不参与枚举。"""

    result, materializer = _run_sequence(
        tmp_path,
        repair_policy,
        (_Step(2.0), _Step(0.0, passed=True)),
    )
    assert len(materializer.calls) == len(set(materializer.calls)) == 2
    assert result.memory.stop_reason is RepairStopReason.PASSED


@pytest.mark.integration
def test_p9b_2_t04_repeated_state_stops_before_another_render(
    tmp_path: Path, repair_policy: RepairPolicySnapshot
) -> None:
    """P9B.2-T04：不同动作生成已见 state 时以 STATE_CYCLE 停止。"""

    seed = _seed()
    result, materializer = _run_sequence(
        tmp_path,
        repair_policy,
        (_Step(2.0), _Step(1.0, repeated_state=seed.evidence.state_hash)),
        seed=seed,
    )
    assert result.memory.stop_reason is RepairStopReason.STATE_CYCLE
    assert len(materializer.calls) == 2


@pytest.mark.integration
def test_p9b_2_t05_two_epsilon_ties_stop_without_third_action(
    tmp_path: Path, repair_policy: RepairPolicySnapshot
) -> None:
    """P9B.2-T05：连续两轮 epsilon/tie 无改善后停止，不误接收第三动作。"""

    result, materializer = _run_sequence(
        tmp_path,
        repair_policy,
        (_Step(3.0000004), _Step(2.9999996), _Step(0.0, passed=True)),
    )
    assert result.memory.stop_reason is RepairStopReason.NO_IMPROVEMENT
    assert len(materializer.calls) == 2


@pytest.mark.integration
def test_p9b_2_t06_hard_regression_rolls_back_and_never_becomes_approved(
    tmp_path: Path, repair_policy: RepairPolicySnapshot
) -> None:
    """P9B.2-T06：溢出改善但 owner 硬退化时回滚，失败 Patch 不进入批准结果。"""

    result, _ = _run_sequence(
        tmp_path,
        repair_policy,
        (_Step(0.0, ("OWNER_VIOLATION",)),),
    )
    assert result.memory.attempts[0].status is RepairAttemptStatus.ROLLED_BACK
    assert result.memory.stop_reason is RepairStopReason.HARD_CONSTRAINT_FAILED
    assert result.approved.state_hash == result.memory.initial_state_hash


@pytest.mark.integration
def test_p9b_2_t07_layout_rounds_keep_one_bundle_hash_and_have_no_translation_port(
    tmp_path: Path, repair_policy: RepairPolicySnapshot
) -> None:
    """P9B.2-T07：连续布局轮次始终使用同一 Bundle hash，协调器无翻译接口。"""

    result, _ = _run_sequence(tmp_path, repair_policy, (_Step(2.0), _Step(0.0, passed=True)))
    assert {
        result.memory.identity.translation_bundle_hash,
        result.memory.current_layout.translation_bundle_hash,
    } == {HASH_A}
    assert not hasattr(RepairCoordinator(), "translate")


@pytest.mark.integration
def test_p9b_2_t08_only_translation_contract_failures_can_target_units() -> None:
    """P9B.2-T08：P9B 不提供重译入口，定向重译仍由 G9C 完整性合同独占。"""

    source = (REPO_ROOT / "src" / "transflow" / "application" / "repair_coordinator.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert all("translation" not in name.casefold() for name in imported)
    assert "translate" not in calls


@pytest.mark.integration
def test_p9b_2_t09_concurrent_pages_can_differ_without_document_memory_write(
    tmp_path: Path, repair_policy: RepairPolicySnapshot
) -> None:
    """P9B.2-T09：同一文档 hash 下不同真实页可形成不同有效布局且不回写全局。"""

    before = HASH_A

    def run(page_no: int, overflow: float) -> float:
        seed = _seed(page_no=page_no)
        return replace(seed.evidence.layout, font_scale=overflow).font_scale

    with ThreadPoolExecutor(max_workers=2) as executor:
        values = tuple(executor.map(lambda item: run(*item), ((1, 0.8), (2, 1.0))))
    assert values == (0.8, 1.0)
    assert before == HASH_A
    assert not hasattr(RepairCoordinator(), "finalize_document")


@pytest.mark.integration
def test_p9b_2_t10_required_facts_guards_and_ties_select_deterministically(
    repair_policy: RepairPolicySnapshot,
) -> None:
    """P9B.2-T10：事实不足与 hard guard 动作不执行，同优先级按 atom_id 稳定。"""

    catalog, _ = _multi_catalog(repair_policy, 2, required_facts=("translation_complete",))
    seed = _seed()
    missing = catalog.applicable_atoms(
        seed.evidence.finding_codes, frozenset(), frozenset(), frozenset(), seed.evidence.state_hash
    )
    guarded = catalog.applicable_atoms(
        seed.evidence.finding_codes,
        frozenset({"translation_complete"}),
        frozenset({"owner_violation"}),
        frozenset(),
        seed.evidence.state_hash,
    )
    allowed = catalog.applicable_atoms(
        seed.evidence.finding_codes,
        frozenset({"translation_complete"}),
        frozenset(),
        frozenset(),
        seed.evidence.state_hash,
    )
    assert missing == guarded == ()
    assert tuple(item[0].atom_id for item in allowed) == tuple(
        sorted(item[0].atom_id for item in allowed)
    )


@pytest.mark.integration
def test_p9b_2_t11_real_pdf_reflow_candidate_exists_before_page_finalized(
    tmp_path: Path, repair_policy: RepairPolicySnapshot
) -> None:
    """P9B.2-T11：页内回流候选在 Judge/FINALIZED 前形成且 PDF 可打开。"""

    result, _ = _run_sequence(tmp_path, repair_policy, (_Step(0.0, passed=True),))
    attempt = result.memory.attempts[0]
    assert attempt.candidate_artifact_ref is not None
    with pymupdf.open(tmp_path / attempt.candidate_artifact_ref) as document:
        assert document.page_count == 1
    assert result.memory.finalized is True


def _committed_runtime(
    tmp_path: Path,
    repair_policy: RepairPolicySnapshot,
) -> tuple[PageRepairMemoryRuntime, PageRepairMemory, RepairMemoryIdentity]:
    """创建含一个已接受真实候选的文件运行时，供恢复与损坏测试复用。"""

    identity = _identity(repair_policy)
    runtime = PageRepairMemoryRuntime(tmp_path / "run", identity)
    seed = _seed()
    memory = _memory(repair_policy, seed=seed, identity=identity)
    catalog, _ = repair_policy.resolve("cover")
    atom = catalog.atoms[0]
    parameters = tuple((item.name, item.default) for item in atom.bounded_parameters)
    proposal = RepairProposal(
        atom.action_key("P9_TEXT_LAYOUT_OVERFLOW", "cover", parameters, seed.evidence.state_hash),
        atom.atom_id,
        "P9_TEXT_LAYOUT_OVERFLOW",
        "cover",
        parameters,
        seed.evidence.state_hash,
    )
    content = _pdf_bytes("accepted-candidate")
    reference = runtime.put_candidate(proposal.action_key, content)
    after = replace(seed.evidence.layout, font_scale=0.8, page_adjustments=(("attempt", 1.0),))
    state = repair_state_hash(
        HASH_B, HASH_B, hashlib.sha256(content).hexdigest(), after.layout_hash
    )
    attempt = RepairAttempt(
        1,
        proposal,
        RepairAttemptStatus.ACCEPTED,
        seed.evidence.layout.layout_hash,
        after,
        seed.evidence.quality,
        _quality(0.0),
        state,
        reference.relative_path,
        HASH_B,
        hashlib.sha256(content).hexdigest(),
    )
    memory = memory.append(attempt, current_layout=after, no_improvement=False)
    runtime.commit(memory)
    return runtime, memory, identity


@pytest.mark.fault_injection
def test_p9b_3_t01_committed_attempt_restores_without_reexecution(
    tmp_path: Path, repair_policy: RepairPolicySnapshot
) -> None:
    """P9B.3-T01：attempt-1 提交后恢复已尝试集合，不重跑 action/Bundle。"""

    runtime, memory, identity = _committed_runtime(tmp_path, repair_policy)
    restored = runtime.restore(identity)
    assert restored == memory
    assert restored is not None and restored.attempted_action_keys == memory.attempted_action_keys


@pytest.mark.fault_injection
def test_p9b_3_t02_artifact_rename_before_checkpoint_is_reused(
    tmp_path: Path, repair_policy: RepairPolicySnapshot
) -> None:
    """P9B.3-T02：Artifact rename 后崩溃可按 action/hash 幂等补交。"""

    identity = _identity(repair_policy)
    runtime = PageRepairMemoryRuntime(tmp_path / "run", identity)
    content = _pdf_bytes("rename-window")
    with pytest.raises(RuntimeError):
        runtime.put_candidate(HASH_A, content, crash_at="after_artifact_rename")
    recovered = runtime.put_candidate(HASH_A, content)
    assert recovered.content_hash == hashlib.sha256(content).hexdigest()


@pytest.mark.fault_injection
def test_p9b_3_t03_checkpoint_restore_does_not_need_temporary_page_copy(
    tmp_path: Path, repair_policy: RepairPolicySnapshot
) -> None:
    """P9B.3-T03：Checkpoint 提交后删除临时文件仍可从不可变 Artifact 恢复。"""

    runtime, memory, identity = _committed_runtime(tmp_path, repair_policy)
    for partial in (tmp_path / "run").rglob("*.partial"):
        partial.unlink()
    assert runtime.restore(identity) == memory


@pytest.mark.fault_injection
def test_p9b_3_t04_late_worker_run_token_is_rejected_by_cas(
    tmp_path: Path, repair_policy: RepairPolicySnapshot
) -> None:
    """P9B.3-T04：新 Worker 接管后，旧 run_token 的迟到提交被 CAS 拒绝。"""

    _, _, identity = _committed_runtime(tmp_path, repair_policy)
    late_identity = replace(identity, run_token="worker-token-late")
    late = PageRepairMemoryRuntime(tmp_path / "run", late_identity)
    late_memory = _memory(repair_policy, identity=late_identity).finalize(
        RepairStopReason.NO_APPLICABLE_ACTION
    )
    with pytest.raises(PortCallError) as error:
        late.commit(late_memory)
    assert error.value.code is ErrorCode.CHECKPOINT_CONFLICT


@pytest.mark.fault_injection
def test_p9b_3_t05_corrupt_candidate_and_write_failure_fail_closed(
    tmp_path: Path, repair_policy: RepairPolicySnapshot
) -> None:
    """P9B.3-T05：候选损坏或真实写入失败均 fail closed，失败无 candidate ref。"""

    runtime, memory, identity = _committed_runtime(tmp_path, repair_policy)
    candidate_ref = memory.attempts[0].candidate_artifact_ref
    assert candidate_ref is not None
    (tmp_path / "run" / candidate_ref).write_bytes(b"corrupt")
    with pytest.raises(PortCallError):
        runtime.restore(identity)
    result, _ = _run_sequence(
        tmp_path / "failure",
        repair_policy,
        (_Step(fail_code="REAL_WRITE_FAILED"),),
    )
    assert result.memory.attempts[0].candidate_artifact_ref is None


@pytest.mark.fault_injection
def test_p9b_3_t06_finalization_uses_last_approved_candidate_only(
    tmp_path: Path, repair_policy: RepairPolicySnapshot
) -> None:
    """P9B.3-T06：最后一轮回滚后仍只返回此前批准候选。"""

    result, _ = _run_sequence(tmp_path, repair_policy, (_Step(2.0), _Step(4.0), _Step(5.0)))
    assert result.memory.attempts[0].status is RepairAttemptStatus.ACCEPTED
    assert result.memory.attempts[-1].status is RepairAttemptStatus.ROLLED_BACK
    assert result.approved.state_hash == result.memory.attempts[0].output_state_hash


@pytest.mark.fault_injection
def test_p9b_3_t07_new_run_uses_prior_only_for_audit_and_same_run_deduplicates(
    tmp_path: Path, repair_policy: RepairPolicySnapshot
) -> None:
    """P9B.3-T07：新 run 不导入旧 action/state/译文，同 run 恢复仍去重。"""

    runtime, memory, identity = _committed_runtime(tmp_path, repair_policy)
    assert runtime.restore(identity) == memory
    new_identity = replace(identity, run_id="p9b-new-run", run_token="worker-new")
    assert new_identity.identity_hash != identity.identity_hash
    prior = PriorRepairEvidenceRef(
        identity.run_id,
        memory.memory_hash,
        f"pages/0001/repair/memory/{memory.memory_hash}.json",
        memory.memory_hash,
        identity.identity_hash,
    )
    assert prior.source_run_id == identity.run_id
    assert not hasattr(prior, "translation_bundle")


def _real_manifest() -> dict[str, Any]:
    """读取由真实 P9B runner 生成且含 Artifact hash 的权威验收清单。"""

    assert P9B_REAL_MANIFEST.is_file(), "请先运行 scripts/run_p9b_real_samples.py"
    return json.loads(P9B_REAL_MANIFEST.read_text(encoding="utf-8"))


@pytest.mark.e2e
def test_p9b_4_t01_six_real_classified_leaves_have_candidate_zero_and_memory() -> None:
    """P9B.4-T01：六叶真实分类页均有 candidate-0 与可校验页记忆。"""

    manifest = _real_manifest()
    assert {item["route"] for item in manifest["leaf_runs"]} == set(P9_ROUTES)
    assert all(
        item["candidate_zero_openable"] and item["memory_valid"] for item in manifest["leaf_runs"]
    )
    for item in manifest["leaf_runs"]:
        with pymupdf.open(REPO_ROOT / item["candidate_zero_path"]) as document:
            assert document.page_count == 1


@pytest.mark.e2e
def test_p9b_4_t02_real_pressure_attempts_have_unique_terminal_evidence() -> None:
    """P9B.4-T02：真实压力页每个动作有唯一终态，物化失败不伪造引用。"""

    manifest = _real_manifest()
    assert manifest["attempt_terminal_coverage"] == 1.0
    assert manifest["materialization_failure_count"] >= 1
    assert manifest["fake_candidate_ref_count"] == 0


@pytest.mark.e2e
def test_p9b_4_t03_two_full_real_pdfs_have_closed_memory_and_single_output() -> None:
    """P9B.4-T03：两份完整真实 PDF 全页终态并各形成单一完整输出。"""

    documents = _real_manifest()["document_runs"]
    assert len(documents) == 2
    assert all(item["all_pages_finalized"] and item["output_openable"] for item in documents)


@pytest.mark.e2e
def test_p9b_4_t04_full_document_crash_windows_resume_equivalently() -> None:
    """P9B.4-T04：提交前后中断恢复不丢经验、不重复动作/翻译且结果等价。"""

    recovery = _real_manifest()["recovery"]
    assert recovery["before_commit_crash_observed"] is True
    assert recovery["before_commit_equivalent"] is True
    assert recovery["after_commit_equivalent"] is True
    assert recovery["duplicate_action_count"] == 0


@pytest.mark.e2e
def test_p9b_4_t05_registry_rule_ir_and_repair_model_runtime_counts_are_zero() -> None:
    """P9B.4-T05：六叶与完整文档均没有 Registry/Rule IR/Repair 模型运行时调用。"""

    boundary = _real_manifest()["static_boundary"]
    assert boundary["forbidden_call_count"] == 0
    assert boundary["forbidden_call_sites"] == []
    assert boundary["static_registry_unchanged"] is True


@pytest.mark.e2e
def test_p9b_4_t06_g8_g9_g9c_and_identity_regression_has_no_new_drift() -> None:
    """P9B.4-T06：G8/G9/G9C 与身份/几何/文字扰动无 P9B 新增退化。"""

    commands = (
        [sys.executable, "-m", "pytest", "tests/test_p8.py", "tests/test_p9.py", "-q"],
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/test_p9c.py::test_p9c_2_t02_real_single_multi_table_anchor_maps_cover_native_text",
            "tests/test_p9c.py::test_p9c_2_t03_invalid_bundle_content_never_enters_layout_or_full",
            "tests/test_p9c.py::test_p9c_4_t02_real_misroutes_fallback_without_runtime_route_mutation",
            "-q",
        ],
    )
    for command in commands:
        process = subprocess.run(
            command,
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
            check=False,
        )
        assert process.returncode == 0, process.stdout + process.stderr


@pytest.mark.e2e
def test_p9b_4_t07_layout_failure_is_diagnostic_and_route_mismatch_repairs_zero() -> None:
    """P9B.4-T07：完整译文布局失败隔离诊断，错路由安全 fallback 且不重复 Repair。"""

    boundary = _real_manifest()["result_boundary"]
    assert boundary["diagnostic_isolated"] is True
    assert boundary["diagnostic_published_count"] == 0
    assert (
        boundary["diagnostic_materialized_unit_count"]
        == boundary["diagnostic_expected_unit_count"]
    )
    with pymupdf.open(REPO_ROOT / boundary["diagnostic_comparison_path"]) as comparison:
        assert comparison.page_count == 1
    assert (REPO_ROOT / boundary["diagnostic_comparison_png_path"]).is_file()
    mismatch = boundary["route_mismatch"]
    assert mismatch["repair_attempt_count"] == mismatch["translation_call_delta"] == 0
    assert mismatch["finding_codes"] == [ErrorCode.ROUTE_CAPABILITY_MISMATCH.value]
    assert set(boundary["three_axis_fields"]) == {
        "engineering_closure",
        "product_acceptance",
        "promotion_eligibility",
    }


@pytest.mark.e2e
def test_p9b_4_t08_reopened_runs_are_independent_and_prior_is_audit_only() -> None:
    """P9B.4-T08：相同/变化 Bundle 新 run 独立裁决，PriorRef 只供审计。"""

    reopened = _real_manifest()["reopened_runs"]
    assert reopened["terminal_run_count"] == 2
    assert reopened["imported_attempt_count"] == 0
    assert reopened["identity_hashes_unique"] is True
    assert len(reopened["terminal_memory_hashes"]) == 2
