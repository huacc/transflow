"""按 P3.1 至 P3.5 计划编号验收全部文件与测试 AI 外部边界。"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

import pymupdf
import pytest

from scripts.fake_ai_service import FakeAiService
from transflow.adapters.ai import (
    DeterministicTranslationAdapter,
    FixedTranslationAdapter,
    HttpAiCapabilityAdapter,
)
from transflow.adapters.filesystem import (
    FilesystemCheckpointAdapter,
    InjectedCrash,
    SharedFilesystemArtifactAdapter,
    StructuredAuditLogger,
)
from transflow.adapters.filesystem.common import sha256_bytes, sha256_file
from transflow.adapters.standalone import StandaloneRunAdapter
from transflow.domain import (
    ArtifactPayload,
    ArtifactReference,
    CheckpointCompatibility,
    CheckpointRecord,
    DomainContractError,
    ErrorCode,
    ModelDecisionRequest,
    PortCallError,
    TranslationBatch,
    TranslationUnit,
)
from transflow.domain.common import content_sha256
from transflow.runtime.config import RuntimeConfig, load_runtime_config
from transflow.runtime.health import HealthService

REPO_ROOT = Path(__file__).resolve().parent.parent
P3_TEST_ROOT = REPO_ROOT / "tmp" / "p3-tests"
INPUT_ROOT = P3_TEST_ROOT / "inputs"
OUTSIDE_ROOT = P3_TEST_ROOT / "outside"
WORKSPACE_ROOT = P3_TEST_ROOT / "workspace"
FAKE_TOKEN = "p3-local-contract-token"
HASH_A = "a" * 64


def create_pdf(path: Path, page_count: int, *, encrypted: bool = False) -> None:
    """创建具有真实 PDF 结构和指定页数的测试输入。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    document = pymupdf.open()
    for page_no in range(page_count):
        page = document.new_page(width=300, height=400)
        page.insert_text((36, 72), f"Transflow P3 page {page_no + 1}")
    if encrypted:
        document.save(
            path,
            encryption=pymupdf.PDF_ENCRYPT_AES_256,
            owner_pw="p3-owner-password",
            user_pw="p3-user-password",
        )
    else:
        document.save(path)
    document.close()


@pytest.fixture(scope="session", autouse=True)
def prepare_p3_inputs() -> Iterator[Path]:
    """创建仓库 tmp 下的真实 P3 PDF、工作区和越界目标。"""

    resolved = P3_TEST_ROOT.resolve()
    if resolved.parent != (REPO_ROOT / "tmp").resolve():
        raise RuntimeError("P3 测试根越出仓库 tmp")
    if resolved.exists():
        shutil.rmtree(resolved)
    INPUT_ROOT.mkdir(parents=True)
    OUTSIDE_ROOT.mkdir(parents=True)
    WORKSPACE_ROOT.mkdir(parents=True)
    create_pdf(INPUT_ROOT / "three-pages.pdf", 3)
    create_pdf(INPUT_ROOT / "one-page.pdf", 1)
    create_pdf(INPUT_ROOT / "encrypted.pdf", 1, encrypted=True)
    create_pdf(OUTSIDE_ROOT / "outside.pdf", 1)
    (INPUT_ROOT / "not-pdf.txt").write_text("plain text", encoding="utf-8")
    (INPUT_ROOT / "zero.pdf").write_bytes(b"")
    (INPUT_ROOT / "damaged.pdf").write_bytes(b"%PDF-damaged")
    yield resolved


def p3_config(workspace_name: str) -> RuntimeConfig:
    """从集中配置派生仅替换测试允许根和 workspace 的 P3 配置。"""

    return replace(
        load_runtime_config(),
        workspace=WORKSPACE_ROOT / workspace_name,
        source_roots=(INPUT_ROOT,),
        ai_timeout_seconds=0.2,
        ai_max_request_bytes=1024 * 1024,
    )


def run_manifest_count(workspace: Path) -> int:
    """统计真实创建的 Standalone run manifest 数量。"""

    if not workspace.is_dir():
        return 0
    return sum(1 for _ in workspace.rglob("run_manifest.json"))


def standalone_adapter(config: RuntimeConfig) -> StandaloneRunAdapter:
    """把集中 RuntimeConfig 的允许根字段装配到 Standalone Adapter。"""

    return StandaloneRunAdapter(config.workspace, config.source_roots)


def fresh_run_root(name: str) -> Path:
    """为一个测试创建隔离且可安全重建的 Run 根。"""

    root = (P3_TEST_ROOT / "runs" / name).resolve()
    root.relative_to((P3_TEST_ROOT / "runs").resolve())
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    return root


