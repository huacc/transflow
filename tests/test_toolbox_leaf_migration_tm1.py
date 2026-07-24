"""验收 TM1 visual_only 输入、静态注册和 runs 目录边界。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pymupdf
import pytest

from scripts.run_toolbox_leaf_migration import (
    OUTPUT_ROOT,
    MigrationContractError,
    compute_page_hash,
    load_leaf_input_manifest,
)
from scripts.toolbox_leaf_migration_drivers import DRIVER_FACTORIES, resolve_route_driver


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _visual_document(path: Path) -> Path:
    """建立四页完整测试 PDF；正式效果验收仍只使用真实文档。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    with pymupdf.open() as document:
        for index in range(4):
            page = document.new_page(width=320, height=420)
            page.draw_rect(
                pymupdf.Rect(40 + index, 50, 260, 350),
                color=(0, 0, 0),
            )
        document.save(path)
    return path


def _manifest(repository_root: Path, source: Path) -> dict[str, object]:
    kinds = (
        "pure_image",
        "pure_vector",
        "scanned_page",
        "mixed_no_editable_text",
    )
    return {
        "schema_version": "transflow.toolbox-leaf-migration-input/v1",
        "route": "visual_only",
        "source_document": {
            "path": source.relative_to(repository_root).as_posix(),
            "sha256": _sha256_file(source),
            "page_count": 4,
        },
        "target_page": {
            "page_no": 1,
            "page_hash": compute_page_hash(source, 1),
            "spike_leaf_contract_route": "visual_only",
        },
        "calibration_pages": [
            {
                "kind": kind,
                "page_no": page_no,
                "page_hash": compute_page_hash(source, page_no),
            }
            for page_no, kind in enumerate(kinds, start=1)
        ],
        "source_language": "en",
        "target_language": "zh-CN",
        "authorization": {
            "approved": True,
            "allowed_routes": ["visual_only"],
            "allowed_operations": ["CLASSIFY", "RENDER", "COMPARE", "FINALIZE"],
            "evidence_ref": "authorizations/tm1.json",
        },
    }


def test_tm1_t01_visual_manifest_requires_four_unique_calibration_kinds(
    tmp_path: Path,
) -> None:
    """TM1-T01：四类真实页事实只来自 Manifest，不进入运行时代码。"""

    source = _visual_document(tmp_path / "samples/document.pdf")
    authorization = tmp_path / "authorizations/tm1.json"
    authorization.parent.mkdir(parents=True)
    authorization.write_text('{"approved":true}\n', encoding="utf-8")
    manifest_path = tmp_path / "manifests/visual_only.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(
        json.dumps(_manifest(tmp_path, source), ensure_ascii=False),
        encoding="utf-8",
    )

    loaded = load_leaf_input_manifest(
        manifest_path,
        stage="TM1",
        route="visual_only",
        repository_root=tmp_path,
    )

    assert loaded.page_no == 1
    assert {item["kind"] for item in loaded.payload["calibration_pages"]} == {
        "pure_image",
        "pure_vector",
        "scanned_page",
        "mixed_no_editable_text",
    }


def test_tm1_t02_visual_manifest_rejects_duplicate_kind(tmp_path: Path) -> None:
    """TM1-T02：缺类或重复类不能被包装成四类校准通过。"""

    source = _visual_document(tmp_path / "samples/document.pdf")
    authorization = tmp_path / "authorizations/tm1.json"
    authorization.parent.mkdir(parents=True)
    authorization.write_text('{"approved":true}\n', encoding="utf-8")
    payload = _manifest(tmp_path, source)
    calibration = payload["calibration_pages"]
    assert isinstance(calibration, list)
    calibration[-1]["kind"] = "pure_image"
    manifest_path = tmp_path / "visual_only.json"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(MigrationContractError, match="VISUAL_CALIBRATION_SET_INVALID"):
        load_leaf_input_manifest(
            manifest_path,
            stage="TM1",
            route="visual_only",
            repository_root=tmp_path,
        )


def test_tm1_t03_migrated_drivers_are_statically_registered() -> None:
    """TM1-T03：已启动迁移的叶均显式登记，仍不做目录发现。"""

    assert set(DRIVER_FACTORIES) == {
        "body.chart",
        "body.diagram",
        "body.flow_text.single",
        "visual_only",
    }
    assert resolve_route_driver("visual_only") is not None
    assert resolve_route_driver("body.flow_text.single") is not None
    assert resolve_route_driver("body.chart") is not None
    assert resolve_route_driver("body.diagram") is not None


def test_tm1_t04_authoritative_output_root_is_runs() -> None:
    """TM1-T04：正式轮次进入 runs，旧 output/pdf 只允许保留索引。"""

    assert OUTPUT_ROOT.name == "toolbox_leaf_migration"
    assert OUTPUT_ROOT.parent.name == "runs"
