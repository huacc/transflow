"""实现 P1 liveness/readiness 语义和内部 FastAPI 健康端点。"""

from __future__ import annotations

import logging
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI

from transflow.runtime.config import RuntimeConfig, load_runtime_config
from transflow.runtime.probes import validate_font_manifest

LOGGER = logging.getLogger("transflow.runtime.health")


@dataclass(frozen=True, slots=True)
class HealthFinding:
    """表达健康探针的稳定错误码与无秘密说明。"""

    code: str
    passed: bool
    detail: str


@dataclass(frozen=True, slots=True)
class HealthReport:
    """表达 liveness 或 readiness 的完整判定结果。"""

    status: str
    ok: bool
    findings: tuple[HealthFinding, ...]

    def as_dict(self) -> dict[str, Any]:
        """把健康结果转换为可由 HTTP 安全返回的字典。"""

        return {
            "status": self.status,
            "ok": self.ok,
            "findings": [asdict(finding) for finding in self.findings],
        }


def _safe_endpoint(raw_url: str) -> str:
    """只保留协议、主机、端口和路径，确保日志不出现凭据或查询串。"""

    parsed = urlsplit(raw_url)
    host = parsed.hostname or "invalid-host"
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{host}{port}{parsed.path}"


def _probe_workspace(workspace: Path) -> HealthFinding:
    """通过真实写入、fsync、rename 和删除验证工作目录可写。"""

    try:
        workspace.mkdir(parents=True, exist_ok=True)
        if not workspace.is_dir():
            raise OSError("工作路径不是目录")
        partial = workspace / f".health-{os.getpid()}.partial"
        final = workspace / f".health-{os.getpid()}.ready"
        with partial.open("wb") as stream:
            stream.write(b"transflow-ready")
            stream.flush()
            os.fsync(stream.fileno())
        partial.replace(final)
        final.unlink()
    except OSError as error:
        return HealthFinding("WORKSPACE_NOT_WRITABLE", False, type(error).__name__)
    return HealthFinding("WORKSPACE_WRITABLE", True, "workspace 写入与原子替换成功")


class HealthService:
    """按统一配置执行不混淆语义的存活与就绪检查。"""

    def __init__(self, config: RuntimeConfig, timeout_seconds: float = 2.0) -> None:
        """保存只读配置与外部 capability 探测超时。"""

        self._config = config
        self._timeout_seconds = timeout_seconds

    def liveness(self) -> HealthReport:
        """确认当前进程仍可响应，不把外部依赖失败误报为进程死亡。"""

        LOGGER.info("调用存活探针，意图=确认 Transflow 进程可响应")
        return HealthReport(
            status="live",
            ok=True,
            findings=(HealthFinding("PROCESS_RESPONSIVE", True, "进程可响应"),),
        )

    def readiness(self) -> HealthReport:
        """验证 workspace、字体与外部 capability，任一失败即 not_ready。"""

        LOGGER.info("调用就绪探针，意图=验证启动前全部硬依赖")
        findings: list[HealthFinding] = [_probe_workspace(self._config.workspace)]
        font_findings = validate_font_manifest(self._config.font_manifest)
        if font_findings and all(item.passed for item in font_findings):
            findings.append(HealthFinding("FONTS_READY", True, "受控字体清单有效"))
        else:
            codes = ",".join(item.code for item in font_findings) or "FONT_MANIFEST_EMPTY"
            findings.append(HealthFinding("FONT_DEPENDENCY_FAILED", False, codes))
        safe_endpoint = _safe_endpoint(self._config.ai_capability_url)
        try:
            LOGGER.info("调用外部健康接口，意图=验证 AI capability 可达 endpoint=%s", safe_endpoint)
            with httpx.Client(timeout=self._timeout_seconds, trust_env=False) as client:
                response = client.get(self._config.ai_capability_url)
            reachable = 200 <= response.status_code < 300
            findings.append(
                HealthFinding(
                    "AI_CAPABILITY_REACHABLE" if reachable else "AI_CAPABILITY_UNHEALTHY",
                    reachable,
                    f"endpoint={safe_endpoint},status={response.status_code}",
                )
            )
        except httpx.HTTPError as error:
            findings.append(
                HealthFinding(
                    "AI_CAPABILITY_UNREACHABLE",
                    False,
                    f"endpoint={safe_endpoint},error={type(error).__name__}",
                )
            )
        ready = all(finding.passed for finding in findings)
        return HealthReport(
            status="ready" if ready else "not_ready",
            ok=ready,
            findings=tuple(findings),
        )


def create_health_app(config: RuntimeConfig | None = None) -> FastAPI:
    """构造只暴露内部 live/ready 合同的最小 FastAPI 应用。"""

    selected = config or load_runtime_config()
    service = HealthService(selected)
    app = FastAPI(title="Transflow Health", version="1.0.0")

    @app.get("/health/live")
    def health_live() -> dict[str, Any]:
        """返回不依赖外部服务的存活结果。"""

        return service.liveness().as_dict()

    @app.get("/health/ready")
    def health_ready() -> dict[str, Any]:
        """返回 workspace、字体和 capability 的联合就绪结果。"""

        return service.readiness().as_dict()

    return app


def main() -> int:
    """演示读取统一配置并执行一次 live/ready 健康检查。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    service = HealthService(load_runtime_config())
    print(service.liveness().as_dict())
    print(service.readiness().as_dict())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
