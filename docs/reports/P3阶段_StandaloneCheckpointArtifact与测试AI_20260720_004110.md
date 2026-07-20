# P3 阶段执行报告：Standalone、Checkpoint、Artifact 与测试 AI

- 所属阶段：P3
- 执行完成时间：2026-07-20 00:41:10 +08:00
- 正式 Gate：G3
- Gate 结论：PASS
- 最终机器证据：`docs/reports/gates/G3_evidence.json`
- 首次失败证据：`docs/reports/gates/G3_attempt1_FAIL_evidence.json`

## 1. 范围与实现结论

本阶段只实现 P3.1～P3.5，没有进入 P4 的 PDF 枚举、页面流水线或文档最终化。交付内容包括：

- 仅接受一份完整 PDF 的 `StandaloneRunAdapter`，在创建 Run 前完成允许根、真实路径、文件类型、PDF 可读性、加密状态、页数与哈希验证。
- Run 私有的 `FilesystemCheckpointAdapter`，支持版本单调、幂等提交、冲突拒绝、Artifact 引用校验和崩溃恢复。
- 内容寻址且不可变的 `SharedFilesystemArtifactAdapter`，支持原子写入、最终发布指针、孤儿识别和跨 Run 隔离。
- 带固定字段、脱敏和截断的 JSONL 结构化审计日志。
- `FixedTranslationAdapter`、`DeterministicTranslationAdapter`、真实 loopback HTTP fake AI 服务及共用两个 AI Port 的 `HttpAiCapabilityAdapter`。
- 五类真实文件崩溃窗口测试和 production wheel 边界审计。

P3 不需要真实模型能力，因此没有使用、写入或回显任何模型 API Key；生产 wheel 也不包含 fake 服务、真实 Provider SDK 或模型密钥配置。

## 2. P3.1 Standalone 输入边界验收

结论：通过。三页/一页完整 PDF 和重复提交正向合同通过；PDF 列表、目录、相对路径、允许根逃逸、重解析点逃逸、空/损坏/加密文件及错误字段均在创建 workspace 前被拒绝。

实际执行命令：

```powershell
.\.venv\Scripts\python.exe -m pytest -vv tests/test_p3.py -k "p3_1_t01 or p3_1_t02 or p3_1_t09"
.\.venv\Scripts\python.exe -m pytest -vv tests/test_p3.py -k "p3_1_t03 or p3_1_t04 or p3_1_t05 or p3_1_t06 or p3_1_t07 or p3_1_t08"
```

实际输出：

```text
tests/test_p3.py::test_p3_1_t01_accepts_three_page_complete_pdf_with_correct_hash PASSED [ 33%]
tests/test_p3.py::test_p3_1_t02_accepts_one_page_complete_document PASSED [ 66%]
tests/test_p3.py::test_p3_1_t09_duplicate_submission_creates_independent_run_identity PASSED [100%]
====================== 3 passed, 31 deselected in 0.65s =======================

tests/test_p3.py::test_p3_1_t03_rejects_every_pdf_list_without_creating_run PASSED [ 16%]
tests/test_p3.py::test_p3_1_t04_rejects_directory_without_recursive_search PASSED [ 33%]
tests/test_p3.py::test_p3_1_t05_rejects_relative_and_normalized_escape_paths PASSED [ 50%]
tests/test_p3.py::test_p3_1_t06_rejects_reparse_target_outside_allowed_root PASSED [ 66%]
tests/test_p3.py::test_p3_1_t07_rejects_unsupported_empty_damaged_and_encrypted_files PASSED [ 83%]
tests/test_p3.py::test_p3_1_t08_rejects_missing_or_wrong_typed_fields_without_workspace PASSED [100%]
====================== 6 passed, 28 deselected in 0.56s =======================
```

## 3. P3.2 Checkpoint 与 workspace 验收

结论：通过。首个页快照、升版、同版本幂等、分叉冲突、rename 前后崩溃、manifest 后重启、路径逃逸和跨 Run 保留全部通过；没有低版本覆盖或跨 Run 删除。

实际执行命令：

```powershell
.\.venv\Scripts\python.exe -m pytest -vv tests/test_p3.py -k p3_2
```

实际输出：

