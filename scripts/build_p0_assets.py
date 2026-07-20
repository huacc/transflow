"""生成并核验 Transflow P0 的可重算基线、迁移台账和追溯矩阵。"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import subprocess
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger("transflow.p0.assets")
REPO_ROOT = Path(__file__).resolve().parent.parent
DESIGN_PATH = REPO_ROOT / "docs" / "设计" / "Transflow_PDF翻译排版引擎_总体设计_v0.1.md"
PLAN_PATH = REPO_ROOT / "docs" / "计划" / "Transflow_PDF翻译排版引擎_详细开发计划_v0.1.md"
CLASSIFICATION_ROOT = REPO_ROOT / "spikes" / "page_classification_engine_puncture_v1"
TOOLBOX_ROOT = REPO_ROOT / "spikes" / "page_toolbox_engine_puncture_v1"
MERQFIN_ROOT = REPO_ROOT.parent / "MerqFin"
MIGRATION_ROOT = REPO_ROOT / "docs" / "迁移"

BASELINE_PATH = MIGRATION_ROOT / "baseline_manifest.json"
LEDGER_PATH = MIGRATION_ROOT / "migration_ledger.json"
TRACEABILITY_PATH = MIGRATION_ROOT / "traceability_matrix.json"

SKIPPED_DIRECTORY_NAMES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "artifacts",
    "recorded_inputs",
    "reports",
    "runs",
    "samples",
    "tmp",
}
SKIPPED_BINARY_SUFFIXES = {".jpeg", ".jpg", ".log", ".pdf", ".png", ".pyc"}

EXPECTED_ROUTE_BEHAVIORS = (
    "cover",
    "contents",
    "end",
    "visual_only",
    "body.flow_text.single",
    "body.flow_text.multi",
    "body.flow_text.visual_anchored",
    "body.table",
    "body.chart",
    "body.diagram",
    "body.anchored_blocks",
    "body.composite.flow_text_table",
    "body.composite.anchored_blocks_chart",
    "body.composite.chart_table",
    "body.composite.flow_text_chart",
    "body.composite.flow_text_diagram",
    "body.freeform",
)

TOOLBOX_LEAF_SOURCES = {
    "cover": TOOLBOX_ROOT / "toolboxes" / "cover",
    "contents": TOOLBOX_ROOT / "toolboxes" / "contents",
    "end": TOOLBOX_ROOT / "toolboxes" / "end",
    "visual_only": CLASSIFICATION_ROOT / "分类结果" / "visual_only",
    "body.flow_text.single": TOOLBOX_ROOT / "toolboxes" / "body" / "flow_text" / "single",
    "body.flow_text.multi": TOOLBOX_ROOT / "toolboxes" / "body" / "flow_text" / "multi",
    "body.flow_text.visual_anchored": (
        TOOLBOX_ROOT / "toolboxes" / "body" / "flow_text" / "visual_anchored"
    ),
    "body.table": TOOLBOX_ROOT / "toolboxes" / "body" / "table",
    "body.chart": TOOLBOX_ROOT / "toolboxes" / "body" / "chart",
    "body.diagram": TOOLBOX_ROOT / "toolboxes" / "body" / "diagram",
    "body.anchored_blocks": TOOLBOX_ROOT / "toolboxes" / "body" / "anchored_blocks",
    "body.composite.flow_text_table": (
        TOOLBOX_ROOT / "toolboxes" / "body" / "composite" / "flow_text_table"
    ),
    "body.composite.anchored_blocks_chart": (
        TOOLBOX_ROOT / "toolboxes" / "body" / "composite" / "anchored_blocks_chart"
    ),
    "body.composite.chart_table": (
        TOOLBOX_ROOT / "toolboxes" / "body" / "composite" / "chart_table"
    ),
    "body.composite.flow_text_chart": (
        TOOLBOX_ROOT / "toolboxes" / "body" / "composite" / "flow_text_chart"
    ),
    "body.composite.flow_text_diagram": (
        TOOLBOX_ROOT / "toolboxes" / "body" / "composite" / "flow_text_diagram"
    ),
    "body.freeform": CLASSIFICATION_ROOT / "分类结果" / "body" / "freeform",
}


def configure_logging() -> None:
    """配置 P0 资产脚本的结构清晰日志。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")