def compatibility() -> CheckpointCompatibility:
    """构造五项资源一致的真实 Checkpoint 兼容指纹。"""

    return CheckpointCompatibility(HASH_A, HASH_A, HASH_A, HASH_A, HASH_A)


def checkpoint_record(
    run_id: str,
    version: int,
    payload: bytes,
    references: tuple[ArtifactReference, ...] = (),
) -> CheckpointRecord:
    """按真实 payload 哈希构造 CheckpointRecord。"""

    return CheckpointRecord(
        run_id,
        version,
        sha256_bytes(payload),
        payload,
        compatibility(),
        references,
    )


def artifact_payload(
    artifact_id: str,
    content: bytes,
    media_type: str = "application/json",
) -> ArtifactPayload:
    """按真实内容哈希构造 ArtifactPayload。"""

    return ArtifactPayload(artifact_id, media_type, content, sha256_bytes(content))


def translation_batch(source_text: str = "Revenue") -> TranslationBatch:
    """构造两个按阅读顺序排列的真实文本翻译单元。"""

    return TranslationBatch(
        "batch-p3",
        "en",
        "zh-CN",
        (
            TranslationUnit("unit-1", 0, 0, source_text, "region-1"),
            TranslationUnit("unit-2", 0, 1, "Profit", "region-2"),
        ),
    )


def audit_event(outcome: str, *, error_code: str = "", fallback: str = "NONE") -> dict[str, Any]:
    """构造包含设计要求全部必填上下文的审计事件。"""

    return {
        "artifact_ref": "artifact-1",
        "attempt": 1,
        "classification_path": "body.table",
        "duration_ms": 12,
        "error_code": error_code,
        "fallback": fallback,
        "job_id": "job-1",
        "outcome": outcome,
        "page_no": 0,
        "run_id": "run-1",
        "service": "transflow",
        "stage": "P3",
        "state": "FINALIZED",
        "toolbox_key_version": "body.table/v1",
        "unit_or_region_id": "unit-1",
    }


@contextmanager
def running_fake_service(*, max_request_bytes: int = 64 * 1024) -> Iterator[FakeAiService]:
    """启动并可靠停止一个真实 loopback fake AI HTTP 服务。"""

    service = FakeAiService(FAKE_TOKEN, max_request_bytes=max_request_bytes).start()
    try:
        yield service
    finally:
        service.stop()


def http_adapter(
    service: FakeAiService,
    *,
    token: str = FAKE_TOKEN,
    timeout: float = 0.2,
    max_request_bytes: int = 1024 * 1024,
) -> HttpAiCapabilityAdapter:
    """构造连接真实 fake 服务且限制明确的 HTTP Adapter。"""

    return HttpAiCapabilityAdapter(service.base_url, token, timeout, max_request_bytes)


def create_symlink(link: Path, target: Path, *, directory: bool = False) -> None:
    """创建真实符号链接；无权限时为目录目标创建 Windows Junction。"""

    if link.exists() or link.is_symlink():
        if link.is_dir():
            link.rmdir()
        else:
            link.unlink()
    try:
        os.symlink(target, link, target_is_directory=directory)
    except OSError as error:
        if os.name != "nt" or not directory or getattr(error, "winerror", None) != 1314:
            raise
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stdout + completed.stderr) from error


@pytest.mark.integration
def test_p3_1_t01_accepts_three_page_complete_pdf_with_correct_hash() -> None:
    """P3.1-T01：允许根内真实三页完整 PDF 被接受且哈希正确。"""

    config = p3_config("p3-1-t01")
    adapter = standalone_adapter(config)
    source = (INPUT_ROOT / "three-pages.pdf").resolve()
    run = adapter.submit(source, "en", "zh-CN", {"profile": "fixed", "version": 1})
    assert run.request.source_pdf_path == str(source)
    assert run.request.source_hash == sha256_file(source)
    assert run.workspace.joinpath("job/run_manifest.json").is_file()
    assert adapter.acquire() is not None


@pytest.mark.integration
def test_p3_1_t02_accepts_one_page_complete_document() -> None:
    """P3.1-T02：一页完整文档合法，不与预拆页面列表混淆。"""

    adapter = standalone_adapter(p3_config("p3-1-t02"))
    run = adapter.submit((INPUT_ROOT / "one-page.pdf").resolve(), "en", "zh-CN", {"v": 1})
    with pymupdf.open(run.request.source_pdf_path) as document:
        assert document.page_count == 1


