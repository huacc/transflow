"""提供 chart 与 diagram 原生文本标签的独立复核叶类型。"""

from __future__ import annotations

import logging
from pathlib import Path

from transflow.toolboxes.leaves.policy import P8ToolboxPolicy
from transflow.toolboxes.leaves.text_patch import (
    ROUTE_CHART,
    ROUTE_DIAGRAM,
    TextPatchToolbox,
)

LOGGER = logging.getLogger("transflow.toolboxes.leaves.native_labels")
PACKAGE_ROOT = Path(__file__).resolve().parent.parent


class ChartTextToolbox(TextPatchToolbox):
    """只为有原生绘图锚点且无栅格图的 chart 选择文本标签。"""

    def __init__(self, policy: P8ToolboxPolicy, font_path: Path) -> None:
        """绑定 chart 私有 Route；是否生产启用仍由独立叶 Gate 决定。"""

        super().__init__(ROUTE_CHART, policy, font_path)


class DiagramTextToolbox(TextPatchToolbox):
    """只为原生 diagram 绘图附近的文本选择候选 label owner。"""

    def __init__(self, policy: P8ToolboxPolicy, font_path: Path) -> None:
        """绑定 diagram 私有 Route；不继承 chart 的 Gate 结论。"""

        super().__init__(ROUTE_DIAGRAM, policy, font_path)


def main() -> int:
    """记录两个非盲旧叶必须独立复核且默认不注册生产 factory。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    LOGGER.info("NativeLabelToolbox 示例，意图=仅处理明确原生标签")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
