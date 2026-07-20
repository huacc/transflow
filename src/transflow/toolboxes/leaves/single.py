"""提供 body.flow_text.single 的正式生产叶类型。"""

from __future__ import annotations

import logging
from pathlib import Path

from transflow.toolboxes.leaves.policy import P8ToolboxPolicy
from transflow.toolboxes.leaves.text_patch import ROUTE_SINGLE, TextPatchToolbox

LOGGER = logging.getLogger("transflow.toolboxes.leaves.single")
PACKAGE_ROOT = Path(__file__).resolve().parent.parent


class SingleFlowTextToolbox(TextPatchToolbox):
    """把单栏正文结构规则绑定到稳定文本 Patch 六阶段。"""

    def __init__(self, policy: P8ToolboxPolicy, font_path: Path) -> None:
        """注入集中策略和受控字体，不接受样本身份参数。"""

        super().__init__(ROUTE_SINGLE, policy, font_path)


def main() -> int:
    """记录 single 叶只拥有页面中部连续正文。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    LOGGER.info("SingleFlowTextToolbox 示例，意图=处理单栏连续正文")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