```text
tests/test_p3.py::test_p3_2_t01_first_page_checkpoint_and_manifest_are_consistent PASSED [ 12%]
tests/test_p3.py::test_p3_2_t02_higher_version_replaces_authority_and_lower_is_rejected PASSED [ 25%]
tests/test_p3.py::test_p3_2_t03_same_version_is_idempotent_but_fork_conflicts PASSED [ 37%]
tests/test_p3.py::test_p3_2_t04_crash_before_checkpoint_rename_cleans_registered_partial PASSED [ 50%]
tests/test_p3.py::test_p3_2_t05_crash_after_rename_reports_unreferenced_orphan PASSED [ 62%]
tests/test_p3.py::test_p3_2_t06_restart_after_manifest_loads_v2_without_replay PASSED [ 75%]
tests/test_p3.py::test_p3_2_t07_page_path_escape_and_external_reparse_are_rejected PASSED [ 87%]
tests/test_p3.py::test_p3_2_t08_recovery_preserves_unknown_and_other_run_files PASSED [100%]
====================== 8 passed, 26 deselected in 0.75s =======================
```

## 4. P3.3 Artifact 与日志验收

结论：通过。Artifact 的真实内容、哈希、路径、标签和引用一致；重复写幂等、不同内容覆盖被拒绝；Checkpoint 不接受缺失、损坏或哈希漂移 Artifact；日志必填字段、秘密脱敏和无界字段截断全部通过。

实际执行命令：

```powershell
.\.venv\Scripts\python.exe -m pytest -vv tests/test_p3.py -k p3_3
```

实际输出：

```text
tests/test_p3.py::test_p3_3_t01_artifact_put_and_verify_match_content_hash_path_and_label PASSED [ 20%]
tests/test_p3.py::test_p3_3_t02_artifact_replay_is_idempotent_and_overwrite_is_rejected PASSED [ 40%]
tests/test_p3.py::test_p3_3_t03_checkpoint_rejects_missing_corrupt_and_hash_mismatch_artifacts PASSED [ 60%]
tests/test_p3.py::test_p3_3_t04_success_degradation_and_error_logs_have_all_fields PASSED [ 80%]
tests/test_p3.py::test_p3_3_t05_logs_redact_secrets_and_truncate_unbounded_payloads PASSED [100%]
====================== 5 passed, 29 deselected in 0.58s =======================
```

## 5. P3.4 固定翻译、fake AI 与生产边界验收

结论：通过。固定/确定性翻译可复现；fake 服务通过真实 HTTP 覆盖两个合同、鉴权、请求大小、超时、429、5xx、非法 JSON、Schema/身份错误以及独立 readiness；production wheel 中测试服务和真实 Provider 依赖命中数为 0。

实际执行命令：

```powershell
.\.venv\Scripts\python.exe -m pytest -vv tests/test_p3.py -k "p3_4_t01 or p3_4_t06"
.\.venv\Scripts\python.exe -m pytest -vv tests/test_p3.py -k "p3_4_t02 or p3_4_t03 or p3_4_t04 or p3_4_t05 or p3_4_t07"
```

实际输出：

```text
tests/test_p3.py::test_p3_4_t01_fixed_and_deterministic_translation_are_reproducible PASSED [ 50%]
tests/test_p3.py::test_p3_4_t06_production_wheel_excludes_real_provider_and_fake_service PASSED [100%]
====================== 2 passed, 32 deselected in 11.75s ======================

tests/test_p3.py::test_p3_4_t02_real_fake_service_returns_valid_decision_and_translation PASSED [ 20%]
tests/test_p3.py::test_p3_4_t03_timeout_5xx_and_429_map_to_retryable_errors PASSED [ 40%]
tests/test_p3.py::test_p3_4_t04_invalid_json_schema_and_unit_identity_are_rejected PASSED [ 60%]
tests/test_p3.py::test_p3_4_t05_bad_token_and_oversized_request_are_rejected_without_leak PASSED [ 80%]
tests/test_p3.py::test_p3_4_t07_real_fake_liveness_readiness_and_shutdown_are_truthful PASSED [100%]
====================== 5 passed, 29 deselected in 9.15s =======================
```

## 6. P3.5 崩溃窗口验收

结论：通过。Artifact rename 前、rename 后、manifest 后、最终文件 rename 后发布前以及恢复清理五类窗口全部收敛；权威状态无歧义，跨 Run/未知文件/重解析目标删除数为 0。

实际执行命令：

```powershell
.\.venv\Scripts\python.exe -m pytest -vv tests/test_p3.py -k p3_5
```

实际输出：

```text
tests/test_p3.py::test_p3_5_t01_artifact_crash_before_rename_has_no_authority_and_replays PASSED [ 20%]
tests/test_p3.py::test_p3_5_t02_artifact_after_rename_is_orphan_not_committed_state PASSED [ 40%]
tests/test_p3.py::test_p3_5_t03_crash_after_manifest_reads_by_hash_and_skips_replay PASSED [ 60%]
tests/test_p3.py::test_p3_5_t04_final_rename_before_publish_preserves_old_authority PASSED [ 80%]
tests/test_p3.py::test_p3_5_t05_recovery_never_deletes_unknown_cross_run_or_reparse_targets PASSED [100%]
====================== 5 passed, 29 deselected in 0.68s =======================
```