def repository_relative(path: Path) -> str:
    """把仓库内路径转换为稳定的 POSIX 相对路径。"""

    return path.resolve().relative_to(REPO_ROOT.resolve()).as_posix()


def sha256_bytes(content: bytes) -> str:
    """计算字节内容的 SHA-256。"""

    return hashlib.sha256(content).hexdigest()


def sha256_file(path: Path) -> str:
    """计算单个文件的 SHA-256，并记录计算意图。"""

    LOGGER.debug("计算文件哈希 path=%s", repository_relative(path))
    return sha256_bytes(path.read_bytes())


def should_hash_file(path: Path) -> bool:
    """判断文件是否属于迁移源码和合同哈希范围。"""

    lowered_parts = {part.casefold() for part in path.parts}
    if lowered_parts & {name.casefold() for name in SKIPPED_DIRECTORY_NAMES}:
        return False
    return path.suffix.casefold() not in SKIPPED_BINARY_SUFFIXES


def combined_path_hash(paths: list[Path]) -> str:
    """按相对路径排序组合多个文件或目录的内容哈希。"""

    entries: list[tuple[str, str]] = []
    for path in paths:
        if path.is_file():
            entries.append((repository_relative(path), sha256_file(path)))
            continue
        if path.is_dir():
            for child in sorted(path.rglob("*"), key=lambda item: item.as_posix()):
                if child.is_file() and should_hash_file(child):
                    entries.append((repository_relative(child), sha256_file(child)))
            continue
        entries.append((repository_relative(path), "MISSING"))
    payload = "\n".join(f"{name}\0{digest}" for name, digest in sorted(entries))
    return sha256_bytes(payload.encode("utf-8"))