@pytest.mark.contract
def test_p3_1_t03_rejects_every_pdf_list_without_creating_run() -> None:
    """P3.1-T03：一个或多个 PDF 组成的 list 都在合同层拒绝且无副作用。"""

    config = p3_config("p3-1-t03")
    adapter = standalone_adapter(config)
    before = run_manifest_count(config.workspace)
    lists = (
        [INPUT_ROOT / "one-page.pdf"],
        [INPUT_ROOT / "one-page.pdf", INPUT_ROOT / "three-pages.pdf"],
    )
    for value in lists:
        with pytest.raises(DomainContractError) as captured:
            adapter.submit(value, "en", "zh-CN", {"v": 1})
        assert captured.value.code is ErrorCode.INPUT_SHAPE_INVALID
    assert run_manifest_count(config.workspace) == before


@pytest.mark.contract
def test_p3_1_t04_rejects_directory_without_recursive_search() -> None:
    """P3.1-T04：允许根目录本身不是普通 PDF 文件且不会被递归搜索。"""

    config = p3_config("p3-1-t04")
    adapter = standalone_adapter(config)
    before = run_manifest_count(config.workspace)
    with pytest.raises(DomainContractError) as captured:
        adapter.submit(INPUT_ROOT.resolve(), "en", "zh-CN", {"v": 1})
    assert captured.value.code is ErrorCode.SOURCE_NOT_REGULAR_FILE
    assert run_manifest_count(config.workspace) == before


@pytest.mark.contract
def test_p3_1_t05_rejects_relative_and_normalized_escape_paths() -> None:
    """P3.1-T05：相对路径和规范化后越出允许根的绝对路径均拒绝。"""

    config = p3_config("p3-1-t05")
    adapter = standalone_adapter(config)
    escaped = (INPUT_ROOT / ".." / "outside" / "outside.pdf").absolute()
    for value in (Path("../outside.pdf"), escaped):
        with pytest.raises(DomainContractError) as captured:
            adapter.submit(value, "en", "zh-CN", {"v": 1})
        assert captured.value.code is ErrorCode.PATH_OUTSIDE_ALLOWED_ROOT
    assert run_manifest_count(config.workspace) == 0


@pytest.mark.contract
def test_p3_1_t06_rejects_reparse_target_outside_allowed_root() -> None:
    """P3.1-T06：允许根内链接若解析到根外文件则按最终路径拒绝。"""

    link_root = INPUT_ROOT / "outside-link"
    create_symlink(link_root, OUTSIDE_ROOT, directory=True)
    linked_pdf = link_root / "outside.pdf"
    config = p3_config("p3-1-t06")
    with pytest.raises(DomainContractError) as captured:
        standalone_adapter(config).submit(linked_pdf.absolute(), "en", "zh-CN", {"v": 1})
    assert captured.value.code is ErrorCode.PATH_OUTSIDE_ALLOWED_ROOT
    assert run_manifest_count(config.workspace) == 0


@pytest.mark.contract
def test_p3_1_t07_rejects_unsupported_empty_damaged_and_encrypted_files() -> None:
    """P3.1-T07：文本、零字节、损坏和加密 PDF 均明确失败且不创建 Run。"""

    config = p3_config("p3-1-t07")
    adapter = standalone_adapter(config)
    cases = {
        "not-pdf.txt": ErrorCode.SOURCE_UNSUPPORTED,
        "zero.pdf": ErrorCode.SOURCE_UNSUPPORTED,
        "damaged.pdf": ErrorCode.SOURCE_NOT_READABLE,
        "encrypted.pdf": ErrorCode.SOURCE_UNSUPPORTED,
    }
    for name, expected_code in cases.items():
        with pytest.raises(DomainContractError) as captured:
            adapter.submit((INPUT_ROOT / name).resolve(), "en", "zh-CN", {"v": 1})
        assert captured.value.code is expected_code
    assert run_manifest_count(config.workspace) == 0


@pytest.mark.contract
def test_p3_1_t08_rejects_missing_or_wrong_typed_fields_without_workspace() -> None:
    """P3.1-T08：语言缺失和配置类型错误均列明字段且不创建 workspace。"""

    config = p3_config("p3-1-t08")
    adapter = standalone_adapter(config)
    source = (INPUT_ROOT / "one-page.pdf").resolve()
    cases = ((None, "zh-CN", {"v": 1}), ("en", None, {"v": 1}), ("en", "zh-CN", []))
    for source_language, target_language, snapshot in cases:
        with pytest.raises(DomainContractError):
            adapter.submit(source, source_language, target_language, snapshot)
    assert run_manifest_count(config.workspace) == 0