## 7. Gate G3 逐项结论

| Gate 项 | 结论 | 可追溯证据 |
|---|---|---|
| G3-1 合法完整 PDF | 通过 | P3.1-T01/T02/T09：3 passed |
| G3-2 非法输入无副作用 | 通过 | P3.1-T03～T08：6 passed |
| G3-3 Checkpoint | 通过 | P3.2-T01～T08：8 passed |
| G3-4 Artifact/日志 | 通过 | P3.3-T01～T05：5 passed |
| G3-5 确定性测试翻译/生产依赖 | 通过 | P3.4-T01/T06：2 passed；真实 Provider production 命中 0 |
| G3-6 fake AI | 通过 | P3.4-T02～T05/T07：5 passed；两个 HTTP 合同均真实执行 |
| G3-7 崩溃窗口 | 通过 | P3.5-T01～T05：5 passed；权威歧义和跨 Run 删除 0 |

P3 全量测试实际输出：

```text
collected 34 items
tests/test_p3.py::test_p3_1_t01_accepts_three_page_complete_pdf_with_correct_hash PASSED [  2%]
tests/test_p3.py::test_p3_2_t08_recovery_preserves_unknown_and_other_run_files PASSED [ 50%]
tests/test_p3.py::test_p3_4_t07_real_fake_liveness_readiness_and_shutdown_are_truthful PASSED [ 85%]
tests/test_p3.py::test_p3_5_t05_recovery_never_deletes_unknown_cross_run_or_reparse_targets PASSED [100%]
============================= 34 passed in 22.07s =============================
```

P3 审计实际输出：

```text
P3_VERIFY check=checkpoints status=PASS violations=0
P3_VERIFY check=artifacts status=PASS violations=0
P3_VERIFY check=safety status=PASS violations=0
P3_VERIFY check=production status=PASS violations=0
P3_VERIFY check=review status=PASS violations=0
P3_VERIFY_SUMMARY status=PASS checks=5
```

## 8. 既有阶段回归与静态质量

结论：通过。P0/P1/P2 共 50 项回归通过；ruff 与 mypy 均无问题。

```text
All checks passed!
Success: no issues found in 44 source files
..................................................                       [100%]
50 passed in 165.00s (0:02:45)
```

正式 Gate 元数据：

```text
gate        : G3
started_at  : 2026-07-20T00:37:32+08:00
finished_at : 2026-07-20T00:41:10+08:00
duration_ms : 218210
conclusion  : PASS
```

## 9. 首次 Gate 失败、定位与闭环

首次正式 G3 没有通过，失败证据被原样保留。原因是 P0 的源码扫描把 `http://` 中的 `p:/` 误判为 Windows 盘符路径，并把只承载异常类型身份的 `InjectedCrash(RuntimeError)` 误判为空业务类。修复只收紧这两个扫描判定，没有放宽禁止绝对路径或业务空壳类的边界。

首次实际输出：

```text
FAILED tests/test_p0.py::test_p0_2_t02_production_import_graph_is_clean
FAILED tests/test_p0.py::test_p0_3_t01_smoke_coverage_and_architecture_checks_execute
2 failed, 48 passed in 158.79s (0:02:38)
```

修复后定向复测实际输出：

```text
tests/test_p0.py::test_p0_2_t02_production_import_graph_is_clean PASSED  [ 50%]
tests/test_p0.py::test_p0_3_t01_smoke_coverage_and_architecture_checks_execute PASSED [100%]
====================== 2 passed, 13 deselected in 9.12s =======================
All checks passed!
Success: no issues found in 1 source file
```

## 10. 设计问题与处理

- Windows 普通用户默认可能没有创建符号链接的权限。验收仍未跳过：测试在权限不足时创建真实目录 Junction，继续验证解析后的重解析点逃逸。
- P3 fake AI 的目标是合同和错误映射，不是模型质量。服务只在测试进程中运行，真实 HTTP 返回经过领域合同校验，production wheel 明确排除服务脚本。
- 文件系统与进程状态不宣称分布式原子性；权威点固定为 manifest。rename 后未提交文件按孤儿处理，manifest 后则按内容哈希复用。

最终结论：P3 全部二级子计划和 Gate G3 技术验收通过，可以进入 P4。