def run_git(repo: Path, arguments: list[str], allow_failure: bool = False) -> str:
    """在指定仓库执行只读 Git 命令并返回标准输出。"""

    LOGGER.debug("执行 Git 读取 repo=%s args=%s", repo.name, arguments)
    result = subprocess.run(
        ["git", *arguments],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0 and not allow_failure:
        raise RuntimeError(
            f"Git 命令失败 repo={repo.name} args={arguments} stderr={result.stderr.strip()}"
        )
    return result.stdout.strip()


def tracked_files(relative_root: str) -> list[Path]:
    """读取某个迁移来源下由 Transflow Git 跟踪的文件。"""

    output = run_git(REPO_ROOT, ["ls-files", "--", f"{relative_root}/**"])
    return [REPO_ROOT / line for line in output.splitlines() if line]


def tracked_worktree_hash(relative_root: str) -> tuple[str, int]:
    """计算迁移来源当前工作树中全部已跟踪文件的稳定哈希。"""

    paths = tracked_files(relative_root)
    entries: list[tuple[str, str]] = []
    for path in paths:
        relative = repository_relative(path)
        digest = sha256_file(path) if path.is_file() else "MISSING"
        entries.append((relative, digest))
    payload = "\n".join(f"{name}\0{digest}" for name, digest in sorted(entries))
    return sha256_bytes(payload.encode("utf-8")), len(paths)


def dirty_entries(relative_root: str) -> list[str]:
    """记录 P0 开始前迁移来源已有的已跟踪工作树变化。"""

    output = run_git(
        REPO_ROOT,
        ["status", "--porcelain=v1", "--untracked-files=no", "--", f"{relative_root}/**"],
    )
    return [line for line in output.splitlines() if line]


def extract_design_merqfin_commit() -> str:
    """从批准设计中提取 MerqFin 参考 main 提交。"""

    match = re.search(r"main@([0-9a-f]{7,40})", DESIGN_PATH.read_text(encoding="utf-8"))
    if match is None:
        raise ValueError("总体设计未包含 MerqFin main 参考提交")
    return match.group(1)


def collect_baseline_manifest() -> dict[str, Any]:
    """收集设计、计划、两个 spike 和 MerqFin 的可重算基线。"""

    LOGGER.info("收集 P0 输入基线")
    classification_hash, classification_count = tracked_worktree_hash(
        "spikes/page_classification_engine_puncture_v1"
    )
    toolbox_hash, toolbox_count = tracked_worktree_hash(
        "spikes/page_toolbox_engine_puncture_v1"
    )
    design_merqfin_commit = extract_design_merqfin_commit()
    merqfin_origin_main = run_git(MERQFIN_ROOT, ["rev-parse", "origin/main"])
    return {
        "schema_version": "transflow.baseline-manifest/v1",
        "design": {
            "path": repository_relative(DESIGN_PATH),
            "version": "v0.1",
            "sha256": sha256_file(DESIGN_PATH),
        },
        "plan": {
            "path": repository_relative(PLAN_PATH),
            "version": "v0.1-r1",
            "sha256": sha256_file(PLAN_PATH),
        },
        "transflow_repository": {
            "head": run_git(REPO_ROOT, ["rev-parse", "HEAD"]),
            "branch": run_git(REPO_ROOT, ["branch", "--show-current"]),
        },
        "classification_spike": {
            "path": "spikes/page_classification_engine_puncture_v1",
            "git_head": run_git(REPO_ROOT, ["rev-parse", "HEAD"]),
            "tracked_file_count": classification_count,
            "tracked_worktree_sha256": classification_hash,
            "preexisting_dirty_entries": dirty_entries(
                "spikes/page_classification_engine_puncture_v1"
            ),
        },
        "toolbox_spike": {
            "path": "spikes/page_toolbox_engine_puncture_v1",
            "git_head": run_git(REPO_ROOT, ["rev-parse", "HEAD"]),
            "tracked_file_count": toolbox_count,
            "tracked_worktree_sha256": toolbox_hash,
            "preexisting_dirty_entries": dirty_entries("spikes/page_toolbox_engine_puncture_v1"),
        },
        "merqfin_reference": {
            "path": "../MerqFin",
            "design_main_commit": design_merqfin_commit,
            "origin_main_commit": merqfin_origin_main,
            "origin_main_matches_design": merqfin_origin_main.startswith(design_merqfin_commit),
            "local_head": run_git(MERQFIN_ROOT, ["rev-parse", "HEAD"]),
            "local_branch": run_git(MERQFIN_ROOT, ["branch", "--show-current"]),
            "role": "REFERENCE_ONLY_UNTIL_P15",
        },
    }


def migration_record(
    *,
    unit_id: str,
    category: str,
    source_paths: list[Path],
    target_path: str,
    change_policy: str,
    evidence_status: str,
    evidence_refs: list[str],
) -> dict[str, Any]:
    """构造一条字段完整的迁移单元记录。"""

    return {
        "unit_id": unit_id,
        "category": category,
        "source_path": " + ".join(repository_relative(path) for path in source_paths),
        "source_hash": combined_path_hash(source_paths),
        "target_path": target_path,
        "change_policy": change_policy,
        "evidence_status": evidence_status,
        "evidence_ref": evidence_refs,
    }


def classification_records() -> list[dict[str, Any]]:
    """清点分类源码、Prompt、exemplar、taxonomy 和结果语义。"""

    records: list[dict[str, Any]] = []
    source_targets = {
        "__init__.py": "src/transflow/classification/",
        "config.py": "src/transflow/runtime/settings.py",
        "engine.py": "src/transflow/classification/engine.py",
        "evidence.py": "src/transflow/classification/evidence.py",
        "io_utils.py": "tests/migration/classification/",
        "models.py": "src/transflow/domain/classification.py",
        "provider.py": "tests/migration/classification/provider.py",
        "qwen.py": "src/transflow/classification/decision_adapter.py",
        "resolver.py": "src/transflow/classification/resolver.py",
        "rules.py": "src/transflow/classification/rules.py",
    }
    source_root = CLASSIFICATION_ROOT / "src" / "page_classifier"
    for path in sorted(source_root.glob("*.py"), key=lambda item: item.name):
        records.append(
            migration_record(
                unit_id=f"classification.source.{path.stem}",
                category="classification_source",
                source_paths=[path],
                target_path=source_targets[path.name],
                change_policy="LIFT_AND_WRAP_OR_MIGRATION_TEST_ONLY",
                evidence_status="NO_ROOT_GATE",
                evidence_refs=["docs/设计/Transflow_PDF翻译排版引擎_总体设计_v0.1.md#205-分类-spike-到生产模块"],
            )
        )
    prompt_root = CLASSIFICATION_ROOT / "prompts"
    for path in sorted(prompt_root.rglob("*.md"), key=lambda item: item.as_posix()):
        prompt_relative = path.relative_to(prompt_root).as_posix()
        prompt_unit_name = prompt_relative.replace("/", ".").removesuffix(".md")
        records.append(
            migration_record(
                unit_id=f"classification.prompt.{prompt_unit_name}",
                category="classification_prompt",
                source_paths=[path],
                target_path=f"resources/prompts/classification/{prompt_relative}",
                change_policy="COPY_VERSIONED_REMOVE_IDENTITY_LEAKAGE",
                evidence_status="NO_ROOT_GATE",
                evidence_refs=["spikes/page_classification_engine_puncture_v1/README.md"],
            )
        )
    exemplar_manifest = CLASSIFICATION_ROOT / "exemplars" / "manifest.jsonl"
    records.append(
        migration_record(
            unit_id="classification.exemplars.manifest",
            category="classification_exemplar",
            source_paths=[exemplar_manifest],
            target_path="resources/exemplars/classification/manifest.jsonl",
            change_policy="APPROVED_MINIMUM_ONLY",
            evidence_status="NO_ROOT_GATE",
            evidence_refs=["spikes/page_classification_engine_puncture_v1/exemplars/manifest.jsonl"],
        )
    )
    taxonomy_paths = [
        CLASSIFICATION_ROOT / "README.md",
        CLASSIFICATION_ROOT / "分类结果" / "分类说明.md",
    ]
    records.append(
        migration_record(
            unit_id="classification.taxonomy_and_result_semantics",
            category="classification_taxonomy",
            source_paths=taxonomy_paths,
            target_path="resources/manifests/classification_taxonomy.json",
            change_policy="FREEZE_SEMANTICS_REBUILD_ANONYMOUS_BASELINE",
            evidence_status="NO_ROOT_GATE",
            evidence_refs=[repository_relative(path) for path in taxonomy_paths],
        )
    )
    return records


def kernel_and_contract_records() -> list[dict[str, Any]]:
    """清点 SharedPdfKernel、合同和工具箱治理资产。"""

    records: list[dict[str, Any]] = []
    kernel_root = TOOLBOX_ROOT / "src" / "shared_pdf_kernel"
    for path in sorted(kernel_root.glob("*.py"), key=lambda item: item.name):
        records.append(
            migration_record(
                unit_id=f"pdf_kernel.source.{path.stem}",
                category="pdf_kernel_source",
                source_paths=[path],
                target_path=f"src/transflow/pdf_kernel/{path.name}",
                change_policy="LIFT_AND_WRAP_NO_PAGE_SEMANTICS",
                evidence_status="NO_ROOT_GATE",
                evidence_refs=["docs/设计/Transflow_PDF翻译排版引擎_总体设计_v0.1.md#206-工具箱-spike-到生产模块"],
            )
        )
    for path in sorted((TOOLBOX_ROOT / "contracts").glob("*.json"), key=lambda item: item.name):
        records.append(
            migration_record(
                unit_id=f"toolbox.contract.schema.{path.stem}",
                category="toolbox_contract",
                source_paths=[path],
                target_path=f"tests/migration/contracts/{path.name}",
                change_policy="PRESERVE_AS_MIGRATION_CONTRACT",
                evidence_status="NO_ROOT_GATE",
                evidence_refs=[repository_relative(path)],
            )
        )
    contract_python = TOOLBOX_ROOT / "src" / "page_toolbox_puncture" / "contracts.py"
    translation_python = TOOLBOX_ROOT / "src" / "page_toolbox_puncture" / "translation.py"
    records.extend(
        [
            migration_record(
                unit_id="toolbox.contract.python",
                category="toolbox_contract",
                source_paths=[contract_python],
                target_path="src/transflow/domain/toolbox.py + src/transflow/domain/translation.py",
                change_policy="MERGE_DUPLICATE_DTOS_PRESERVE_IDS",
                evidence_status="NO_ROOT_GATE",
                evidence_refs=[repository_relative(contract_python)],
            ),
            migration_record(
                unit_id="toolbox.translation.compatibility",
                category="toolbox_contract",
                source_paths=[translation_python],
                target_path="src/transflow/adapters/translation/fixed.py",
                change_policy="FIRST_STAGE_FIXED_ADAPTER",
                evidence_status="NO_ROOT_GATE",
                evidence_refs=[repository_relative(translation_python)],
            ),
        ]
    )
    cadence_root = TOOLBOX_ROOT / "src" / "toolbox_cadence"
    records.append(
        migration_record(
            unit_id="toolbox.governance.cadence",
            category="toolbox_governance",
            source_paths=[cadence_root],
            target_path="scripts/ + tests/migration/",
            change_policy="GATE_TOOLING_ONLY_NOT_RUNTIME",
            evidence_status="NO_ROOT_GATE",
            evidence_refs=[repository_relative(cadence_root)],
        )
    )
    return records


def toolbox_target_path(toolbox_key: str) -> str:
    """把分类叶映射为总体设计规定的生产落点。"""

    if toolbox_key == "visual_only":
        return "src/transflow/toolboxes/catalog.py#visual_only_passthrough"
    if toolbox_key == "body.freeform":
        return "src/transflow/freeform/recovery.py"
    parts = toolbox_key.split(".")
    if parts[0] == "body":
        return "src/transflow/toolboxes/body/" + "/".join(parts[1:]) + "/"
    return f"src/transflow/toolboxes/{toolbox_key}/"


def leaf_evidence(source: Path) -> tuple[str, list[str], bool]:
    """从叶根级 Gate 读取真实成熟度；缺少根 Gate 时明确返回 NO_ROOT_GATE。"""

    stage_gate = source / "stage_gate.json"
    promotion_manifest = source / "promotion_manifest.json"
    if not stage_gate.is_file():
        refs = [repository_relative(source)]
        return "NO_ROOT_GATE", refs, promotion_manifest.is_file()
    payload = json.loads(stage_gate.read_text(encoding="utf-8"))
    decision = str(payload.get("decision", "NO_ROOT_GATE"))
    return decision, [repository_relative(stage_gate)], promotion_manifest.is_file()


def toolbox_leaf_records() -> list[dict[str, Any]]:
    """为分类树全部生产行为叶建立且仅建立一条迁移记录。"""

    records: list[dict[str, Any]] = []
    for toolbox_key in EXPECTED_ROUTE_BEHAVIORS:
        source = TOOLBOX_LEAF_SOURCES[toolbox_key]
        evidence_status, evidence_refs, promotion_present = leaf_evidence(source)
        if toolbox_key == "visual_only":
            change_policy = "DEFINE_DETERMINISTIC_PASSTHROUGH"
        elif toolbox_key == "body.freeform":
            change_policy = "NEW_BOUNDED_RECOVERY_NO_EXISTING_TOOLBOX"
        else:
            change_policy = "LIFT_AND_WRAP_ENABLE_ONLY_AFTER_NEW_GATE"
        record = migration_record(
            unit_id=f"toolbox.leaf.{toolbox_key}",
            category="toolbox_leaf",
            source_paths=[source],
            target_path=toolbox_target_path(toolbox_key),
            change_policy=change_policy,
            evidence_status=evidence_status,
            evidence_refs=evidence_refs,
        )
        record["promotion_manifest_present"] = promotion_present
        records.append(record)
    return records


def collect_migration_ledger() -> dict[str, Any]:
    """汇总全部 P0 迁移单元并检查唯一性。"""

    LOGGER.info("清点分类、Kernel、合同与全部路由行为叶")
    units = classification_records() + kernel_and_contract_records() + toolbox_leaf_records()
    unit_ids = [str(unit["unit_id"]) for unit in units]
    if len(unit_ids) != len(set(unit_ids)):
        raise ValueError("迁移台账出现重复 unit_id")
    return {
        "schema_version": "transflow.migration-ledger/v1",
        "required_fields": [
            "source_path",
            "source_hash",
            "target_path",
            "change_policy",
            "evidence_status",
            "evidence_ref",
        ],
        "route_behavior_keys": list(EXPECTED_ROUTE_BEHAVIORS),
        "units": sorted(units, key=lambda item: str(item["unit_id"])),
    }


def expand_test_references(text: str) -> set[str]:
    """展开 Gate 中 P0.1-T01~T03 一类测试范围引用。"""

    references = set(re.findall(r"P\d+\.\d+-T\d{2}", text))
    pattern = re.compile(r"(?P<prefix>P\d+\.\d+-T)(?P<start>\d{2})(?:～|~)T(?P<end>\d{2})")
    for match in pattern.finditer(text):
        start = int(match.group("start"))
        end = int(match.group("end"))
        references.update(
            f"{match.group('prefix')}{number:02d}" for number in range(start, end + 1)
        )
    return references


def collect_traceability_matrix() -> dict[str, Any]:
    """从批准计划生成设计、任务、交付、测试和 Gate 的双向追溯。"""

    LOGGER.info("解析详细计划并生成双向追溯矩阵")
    plan_text = PLAN_PATH.read_text(encoding="utf-8")
    design_text = DESIGN_PATH.read_text(encoding="utf-8")
    design_headings = set(
        re.findall(r"(?m)^#{1,6} (?P<section>\d+(?:\.\d+)*)\b", design_text)
    )
    task_pattern = re.compile(
        r"(?ms)^### (?P<id>P\d+\.\d+) (?P<title>[^\r\n]+)\r?\n"
        r"(?P<body>.*?)(?=^### |^## P|^# 第二部分|^# 附录|\Z)"
    )
    gate_rows: list[dict[str, Any]] = []
    for line in plan_text.splitlines():
        if not re.match(r"^\| G\d+-\d+ \|", line):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) != 5:
            raise ValueError(f"Gate 行列数不正确: {line}")
        gate_rows.append(
            {
                "gate_item": cells[0],
                "trace": cells[3],
                "evidence": cells[4],
                "test_refs": sorted(expand_test_references(cells[4])),
            }
        )
    defined_tests = set(
        re.findall(r"(?m)^- `(?P<id>P\d+\.\d+-T\d{2})`：", plan_text)
    )
    referenced_tests = set()
    for row in gate_rows:
        referenced_tests.update(row["test_refs"])
    tasks: list[dict[str, Any]] = []
    invalid_design_refs: set[str] = set()
    for match in task_pattern.finditer(plan_text):
        task_id = match.group("id")
        body = match.group("body")
        trace_match = re.search(r"\*\*设计追溯：\*\*(?P<value>[^\r\n]+)", body)
        delivery_match = re.search(
            r"\*\*前置依赖与交付接口：\*\*(?P<value>[^\r\n]+)", body
        )
        if trace_match is None or delivery_match is None:
            raise ValueError(f"任务缺少追溯或交付接口: {task_id}")
        trace_text = trace_match.group("value")
        design_part = trace_text.split("本计划", maxsplit=1)[0]
        design_sections = sorted(set(re.findall(r"§(\d+(?:\.\d+)*)", design_part)))
        invalid_design_refs.update(set(design_sections) - design_headings)
        task_tests = sorted(set(re.findall(r"P\d+\.\d+-T\d{2}", body)))
        related_gates = sorted(
            {
                str(row["gate_item"])
                for row in gate_rows
                if task_id in str(row["trace"])
                or bool(set(task_tests) & set(row["test_refs"]))
            }
        )
        tasks.append(
            {
                "stage": task_id.split(".", maxsplit=1)[0],
                "task_id": task_id,
                "task_title": match.group("title").strip(),
                "design_sections": design_sections,
                "delivery_contract": delivery_match.group("value").strip(),
                "test_ids": task_tests,
                "gate_items": related_gates,
            }
        )
    by_design: dict[str, list[str]] = {}
    by_test: dict[str, str] = {}
    for task in tasks:
        for section in task["design_sections"]:
            by_design.setdefault(str(section), []).append(str(task["task_id"]))
        for test_id in task["test_ids"]:
            by_test[str(test_id)] = str(task["task_id"])
    dangling_tests = sorted(referenced_tests - defined_tests)
    unreferenced_definitions = sorted(defined_tests - set(by_test))
    return {
        "schema_version": "transflow.traceability/v1",
        "source_plan": {
            "path": repository_relative(PLAN_PATH),
            "sha256": sha256_file(PLAN_PATH),
        },
        "tasks": tasks,
        "gate_rows": gate_rows,
        "indexes": {
            "by_design_section": {key: sorted(value) for key, value in sorted(by_design.items())},
            "by_test_id": dict(sorted(by_test.items())),
        },
        "validation": {
            "invalid_design_references": sorted(invalid_design_refs),
            "dangling_gate_test_references": dangling_tests,
            "unowned_test_definitions": unreferenced_definitions,
        },
    }


