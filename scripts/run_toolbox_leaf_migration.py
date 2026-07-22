"""提供 TM0～TM18 共用的参数化单叶迁移入口和冻结证据工具。"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import logging
import os
import re
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pymupdf

# 计划中的正式命令使用 ``python scripts/<name>.py``；直接执行时先显式加入
# 仓库根和 src，确保与 ``python -m``、pytest 使用同一生产实现。
_BOOTSTRAP_ROOT = Path(__file__).resolve().parent.parent
for _bootstrap_path in (_BOOTSTRAP_ROOT, _BOOTSTRAP_ROOT / "src"):
    if str(_bootstrap_path) not in sys.path:
        sys.path.insert(0, str(_bootstrap_path))

# ``python -m`` 会把本文件命名为 __main__；先登记规范模块名，避免 Route 驱动
# 再次导入本文件并形成第二个 MigrationContractError 类型。
if __name__ == "__main__":
    sys.modules["scripts.run_toolbox_leaf_migration"] = sys.modules[__name__]

from scripts.toolbox_leaf_migration_drivers import (  # noqa: E402
    DRIVER_FACTORIES,
    LeafMigrationRunContext,
    resolve_route_driver,
)
from transflow.domain.common import content_sha256, json_ready  # noqa: E402
from transflow.domain.translation import TranslationBatch, TranslationBundle  # noqa: E402
from transflow.pdf_kernel.facts import PageFactsExtractor  # noqa: E402

LOGGER = logging.getLogger("transflow.toolbox_leaf_migration.run")
REPO_ROOT = _BOOTSTRAP_ROOT
CATALOG_PATH = REPO_ROOT / "resources" / "catalogs" / "page_toolbox_catalog_v4.json"
EVIDENCE_ROOT = REPO_ROOT / "resources" / "evidence" / "toolbox_leaf_migration"
TM0_OUTPUT_ROOT = REPO_ROOT / "output" / "pdf" / "toolbox_leaf_migration"
# TM1 起按负责人要求把完整轮次统一放在 runs；旧 output/pdf 只保留索引。
OUTPUT_ROOT = REPO_ROOT / "runs" / "toolbox_leaf_migration"
MANIFEST_ROOT = REPO_ROOT / "resources" / "manifests" / "toolbox_leaf_migration"
TM0_BASELINE_POINTER = MANIFEST_ROOT / "tm0_baseline.json"
GATE_ROOT = MANIFEST_ROOT

ROUTE_STAGES = MappingProxyType(
    {
        "visual_only": "TM1",
        "body.flow_text.single": "TM2",
        "body.chart": "TM3",
        "body.diagram": "TM4",
        "cover": "TM5",
        "contents": "TM6",
        "end": "TM7",
        "body.flow_text.multi": "TM8",
        "body.table": "TM9",
        "body.anchored_blocks": "TM10",
        "body.flow_text.visual_anchored": "TM11",
        "body.composite.flow_text_table": "TM12",
        "body.composite.chart_table": "TM13",
        "body.composite.flow_text_chart": "TM14",
        "body.composite.flow_text_diagram": "TM15",
        "body.composite.anchored_blocks_chart": "TM16",
        "body.freeform": "TM17",
    }
)
STAGE_DEPENDENCIES = MappingProxyType(
    {
        "TM0": (),
        "TM1": ("TM0",),
        "TM2": ("TM1",),
        "TM3": ("TM2",),
        "TM4": ("TM3",),
        "TM5": ("TM4",),
        "TM6": ("TM5",),
        "TM7": ("TM6",),
        "TM8": ("TM7", "TM2"),
        "TM9": ("TM8",),
        "TM10": ("TM9",),
        "TM11": ("TM2", "TM8", "TM10"),
        "TM12": ("TM2", "TM9"),
        "TM13": ("TM3", "TM9"),
        "TM14": ("TM2", "TM3"),
        "TM15": ("TM2", "TM4"),
        "TM16": ("TM3", "TM10"),
        "TM17": ("TM2", "TM3", "TM4", "TM8", "TM9", "TM10", "TM11"),
        "TM18": tuple(f"TM{index}" for index in range(1, 18)),
    }
)

STAGE_PATTERN = re.compile(r"^TM0*(\d+)$", re.IGNORECASE)
RUN_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,95}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
WINDOWS_ABSOLUTE_PATH = re.compile(r"(?i)(?:^|[^A-Za-z0-9_])[A-Z]:[\\/]")
SECRET_TEXT = re.compile(r"(?i)bearer\s+|api[_-]?key\s*[:=]")
FORBIDDEN_SECRET_KEYS = {
    "api_key",
    "authorization_header",
    "provider_response",
    "raw_provider_payload",
    "raw_provider_response",
}
ALLOWED_DEPENDENCY_STATES = {"ACCEPTED", "ACCEPT_DISABLED"}
ALL_GATE_IDS = tuple(f"G-TM-{index:02d}" for index in range(1, 15))
VISUAL_CALIBRATION_KINDS = {
    "mixed_no_editable_text",
    "pure_image",
    "pure_vector",
    "scanned_page",
}


class MigrationContractError(RuntimeError):
    """表示 runner 可稳定报告、且不得转换为成功的合同失败。"""

    def __init__(self, code: str, detail: str) -> None:
        """保存无秘密的稳定错误码和简短说明。"""

        self.code = code
        self.detail = detail
        super().__init__(f"{code}:{detail}")


@dataclass(frozen=True, slots=True)
class LeafInputManifest:
    """保存已经核对完整文档、页身份、Route 和授权的输入。"""

    stage: str
    route: str
    source_path: Path
    source_hash: str
    page_count: int
    page_no: int
    page_hash: str
    source_language: str
    target_language: str
    authorization_evidence_ref: str
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class StoredTranslationBundle:
    """指向一份已校验、内容寻址且不含源文的 TranslationBundle。"""

    bundle_hash: str
    path: Path


@dataclass(frozen=True, slots=True)
class TM0FreezeResult:
    """返回 TM0 冻结基线、指针和无 PDF 输出声明。"""

    run_id: str
    baseline_hash: str
    baseline_path: Path
    pointer_path: Path
    output_manifest_path: Path


def canonical_stage(value: str) -> str:
    """把 TM02 等命令写法规范为 TM2，并拒绝计划外阶段。"""

    match = STAGE_PATTERN.fullmatch(value.strip())
    if match is None:
        raise MigrationContractError("STAGE_INVALID", "阶段必须使用 TM0～TM18")
    number = int(match.group(1))
    if number > 18:
        raise MigrationContractError("STAGE_INVALID", "阶段必须使用 TM0～TM18")
    return f"TM{number}"


def route_slug(route: str) -> str:
    """把显式 Route 转为稳定目录名，不从文件身份推导行为。"""

    if route not in ROUTE_STAGES:
        raise MigrationContractError("ROUTE_NOT_REGISTERED", "Route 不在冻结执行序列")
    return route.replace(".", "_")


def _validate_run_id(run_id: str) -> str:
    """拒绝目录逃逸、空值和不可复用的运行身份。"""

    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise MigrationContractError(
            "RUN_ID_INVALID",
            "run-id 只能含小写字母、数字、点、横线和下划线",
        )
    return run_id


def _sha256_file(path: Path) -> str:
    """流式计算文件内容哈希。"""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _assert_sha256(value: object, field: str) -> str:
    """校验外部 Manifest 中的小写 SHA-256。"""

    if not isinstance(value, str) or SHA256_PATTERN.fullmatch(value) is None:
        raise MigrationContractError("MANIFEST_HASH_INVALID", f"{field} 不是 SHA-256")
    return value


def _assert_exact_keys(value: object, expected: set[str], context: str) -> dict[str, Any]:
    """拒绝 Manifest 通过额外字段携带强制 Route、gold 或执行代码。"""

    if not isinstance(value, dict) or set(value) != expected:
        raise MigrationContractError("MANIFEST_FIELDS_INVALID", f"{context} 字段集合不符合合同")
    return value


def _assert_payload_safe(value: object, context: str = "payload") -> None:
    """递归阻止绝对宿主路径、授权头和 Provider 原始响应落盘。"""

    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).casefold() in FORBIDDEN_SECRET_KEYS:
                raise MigrationContractError("PERSISTED_SECRET_FORBIDDEN", f"{context}.{key}")
            _assert_payload_safe(item, f"{context}.{key}")
        return
    if isinstance(value, list | tuple):
        for index, item in enumerate(value):
            _assert_payload_safe(item, f"{context}[{index}]")
        return
    if isinstance(value, str) and (
        WINDOWS_ABSOLUTE_PATH.search(value) is not None or SECRET_TEXT.search(value) is not None
    ):
        raise MigrationContractError("PERSISTED_SECRET_OR_PATH_FORBIDDEN", context)


def _write_json(path: Path, payload: object, *, allowed_root: Path) -> None:
    """在受控根内原子写 UTF-8 JSON，并在写前执行秘密与路径审计。"""

    resolved = path.resolve()
    try:
        resolved.relative_to(allowed_root.resolve())
    except ValueError as error:
        raise MigrationContractError("OUTPUT_PATH_OUTSIDE_ROOT", path.name) from error
    prepared = json_ready(payload)
    _assert_payload_safe(prepared)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(prepared, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _load_json(path: Path, code: str) -> dict[str, Any]:
    """读取 JSON 对象，并把缺失或语法错误收敛为稳定合同失败。"""

    if not path.is_file():
        raise MigrationContractError(code, "所需 JSON 文件不存在")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise MigrationContractError(code, "所需 JSON 文件不可读取") from error
    if not isinstance(payload, dict):
        raise MigrationContractError(code, "JSON 根必须是对象")
    return payload


def _resolve_repository_file(value: object, repository_root: Path, field: str) -> Path:
    """只接受 Manifest 内的仓库相对文件路径。"""

    if not isinstance(value, str) or not value.strip():
        raise MigrationContractError("MANIFEST_PATH_INVALID", f"{field} 必须是相对路径")
    candidate = Path(value)
    if candidate.is_absolute() or ".." in candidate.parts or WINDOWS_ABSOLUTE_PATH.search(value):
        raise MigrationContractError("MANIFEST_PATH_INVALID", f"{field} 必须是仓库相对路径")
    resolved = (repository_root / candidate).resolve()
    try:
        resolved.relative_to(repository_root.resolve())
    except ValueError as error:
        raise MigrationContractError("MANIFEST_PATH_INVALID", f"{field} 逃逸仓库") from error
    if not resolved.is_file():
        raise MigrationContractError("MANIFEST_PATH_MISSING", f"{field} 指向的文件不存在")
    return resolved


def _catalog_payload(catalog_path: Path = CATALOG_PATH) -> dict[str, Any]:
    """读取显式 v4 Catalog 并核对 Route 唯一和 TM1～TM17 覆盖。"""

    payload = _load_json(catalog_path, "CATALOG_MISSING_OR_INVALID")
    if payload.get("schema_version") != "transflow.page-toolbox-catalog/v4":
        raise MigrationContractError("CATALOG_SCHEMA_INVALID", "TM0 只冻结 v4 Catalog")
    entries = payload.get("entries")
    if not isinstance(entries, list) or not entries:
        raise MigrationContractError("CATALOG_ENTRIES_INVALID", "Catalog entries 为空")
    routes = tuple(item.get("route") for item in entries if isinstance(item, dict))
    if len(routes) != len(entries) or len(routes) != len(set(routes)):
        raise MigrationContractError("CATALOG_ROUTE_AMBIGUOUS", "Catalog Route 不唯一")
    if set(routes) != set(ROUTE_STAGES):
        raise MigrationContractError(
            "CATALOG_ROUTE_COVERAGE_INVALID",
            "Catalog 与 TM Route 集不一致",
        )
    return payload


def catalog_route_selection(
    route: str,
    *,
    catalog_path: Path = CATALOG_PATH,
) -> dict[str, Any]:
    """只按命令 Route 在显式 Catalog 中选择唯一条目，不读取样本身份。"""

    if route not in ROUTE_STAGES:
        raise MigrationContractError("ROUTE_NOT_REGISTERED", "Route 不在冻结执行序列")
    payload = _catalog_payload(catalog_path)
    matched = tuple(item for item in payload["entries"] if item["route"] == route)
    if len(matched) != 1:
        raise MigrationContractError("CATALOG_ROUTE_AMBIGUOUS", "目标 Route 不是唯一 Catalog 条目")
    return {**matched[0], "catalog_hash": _sha256_file(catalog_path)}


def compute_page_hash(source_pdf: Path, page_no: int) -> str:
    """使用 SharedPdfKernel 事实哈希计算 1-based 目标页身份。"""

    if page_no < 1:
        raise MigrationContractError("TARGET_PAGE_INVALID", "page_no 必须从 1 开始")
    source_hash = _sha256_file(source_pdf)
    try:
        facts = PageFactsExtractor().extract_page(source_pdf, source_hash, page_no)
    except Exception as error:
        raise MigrationContractError("TARGET_PAGE_INVALID", "page_no 超出完整文档") from error
    return facts.kernel_facts_hash


def load_leaf_input_manifest(
    manifest_path: Path,
    *,
    stage: str,
    route: str,
    repository_root: Path = REPO_ROOT,
) -> LeafInputManifest:
    """校验逐叶 Manifest，不允许其改写分类结果或携带样本答案。"""

    selected_stage = canonical_stage(stage)
    expected_stage = ROUTE_STAGES.get(route)
    if expected_stage is None or selected_stage != expected_stage:
        raise MigrationContractError("STAGE_ROUTE_MISMATCH", "阶段与冻结 Route 顺序不一致")
    resolved_manifest = manifest_path.resolve()
    try:
        resolved_manifest.relative_to(repository_root.resolve())
    except ValueError as error:
        raise MigrationContractError("MANIFEST_PATH_INVALID", "Manifest 必须位于仓库内") from error
    payload = _load_json(resolved_manifest, "MANIFEST_MISSING_OR_INVALID")
    _assert_payload_safe(payload, "manifest")
    top_fields = {
        "schema_version",
        "route",
        "source_document",
        "target_page",
        "source_language",
        "target_language",
        "authorization",
    }
    if route == "visual_only":
        top_fields.add("calibration_pages")
    top = _assert_exact_keys(
        payload,
        top_fields,
        "manifest",
    )
    if top["schema_version"] != "transflow.toolbox-leaf-migration-input/v1":
        raise MigrationContractError("MANIFEST_SCHEMA_INVALID", "Manifest schema_version 不支持")
    if top["route"] != route:
        raise MigrationContractError("MANIFEST_ROUTE_MISMATCH", "Manifest Route 与命令不一致")

    source = _assert_exact_keys(
        top["source_document"],
        {"path", "sha256", "page_count"},
        "source_document",
    )
    source_path = _resolve_repository_file(source["path"], repository_root, "source_document.path")
    source_hash = _assert_sha256(source["sha256"], "source_document.sha256")
    if _sha256_file(source_path) != source_hash:
        raise MigrationContractError("SOURCE_HASH_MISMATCH", "完整源 PDF 内容已变化")
    if not isinstance(source["page_count"], int) or source["page_count"] < 1:
        raise MigrationContractError("SOURCE_PAGE_COUNT_INVALID", "page_count 必须为正整数")
    try:
        with pymupdf.open(source_path) as document:
            actual_page_count = document.page_count
    except Exception as error:
        raise MigrationContractError("SOURCE_PDF_INVALID", "完整源 PDF 不可打开") from error
    if actual_page_count != source["page_count"]:
        raise MigrationContractError("SOURCE_PAGE_COUNT_MISMATCH", "完整源 PDF 页数不一致")

    target = _assert_exact_keys(
        top["target_page"],
        {"page_no", "page_hash", "spike_leaf_contract_route"},
        "target_page",
    )
    if not isinstance(target["page_no"], int) or target["page_no"] < 1:
        raise MigrationContractError("TARGET_PAGE_INVALID", "target_page.page_no 无效")
    page_hash = _assert_sha256(target["page_hash"], "target_page.page_hash")
    if target["spike_leaf_contract_route"] != route:
        raise MigrationContractError("SPIKE_ROUTE_MISMATCH", "Spike 叶合同与目标 Route 不一致")
    if compute_page_hash(source_path, target["page_no"]) != page_hash:
        raise MigrationContractError("TARGET_PAGE_HASH_MISMATCH", "目标页事实哈希已变化")

    if route == "visual_only":
        calibration_pages = top["calibration_pages"]
        if not isinstance(calibration_pages, list) or len(calibration_pages) != 4:
            raise MigrationContractError(
                "VISUAL_CALIBRATION_SET_INVALID",
                "visual_only 必须恰好登记四类校准页",
            )
        page_numbers: list[int] = []
        kinds: list[str] = []
        for index, raw_page in enumerate(calibration_pages):
            calibration = _assert_exact_keys(
                raw_page,
                {"kind", "page_no", "page_hash"},
                f"calibration_pages[{index}]",
            )
            page_number = calibration["page_no"]
            if not isinstance(page_number, int) or not 1 <= page_number <= actual_page_count:
                raise MigrationContractError(
                    "VISUAL_CALIBRATION_PAGE_INVALID",
                    "visual_only 校准页号无效",
                )
            calibration_hash = _assert_sha256(
                calibration["page_hash"],
                f"calibration_pages[{index}].page_hash",
            )
            if compute_page_hash(source_path, page_number) != calibration_hash:
                raise MigrationContractError(
                    "VISUAL_CALIBRATION_HASH_MISMATCH",
                    "visual_only 校准页事实哈希已变化",
                )
            page_numbers.append(page_number)
            kinds.append(str(calibration["kind"]))
        if (
            len(set(page_numbers)) != len(page_numbers)
            or set(kinds) != VISUAL_CALIBRATION_KINDS
            or target["page_no"] not in page_numbers
        ):
            raise MigrationContractError(
                "VISUAL_CALIBRATION_SET_INVALID",
                "visual_only 四类校准页必须唯一且包含目标页",
            )

    source_language = top["source_language"]
    target_language = top["target_language"]
    if (
        not isinstance(source_language, str)
        or not source_language.strip()
        or not isinstance(target_language, str)
        or not target_language.strip()
        or source_language == target_language
    ):
        raise MigrationContractError("LANGUAGE_DIRECTION_INVALID", "语言方向无效")

    authorization = _assert_exact_keys(
        top["authorization"],
        {"approved", "allowed_routes", "allowed_operations", "evidence_ref"},
        "authorization",
    )
    if authorization["approved"] is not True or authorization["allowed_routes"] != [route]:
        raise MigrationContractError("AUTHORIZATION_INVALID", "授权未唯一覆盖目标 Route")
    operations = authorization["allowed_operations"]
    if not isinstance(operations, list) or any(not isinstance(item, str) for item in operations):
        raise MigrationContractError("AUTHORIZATION_INVALID", "allowed_operations 无效")
    required_operations = {"CLASSIFY", "RENDER", "COMPARE", "FINALIZE"}
    if route != "visual_only":
        required_operations.add("TRANSLATE")
    if not required_operations.issubset(set(operations)):
        raise MigrationContractError("AUTHORIZATION_INVALID", "授权缺少计划要求的操作")
    evidence_ref = authorization["evidence_ref"]
    _resolve_repository_file(evidence_ref, repository_root, "authorization.evidence_ref")
    return LeafInputManifest(
        selected_stage,
        route,
        source_path,
        source_hash,
        actual_page_count,
        target["page_no"],
        page_hash,
        source_language,
        target_language,
        evidence_ref,
        payload,
    )


def provider_configuration_snapshot() -> dict[str, object]:
    """只记录迁移 Provider 三项环境变量是否配置，不返回任何值。"""

    return {
        "adapter": "tests.migration.p9_qwen_translation_adapter",
        "base_url_configured": bool(
            os.environ.get("TRANSFLOW_MIGRATION_QWEN_BASE_URL", "").strip()
        ),
        "api_key_configured": bool(
            os.environ.get("TRANSFLOW_MIGRATION_QWEN_API_KEY", "").strip()
        ),
        "model_configured": bool(
            os.environ.get("TRANSFLOW_MIGRATION_QWEN_MODEL", "").strip()
        ),
        "raw_response_persisted": False,
    }


def store_translation_bundle(
    batch: TranslationBatch,
    bundle: TranslationBundle,
    storage_root: Path,
    provider_snapshot: dict[str, object],
) -> StoredTranslationBundle:
    """校验并内容寻址保存规范 Bundle，供 Spike/Transflow 同哈希消费。"""

    if bundle.batch_id != batch.batch_id or bundle.requested_unit_ids != batch.ordered_unit_ids:
        raise MigrationContractError("TRANSLATION_BUNDLE_MISMATCH", "Bundle 与 Batch 身份不一致")
    bundle_payload = {
        "batch_id": bundle.batch_id,
        "requested_unit_ids": list(bundle.requested_unit_ids),
        "units": [
            {"unit_id": item.unit_id, "translated_text": item.translated_text}
            for item in bundle.units
        ],
    }
    bundle_hash = content_sha256(bundle_payload)
    payload = {
        "schema_version": "transflow.toolbox-leaf-translation-bundle/v1",
        "batch_hash": content_sha256(batch),
        "bundle_hash": bundle_hash,
        "bundle": bundle_payload,
        "provider_snapshot": provider_snapshot,
        "raw_provider_response_persisted": False,
        "consumption_contract": "SAME_HASH_FOR_SPIKE_AND_TRANSFLOW",
    }
    _assert_payload_safe(payload, "translation_bundle")
    target = storage_root / "translation_bundles" / f"{bundle_hash}.json"
    if target.exists():
        existing = _load_json(target, "TRANSLATION_BUNDLE_CACHE_INVALID")
        if existing.get("bundle_hash") != bundle_hash or existing.get("bundle") != bundle_payload:
            raise MigrationContractError("TRANSLATION_BUNDLE_HASH_COLLISION", bundle_hash)
        return StoredTranslationBundle(bundle_hash, target)
    _write_json(target, payload, allowed_root=storage_root)
    return StoredTranslationBundle(bundle_hash, target)


def dependencies_satisfied(
    stage: str,
    *,
    gate_root: Path = GATE_ROOT,
) -> tuple[str, ...]:
    """检查所有前置阶段已有人工接受或明确接受 disabled 的结论。"""

    selected = canonical_stage(stage)
    violations: list[str] = []
    for dependency in STAGE_DEPENDENCIES[selected]:
        gate_path = gate_root / f"{dependency.casefold()}_gate.json"
        if not gate_path.is_file():
            violations.append(f"DEPENDENCY_GATE_MISSING:{dependency}")
            continue
        gate = _load_json(gate_path, "DEPENDENCY_GATE_INVALID")
        status = str(gate.get("status", "MISSING"))
        if status not in ALLOWED_DEPENDENCY_STATES:
            violations.append(f"DEPENDENCY_NOT_ACCEPTED:{dependency}:{status}")
    return tuple(violations)


def _repository_relative(path: Path, repository_root: Path = REPO_ROOT) -> str:
    """把冻结文件记录成仓库相对 POSIX 路径。"""

    return path.resolve().relative_to(repository_root.resolve()).as_posix()


def _file_records(paths: tuple[Path, ...]) -> tuple[dict[str, str], ...]:
    """对文件或目录下 Python 文件生成稳定、去重的基线记录。"""

    selected: set[Path] = set()
    for path in paths:
        if path.is_file():
            selected.add(path.resolve())
        elif path.is_dir():
            selected.update(item.resolve() for item in path.rglob("*.py") if item.is_file())
        else:
            raise MigrationContractError("BASELINE_RESOURCE_MISSING", path.name)
    return tuple(
        {"path": _repository_relative(path), "sha256": _sha256_file(path)}
        for path in sorted(selected, key=lambda item: _repository_relative(item))
    )


def _baseline_groups() -> dict[str, dict[str, object]]:
    """冻结上游、Toolbox、计划和迁移设施的当前实现指纹。"""

    plans = (
        REPO_ROOT / "docs" / "设计" / "Transflow_PDF翻译排版引擎_总体设计_v0.1.md",
        REPO_ROOT / "docs" / "计划" / "Transflow_PDF翻译排版引擎_详细开发计划_v0.1.md",
        REPO_ROOT
        / "docs"
        / "计划"
        / "Transflow_Toolbox逐类别核心逻辑迁移与全流程验收计划_v0.1.md",
    )
    resource_files = tuple(
        sorted((REPO_ROOT / "resources" / "catalogs").glob("*.json"))
    ) + tuple(sorted((REPO_ROOT / "resources" / "manifests").glob("*.json")))
    groups = {
        "classification": (REPO_ROOT / "src" / "transflow" / "classification",),
        "pdf_kernel": (REPO_ROOT / "src" / "transflow" / "pdf_kernel",),
        "application_domain": (
            REPO_ROOT / "src" / "transflow" / "application",
            REPO_ROOT / "src" / "transflow" / "domain",
        ),
        "toolboxes": (REPO_ROOT / "src" / "transflow" / "toolboxes",),
        "plans": plans,
        "resources": resource_files,
        "migration_facility": (
            REPO_ROOT / "scripts" / "run_toolbox_leaf_migration.py",
            REPO_ROOT / "scripts" / "verify_toolbox_leaf_migration.py",
            REPO_ROOT / "scripts" / "toolbox_leaf_migration_drivers.py",
            REPO_ROOT / "scripts" / "toolbox_leaf_migration_visual_only.py",
            REPO_ROOT / "tests" / "test_toolbox_leaf_migration.py",
        ),
    }
    result: dict[str, dict[str, object]] = {}
    for name, paths in groups.items():
        records = _file_records(paths)
        result[name] = {"files": records, "group_hash": content_sha256(records)}
    return result


def _forbidden_production_dependencies() -> tuple[dict[str, object], ...]:
    """扫描生产 import 和动态加载，证明运行时不依赖 Spike、测试或历史 run。"""

    violations: list[dict[str, object]] = []
    source_root = REPO_ROOT / "src" / "transflow"
    forbidden_roots = {"spikes", "tests", "runs"}
    for path in sorted(source_root.rglob("*.py")):
        relative = _repository_relative(path)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        for node in ast.walk(tree):
            modules: tuple[str, ...] = ()
            if isinstance(node, ast.Import):
                modules = tuple(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = (node.module,)
            for module in modules:
                if module.split(".", maxsplit=1)[0] in forbidden_roots:
                    violations.append(
                        {
                            "code": "FORBIDDEN_PRODUCTION_IMPORT",
                            "path": relative,
                            "line": getattr(node, "lineno", 0),
                            "module": module,
                        }
                    )
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in {"import_module", "exec_module"}:
                    violations.append(
                        {
                            "code": "DYNAMIC_MODULE_LOADING",
                            "path": relative,
                            "line": node.lineno,
                        }
                    )
    return tuple(violations)


def freeze_tm0_baseline(
    run_id: str,
    *,
    evidence_root: Path = EVIDENCE_ROOT,
    output_root: Path = TM0_OUTPUT_ROOT,
    baseline_pointer_path: Path = TM0_BASELINE_POINTER,
) -> TM0FreezeResult:
    """冻结 TM0 基线并声明本阶段不产生任何叶候选或产品结论。"""

    selected_run_id = _validate_run_id(run_id)
    baseline_path = evidence_root / "tm0" / selected_run_id / "baseline.json"
    output_manifest_path = output_root / "TM0" / selected_run_id / "run_manifest.json"
    if baseline_path.parent.exists() or output_manifest_path.parent.exists():
        raise MigrationContractError("RUN_ID_ALREADY_EXISTS", "TM0 不覆盖历史 run")
    if baseline_pointer_path.exists():
        raise MigrationContractError("BASELINE_ALREADY_FROZEN", "TM0 基线指针已存在")
    if DRIVER_FACTORIES:
        raise MigrationContractError("TM0_ROUTE_DRIVER_PREMATURE", "TM0 不得提前注册具体 Route")

    catalog_before = _sha256_file(CATALOG_PATH)
    catalog = _catalog_payload()
    p9b_gate_path = REPO_ROOT / "resources" / "manifests" / "p9b_gate.json"
    p9b_manifest_path = REPO_ROOT / "resources" / "evidence" / "p9b" / "real_run_manifest.json"
    comparison_path = (
        REPO_ROOT / "output" / "pdf" / "P9B_toolbox_comparison" / "comparison_metrics.json"
    )
    p9b_gate = _load_json(p9b_gate_path, "P9B_GATE_MISSING")
    if p9b_gate.get("status") != "PASS":
        raise MigrationContractError("P9B_NOT_PASSED", "TM0 前置 Gate 不是 PASS")
    for required in (p9b_manifest_path, comparison_path):
        if not required.is_file():
            raise MigrationContractError("P9B_EVIDENCE_MISSING", required.name)
    forbidden = _forbidden_production_dependencies()
    if forbidden:
        raise MigrationContractError("PRODUCTION_DEPENDENCY_VIOLATION", "生产包存在禁止依赖")

    catalog_after = _sha256_file(CATALOG_PATH)
    if catalog_after != catalog_before:
        raise MigrationContractError("DEFAULT_CATALOG_MUTATED", "TM0 执行期间 Catalog 发生变化")
    entries = tuple(
        {
            "route": item["route"],
            "stage": ROUTE_STAGES[item["route"]],
            "toolbox_key": item["toolbox_key"],
            "toolbox_version": item["toolbox_version"],
            "fingerprint": item["fingerprint"],
            "enabled": item["enabled"],
            "evidence_state": item["evidence_state"],
        }
        for item in catalog["entries"]
    )
    created_at = datetime.now().astimezone().isoformat(timespec="seconds")
    baseline_body: dict[str, Any] = {
        "schema_version": "transflow.toolbox-leaf-migration-tm0-baseline/v1",
        "stage": "TM0",
        "run_id": selected_run_id,
        "state": "BASELINE_FROZEN",
        "created_at": created_at,
        "catalog": {
            "path": _repository_relative(CATALOG_PATH),
            "hash_before": catalog_before,
            "hash_after": catalog_after,
            "mutation_count": 0,
            "entries": entries,
            "enabled_routes": [item["route"] for item in entries if item["enabled"]],
        },
        "route_stage_coverage": {
            "catalog_route_count": len(entries),
            "mapped_route_count": len(ROUTE_STAGES),
            "unmapped_routes": [],
        },
        "stage_dependencies": {
            key: list(value) for key, value in STAGE_DEPENDENCIES.items()
        },
        "source_groups": _baseline_groups(),
        "p9b_reference": {
            "gate_path": _repository_relative(p9b_gate_path),
            "gate_hash": _sha256_file(p9b_gate_path),
            "gate_status": "PASS",
            "real_run_manifest_path": _repository_relative(p9b_manifest_path),
            "real_run_manifest_hash": _sha256_file(p9b_manifest_path),
            "comparison_path": _repository_relative(comparison_path),
            "comparison_hash": _sha256_file(comparison_path),
            "verification_command": "python -m scripts.verify_p9b",
        },
        "provider_configuration": provider_configuration_snapshot(),
        "production_dependency_scan": {
            "forbidden_count": len(forbidden),
            "violations": forbidden,
        },
        "route_drivers": {
            "registered_count": len(DRIVER_FACTORIES),
            "registered_routes": sorted(DRIVER_FACTORIES),
            "policy": "STATIC_EXPLICIT_NO_DISCOVERY",
        },
        "scope": {
            "leaf_core_migration": "NOT_STARTED",
            "catalog_enablement_changes": 0,
            "product_acceptance": "NOT_EVALUATED",
            "candidate_pdf_count": 0,
        },
    }
    baseline_hash = content_sha256(baseline_body)
    baseline = {**baseline_body, "baseline_hash": baseline_hash}
    pointer = {
        "schema_version": "transflow.toolbox-leaf-migration-tm0-pointer/v1",
        "stage": "TM0",
        "run_id": selected_run_id,
        "state": "BASELINE_FROZEN",
        "baseline_ref": f"tm0/{selected_run_id}/baseline.json",
        "baseline_hash": baseline_hash,
        "catalog_hash": catalog_before,
    }
    output_manifest = {
        "schema_version": "transflow.toolbox-leaf-migration-output/v1",
        "stage": "TM0",
        "run_id": selected_run_id,
        "state": "BASELINE_FROZEN",
        "baseline_hash": baseline_hash,
        "artifacts": {
            "input_pdf": {"present": False, "reason": "TM0_FACILITY_ONLY"},
            "spike_candidate": {"present": False, "reason": "TM0_FACILITY_ONLY"},
            "transflow_candidate": {"present": False, "reason": "TM0_FACILITY_ONLY"},
            "final_delivery": {"present": False, "reason": "TM0_FACILITY_ONLY"},
            "comparison": {"present": False, "reason": "TM0_FACILITY_ONLY"},
        },
        "false_candidate_count": 0,
    }
    _write_json(baseline_path, baseline, allowed_root=evidence_root)
    _write_json(baseline_pointer_path, pointer, allowed_root=baseline_pointer_path.parent)
    _write_json(output_manifest_path, output_manifest, allowed_root=output_root)
    return TM0FreezeResult(
        selected_run_id,
        baseline_hash,
        baseline_path,
        baseline_pointer_path,
        output_manifest_path,
    )


def _load_tm0_pointer() -> dict[str, Any]:
    """读取唯一 TM0 指针，供所有叶绑定同一冻结基线。"""

    pointer = _load_json(TM0_BASELINE_POINTER, "TM0_BASELINE_MISSING")
    if pointer.get("state") != "BASELINE_FROZEN":
        raise MigrationContractError("TM0_BASELINE_INVALID", "TM0 基线状态无效")
    return pointer


def _prepare_leaf_run(
    manifest: LeafInputManifest,
    run_id: str,
    pointer: dict[str, Any],
) -> LeafMigrationRunContext:
    """保存不可覆盖的完整输入和目标页诊断副本，再交给显式 Route 驱动。"""

    selected_run_id = _validate_run_id(run_id)
    slug = route_slug(manifest.route)
    evidence_run_root = EVIDENCE_ROOT / slug / selected_run_id
    output_run_root = OUTPUT_ROOT / manifest.stage / selected_run_id
    if evidence_run_root.exists() or output_run_root.exists():
        raise MigrationContractError("RUN_ID_ALREADY_EXISTS", "逐叶运行不得覆盖历史 run")
    input_root = output_run_root / "input"
    input_root.mkdir(parents=True)
    shutil.copyfile(manifest.source_path, input_root / "source_document.pdf")
    with pymupdf.open(manifest.source_path) as source_document:
        with pymupdf.open() as target_document:
            target_document.insert_pdf(
                source_document,
                from_page=manifest.page_no - 1,
                to_page=manifest.page_no - 1,
            )
            target_document.save(input_root / "target_page.pdf")
    source_manifest = {
        "schema_version": "transflow.toolbox-leaf-migration-source/v1",
        "stage": manifest.stage,
        "route": manifest.route,
        "run_id": selected_run_id,
        "source_hash": manifest.source_hash,
        "page_count": manifest.page_count,
        "target_page_no": manifest.page_no,
        "target_page_hash": manifest.page_hash,
        "source_language": manifest.source_language,
        "target_language": manifest.target_language,
        "authorization_evidence_ref": manifest.authorization_evidence_ref,
    }
    if manifest.route == "visual_only":
        source_manifest["calibration_pages"] = manifest.payload["calibration_pages"]
    _write_json(input_root / "source_manifest.json", source_manifest, allowed_root=OUTPUT_ROOT)
    initial = {
        "schema_version": "transflow.toolbox-leaf-migration-run/v1",
        "stage": manifest.stage,
        "route": manifest.route,
        "route_slug": slug,
        "run_id": selected_run_id,
        "status": "IN_PROGRESS",
        "last_successful_state": "SOURCE_FROZEN",
        "baseline_hash": pointer["baseline_hash"],
        "catalog_hash": pointer["catalog_hash"],
        "input_manifest_hash": content_sha256(manifest.payload),
    }
    _write_json(evidence_run_root / "run_manifest.json", initial, allowed_root=EVIDENCE_ROOT)
    return LeafMigrationRunContext(
        manifest.stage,
        manifest.route,
        slug,
        selected_run_id,
        REPO_ROOT,
        evidence_run_root,
        output_run_root,
        manifest.payload,
        str(pointer["baseline_hash"]),
        str(pointer["catalog_hash"]),
    )


def _write_leaf_failure(context: LeafMigrationRunContext, error: MigrationContractError) -> None:
    """保留失败前已完成状态和机器可读原因，不创建伪候选。"""

    payload = {
        "schema_version": "transflow.toolbox-leaf-migration-run/v1",
        "stage": context.stage,
        "route": context.route,
        "route_slug": context.route_slug,
        "run_id": context.run_id,
        "status": "FAIL",
        "last_successful_state": "SOURCE_FROZEN",
        "baseline_hash": context.baseline_hash,
        "catalog_hash": context.catalog_hash,
        "failure": {"code": error.code, "detail": error.detail},
        "candidate_artifacts": {
            "present": False,
            "reason": error.code,
        },
    }
    _write_json(
        context.evidence_root / "run_manifest.json",
        payload,
        allowed_root=EVIDENCE_ROOT,
    )
    _write_json(
        context.output_root / "run_manifest.json",
        payload,
        allowed_root=OUTPUT_ROOT,
    )


def _validate_driver_result(context: LeafMigrationRunContext, result: dict[str, Any]) -> None:
    """要求驱动证据闭合到 FULL_E2E_PASS，且 G-TM-14 保持人工待审。"""

    required = {
        "schema_version",
        "stage",
        "route",
        "run_id",
        "state",
        "route_attestation",
        "translation",
        "artifacts",
        "trace",
        "gate_results",
        "axes",
        "known_issues",
    }
    _assert_exact_keys(result, required, "driver_result")
    if (
        result["schema_version"] != "transflow.toolbox-leaf-migration-execution/v1"
        or result["stage"] != context.stage
        or result["route"] != context.route
        or result["run_id"] != context.run_id
        or result["state"] != "FULL_E2E_PASS"
    ):
        raise MigrationContractError("DRIVER_RESULT_IDENTITY_INVALID", "驱动结果身份或状态无效")
    gates = result["gate_results"]
    if not isinstance(gates, dict) or set(gates) != set(ALL_GATE_IDS):
        raise MigrationContractError("DRIVER_GATE_SET_INVALID", "驱动未逐项报告十四个 Gate")
    if any(gates[item].get("status") != "PASS" for item in ALL_GATE_IDS[:-1]):
        raise MigrationContractError("LEAF_GATE_FAILED", "G-TM-01～13 存在失败")
    if gates["G-TM-14"].get("status") != "REVIEW_PENDING":
        raise MigrationContractError("HUMAN_REVIEW_STATE_INVALID", "G-TM-14 必须等待人工确认")
    _assert_payload_safe(result, "driver_result")


def execute_leaf_stage(
    *,
    stage: str,
    route: str,
    manifest_path: Path,
    run_id: str,
) -> dict[str, Any]:
    """验证依赖和输入后调用唯一显式 Route 驱动，并停在 REVIEW_PENDING。"""

    selected_stage = canonical_stage(stage)
    dependency_violations = dependencies_satisfied(selected_stage)
    if dependency_violations:
        raise MigrationContractError("STAGE_DEPENDENCY_BLOCKED", dependency_violations[0])
    selection = catalog_route_selection(route)
    pointer = _load_tm0_pointer()
    if selection["catalog_hash"] != pointer.get("catalog_hash"):
        raise MigrationContractError("DEFAULT_CATALOG_DRIFT", "默认 Catalog 与阶段基线不一致")
    manifest = load_leaf_input_manifest(manifest_path, stage=selected_stage, route=route)
    context = _prepare_leaf_run(manifest, run_id, pointer)
    driver = resolve_route_driver(route)
    if driver is None:
        error = MigrationContractError(
            "ROUTE_DRIVER_NOT_REGISTERED",
            "当前 Route 必须在对应 TM 阶段实现私有迁移驱动",
        )
        _write_leaf_failure(context, error)
        raise error
    try:
        result = driver.execute(context)
        _validate_driver_result(context, result)
        if _sha256_file(CATALOG_PATH) != pointer["catalog_hash"]:
            raise MigrationContractError(
                "DEFAULT_CATALOG_MUTATED",
                "人工确认前不得修改默认 Catalog",
            )
        completed = {
            **result,
            "status": "REVIEW_PENDING",
            "baseline_hash": context.baseline_hash,
            "catalog_hash": context.catalog_hash,
        }
        _write_json(
            context.evidence_root / "run_manifest.json",
            completed,
            allowed_root=EVIDENCE_ROOT,
        )
        _write_json(
            context.output_root / "run_manifest.json",
            completed,
            allowed_root=OUTPUT_ROOT,
        )
        _write_json(
            TM0_OUTPUT_ROOT / context.stage / context.run_id / "run_index.json",
            {
                "schema_version": "transflow.toolbox-leaf-migration-run-index/v1",
                "stage": context.stage,
                "route": context.route,
                "run_id": context.run_id,
                "authoritative_run_ref": _repository_relative(context.output_root),
                "run_manifest_ref": _repository_relative(
                    context.output_root / "run_manifest.json"
                ),
            },
            allowed_root=TM0_OUTPUT_ROOT,
        )
        return completed
    except MigrationContractError as error:
        _write_leaf_failure(context, error)
        raise
    except Exception as error:
        wrapped = MigrationContractError(
            "ROUTE_DRIVER_EXECUTION_FAILED",
            type(error).__name__,
        )
        _write_leaf_failure(context, wrapped)
        raise wrapped from error


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析 TM0 特例或 TM1～TM17 共用的 stage/route/manifest 入口。"""

    parser = argparse.ArgumentParser(description="执行 Transflow Toolbox 逐叶迁移")
    parser.add_argument("--stage", required=True, help="TM0～TM18；TM02 会规范为 TM2")
    parser.add_argument("--route", help="显式 Catalog Route；TM0 不填写")
    parser.add_argument("--manifest", help="仓库相对逐叶输入 Manifest；TM0 不填写")
    parser.add_argument("--run-id", help="稳定运行身份；省略时按秒生成")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """执行 TM0 冻结或单叶驱动；任何合同/Gate 失败均非零退出。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    try:
        arguments = parse_args(argv)
        stage = canonical_stage(arguments.stage)
        run_id = arguments.run_id or f"{stage.casefold()}-{datetime.now():%Y%m%d-%H%M%S}"
        if stage == "TM0":
            if arguments.route is not None or arguments.manifest is not None:
                raise MigrationContractError("TM0_ARGUMENTS_INVALID", "TM0 不选择具体 Route 或样本")
            result = freeze_tm0_baseline(run_id)
            output = {
                "status": "BASELINE_FROZEN",
                "stage": "TM0",
                "run_id": result.run_id,
                "baseline_hash": result.baseline_hash,
                "baseline_ref": f"tm0/{result.run_id}/baseline.json",
                "next_state": "REVIEW_PENDING_AFTER_TECHNICAL_GATES",
            }
        else:
            if not arguments.route or not arguments.manifest:
                raise MigrationContractError(
                    "LEAF_ARGUMENTS_MISSING",
                    "逐叶阶段必须提供 route 和 manifest",
                )
            manifest_path = Path(arguments.manifest)
            if not manifest_path.is_absolute():
                manifest_path = REPO_ROOT / manifest_path
            completed = execute_leaf_stage(
                stage=stage,
                route=arguments.route,
                manifest_path=manifest_path,
                run_id=run_id,
            )
            output = {
                "status": completed["status"],
                "stage": stage,
                "route": arguments.route,
                "run_id": run_id,
            }
        print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except MigrationContractError as error:
        print(
            json.dumps(
                {"status": "FAIL", "error_code": error.code, "detail": error.detail},
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