@pytest.mark.contract
def test_p3_1_t09_duplicate_submission_creates_independent_run_identity() -> None:
    """P3.1-T09：同一合法请求重复提交产生独立 Run，但内容指纹完全一致。"""

    config = p3_config("p3-1-t09")
    adapter = standalone_adapter(config)
    source = (INPUT_ROOT / "one-page.pdf").resolve()
    first = adapter.submit(source, "en", "zh-CN", {"profile": "fixed"})
    second = adapter.submit(source, "en", "zh-CN", {"profile": "fixed"})
    assert first.request.run_id != second.request.run_id
    assert first.request.job_id != second.request.job_id
    assert first.request.source_hash == second.request.source_hash
    assert first.request.config_snapshot_hash == second.request.config_snapshot_hash
    assert run_manifest_count(config.workspace) == 2
    assert not config.workspace.joinpath("product_jobs").exists()


@pytest.mark.integration
def test_p3_2_t01_first_page_checkpoint_and_manifest_are_consistent() -> None:
    """P3.2-T01：首次提交 v1 后文件与 manifest 同时可读且哈希一致。"""

    root = fresh_run_root("p3-2-t01")
    adapter = FilesystemCheckpointAdapter(root, "run-1")
    record = checkpoint_record("run-1", 1, b"page-state-v1")
    assert adapter.commit_page(0, record, 0) == record
    assert adapter.load_page(0) == record
    manifest = json.loads((root / "job/checkpoint_manifest.json").read_text("utf-8"))
    entry = manifest["pages"]["0"]
    assert sha256_file(root / entry["relative_path"]) == entry["file_hash"]


@pytest.mark.integration
def test_p3_2_t02_higher_version_replaces_authority_and_lower_is_rejected() -> None:
    """P3.2-T02：v2 成为权威，再提交 v1 被拒绝且 v2 保持不变。"""

    root = fresh_run_root("p3-2-t02")
    adapter = FilesystemCheckpointAdapter(root, "run-2")
    first = checkpoint_record("run-2", 1, b"v1")
    second = checkpoint_record("run-2", 2, b"v2")
    adapter.commit_page(0, first, 0)
    adapter.commit_page(0, second, 1)
    with pytest.raises(PortCallError) as captured:
        adapter.commit_page(0, first, 2)
    assert captured.value.code is ErrorCode.CHECKPOINT_CONFLICT
    assert adapter.load_page(0) == second


@pytest.mark.integration
def test_p3_2_t03_same_version_is_idempotent_but_fork_conflicts() -> None:
    """P3.2-T03：同版本相同内容幂等，同版本不同内容冲突且无重复权威文件。"""

    root = fresh_run_root("p3-2-t03")
    adapter = FilesystemCheckpointAdapter(root, "run-3")
    record = checkpoint_record("run-3", 2, b"same-v2")
    adapter.commit_page(0, record, 0)
    adapter.commit_page(0, record, 0)
    assert len(tuple(root.glob("pages/0000/checkpoints/*.json"))) == 1
    fork = checkpoint_record("run-3", 2, b"fork-v2")
    with pytest.raises(PortCallError) as captured:
        adapter.commit_page(0, fork, 2)
    assert captured.value.code is ErrorCode.CHECKPOINT_CONFLICT
    assert adapter.load_page(0) == record


@pytest.mark.fault_injection
def test_p3_2_t04_crash_before_checkpoint_rename_cleans_registered_partial() -> None:
    """P3.2-T04：rename 前崩溃不产生权威项，恢复清理 partial 后可重放。"""

    root = fresh_run_root("p3-2-t04")
    adapter = FilesystemCheckpointAdapter(root, "run-4")
    record = checkpoint_record("run-4", 1, b"crash-before-rename")
    with pytest.raises(InjectedCrash):
        adapter.commit_page(0, record, 0, crash_at="before_checkpoint_rename")
    recovery = adapter.recover()
    assert recovery["cleaned_partials"]
    assert adapter.load_page(0) is None
    assert adapter.commit_page(0, record, 0) == record