def write_json(path: Path, payload: dict[str, Any]) -> None:
    """以稳定 UTF-8 格式写入机器可读治理文件。"""

    LOGGER.info("写入 P0 资产 path=%s", repository_relative(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def expected_assets() -> dict[Path, dict[str, Any]]:
    """在内存中重算全部 P0 生成资产。"""

    return {
        BASELINE_PATH: collect_baseline_manifest(),
        LEDGER_PATH: collect_migration_ledger(),
        TRACEABILITY_PATH: collect_traceability_matrix(),
    }


def write_assets() -> None:
    """首次冻结并写入 P0 三类权威资产。"""

    for path, payload in expected_assets().items():
        write_json(path, payload)


def check_assets() -> list[str]:
    """重算资产并返回与已冻结文件不一致的路径。"""

    mismatches: list[str] = []
    for path, expected in expected_assets().items():
        if not path.is_file():
            mismatches.append(f"MISSING:{repository_relative(path)}")
            continue
        actual = json.loads(path.read_text(encoding="utf-8"))
        if actual != expected:
            mismatches.append(f"DRIFT:{repository_relative(path)}")
    return mismatches


def parse_args() -> argparse.Namespace:
    """解析 P0 资产生成或核验动作。"""

    parser = argparse.ArgumentParser(description="生成或核验 Transflow P0 基线资产")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--write", action="store_true", help="写入重算后的基线资产")
    action.add_argument("--check", action="store_true", help="只核验现有资产是否漂移")
    return parser.parse_args()


def main() -> int:
    """执行 P0 基线生成或只读核验，并返回真实退出码。"""

    configure_logging()
    args = parse_args()
    if args.write:
        write_assets()
        LOGGER.info("P0 基线资产写入完成")
        return 0
    mismatches = check_assets()
    if mismatches:
        for mismatch in mismatches:
            LOGGER.error("P0 基线漂移 %s", mismatch)
        return 1
    LOGGER.info("P0 基线资产核验通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
