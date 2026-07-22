"""验收 TM0 参数化逐叶迁移入口、冻结基线和证据安全边界。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pymupdf
import pytest

import scripts.run_toolbox_leaf_migration as migration_runner
from scripts.run_toolbox_leaf_migration import (
    CATALOG_PATH,
    ROUTE_STAGES,
    MigrationContractError,
    canonical_stage,
    catalog_route_selection,
    compute_page_hash,
    dependencies_satisfied,
    freeze_tm0_baseline,
    load_leaf_input_manifest,
    provider_configuration_snapshot,
    route_slug,
    store_translation_bundle,
)
from scripts.run_toolbox_leaf_migration import main as run_main
from scripts.verify_toolbox_leaf_migration import verify_tm0_baseline
from transflow.domain.translation import (
    TranslatedUnit,
    TranslationBatch,
    TranslationBundle,
    TranslationUnit,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def _sha256_file(path: Path) -> str:
    """计算测试 PDF 的稳定内容哈希。"""

    return hashlib.sha256(path.read_bytes()).hexdigest()


def _complete_pdf(path: Path) -> Path:
    """创建两页完整 PDF，证明 Manifest 不能用拆页结果冒充产品输入。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    with pymupdf.open() as document:
        first = document.new_page(width=320, height=420)
        first.insert_text((36, 72), "First complete document page", fontsize=11)
        second = document.new_page(width=320, height=420)
        second.insert_text((36, 72), "Second target page", fontsize=11)
        document.save(path)
    return path


def _manifest_payload(repository_root: Path, source: Path) -> dict[str, object]:
    """建立不含样本身份、gold 或强制 Route 的最小合法输入。"""

    return {
        "schema_version": "transflow.toolbox-leaf-migration-input/v1",
        "route": "body.flow_text.single",
        "source_document": {
            "path": source.relative_to(repository_root).as_posix(),
            "sha256": _sha256_file(source),
            "page_count": 2,
        },
        "target_page": {
            "page_no": 2,
            "page_hash": compute_page_hash(source, 2),
            "spike_leaf_contract_route": "body.flow_text.single",
        },
        "source_language": "en",
        "target_language": "zh-CN",
        "authorization": {
            "approved": True,
            "allowed_routes": ["body.flow_text.single"],
            "allowed_operations": [
                "CLASSIFY",
                "TRANSLATE",
                "RENDER",
                "COMPARE",
                "FINALIZE",
            ],
            "evidence_ref": "authorizations/tm2.json",
        },
    }


def test_tm0_t01_route_stage_map_covers_explicit_catalog_without_override() -> None:
    """TM0-T01：同一入口覆盖全部显式 Route，且阶段与 Route 一一固定。"""

    payload = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    catalog_routes = {item["route"] for item in payload["entries"]}
    assert set(ROUTE_STAGES) == catalog_routes
    for route, stage in ROUTE_STAGES.items():
        selection = catalog_route_selection(route)
        assert selection["route"] == route
        assert canonical_stage(stage) == stage
    assert canonical_stage("TM02") == "TM2"
    assert route_slug("body.composite.flow_text_table") == "body_composite_flow_text_table"


def test_tm0_t02_manifest_binds_complete_pdf_route_page_and_authorization(
    tmp_path: Path,
) -> None:
    """TM0-T02：输入合同只接受完整文档、同 Route、真实页哈希和授权集合。"""

    source = _complete_pdf(tmp_path / "samples" / "document.pdf")
    authorization = tmp_path / "authorizations" / "tm2.json"
    authorization.parent.mkdir(parents=True)
    authorization.write_text('{"approved":true}\n', encoding="utf-8")
    manifest = tmp_path / "manifests" / "single.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps(_manifest_payload(tmp_path, source), ensure_ascii=False),
        encoding="utf-8",
    )

    loaded = load_leaf_input_manifest(
        manifest,
        stage="TM2",
        route="body.flow_text.single",
        repository_root=tmp_path,
    )

    assert loaded.source_path == source.resolve()
    assert loaded.page_no == 2
    assert loaded.page_hash == compute_page_hash(source, 2)


@pytest.mark.parametrize(
    ("mutation", "error_code"),
    (
        ({"forced_route": "body.flow_text.single"}, "MANIFEST_FIELDS_INVALID"),
        ({"gold": "expected-answer"}, "MANIFEST_FIELDS_INVALID"),
    ),
)
def test_tm0_t03_manifest_rejects_hidden_route_or_gold(
    tmp_path: Path,
    mutation: dict[str, object],
    error_code: str,
) -> None:
    """TM0-T03：Manifest 不能成为强制分类或样本答案的隐藏通道。"""

    source = _complete_pdf(tmp_path / "samples" / "document.pdf")
    authorization = tmp_path / "authorizations" / "tm2.json"
    authorization.parent.mkdir(parents=True)
    authorization.write_text('{"approved":true}\n', encoding="utf-8")
    payload = _manifest_payload(tmp_path, source)
    payload.update(mutation)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(MigrationContractError, match=error_code):
        load_leaf_input_manifest(
            manifest,
            stage="TM2",
            route="body.flow_text.single",
            repository_root=tmp_path,
        )


