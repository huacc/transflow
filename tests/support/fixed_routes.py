"""提供仅测试可达、按稳定页面身份声明的 P4 固定路由。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from transflow.application.contracts import EnumeratedPage
from transflow.application.page_pipeline import ROUTE_PASSTHROUGH

LOGGER = logging.getLogger("transflow.tests.fixed_routes")
TESTS_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True, slots=True)
class FixedRouteFixture:
    """只按完整页面身份查表，未声明页面始终透传。"""

    routes_by_page_identity: dict[str, str]

    def __call__(self, page: EnumeratedPage) -> str:
        """返回显式 fixture Route，不读取文件名、公司名或样本 ID。"""

        route = self.routes_by_page_identity.get(page.facts.page_identity, ROUTE_PASSTHROUGH)
        LOGGER.info(
            "调用测试固定路由，意图=按稳定页面身份选择行为 page_no=%s route=%s",
            page.context.page_no,
            route,
        )
        return route


def main() -> int:
    """记录固定 Route 只允许测试装配导入。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    LOGGER.info("FixedRouteFixture 示例，意图=未声明页面默认透传")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
