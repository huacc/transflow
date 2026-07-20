"""显式构造 P8/P9 已通过 Gate 的生产 Toolbox factory 集。"""

from __future__ import annotations

import logging
from pathlib import Path

from transflow.pdf_kernel.fonts import ControlledFontRegistry
from transflow.toolboxes.catalog import ToolboxFactory
from transflow.toolboxes.leaves.ordinary_policy import load_p9_ordinary_leaf_policy
from transflow.toolboxes.leaves.policy import load_p8_toolbox_policy
from transflow.toolboxes.leaves.single import SingleFlowTextToolbox
from transflow.toolboxes.leaves.visual_only import VisualOnlyToolbox

LOGGER = logging.getLogger("transflow.toolboxes.leaves.factory")
PACKAGE_ROOT = Path(__file__).resolve().parent.parent


def build_p8_toolbox_factories(
    policy_path: Path,
    font_manifest_path: Path,
    repository_root: Path,
) -> dict[str, ToolboxFactory]:
    """只登记 P8 证据允许启用的 visual_only 和 single 两个 factory。"""

    LOGGER.info("调用 P8 factory 构造，意图=显式注册已通过叶，不做动态发现")
    policy = load_p8_toolbox_policy(policy_path)
    fonts = ControlledFontRegistry(font_manifest_path, repository_root)
    font_path = fonts.resolve(policy.font_id).path
    return {
        "visual_only": VisualOnlyToolbox,
        "body.flow_text.single": lambda: SingleFlowTextToolbox(policy, font_path),
    }


def build_p9_toolbox_factories(
    p8_policy_path: Path,
    p9_policy_path: Path,
    font_manifest_path: Path,
    repository_root: Path,
) -> dict[str, ToolboxFactory]:
    """校验 P9 集中策略，并只返回当前证据允许启用的生产 factory。"""

    LOGGER.info("调用 P9 factory 构造，意图=验证普通叶配置但不注册证据不足叶")
    load_p9_ordinary_leaf_policy(p9_policy_path)
    # 六个 P9 普通叶均缺少新的独立真实盲测，Catalog 保持 disabled；
    # 因此生产 factory 集必须严格等于已通过 G8 的启用集合。
    return build_p8_toolbox_factories(
        p8_policy_path,
        font_manifest_path,
        repository_root,
    )


def main() -> int:
    """记录任何独立盲测不足叶都不进入生产 factory 集。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    LOGGER.info("P8/P9 factory 示例，意图=只暴露通过 PASS_ENABLE 的叶")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
