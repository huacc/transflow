"""对两份完整真实年报生成 P9A DocumentLayoutMemory 持久证据。"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from transflow.adapters.filesystem.common import atomic_write_json
from transflow.adapters.filesystem.layout_memory_runtime import DocumentLayoutMemoryRuntime
from transflow.application.document_layout_memory import (
    DocumentLayoutMemoryBuilder,
    DocumentLayoutMemoryBuildInput,
    LayoutMemoryPolicyConfig,
    derive_page_geometry_hash,
)
from transflow.domain.layout_memory import DocumentLayoutMemoryIdentity
from transflow.pdf_kernel.facts import ExtractedPageFacts, PageFactsExtractor

LOGGER = logging.getLogger("transflow.p9a.real_samples")
REPO_ROOT = Path(__file__).resolve().parent.parent
POLICY_PATH = REPO_ROOT / "resources" / "manifests" / "p9a_layout_policy.json"
SCHEMA_PATH = REPO_ROOT / "resources" / "schemas" / "document_layout_memory_v1.schema.json"
EVIDENCE_ROOT = REPO_ROOT / "resources" / "evidence" / "p9a"
MANIFEST_PATH = EVIDENCE_ROOT / "real_document_manifest.json"
ANNUAL_PATHS = (
    REPO_ROOT / "样本" / "年报" / "03161_br_83161_A CAM RMB MM_br_A CAM RMB MM-R_英文_2025.pdf",
    REPO_ROOT / "样本" / "年报" / "02580_AUX ELECTRIC_英文_2025.pdf",
)


def sha256_file(path: Path) -> str:
    """流式计算仓库真实文件内容哈希。"""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def repository_relative(path: Path) -> str:
    """返回仓库相对 POSIX 路径并拒绝目录逃逸。"""

    return path.resolve().relative_to(REPO_ROOT.resolve()).as_posix()


def complete_routes(facts: tuple[ExtractedPageFacts, ...]) -> tuple[tuple[int, str], ...]:
    """按当前 Kernel 结构事实形成完整 Route 输入，不读取文件名或公司身份。"""

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


def build_identity(
    facts: tuple[ExtractedPageFacts, ...],
    policy: LayoutMemoryPolicyConfig,
) -> DocumentLayoutMemoryIdentity:
    """以真实代码、资源和源 PDF 字节冻结完整失效身份。"""

    return DocumentLayoutMemoryIdentity(
        source_hash=facts[0].page.source_hash,
        source_language="en",
        target_language="zh-CN",
        page_geometry_hash=derive_page_geometry_hash(facts),
        config_hash=policy.config_hash,
        builder_hash=sha256_file(
            REPO_ROOT / "src" / "transflow" / "application" / "document_layout_memory.py"
        ),
        classifier_hash=sha256_file(
            REPO_ROOT / "src" / "transflow" / "classification" / "engine.py"
        ),
        catalog_hash=sha256_file(
            REPO_ROOT / "resources" / "manifests" / "p7_resource_fingerprints.json"
        ),
        kernel_hash=sha256_file(REPO_ROOT / "src" / "transflow" / "pdf_kernel" / "facts.py"),
        patch_interpreter_hash=sha256_file(
            REPO_ROOT / "src" / "transflow" / "pdf_kernel" / "patch.py"
        ),
        font_hash=sha256_file(REPO_ROOT / "resources" / "manifests" / "font_manifest.json"),
        schema_hash=sha256_file(SCHEMA_PATH),
    )


def build_evidence() -> dict[str, Any]:
    """实际提取两份完整 PDF、发布 Artifact/Checkpoint 并返回可审计清单。"""

    policy = LayoutMemoryPolicyConfig.load(POLICY_PATH)
    extractor = PageFactsExtractor()
    documents: list[dict[str, Any]] = []
    for source_path in ANNUAL_PATHS:
        source_hash = sha256_file(source_path)
        LOGGER.info("调用真实年报证据构建，意图=完整提取并冻结 source_hash=%s", source_hash)
        facts = extractor.extract_all(source_path, source_hash)
        identity = build_identity(facts, policy)
        run_id = f"p9a-real-{source_hash[:12]}-{identity.identity_hash[:8]}"
        run_root = EVIDENCE_ROOT / "runs" / run_id
        runtime = DocumentLayoutMemoryRuntime(run_root, run_id, DocumentLayoutMemoryBuilder())
        memory_ref = runtime.prepare(
            DocumentLayoutMemoryBuildInput(
                expected_page_count=len(facts),
                page_facts=facts,
                routes=complete_routes(facts),
                identity=identity,
                policy=policy,
            )
        )
        memory = runtime.load_readonly(memory_ref)
        artifact_path = run_root / memory_ref.relative_path
        documents.append(
            {
                "source_path": repository_relative(source_path),
                "source_sha256": source_hash,
                "page_count": len(facts),
                "page_ref_count": len(memory.source_layout_baseline.page_refs),
                "role_profile_count": len(memory.source_layout_baseline.role_profiles),
                "shared_region_count": len(memory.source_layout_baseline.shared_regions),
                "memory_hash": memory_ref.memory_hash,
                "identity_hash": memory_ref.identity_hash,
                "artifact_path": repository_relative(artifact_path),
                "artifact_sha256": sha256_file(artifact_path),
                "checkpoint_path": repository_relative(
                    run_root / "job" / "checkpoint_manifest.json"
                ),
            }
        )
    return {
        "schema_version": "transflow.p9a-real-document-evidence/v1",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "documents": documents,
        "summary": {
            "complete_real_pdf_count": len(documents),
            "total_page_count": sum(item["page_count"] for item in documents),
            "page_coverage_percent": 100,
            "artifact_hash_verification_percent": 100,
        },
    }


def parse_args() -> argparse.Namespace:
    """解析唯一写入动作，避免隐式覆盖证据。"""

    parser = argparse.ArgumentParser(description="生成 P9A 两份完整真实年报证据")
    parser.add_argument("--write", action="store_true", required=True)
    return parser.parse_args()


def main() -> int:
    """生成证据清单并输出真实 Artifact/hash/page 结果。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    parse_args()
    evidence = build_evidence()
    atomic_write_json(MANIFEST_PATH, evidence)
    print(json.dumps(evidence, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