@pytest.mark.fault_injection
def test_p3_2_t05_crash_after_rename_reports_unreferenced_orphan() -> None:
    """P3.2-T05：rename 后 manifest 前崩溃只产生孤儿，不伪装已提交状态。"""

    root = fresh_run_root("p3-2-t05")
    adapter = FilesystemCheckpointAdapter(root, "run-5")
    record = checkpoint_record("run-5", 1, b"orphan-after-rename")
    with pytest.raises(InjectedCrash):
        adapter.commit_page(0, record, 0, crash_at="after_checkpoint_rename")
    assert adapter.load_page(0) is None
    recovery = adapter.recover()
    assert len(recovery["orphans"]) == 1
    assert adapter.commit_page(0, record, 0) == record


@pytest.mark.fault_injection
def test_p3_2_t06_restart_after_manifest_loads_v2_without_replay() -> None:
    """P3.2-T06：manifest 提交后崩溃，重启校验并直接加载 v2。"""

    root = fresh_run_root("p3-2-t06")
    adapter = FilesystemCheckpointAdapter(root, "run-6")
    record = checkpoint_record("run-6", 2, b"committed-v2")
    with pytest.raises(InjectedCrash):
        adapter.commit_page(0, record, 0, crash_at="after_checkpoint_manifest")
    restarted = FilesystemCheckpointAdapter(root, "run-6")
    restarted.recover()
    assert restarted.load_page(0) == record
    assert restarted.commit_page(0, record, 0) == record
    assert len(tuple(root.glob("pages/0000/checkpoints/*.json"))) == 1


@pytest.mark.contract
def test_p3_2_t07_page_path_escape_and_external_reparse_are_rejected() -> None:
    """P3.2-T07：父目录逃逸和指向其他 Run 的重解析路径均拒绝。"""

    root = fresh_run_root("p3-2-t07")
    other = fresh_run_root("p3-2-t07-other")
    adapter = FilesystemCheckpointAdapter(root, "run-7")
    with pytest.raises(PortCallError):
        adapter.resolve_run_relative("../p3-2-t07-other/file.json")
    other_file = other / "outside.json"
    other_file.write_text("{}", encoding="utf-8")
    link = root / "linked-other"
    create_symlink(link, other, directory=True)
    with pytest.raises(PortCallError):
        adapter.resolve_run_relative("linked-other/outside.json", must_exist=True)


@pytest.mark.integration
def test_p3_2_t08_recovery_preserves_unknown_and_other_run_files() -> None:
    """P3.2-T08：恢复扫描只报告孤儿，不递归删除未知或其他 Run 文件。"""

    root = fresh_run_root("p3-2-t08")
    other = fresh_run_root("p3-2-t08-other")
    unknown = root / "pages/0000/checkpoints/unknown.json"
    unknown.parent.mkdir(parents=True)
    unknown.write_text("{}", encoding="utf-8")
    other_file = other / "keep.txt"
    other_file.write_text("keep", encoding="utf-8")
    recovery = FilesystemCheckpointAdapter(root, "run-8").recover()
    assert unknown.relative_to(root).as_posix() in recovery["orphans"]
    assert unknown.is_file()
    assert other_file.is_file()


@pytest.mark.integration
def test_p3_3_t01_artifact_put_and_verify_match_content_hash_path_and_label() -> None:
    """P3.3-T01：Artifact put 后内容、哈希、路径和标签全部一致。"""

    root = fresh_run_root("p3-3-t01")
    store = SharedFilesystemArtifactAdapter(root, "run-a1")
    payload = artifact_payload("artifact-1", b'{"status":"ok"}')
    reference = store.put_atomic(payload, "reports/result.json", "report")
    assert store.verify(reference)
    assert store.get(reference.artifact_id) == payload.content
    assert reference.relative_path == "reports/result.json"
    assert reference.label == "report"


@pytest.mark.integration
def test_p3_3_t02_artifact_replay_is_idempotent_and_overwrite_is_rejected() -> None:
    """P3.3-T02：相同内容复用同一引用，同路径不同内容不可覆盖。"""

    root = fresh_run_root("p3-3-t02")
    store = SharedFilesystemArtifactAdapter(root, "run-a2")
    first_payload = artifact_payload("artifact-1", b"first")
    first = store.put_atomic(first_payload, "reports/immutable.bin", "report")
    assert store.put_atomic(first_payload, "reports/immutable.bin", "report") == first
    with pytest.raises(PortCallError) as captured:
        store.put_atomic(
            artifact_payload("artifact-2", b"second"),
            "reports/immutable.bin",
            "report",
        )
    assert captured.value.code is ErrorCode.ARTIFACT_IMMUTABLE_CONFLICT
    assert store.get("artifact-1") == b"first"


