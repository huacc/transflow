"""从冻结清单下载并校验 Transflow P1 非 Git 字体资产。"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import urllib.request
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger("transflow.p1.bootstrap")
REPO_ROOT = Path(__file__).resolve().parent.parent
FONT_MANIFEST_PATH = REPO_ROOT / "resources" / "manifests" / "font_manifest.json"


def configure_logging() -> None:
    """配置 P1 资产安装日志。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")


def sha256_file(path: Path) -> str:
    """流式计算下载资产的 SHA-256。"""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_manifest() -> dict[str, Any]:
    """读取冻结字体清单并校验 schema。"""

    payload = json.loads(FONT_MANIFEST_PATH.read_text(encoding="utf-8"))
    if payload.get("schema_version") != "transflow.font-manifest/v1":
        raise ValueError("字体清单 schema_version 不受支持")
    return payload


def _download_atomic(url: str, target: Path, expected_sha256: str) -> None:
    """下载到 partial、校验哈希并原子替换目标文件。"""

    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(f"{target.name}.partial")
    LOGGER.info("调用资产下载，意图=恢复冻结 P1 资产 target=%s", target)
    with urllib.request.urlopen(url, timeout=120) as response, partial.open("wb") as stream:
        while chunk := response.read(1024 * 1024):
            stream.write(chunk)
        stream.flush()
        os.fsync(stream.fileno())
    actual_sha256 = sha256_file(partial)
    if actual_sha256 != expected_sha256:
        partial.unlink(missing_ok=True)
        raise ValueError(f"下载资产 SHA-256 不匹配: {target.name}")
    partial.replace(target)


def ensure_asset(url: str, relative_path: str, expected_sha256: str) -> str:
    """复用哈希正确的本地资产，否则按固定 URL 重新下载。"""

    target = (REPO_ROOT / relative_path).resolve()
    try:
        target.relative_to(REPO_ROOT.resolve())
    except ValueError as error:
        raise ValueError(f"字体资产路径越出仓库根: {relative_path}") from error
    if target.is_file() and sha256_file(target) == expected_sha256:
        LOGGER.info("资产已存在，意图=复用冻结内容 target=%s", target)
        return "reused"
    _download_atomic(url, target, expected_sha256)
    return "downloaded"


def bootstrap_fonts() -> dict[str, str]:
    """按 manifest 恢复字体及许可证，并返回逐项真实处理状态。"""

    results: dict[str, str] = {}
    for asset in load_manifest()["assets"]:
        asset_id = str(asset["id"])
        results[f"{asset_id}:font"] = ensure_asset(
            str(asset["source_url"]),
            str(asset["path"]),
            str(asset["sha256"]),
        )
        results[f"{asset_id}:license"] = ensure_asset(
            str(asset["license_url"]),
            str(asset["license_path"]),
            str(asset["license_sha256"]),
        )
    return results


def parse_args() -> argparse.Namespace:
    """解析 P1 资产恢复命令参数。"""

    parser = argparse.ArgumentParser(description="恢复并校验 Transflow P1 字体资产")
    parser.add_argument("--check", action="store_true", help="仅校验现有资产，不下载")
    return parser.parse_args()


def check_fonts() -> dict[str, bool]:
    """只读取本地文件并核对清单哈希。"""

    results: dict[str, bool] = {}
    for asset in load_manifest()["assets"]:
        for kind, path_key, hash_key in (
            ("font", "path", "sha256"),
            ("license", "license_path", "license_sha256"),
        ):
            path = REPO_ROOT / str(asset[path_key])
            results[f"{asset['id']}:{kind}"] = (
                path.is_file() and sha256_file(path) == asset[hash_key]
            )
    return results


def main() -> int:
    """执行字体恢复或只读核验并以 JSON 输出结果。"""

    configure_logging()
    args = parse_args()
    results: dict[str, str] | dict[str, bool]
    results = check_fonts() if args.check else bootstrap_fonts()
    print(json.dumps(results, ensure_ascii=False, indent=2, sort_keys=True))
    passed = all(
        value is True or value in {"reused", "downloaded"} for value in results.values()
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