def test_tm0_t04_translation_bundle_is_content_addressed_and_shared(tmp_path: Path) -> None:
    """TM0-T04：同一真实 Bundle 只保存一次，并给两侧返回同一内容哈希。"""

    unit = TranslationUnit("unit-1", 2, 0, "Annual report", "body-1")
    batch = TranslationBatch("batch-1", "en", "zh-CN", (unit,))
    bundle = TranslationBundle.from_batch(
        batch,
        (TranslatedUnit("unit-1", "年度报告"),),
    )
    provider = {
        "adapter": "migration_qwen_translation_adapter",
        "base_url_configured": True,
        "api_key_configured": True,
        "model_configured": True,
    }

    first = store_translation_bundle(batch, bundle, tmp_path, provider)
    second = store_translation_bundle(batch, bundle, tmp_path, provider)
    stored = json.loads(first.path.read_text(encoding="utf-8"))

    assert first == second
    assert first.path.name == f"{first.bundle_hash}.json"
    assert stored["bundle_hash"] == first.bundle_hash
    assert stored["consumption_contract"] == "SAME_HASH_FOR_SPIKE_AND_TRANSFLOW"
    assert "Annual report" not in first.path.read_text(encoding="utf-8")


def test_tm0_t05_provider_snapshot_never_persists_secret_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TM0-T05：Provider 快照只记录配置状态，不读取或输出秘密值。"""

    monkeypatch.setenv("TRANSFLOW_MIGRATION_QWEN_BASE_URL", "https://secret.invalid")
    monkeypatch.setenv("TRANSFLOW_MIGRATION_QWEN_API_KEY", "super-secret-token")
    monkeypatch.setenv("TRANSFLOW_MIGRATION_QWEN_MODEL", "secret-model")

    snapshot = provider_configuration_snapshot()
    serialized = json.dumps(snapshot, ensure_ascii=False)

    assert snapshot["base_url_configured"] is True
    assert snapshot["api_key_configured"] is True
    assert snapshot["model_configured"] is True
    assert "super-secret-token" not in serialized
    assert "https://secret.invalid" not in serialized
    assert "secret-model" not in serialized


def test_tm0_t06_baseline_freeze_is_reproducible_and_verifiable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TM0-T06：冻结证据可复算，且不修改默认 Catalog。"""

    # 历史 TM0 冻结发生在具体 Route 驱动注册前；后续阶段回归时显式恢复该时点。
    monkeypatch.setattr(migration_runner, "DRIVER_FACTORIES", {})
    evidence_root = tmp_path / "evidence"
    output_root = tmp_path / "output"
    pointer = tmp_path / "manifests" / "tm0_baseline.json"
    catalog_before = CATALOG_PATH.read_bytes()

    result = freeze_tm0_baseline(
        "tm0-test-run",
        evidence_root=evidence_root,
        output_root=output_root,
        baseline_pointer_path=pointer,
    )
    violations = verify_tm0_baseline(
        "tm0-test-run",
        evidence_root=evidence_root,
        output_root=output_root,
        baseline_pointer_path=pointer,
    )

    assert not violations
    assert result.baseline_hash == json.loads(pointer.read_text(encoding="utf-8"))[
        "baseline_hash"
    ]
    assert CATALOG_PATH.read_bytes() == catalog_before


def test_tm0_t07_stage_dependency_blocks_review_pending_and_accepts_owner_decision(
    tmp_path: Path,
) -> None:
    """TM0-T07：TM1 在 TM0 人工确认前必须被硬阻断。"""

    gate_root = tmp_path / "gates"
    gate_root.mkdir()
    gate = gate_root / "tm0_gate.json"
    gate.write_text('{"stage":"TM0","status":"REVIEW_PENDING"}\n', encoding="utf-8")
    assert dependencies_satisfied("TM1", gate_root=gate_root) == (
        "DEPENDENCY_NOT_ACCEPTED:TM0:REVIEW_PENDING",
    )

    gate.write_text('{"stage":"TM0","status":"ACCEPTED"}\n', encoding="utf-8")
    assert dependencies_satisfied("TM1", gate_root=gate_root) == ()


def test_tm0_t08_cli_failure_is_nonzero_and_never_claims_pass(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """TM0-T08：入口合同失败必须非零退出，输出不得伪称通过。"""

    return_code = run_main(
        [
            "--stage",
            "TM2",
            "--route",
            "body.flow_text.single",
            "--manifest",
            "resources/manifests/toolbox_leaf_migration/missing.json",
            "--run-id",
            "tm0-contract-failure",
        ]
    )
    output = capsys.readouterr().out

    assert return_code != 0
    assert '"status": "FAIL"' in output
    assert '"status": "PASS"' not in output