@pytest.mark.integration
def test_p3_3_t03_checkpoint_rejects_missing_corrupt_and_hash_mismatch_artifacts() -> None:
    """P3.3-T03：不存在、损坏或哈希不匹配的 Artifact 引用均阻断 Checkpoint。"""

    root = fresh_run_root("p3-3-t03")
    store = SharedFilesystemArtifactAdapter(root, "run-a3")
    checkpoints = FilesystemCheckpointAdapter(root, "run-a3", store)
    missing = ArtifactReference(
        "missing",
        "application/json",
        sha256_bytes(b"missing"),
        len(b"missing"),
        "reports/missing.json",
        "report",
    )
    with pytest.raises(PortCallError) as captured:
        checkpoints.commit_page(0, checkpoint_record("run-a3", 1, b"state", (missing,)), 0)
    assert captured.value.code is ErrorCode.ARTIFACT_INTEGRITY_FAILED
    reference = store.put_atomic(
        artifact_payload("artifact-1", b"valid"),
        "reports/valid.bin",
        "report",
    )
    (root / "reports/valid.bin").write_bytes(b"corrupt")
    with pytest.raises(PortCallError):
        checkpoints.commit_page(0, checkpoint_record("run-a3", 1, b"state", (reference,)), 0)
    mismatch = replace(reference, content_hash=HASH_A)
    with pytest.raises(PortCallError):
        checkpoints.commit_page(0, checkpoint_record("run-a3", 1, b"state", (mismatch,)), 0)
    assert checkpoints.load_page(0) is None
    shutil.rmtree(root)


@pytest.mark.integration
def test_p3_3_t04_success_degradation_and_error_logs_have_all_fields() -> None:
    """P3.3-T04：成功、降级和错误日志的必填字段完整率为 100%。"""

    root = fresh_run_root("p3-3-t04")
    audit = StructuredAuditLogger(root)
    expected_keys = set(audit_event("COMPLETED"))
    events = (
        audit.write(audit_event("COMPLETED")),
        audit.write(audit_event("COMPLETED_WITH_DEGRADATION", fallback="REGION_FALLBACK")),
        audit.write(audit_event("PROCESS_FAILED", error_code="E_RENDER")),
    )
    assert all(set(event) == expected_keys for event in events)
    assert audit.read_events() == events


@pytest.mark.integration
def test_p3_3_t05_logs_redact_secrets_and_truncate_unbounded_payloads() -> None:
    """P3.3-T05：API key、token 和长原文/响应不会原样进入日志。"""

    root = fresh_run_root("p3-3-t05")
    audit = StructuredAuditLogger(root, limit=64)
    event = audit_event("PROCESS_FAILED", error_code="E_PROVIDER")
    secret = "sk-p3-sensitive-secret"
    event.update(
        {
            "api_key": secret,
            "provider_response": f"Bearer {secret} " + "R" * 1000,
            "source_text": "S" * 1000,
            "token": secret,
        }
    )
    sanitized = audit.write(event)
    serialized = json.dumps(sanitized, ensure_ascii=False)
    assert secret not in serialized
    assert "api_key" not in serialized and "token" not in serialized
    assert serialized.count("[TRUNCATED]") == 2
    assert len((root / "logs/audit.jsonl").read_text("utf-8")) < 2048


@pytest.mark.contract
def test_p3_4_t01_fixed_and_deterministic_translation_are_reproducible() -> None:
    """P3.4-T01：同一批次两次调用的顺序、译文和响应哈希差异为 0。"""

    batch = translation_batch()
    adapters = (
        FixedTranslationAdapter({"unit-1": "收入", "unit-2": "利润"}),
        DeterministicTranslationAdapter(),
    )
    for adapter in adapters:
        first = adapter.translate(batch)
        second = adapter.translate(batch)
        assert first == second
        assert tuple(unit.unit_id for unit in first.units) == batch.ordered_unit_ids
        assert content_sha256(first) == content_sha256(second)


@pytest.mark.integration
def test_p3_4_t02_real_fake_service_returns_valid_decision_and_translation() -> None:
    """P3.4-T02：真实 fake HTTP 的 ModelDecision 与 Translation 正向合同均有效。"""

    with running_fake_service() as service:
        adapter = http_adapter(service)
        decision_request = ModelDecisionRequest(
            "decision-1",
            "classification",
            "transflow.model-decision-request/v1",
            ("evidence-1",),
        )
        decision = adapter.decide(decision_request)
        bundle = adapter.translate(translation_batch())
    assert decision.decision_id == decision_request.decision_id
    assert decision.result_code == "body.table"
    assert tuple(unit.unit_id for unit in bundle.units) == bundle.requested_unit_ids
    assert len(bundle.units) == len(set(unit.unit_id for unit in bundle.units)) == 2


