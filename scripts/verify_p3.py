"""汇总验证 P3 文件权威状态、日志秘密和 production wheel 边界。"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from scripts.verify_architecture import DEFAULT_SOURCE_ROOT, scan_production_tree

LOGGER = logging.getLogger("transflow.p3.verify")
REPO_ROOT = Path(__file__).resolve().parent.parent
P3_TEST_ROOT = REPO_ROOT / "tmp" / "p3-tests"
REVIEW_PATH = REPO_ROOT / "docs" / "合同" / "P3文件副作用与测试AI评审_v0.1.md"
GOVERNANCE_PATH = REPO_ROOT / "docs" / "迁移" / "governance_registry.json"
KNOWN_SECRET_MARKERS = (
    "p3-local-contract-token",
    "p3-wrong-sensitive-token",
    "sk-p3-sensitive-secret",
)


def load_json(path: Path) -> dict[str, Any]:
    """读取 UTF-8 JSON 对象。"""

    return json.loads(path.read_text(encoding="utf-8"))


def verify_checkpoint_manifests() -> list[str]:
    """验证每个已提交 Checkpoint manifest 只引用存在且哈希一致的文件。"""

    violations: list[str] = []
    for manifest_path in sorted(P3_TEST_ROOT.rglob("checkpoint_manifest.json")):
        manifest = load_json(manifest_path)
        run_root = manifest_path.parent.parent
        entries = list(manifest["pages"].values())
        if manifest["run"] is not None:
            entries.append(manifest["run"])
        for entry in entries:
            path = run_root / entry["relative_path"]
            if not path.is_file():
                violations.append(f"CHECKPOINT_MISSING:{path.relative_to(REPO_ROOT).as_posix()}")
                continue
            content = path.read_bytes()
            if hashlib.sha256(content).hexdigest() != entry["file_hash"]:
                violations.append(f"CHECKPOINT_FILE_HASH:{path.relative_to(REPO_ROOT).as_posix()}")
                continue
            payload = json.loads(content)
            state = base64.b64decode(payload["payload_base64"], validate=True)
            if hashlib.sha256(state).hexdigest() != payload["state_hash"]:
                violations.append(f"CHECKPOINT_STATE_HASH:{path.relative_to(REPO_ROOT).as_posix()}")
    return violations


def verify_artifact_manifests() -> list[str]:
    """验证每个已登记 Artifact 的路径、大小和真实内容哈希。"""

    violations: list[str] = []
    for manifest_path in sorted(P3_TEST_ROOT.rglob("artifact_manifest.json")):
        manifest = load_json(manifest_path)
        run_root = manifest_path.parent.parent
        for entry in manifest["entries"].values():
            path = run_root / entry["relative_path"]
            if not path.is_file():
                violations.append(f"ARTIFACT_MISSING:{path.relative_to(REPO_ROOT).as_posix()}")
                continue
            content = path.read_bytes()
            if len(content) != entry["size_bytes"]:
                violations.append(f"ARTIFACT_SIZE:{path.relative_to(REPO_ROOT).as_posix()}")
            if hashlib.sha256(content).hexdigest() != entry["content_hash"]:
                violations.append(f"ARTIFACT_HASH:{path.relative_to(REPO_ROOT).as_posix()}")
    return violations


def verify_half_writes_and_secrets() -> list[str]:
    """确认没有残留已登记 partial，且 JSONL 不包含测试秘密或无界行。"""

    violations: list[str] = []
    for path in P3_TEST_ROOT.rglob("*.partial"):
        violations.append(f"PARTIAL_REMAINS:{path.relative_to(REPO_ROOT).as_posix()}")
    for path in P3_TEST_ROOT.rglob("*.jsonl"):
        text = path.read_text(encoding="utf-8")
        for marker in KNOWN_SECRET_MARKERS:
            if marker in text:
                violations.append(f"SECRET_LEAK:{path.relative_to(REPO_ROOT).as_posix()}")
        if any(len(line.encode("utf-8")) > 4096 for line in text.splitlines()):
            violations.append(f"UNBOUNDED_LOG:{path.relative_to(REPO_ROOT).as_posix()}")
    for pending_name in ("pending_artifacts.json", "pending_checkpoints.json"):
        for path in P3_TEST_ROOT.rglob(pending_name):
            violations.append(f"STALE_PENDING_JOURNAL:{path.relative_to(REPO_ROOT).as_posix()}")
    return violations


def verify_production_boundary() -> list[str]:
    """确认生产包无架构违规，且 wheel 不含真实 Provider 或 fake 服务。"""

    violations = [
        f"ARCHITECTURE:{item.code}:{item.relative_path}:{item.line}"
        for item in scan_production_tree(DEFAULT_SOURCE_ROOT)
    ]
    wheels = tuple((P3_TEST_ROOT / "wheel").glob("*.whl"))
    if len(wheels) != 1:
        return [*violations, f"WHEEL_COUNT:{len(wheels)}"]
    with zipfile.ZipFile(wheels[0]) as archive:
        names = tuple(name.casefold() for name in archive.namelist())
        sources = "\n".join(
            archive.read(name).decode("utf-8", errors="replace")
            for name in archive.namelist()
            if name.endswith(".py")
        ).casefold()
    forbidden_names = ("qwen.py", "provider.py", "fake_ai_service.py")
    for name in names:
        if name.endswith(forbidden_names):
            violations.append(f"FORBIDDEN_WHEEL_FILE:{name}")
    if "import litellm" in sources or "from litellm" in sources:
        violations.append("FORBIDDEN_WHEEL_IMPORT:litellm")
    return violations


def verify_review() -> list[str]:
    """验证 P3 边界评审通过且没有开放决策。"""

    violations: list[str] = []
    review = REVIEW_PATH.read_text(encoding="utf-8") if REVIEW_PATH.is_file() else ""
    if "评审结论：PASS" not in review:
        violations.append("REVIEW_NOT_PASS")
    if "开放问题：0" not in review:
        violations.append("REVIEW_OPEN_ISSUES")
    governance = load_json(GOVERNANCE_PATH)
    if governance.get("current_stage", {}).get("open_decision_ids"):
        violations.append("OPEN_DECISION")
    return violations


CHECKS: dict[str, Callable[[], list[str]]] = {
    "checkpoints": verify_checkpoint_manifests,
    "artifacts": verify_artifact_manifests,
    "safety": verify_half_writes_and_secrets,
    "production": verify_production_boundary,
    "review": verify_review,
}


def execute(selected: str) -> int:
    """执行一个或全部 P3 检查并逐项输出 PASS/FAIL。"""

    names = tuple(CHECKS) if selected == "all" else (selected,)
    failed = False
    for name in names:
        LOGGER.info("调用 P3 汇总检查，意图=验证外部副作用闭环 check=%s", name)
        violations = CHECKS[name]()
        status = "PASS" if not violations else "FAIL"
        print(f"P3_VERIFY check={name} status={status} violations={len(violations)}")
        for violation in violations:
            print(f"P3_VERIFY_VIOLATION check={name} detail={violation}")
        failed = failed or bool(violations)
    print(f"P3_VERIFY_SUMMARY status={'FAIL' if failed else 'PASS'} checks={len(names)}")
    return 1 if failed else 0


def parse_args() -> argparse.Namespace:
    """解析 P3 检查分组。"""

    parser = argparse.ArgumentParser(description="验证 Transflow P3 文件与测试 AI 边界")
    parser.add_argument("check", choices=("all", *CHECKS), nargs="?", default="all")
    return parser.parse_args()


def main() -> int:
    """执行命令行指定的 P3 汇总检查。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    return execute(parse_args().check)


if __name__ == "__main__":
    raise SystemExit(main())
