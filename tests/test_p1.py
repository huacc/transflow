"""按详细计划逐项验收 Transflow P1.1 至 P1.4。"""

from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pymupdf
import pytest

from scripts.verify_clean_install import verify_two_clean_installs
from scripts.verify_p1 import find_forbidden_dependencies
from transflow.runtime.config import find_plaintext_secrets, load_runtime_config
from transflow.runtime.health import HealthService, create_health_app
from transflow.runtime.probes import (
    assess_atomic_devices,
    atomic_publish_bytes,
    collect_environment_snapshot,
    create_and_reopen_minimal_pdf,
    open_pdf_in_process_pool,
    reject_open_document_payload,
    render_registered_font,
    validate_font_manifest,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
P1_TEST_ROOT = REPO_ROOT / "tmp" / "p1-tests"
CONFIG_PATH = REPO_ROOT / "config" / "transflow.example.toml"
FONT_MANIFEST_PATH = REPO_ROOT / "resources" / "manifests" / "font_manifest.json"


class DisposableHealthHandler(BaseHTTPRequestHandler):
    """为 P1 readiness 提供只存在于测试期间的真实 HTTP 响应。"""

    def do_GET(self) -> None:
        """返回固定的无秘密健康 JSON。"""

        payload = b'{"status":"live"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, message_format: str, *args: object) -> None:
        """把临时服务访问写入标准日志，避免默认写 stderr。"""

        logging.getLogger("transflow.p1.http-stub").info(message_format, *args)


@pytest.fixture(scope="session", autouse=True)
def prepare_p1_test_root() -> Iterator[Path]:
    """创建仓库 tmp 下的 P1 专用真实文件目录，并只清理本测试产物。"""

    resolved = P1_TEST_ROOT.resolve()
    if resolved.parent != (REPO_ROOT / "tmp").resolve():
        raise RuntimeError("P1 测试目录越出仓库 tmp")
    if resolved.exists():
        shutil.rmtree(resolved)
    resolved.mkdir(parents=True)
    yield resolved


@contextmanager
def disposable_http_stub() -> Iterator[str]:
    """启动绑定随机本机端口的真实临时 HTTP 服务并在退出时关闭。"""

    server = ThreadingHTTPServer(("127.0.0.1", 0), DisposableHealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}/health/live"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _unused_local_url() -> str:
    """取得当前未监听端口，供不可达 readiness 测试立即失败。"""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        _, port = probe.getsockname()
    return f"http://127.0.0.1:{port}/health/live"


def _font_path() -> Path:
    """从受控 manifest 返回当前唯一字体资产路径。"""

    manifest = json.loads(FONT_MANIFEST_PATH.read_text(encoding="utf-8"))
    return REPO_ROOT / str(manifest["assets"][0]["path"])


def test_p1_1_t01_development_and_target_environment_inventories_are_comparable() -> None:
    """P1.1-T01：开发与目标角色产生字段完整、可比较的真实环境清单。"""

    development = collect_environment_snapshot("development")
    target = collect_environment_snapshot("target")
    required = {
        "role",
        "host_identity",
        "os_family",
        "os_version",
        "architecture",
        "python_implementation",
        "python_version",
        "python_executable_sha256",
        "cpu",
        "logical_processors",
        "memory_bytes",
        "filesystem",
        "filesystem_device",
        "service_manager",
    }
    assert set(development) == required
    assert set(target) == required
    assert all(value is not None and value != "" for value in development.values())
    assert {key: value for key, value in development.items() if key != "role"} == {
        key: value for key, value in target.items() if key != "role"
    }


def test_p1_1_t02_frozen_pymupdf_opens_and_saves_minimal_pdf() -> None:
    """P1.1-T02：冻结解释器和 PyMuPDF 真实创建、保存并重开最小 PDF。"""

    result = create_and_reopen_minimal_pdf(P1_TEST_ROOT / "minimal-pdf")
    assert result["page_count"] == 1
    assert result["pymupdf_version"] == "1.28.0"
    assert len(result["sha256"]) == 64


def test_p1_1_t03_font_manifest_matches_real_files_and_licenses() -> None:
    """P1.1-T03：字体实际路径、许可、版本、SHA 和必需字形全部一致。"""

    findings = validate_font_manifest(FONT_MANIFEST_PATH)
    assert findings
    assert all(finding.passed for finding in findings), findings


@pytest.mark.integration
def test_p1_2_t01_two_empty_venv_installs_have_identical_packages() -> None:
    """P1.2-T01：两个真实空 venv 按唯一锁安装后的包清单差异为零。"""

    evidence = verify_two_clean_installs()
    assert evidence["package_diff_count"] == 0
    assert evidence["smoke_success_count"] == 2


def test_p1_2_t02_dependency_consistency_has_zero_errors() -> None:
    """P1.2-T02：在当前独立 venv 执行 pip check，冲突和缺失为零。"""

    completed = subprocess.run(
        [sys.executable, "-m", "pip", "check"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert completed.stdout.strip() == "No broken requirements found."


def test_p1_2_t03_forbidden_production_dependency_is_blocked() -> None:
    """P1.2-T03：向输入依赖集合注入 LiteLLM 时，真实 Gate 算法明确阻断。"""

    injected = ["httpx==0.28.1", "litellm==1.0.0"]
    assert find_forbidden_dependencies(injected) == ["litellm"]


def test_p1_2_t04_isolated_import_has_no_cross_repository_visibility() -> None:
    """P1.2-T04：清除 PYTHONPATH 后仅安装包可导入，跨仓库源码不可见。"""

    code = (
        "import importlib.util,transflow;"
        "assert transflow.__file__;"
        "assert importlib.util.find_spec('MerqFin') is None;"
        "assert importlib.util.find_spec('spikes') is None;"
        "assert importlib.util.find_spec('backend') is None;"
        "print('TRANSFLOW_IMPORT_OK;CROSS_REPOSITORY_IMPORTS=0')"
    )
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    completed = subprocess.run(
        [sys.executable, "-I", "-c", code],
        cwd=REPO_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert completed.stdout.strip() == "TRANSFLOW_IMPORT_OK;CROSS_REPOSITORY_IMPORTS=0"


def test_p1_3_t01_live_and_ready_succeed_with_reachable_real_stub() -> None:
    """P1.3-T01：完整配置与真实临时 HTTP 服务使 live/ready 同时成功。"""

    config = load_runtime_config(CONFIG_PATH)
    with disposable_http_stub() as url:
        working_config = replace(
            config,
            workspace=P1_TEST_ROOT / "health-normal",
            ai_capability_url=url,
        )
        app = create_health_app(working_config)
        service = HealthService(working_config)
        live = service.liveness().as_dict()
        ready = service.readiness().as_dict()
    assert {route.path for route in app.routes} >= {"/health/live", "/health/ready"}
    assert live["ok"] is True and live["status"] == "live"
    assert ready["ok"] is True and ready["status"] == "ready"


def test_p1_3_t02_font_missing_and_workspace_unwritable_are_not_ready() -> None:
    """P1.3-T02：缺字体与不可写工作路径都保持 live，但分别明确 not_ready。"""

    config = load_runtime_config(CONFIG_PATH)
    blocked_workspace = P1_TEST_ROOT / "workspace-is-file"
    blocked_workspace.write_text("not a directory", encoding="utf-8")
    with disposable_http_stub() as url:
        missing_font_service = HealthService(
            replace(
                config,
                workspace=P1_TEST_ROOT / "health-missing-font",
                font_manifest=P1_TEST_ROOT / "missing-font-manifest.json",
                ai_capability_url=url,
            )
        )
        blocked_workspace_service = HealthService(
            replace(config, workspace=blocked_workspace, ai_capability_url=url)
        )
        missing_font_ready = missing_font_service.readiness()
        blocked_workspace_ready = blocked_workspace_service.readiness()
    assert missing_font_service.liveness().ok is True
    assert blocked_workspace_service.liveness().ok is True
    assert missing_font_ready.ok is False
    assert blocked_workspace_ready.ok is False
    assert "FONT_DEPENDENCY_FAILED" in {
        finding.code for finding in missing_font_ready.findings
    }
    assert "WORKSPACE_NOT_WRITABLE" in {
        finding.code for finding in blocked_workspace_ready.findings
    }


def test_p1_3_t03_unreachable_stub_is_not_ready_without_secret_leak(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """P1.3-T03：外部服务不可达时准确失败，日志/结果不泄漏 URL 用户信息。"""

    config = load_runtime_config(CONFIG_PATH)
    endpoint = _unused_local_url()
    credential_marker = "-".join(("not", "for", "logs"))
    endpoint_with_userinfo = endpoint.replace(
        "http://", f"http://probe:{credential_marker}@", 1
    )
    service = HealthService(
        replace(
            config,
            workspace=P1_TEST_ROOT / "health-unreachable",
            ai_capability_url=endpoint_with_userinfo,
        ),
        timeout_seconds=0.5,
    )
    with caplog.at_level(logging.INFO):
        report = service.readiness()
    serialized = json.dumps(report.as_dict(), ensure_ascii=False) + caplog.text
    assert report.ok is False and report.status == "not_ready"
    assert "AI_CAPABILITY_UNREACHABLE" in {finding.code for finding in report.findings}
    assert credential_marker not in serialized
    assert not any(name.startswith("transflow.fake_ai") for name in sys.modules)


def test_p1_3_t04_committed_config_and_health_logs_have_zero_plaintext_secrets(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """P1.3-T04：扫描真实模板与健康日志，秘密关键字明文命中为零。"""

    config_text = CONFIG_PATH.read_text(encoding="utf-8")
    service = HealthService(
        replace(
            load_runtime_config(CONFIG_PATH),
            workspace=P1_TEST_ROOT / "health-secret-scan",
            ai_capability_url=_unused_local_url(),
        ),
        timeout_seconds=0.5,
    )
    with caplog.at_level(logging.INFO):
        service.readiness()
    assert find_plaintext_secrets(config_text) == []
    assert find_plaintext_secrets(caplog.text) == []


def test_p1_4_t01_atomic_partial_fsync_rename_and_reread_hash_match() -> None:
    """P1.4-T01：真实 partial 写入、fsync、rename、重读后哈希完全一致。"""

    payload = b"transflow-p1-atomic-publish"
    result = atomic_publish_bytes(P1_TEST_ROOT / "atomic", "artifact.bin", payload)
    assert result["passed"] is True
    assert result["partial_exists"] is False
    assert result["expected_sha256"] == result["actual_sha256"]


def test_p1_4_t02_different_filesystem_facts_reject_atomic_claim() -> None:
    """P1.4-T02：输入不同设备 ID 时，探针拒绝原子发布声明并给出错误码。"""

    finding = assess_atomic_devices(partial_device=100, final_device=200)
    assert finding.passed is False
    assert finding.code == "ATOMIC_FILESYSTEM_MISMATCH"


def test_p1_4_t03_process_pool_opens_path_and_rejects_document() -> None:
    """P1.4-T03：子进程按路径独立开 PDF，打开的 Document 在提交前被拒绝。"""

    workspace = P1_TEST_ROOT / "process-pool"
    minimal = create_and_reopen_minimal_pdf(workspace)
    pdf_path = workspace / str(minimal["path"])
    result = open_pdf_in_process_pool(pdf_path, 0)
    with pymupdf.open(pdf_path) as document:
        rejection = reject_open_document_payload(document)
    assert result["page_count"] == 1 and result["page_number"] == 0
    assert result["pid"] != os.getpid()
    assert rejection.passed is True
    assert rejection.code == "PDF_PROCESS_PAYLOAD_REJECTED"


def test_p1_4_t04_registered_font_renders_cjk_latin_and_reports_missing_glyph() -> None:
    """P1.4-T04：登记字体真实渲染 CJK/Latin/PNG，缺字形成明确 Finding。"""

    result = render_registered_font(P1_TEST_ROOT / "font-render", _font_path())
    assert result["passed"] is True
    assert all(index > 0 for index in result["glyph_indexes"].values())
    assert result["missing_finding"]["code"] == "FONT_GLYPH_MISSING"
    assert result["missing_finding"]["passed"] is True
    assert result["png_width"] > 0 and result["png_height"] > 0
