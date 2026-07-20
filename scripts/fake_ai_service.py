"""提供 P3 合同测试专用、可编程且不进入 production wheel 的真实 HTTP 服务。"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any

LOGGER = logging.getLogger("transflow.fake_ai")
REPO_ROOT = Path(__file__).resolve().parent.parent
TOKEN_ENVIRONMENT_VARIABLE = "TRANSFLOW_TEST_AI_TOKEN"


@dataclass(slots=True)
class FakeAiState:
    """保存可由测试原子切换的响应模式、就绪状态和延迟。"""

    mode: str = "normal"
    translation_ready: bool = True
    decision_ready: bool = True
    timeout_delay_seconds: float = 0.5

    @property
    def ready(self) -> bool:
        """仅在翻译和判定两个合同依赖都可用时返回就绪。"""

        return self.translation_ready and self.decision_ready


class FakeAiService:
    """在随机或指定 loopback 端口提供两个 AI 合同和健康端点。"""

    def __init__(
        self,
        service_token: str,
        *,
        max_request_bytes: int = 64 * 1024,
        port: int = 0,
    ) -> None:
        """保存仅内存令牌、请求上限和初始监听端口。"""

        if not service_token:
            raise ValueError("fake AI service token 为空")
        self.state = FakeAiState()
        self._service_token = service_token
        self._max_request_bytes = max_request_bytes
        self._port = port
        self._server: ThreadingHTTPServer | None = None
        self._thread: Thread | None = None

    def _handler_class(self) -> type[BaseHTTPRequestHandler]:
        """构造绑定当前服务状态的请求处理器类。"""

        service = self

        class Handler(BaseHTTPRequestHandler):
            """处理 fake AI 健康和两个 JSON 合同。"""

            def _send_json(self, status: int, payload: dict[str, Any]) -> None:
                """发送 UTF-8 JSON 响应并写明内容长度。"""

                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    LOGGER.info("fake AI 客户端已断开，意图=正常结束超时故障响应")

            def do_GET(self) -> None:
                """返回 live 或依赖感知 ready 状态。"""

                if self.path == "/health/live":
                    self._send_json(200, {"ok": True, "status": "live"})
                    return
                if self.path == "/health/ready":
                    status = 200 if service.state.ready else 503
                    self._send_json(
                        status,
                        {
                            "decision_ready": service.state.decision_ready,
                            "ok": service.state.ready,
                            "status": "ready" if service.state.ready else "not_ready",
                            "translation_ready": service.state.translation_ready,
                        },
                    )
                    return
                self._send_json(404, {"error": "not_found"})

            def do_POST(self) -> None:
                """执行鉴权、大小限制、故障模式和正常合同响应。"""

                authorization = self.headers.get("Authorization", "")
                if authorization != f"Bearer {service._service_token}":
                    self._send_json(401, {"error": "unauthorized"})
                    return
                length = int(self.headers.get("Content-Length", "0"))
                if length > service._max_request_bytes:
                    self._send_json(413, {"error": "request_too_large"})
                    return
                if service.state.mode == "timeout":
                    time.sleep(service.state.timeout_delay_seconds)
                if service.state.mode == "500":
                    self._send_json(500, {"error": "server_error"})
                    return
                if service.state.mode == "429":
                    self._send_json(429, {"error": "rate_limited"})
                    return
                if service.state.mode == "invalid_json":
                    body = b"{invalid-json"
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                try:
                    request = json.loads(self.rfile.read(length))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    self._send_json(400, {"error": "invalid_json"})
                    return
                if self.path == "/v1/translation":
                    self._translation(request)
                    return
                if self.path == "/v1/model-decision":
                    self._decision(request)
                    return
                self._send_json(404, {"error": "not_found"})

            def _translation(self, request: dict[str, Any]) -> None:
                """生成保持 unit_id 的翻译响应并按模式注入身份错误。"""

                request_units = request.get("units", [])
                units = [
                    {
                        "translated_text": (
                            f"[{request.get('target_language')}]{item['source_text']}"
                        ),
                        "unit_id": item["unit_id"],
                    }
                    for item in request_units
                ]
                requested_ids = [item["unit_id"] for item in request_units]
                if service.state.mode == "missing_id" and units:
                    units.pop()
                elif service.state.mode == "duplicate_id" and units:
                    units[-1]["unit_id"] = units[0]["unit_id"]
                elif service.state.mode == "extra_id":
                    units.append({"translated_text": "extra", "unit_id": "extra-unit"})
                payload = {
                    "batch_id": request.get("batch_id"),
                    "requested_unit_ids": requested_ids,
                    "schema_version": "transflow.translation-bundle/v1",
                    "units": units,
                }
                if service.state.mode == "schema_error":
                    payload.pop("batch_id")
                self._send_json(200, payload)

            def _decision(self, request: dict[str, Any]) -> None:
                """生成 Schema 有效的结构化分类判定。"""

                payload = {
                    "decision_id": request.get("decision_id"),
                    "decision_kind": request.get("decision_kind"),
                    "evidence_ids": request.get("evidence_ids", []),
                    "result_code": "body.table",
                    "schema_version": "transflow.model-decision/v1",
                }
                if service.state.mode == "schema_error":
                    payload.pop("result_code")
                self._send_json(200, payload)

            def log_message(self, message_format: str, *args: object) -> None:
                """只记录请求行和状态，不记录鉴权头或请求正文。"""

                LOGGER.info("fake AI HTTP " + message_format, *args)

        return Handler

    @property
    def base_url(self) -> str:
        """返回已经启动服务的 loopback 基础 URL。"""

        if self._server is None:
            raise RuntimeError("fake AI service 尚未启动")
        raw_host = self._server.server_address[0]
        host = raw_host.decode("ascii") if isinstance(raw_host, bytes) else str(raw_host)
        port = self._server.server_port
        return f"http://{host}:{port}"

    def start(self) -> FakeAiService:
        """启动真实 ThreadingHTTPServer 并等待线程进入服务循环。"""

        if self._server is not None:
            return self
        self._server = ThreadingHTTPServer(("127.0.0.1", self._port), self._handler_class())
        self._thread = Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        LOGGER.info("启动 fake AI Service，意图=提供真实 HTTP 合同 url=%s", self.base_url)
        return self

    def stop(self) -> None:
        """关闭服务、释放端口并等待线程退出。"""

        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._server = None
        self._thread = None
        LOGGER.info("停止 fake AI Service，意图=验证停服后 readiness 不误报")


def parse_args() -> argparse.Namespace:
    """解析 fake 服务监听端口和请求大小上限。"""

    parser = argparse.ArgumentParser(description="运行 Transflow P3 fake AI HTTP 服务")
    parser.add_argument("--port", type=int, default=18081)
    parser.add_argument("--max-request-bytes", type=int, default=65536)
    return parser.parse_args()


def main() -> int:
    """从环境变量读取测试令牌并阻塞运行 fake 服务。"""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    args = parse_args()
    token = os.environ.get(TOKEN_ENVIRONMENT_VARIABLE)
    if not token:
        raise RuntimeError(f"必须设置环境变量 {TOKEN_ENVIRONMENT_VARIABLE}")
    service = FakeAiService(token, max_request_bytes=args.max_request_bytes, port=args.port).start()
    try:
        if service._thread is not None:
            service._thread.join()
    except KeyboardInterrupt:
        service.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