@pytest.mark.integration
def test_p3_4_t03_timeout_5xx_and_429_map_to_retryable_errors() -> None:
    """P3.4-T03：真实超时、5xx 和 429 均映射为可重试错误且无业务结果。"""

    expected = {
        "timeout": ErrorCode.AI_TIMEOUT,
        "500": ErrorCode.AI_SERVER_ERROR,
        "429": ErrorCode.AI_RATE_LIMITED,
    }
    for mode, code in expected.items():
        with running_fake_service() as service:
            service.state.mode = mode
            service.state.timeout_delay_seconds = 0.2
            adapter = http_adapter(service, timeout=0.05 if mode == "timeout" else 0.2)
            with pytest.raises(PortCallError) as captured:
                adapter.translate(translation_batch())
        assert captured.value.code is code
        assert captured.value.retryable is True


@pytest.mark.integration
def test_p3_4_t04_invalid_json_schema_and_unit_identity_are_rejected() -> None:
    """P3.4-T04：非法 JSON、Schema 及缺失/重复/新增 ID 全部拒绝。"""

    for mode in ("invalid_json", "schema_error", "missing_id", "duplicate_id", "extra_id"):
        with running_fake_service() as service:
            service.state.mode = mode
            with pytest.raises(PortCallError) as captured:
                http_adapter(service).translate(translation_batch())
        assert captured.value.code is ErrorCode.AI_RESPONSE_INVALID
        assert captured.value.retryable is False


@pytest.mark.integration
def test_p3_4_t05_bad_token_and_oversized_request_are_rejected_without_leak(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """P3.4-T05：错误 token 与过大请求由真实服务拒绝且日志无令牌。"""

    wrong_token = "p3-wrong-sensitive-token"
    with running_fake_service(max_request_bytes=256) as service, caplog.at_level(logging.INFO):
        with pytest.raises(PortCallError) as captured:
            http_adapter(service, token=wrong_token).translate(translation_batch())
        assert captured.value.code is ErrorCode.AI_AUTH_FAILED
        with pytest.raises(PortCallError) as captured:
            http_adapter(service).translate(translation_batch("X" * 2048))
        assert captured.value.code is ErrorCode.AI_REQUEST_TOO_LARGE
    assert wrong_token not in caplog.text
    assert FAKE_TOKEN not in caplog.text


@pytest.mark.integration
def test_p3_4_t06_production_wheel_excludes_real_provider_and_fake_service() -> None:
    """P3.4-T06：真实构建 wheel 不包含 Qwen/Provider 接线或 fake 服务脚本。"""

    output = P3_TEST_ROOT / "wheel"
    if output.exists():
        shutil.rmtree(output)
    completed = subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(output)],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    wheels = tuple(output.glob("*.whl"))
    assert len(wheels) == 1
    with zipfile.ZipFile(wheels[0]) as archive:
        names = tuple(name.casefold() for name in archive.namelist())
        python_sources = "\n".join(
            archive.read(name).decode("utf-8", errors="replace")
            for name in archive.namelist()
            if name.endswith(".py")
        ).casefold()
    forbidden_names = ("qwen.py", "provider.py", "fake_ai_service.py")
    assert not any(name.endswith(forbidden_names) for name in names)
    assert "import litellm" not in python_sources and "from litellm" not in python_sources
    assert "scripts/fake_ai_service" not in "\n".join(names)


@pytest.mark.integration
def test_p3_4_t07_real_fake_liveness_readiness_and_shutdown_are_truthful() -> None:
    """P3.4-T07：两个依赖决定真实 ready，停服后 P1 探针不误报。"""

    service = FakeAiService(FAKE_TOKEN).start()
    ready_url = f"{service.base_url}/health/ready"
    config = replace(
        p3_config("p3-4-t07"),
        ai_capability_url=ready_url,
    )
    health = HealthService(config, timeout_seconds=0.1)
    assert health.liveness().ok is True
    assert health.readiness().ok is True
    service.state.decision_ready = False
    assert health.readiness().ok is False
    service.state.decision_ready = True
    service.state.translation_ready = False
    assert health.readiness().ok is False
    service.state.translation_ready = True
    service.stop()
    assert health.liveness().ok is True
    assert health.readiness().ok is False


