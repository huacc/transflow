"""读取匿名分类基线并强制执行事前冻结阈值。"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger("transflow.classification.baseline")
PACKAGE_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = PACKAGE_ROOT.parent.parent


class ThresholdFreezeError(ValueError):
    """表示迁移结果产生后未经决策记录修改冻结阈值。"""


def _sha256_file(path: Path) -> str:
    """流式计算文件哈希，供冻结收据核对。"""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class FrozenThresholdRegistry:
    """保存已由独立收据绑定的数值阈值注册表。"""

    payload: dict[str, Any]
    file_sha256: str

    @classmethod
    def load(cls, registry_path: Path, receipt_path: Path) -> FrozenThresholdRegistry:
        """加载阈值和收据，并拒绝哈希漂移或非数值门槛。"""

        LOGGER.info("调用阈值读取，意图=核对 P5 事前冻结收据")
        payload = json.loads(registry_path.read_text(encoding="utf-8"))
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        actual_hash = _sha256_file(registry_path)
        if receipt.get("threshold_registry_sha256") != actual_hash:
            raise ThresholdFreezeError("阈值注册表与事前冻结收据不一致")
        numeric_values = [
            value
            for key, value in payload["global_thresholds"].items()
            if key.endswith(("_min", "_max"))
        ]
        for leaf in payload["leaf_thresholds"].values():
            numeric_values.extend(
                leaf[key] for key in ("minimum_samples", "precision_min", "recall_min")
            )
        if any(
            isinstance(value, bool) or not isinstance(value, int | float)
            for value in numeric_values
        ):
            raise ThresholdFreezeError("阈值注册表包含非数值门槛")
        return cls(payload, actual_hash)

    def require_unchanged(self, candidate_payload: dict[str, Any]) -> None:
        """阻断任何未附决策记录的候选阈值修改。"""

        current = json.dumps(
            self.payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        candidate = json.dumps(
            candidate_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if candidate != current:
            raise ThresholdFreezeError("冻结后阈值修改必须先登记并关闭行为变化决策")


def main() -> int:
    """从仓库相对资源读取并核对默认 P5 阈值。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    manifests = REPO_ROOT / "resources" / "manifests"
    registry = FrozenThresholdRegistry.load(
        manifests / "p5_classification_thresholds.json",
        manifests / "p5_threshold_freeze_receipt.json",
    )
    print(f"P5_THRESHOLD_FREEZE PASS sha256={registry.file_sha256}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
