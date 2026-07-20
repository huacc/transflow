"""集中加载 P8 第一批叶共用的无秘密布局与翻译策略。"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

LOGGER = logging.getLogger("transflow.toolboxes.leaves.policy")
PACKAGE_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True, slots=True)
class P8ToolboxPolicy:
    """保存第一批叶运行时唯一允许使用的结构化参数。"""

    source_language: str
    target_language: str
    font_id: str
    minimum_font_size: float
    maximum_font_size: float
    font_scale: float
    body_margin_top_ratio: float
    body_margin_bottom_ratio: float
    repair_limit: int

    def __post_init__(self) -> None:
        """拒绝空语言/字体、无效字号、边距和超预算修复策略。"""

        if not self.source_language or not self.target_language or not self.font_id:
            raise ValueError("P8 Toolbox 语言和字体配置不得为空")
        if not 0 < self.minimum_font_size <= self.maximum_font_size:
            raise ValueError("P8 Toolbox 字号范围无效")
        if not 0 < self.font_scale <= 1:
            raise ValueError("P8 Toolbox font_scale 必须位于 (0, 1]")
        if not 0 <= self.body_margin_top_ratio < self.body_margin_bottom_ratio <= 1:
            raise ValueError("P8 Toolbox 正文边距比例无效")
        if self.repair_limit != 1:
            raise ValueError("P8 第一批叶只允许一次有界 Repair")


def load_p8_toolbox_policy(path: Path) -> P8ToolboxPolicy:
    """从注入的统一配置路径读取并校验 P8 叶策略。"""

    LOGGER.info("调用 P8 叶策略读取，意图=禁止代码内散落布局参数 path=%s", path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != "transflow.p8-toolbox-policy/v1":
        raise ValueError("P8 Toolbox policy schema_version 不受支持")
    return P8ToolboxPolicy(
        source_language=str(payload["source_language"]),
        target_language=str(payload["target_language"]),
        font_id=str(payload["font_id"]),
        minimum_font_size=float(payload["minimum_font_size"]),
        maximum_font_size=float(payload["maximum_font_size"]),
        font_scale=float(payload["font_scale"]),
        body_margin_top_ratio=float(payload["body_margin_top_ratio"]),
        body_margin_bottom_ratio=float(payload["body_margin_bottom_ratio"]),
        repair_limit=int(payload["repair_limit"]),
    )


def main() -> int:
    """记录 P8 叶参数只允许从统一配置入口注入。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    LOGGER.info("P8ToolboxPolicy 示例，意图=集中管理叶布局参数")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
