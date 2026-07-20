# Transflow P1 环境与服务基线

## 1. 冻结范围

- 解释器：CPython `3.14.3`，由 `.python-version` 与 `runtime_baseline.json` 双重登记。
- PDF 引擎：PyMuPDF `1.28.0`，Windows amd64 wheel 与模块 SHA-256 已登记。
- 依赖：`requirements.lock` 是 production/test 的唯一精确锁；分组入口在 `pyproject.toml`。
- 字体：Noto Sans CJK SC Regular `2.004` 同时承担 CJK、Latin 与 fallback 角色。
- 当前验证拓扑：开发角色与目标角色同处本机 Windows/NTFS；这不是第二台独立服务器证据。

若目标服务器、Python patch、OS、架构或文件系统变化，必须重新执行 G1，不沿用本报告结论。

## 2. 干净环境安装

以下命令必须从 Transflow 仓库根执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip==26.1.2
.\.venv\Scripts\python.exe -m pip install --no-deps -r requirements.lock
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
.\.venv\Scripts\python.exe -m scripts.bootstrap_p1
.\.venv\Scripts\python.exe -m pip check
```

字体二进制位于忽略目录 `resources/fonts/assets/`，不提交 Git。`bootstrap_p1` 仅从 manifest
中的固定标签 URL 下载，写入 partial 后校验 SHA-256，再原子替换正式文件。许可证文本和 manifest
进入仓库。

## 3. 配置

`config/transflow.example.toml` 是唯一提交模板，字段只包含 workspace、字体 manifest、并发占位、
内部 HTTP 地址和日志级别，不允许 token、API key、密码或 URL 用户信息。

源码检出场景直接使用模板。wheel 部署时，把配置保存在 `<runtime-root>/config/`，并让
`TRANSFLOW_CONFIG` 指向该文件；配置内路径统一相对 `<runtime-root>` 解析。VLM、Provider 或模型
秘密只属于后续 AI Capability Service 环境，不进入 Transflow 配置。

## 4. 健康探针

应用工厂为 `transflow.runtime.create_health_app`，提供：

- `/health/live`：仅判断进程可响应，外部依赖失败时仍为 live。
- `/health/ready`：真实检查 workspace 写入/原子替换、受控字体和 AI capability HTTP 可达性。

本阶段仅用测试内 disposable HTTP stub 验证 readiness，不 import 或启动 P3 fake AI Service。

本地示例：

```powershell
.\.venv\Scripts\python.exe -m uvicorn transflow.runtime.health:create_health_app --factory --host 127.0.0.1 --port 8080
```

Docker 不是启动前置条件。若以后采用容器，配置、共享 workspace 和字体资产必须在各进程侧呈现
相同规范路径；P1 不实现路径前缀换算层。

## 5. 验收命令

```powershell
.\.venv\Scripts\python.exe -m scripts.bootstrap_p1 --check
.\.venv\Scripts\python.exe -m scripts.verify_clean_install
.\.venv\Scripts\python.exe -m pytest -vv tests/test_p1.py
.\.venv\Scripts\python.exe -m scripts.run_gate G1 --report docs/reports/gates/G1_evidence.json
```

`scripts.verify_clean_install` 只会删除并重建仓库 `tmp/p1-clean-installs` 专用目录；不会清理其他
workspace、样本或用户文件。

## 6. 当前许可停点

PyMuPDF 为 `AGPL-3.0-only OR LicenseRef-Artifex-Commercial` 双许可，当前项目元数据仍声明
“Proprietary until project license is frozen”。在负责人选择“项目按 AGPLv3 发布”或确认“已获得
Artifex 商业许可”前，决策 `D-P1-001` 保持 OPEN，G1 必须为 `BLOCKED_BY_DECISION`。