@pytest.mark.fault_injection
def test_p3_5_t01_artifact_crash_before_rename_has_no_authority_and_replays() -> None:
    """P3.5-T01：Artifact rename 前崩溃无权威引用，清理 partial 后可重放。"""

    root = fresh_run_root("p3-5-t01")
    store = SharedFilesystemArtifactAdapter(root, "run-f1")
    payload = artifact_payload("artifact-1", b"before-rename")
    with pytest.raises(InjectedCrash):
        store.put_atomic(
            payload,
            "artifacts/audit/value.bin",
            "audit",
            crash_at="before_artifact_rename",
        )
    recovery = store.recover()
    assert recovery["cleaned_partials"]
    with pytest.raises(PortCallError):
        store.get("artifact-1")
    assert store.put_atomic(payload, "artifacts/audit/value.bin", "audit")


@pytest.mark.fault_injection
def test_p3_5_t02_artifact_after_rename_is_orphan_not_committed_state() -> None:
    """P3.5-T02：Artifact rename 后 manifest 前崩溃只形成可识别孤儿。"""

    root = fresh_run_root("p3-5-t02")
    store = SharedFilesystemArtifactAdapter(root, "run-f2")
    payload = artifact_payload("artifact-1", b"after-rename")
    with pytest.raises(InjectedCrash):
        store.put_atomic(
            payload,
            "artifacts/audit/orphan.bin",
            "audit",
            crash_at="after_artifact_rename",
        )
    assert store.scan_orphans() == ("artifacts/audit/orphan.bin",)
    with pytest.raises(PortCallError):
        store.get("artifact-1")
    assert store.put_atomic(payload, "artifacts/audit/orphan.bin", "audit")


@pytest.mark.fault_injection
def test_p3_5_t03_crash_after_manifest_reads_by_hash_and_skips_replay() -> None:
    """P3.5-T03：manifest 后崩溃的 Artifact 按哈希恢复并幂等跳过重放。"""

    root = fresh_run_root("p3-5-t03")
    store = SharedFilesystemArtifactAdapter(root, "run-f3")
    payload = artifact_payload("artifact-1", b"after-manifest")
    with pytest.raises(InjectedCrash):
        store.put_atomic(
            payload,
            "artifacts/audit/committed.bin",
            "audit",
            crash_at="after_artifact_manifest",
        )
    restarted = SharedFilesystemArtifactAdapter(root, "run-f3")
    restarted.recover()
    reference = restarted.put_atomic(payload, "artifacts/audit/committed.bin", "audit")
    assert restarted.verify(reference)
    assert restarted.get("artifact-1") == payload.content


@pytest.mark.fault_injection
def test_p3_5_t04_final_rename_before_publish_preserves_old_authority() -> None:
    """P3.5-T04：新 final 已存在但发布前崩溃时，旧权威指针保持不变。"""

    root = fresh_run_root("p3-5-t04")
    store = SharedFilesystemArtifactAdapter(root, "run-f4")
    old = store.put_atomic(
        artifact_payload("final-old", b"old-final", "application/pdf"),
        "final/old.pdf",
        "final",
    )
    store.publish_final(old)
    new = store.put_atomic(
        artifact_payload("final-new", b"new-final", "application/pdf"),
        "final/new.pdf",
        "final",
    )
    with pytest.raises(InjectedCrash):
        store.publish_final(new, crash_at="before_final_manifest")
    assert store.published_final() == old
    assert store.verify(new)
    store.publish_final(new)
    assert store.published_final() == new


@pytest.mark.fault_injection
def test_p3_5_t05_recovery_never_deletes_unknown_cross_run_or_reparse_targets() -> None:
    """P3.5-T05：恢复只处理本 Run journal 项，不删除未知、跨 Run 或链接目标。"""

    root = fresh_run_root("p3-5-t05")
    other = fresh_run_root("p3-5-t05-other")
    store = SharedFilesystemArtifactAdapter(root, "run-f5")
    with pytest.raises(InjectedCrash):
        store.put_atomic(
            artifact_payload("pending", b"pending"),
            "artifacts/audit/pending.bin",
            "audit",
            crash_at="before_artifact_rename",
        )
    unknown = root / "artifacts/audit/unknown.bin"
    unknown.write_bytes(b"unknown")
    other_file = other / "keep.bin"
    other_file.write_bytes(b"keep")
    link = root / "artifacts/audit/cross-run-link"
    create_symlink(link, other, directory=True)
    recovery = store.recover()
    assert recovery["cleaned_partials"]
    assert unknown.is_file()
    assert link.is_symlink() or link.is_junction()
    assert other_file.read_bytes() == b"keep"
