# P6 阶段执行报告：SharedPdfKernel 与 PDF Preservation

- 所属阶段：P6
- 报告生成时间：2026-07-20 12:32:22 +08:00
- 正式 Gate 执行时间：2026-07-20 12:31:12 +08:00 ～ 2026-07-20 12:32:15 +08:00
- 工作目录：`transflow`
- 计划范围：P6.1～P6.5、Gate G6
- 总结论：通过
- 权威原始证据：`docs/reports/gates/G6_evidence.json`

## 1. 范围与边界

本阶段严格在 P4 已验证的 `src/transflow/pdf_kernel` 原位补全机械能力，没有引入第二套
Patch/渲染语义，也没有实现 P7 Toolbox 生产合同。主要交付如下：

1. 补全 geometry、text span、image、drawing、table、locked object 等只读直接事实，提供
   跨进程规范序列化与 job/run/page 私有工作区。
2. 补全受控字体字形探测、Patch manifest、候选/final 共用解释器、区域外像素比较。
3. 增加结构化 `KernelFinding`、集中机械硬约束和轮次/操作数/时间三重受限 Repair。
4. 增加版本化 Preservation 支持矩阵、fixture catalog、签名/加密/结构树/未知关键特征
   预检、文档级特征快照、最终 diff 和完整源 PDF 应急透传。
5. 增加统一 Kernel/font/facts/preservation 指纹和多进程故障重建测试。
6. 导出项目内可查看结果到 `output/pdf/p6`，该 PDF 用于展示 Preservation，不代表 P7
   之后的 Toolbox 排版能力。

## 2. 正式 Gate 总证据

执行命令：

```text
python -m scripts.run_gate G6 --report docs/reports/gates/G6_evidence.json
```

实际汇总输出：

```text
GATE_EVIDENCE gate=G6 conclusion=PASS steps=12 failed_steps=0 started_at=2026-07-20T12:31:12+08:00 finished_at=2026-07-20T12:32:15+08:00
```

静态、类型和 P6 审计实际输出：

```text
All checks passed!
Success: no issues found in 75 source files
P6_VERIFY check=migration status=PASS violations=0
P6_VERIFY check=support_matrix status=PASS violations=0
P6_VERIFY check=forbidden_api status=PASS violations=0
P6_VERIFY check=semantic_boundary status=PASS violations=0
P6_VERIFY check=determinism status=PASS violations=0
P6_VERIFY check=test_inventory status=PASS violations=0
P6_VERIFY_SUMMARY status=PASS checks=6
```

## 3. Gate G6 逐项验收

### G6-1 Facts/工作区：通过

对照标准：来源/字段覆盖 100%；对象身份稳定；源修改、串扰和非序列化对象进入合同为 0。

实际命令输出：

```text
tests/test_p6.py::test_p6_1_t01_spike_and_production_facts_match_with_declared_differences PASSED [ 16%]
tests/test_p6.py::test_p6_1_t02_facts_ids_and_serialization_are_stable_across_processes PASSED [ 33%]
tests/test_p6.py::test_p6_1_t03_workspace_isolates_runs_pages_logs_and_temp PASSED [ 50%]
tests/test_p6.py::test_p6_1_t04_facts_extraction_never_modifies_source PASSED [ 66%]
tests/test_p6.py::test_p6_1_t05_rotation_crop_image_drawing_table_and_locked_facts_exist PASSED [ 83%]
tests/test_p6.py::test_p6_1_t06_open_document_and_page_are_rejected_at_serialization_boundary PASSED [100%]

====================== 6 passed, 24 deselected in 1.09s =======================
```

结论：迁移来源哈希与六项批准差异已登记；同页跨进程序列化一致；源哈希与 mtime 不变；
组合 F3 Facts 和禁止进程内对象进入 DTO 均通过。

### G6-2 字体/Patch/渲染：通过

对照标准：candidate/final 语义差异、禁止 API、manifest 外字体和越权接受均为 0；缺字安全
拒绝率 100%。

实际命令输出：

```text
tests/test_p6.py::test_p6_2_t01_candidate_and_final_share_patch_manifest_semantics PASSED [ 16%]
tests/test_p6.py::test_p6_2_t02_manifest_font_covers_cjk_latin_numeric_and_financial_symbols PASSED [ 33%]
tests/test_p6.py::test_p6_2_t03_missing_font_hash_file_and_glyph_fail_without_system_fallback PASSED [ 50%]
tests/test_p6.py::test_p6_2_t04_protected_owner_and_bounds_reject_before_source_commit PASSED [ 66%]
tests/test_p6.py::test_p6_2_t05_forbidden_renderers_and_system_fonts_are_absent PASSED [ 83%]
tests/test_p6.py::test_p6_2_t06_repeated_candidate_render_has_identical_manifest_and_pixels PASSED [100%]

====================== 6 passed, 24 deselected in 1.77s =======================
```

结论：候选与最终回放的 interpreter、owner、operation、manifest 和写入结果一致；受控字体覆盖
中英、数字和金融符号；缺登记、错哈希、缺文件、缺字形均没有系统字体回退；禁止 API 变异
会被审计器命中。

### G6-3 硬约束/Repair：通过

对照标准：已定义机械硬约束检出率 100%；越权、绕过重验、无界 Repair 和页面语义分支为 0。

实际命令输出：

```text
tests/test_p6.py::test_p6_3_t01_hard_constraints_emit_structured_findings PASSED [ 16%]
tests/test_p6.py::test_p6_3_t02_multiple_findings_are_retained_and_stably_sorted PASSED [ 33%]
tests/test_p6.py::test_p6_3_t03_wrong_source_page_owner_and_protected_target_never_write PASSED [ 50%]
tests/test_p6.py::test_p6_3_t04_bounded_repair_rechecks_full_constraints_before_accepting PASSED [ 66%]
tests/test_p6.py::test_p6_3_t05_irreparable_and_looping_repair_stop_within_budget PASSED [ 83%]
tests/test_p6.py::test_p6_3_t06_kernel_constraints_have_no_classification_or_toolbox_semantics PASSED [100%]

====================== 6 passed, 24 deselected in 8.94s =======================
```

结论：越界、溢出、写入重叠、残留源文、锁定对象变化、非法字体和损坏候选均由真实文件检查
产生阻断 Finding；Repair 使用真实 ConstraintChecker 重验，并在无改善或预算耗尽时停止。

### G6-4 Preservation：通过

对照标准：F3 决策和强约束验证准确率 100%；签名修改版、静默特征丢失、无 fixture 扩大承诺
和误发布不安全 target 为 0。

实际命令输出：

```text
tests/test_p6.py::test_p6_4_t01_f3_features_match_support_matrix_after_byte_copy PASSED [ 16%]
tests/test_p6.py::test_p6_4_t02_signature_preflight_forces_whole_source_passthrough PASSED [ 33%]
tests/test_p6.py::test_p6_4_t03_encrypted_readable_unreadable_and_unknown_critical_are_explicit PASSED [ 50%]
tests/test_p6.py::test_p6_4_t04_bookmark_link_order_rotation_and_crop_mutations_are_detected PASSED [ 66%]
tests/test_p6.py::test_p6_4_t05_validation_failure_falls_back_and_unpublishable_source_fails PASSED [ 83%]
tests/test_p6.py::test_p6_4_t06_support_matrix_never_promises_unverified_features PASSED [100%]

====================== 6 passed, 24 deselected in 0.65s =======================
```

结论：metadata、bookmark、page label、link、annotation、form、attachment 均参与最终 hash/count
验证；签名字段、加密、结构树和未知关键 Catalog 特征不会进入修改路径；可认证加密 PDF 按完整
源字节发布，不可认证源进入 `PROCESS_FAILED`；没有已登记检测器、验证器和 fixture 的新增承诺
会被合同拒绝。

### G6-5 并发/跨环境：通过

对照标准：多 run/page/同名 source 串扰为 0；worker 故障可收敛；批准环境角色的结构指标满足
冻结零差异容差。

实际命令输出：

```text
tests/test_p6.py::test_p6_5_t01_multi_process_same_name_runs_have_no_facts_crosstalk PASSED [ 16%]
tests/test_p6.py::test_p6_5_t02_approved_roles_share_frozen_font_and_kernel_fingerprint PASSED [ 33%]
tests/test_p6.py::test_p6_5_t03_rebuilt_worker_replays_same_facts_without_affecting_other_run PASSED [ 50%]
tests/test_p6.py::test_p6_5_t04_g4_static_and_real_fixture_checks_remain_green PASSED [ 66%]
tests/test_p6.py::test_p6_5_t05_g5_anonymous_baseline_identity_and_routes_have_no_drift PASSED [ 83%]
tests/test_p6.py::test_p6_5_t06_kernel_fingerprint_mismatch_rejects_old_checkpoint PASSED [100%]

====================== 6 passed, 24 deselected in 10.35s ======================
```

结论：测试真实终止一个 ProcessPool worker，再建立新 worker 重放同一页；并行的另一个 run 未受
污染。P1 当前批准的 `development` 和 `target` 是同一 Windows 主机上的两个角色，不是两种 OS；
本报告只声明这两个已批准角色达到冻结零差异，不扩大成未经 P1 批准的跨 OS 能力。

### G6-6 上游回归/指纹：通过

对照标准：G4/G5 page identity、facts 和 Route 无解释退化为 0；Kernel fingerprint 漂移识别率
100%。

G4 真实完整年报与故障 E2E 实际输出：

```text
tests/test_p4.py::test_p4_5_t01_complete_real_annual_report_produces_single_pdf PASSED [ 20%]
tests/test_p4.py::test_p4_5_t02_restart_skips_all_committed_pages PASSED [ 40%]
tests/test_p4.py::test_p4_5_t03_last_page_translation_failure_passthroughs_and_completes PASSED [ 60%]
tests/test_p4.py::test_p4_5_t04_all_passthrough_preserves_structure_and_marks_degradation PASSED [ 80%]
tests/test_p4.py::test_p4_5_t05_final_write_failure_retries_without_partial_authority PASSED [100%]

====================== 5 passed, 20 deselected in 24.40s ======================
```

G5 匿名基线实际输出：

```text
P5_ANONYMOUS_BASELINE PASS anonymous_case_count=22 baseline_content_sha256=bb689f23d3aef97c6fe3ca65581d992e988855c81400cc283c56dab8e2cf0139 identity_leak_count=0 located_real_pdf_count=22 post_freeze_change_count=0 stratum_count=11
```

G5 关键 Route/identity 回归实际输出：

```text
tests/test_p5.py::test_p5_2_t01_rule_and_evidence_match_legacy_except_declared_identity_fields PASSED [ 25%]
tests/test_p5.py::test_p5_4_t01_mixed_pdf_has_one_route_per_page_and_stable_identity PASSED [ 50%]
tests/test_p5.py::test_p5_4_t01_run_classified_finalizes_one_complete_pdf PASSED [ 75%]
tests/test_p5.py::test_p5_5_t01_filename_and_body_page_order_do_not_change_route PASSED [100%]

====================== 4 passed, 19 deselected in 1.38s =======================
```

G5 冻结质量和生产边界审计实际输出：

```text
P5_VERIFY check=baseline status=PASS violations=0
P5_VERIFY check=prompts status=PASS violations=0
P5_VERIFY check=production status=PASS violations=0
P5_VERIFY check=architecture status=PASS violations=0
P5_VERIFY check=migration_adapter status=PASS violations=0
P5_VERIFY check=quality status=PASS violations=0
P5_VERIFY_SUMMARY status=PASS checks=6
```

结论：G4 完整年报与四类故障恢复没有退化，G5 二十二个真实匿名输入及关键 Route 集合没有
身份或路由漂移；Kernel/facts/font/support matrix 指纹变化会触发旧 Checkpoint 不兼容拒绝。

## 4. 路径、秘密和项目内结果验收

实际审计输出：

```text
SECRET_SCAN PASS hits=0
DRIVE_LITERAL_SCAN PASS hits=0
P6_EXPORT byte_identity=True page_count=2 source_sha256=7963b9cdd5e88cc79aebd17a324c0354605768450586b6178899fa5ea2366cac final_sha256=7963b9cdd5e88cc79aebd17a324c0354605768450586b6178899fa5ea2366cac
```

结论：用户提供的模型端点、API Key 和模型名没有写入 P6 源码、配置、测试、迁移表或导出结果；
P6 Python 文件没有盘符绝对路径字面量。项目内展示 PDF 的源/最终哈希完全一致，页数为 2；首屏
PNG 已人工查看，文字、表单值和批注图标均可显示，无乱码。

## 5. 设计澄清与保守决策

1. 支持矩阵把任何签名字段都视为不安全并整文透传；本阶段不宣称能够修改并重新签署 PDF。
2. 加密 PDF 只有在提供正确密码并可生成结构快照时才判定为“可读但必须透传”；密码不进入配置
   或报告。无密码或认证失败只判定源不可读，不尝试绕过加密。
3. 未知关键 Catalog 键、结构树等当前没有修改安全证明，因此保守整文透传，不静默删除。
4. P6.5 的“跨平台”只按 P1 已批准的环境角色验收；若后续要求 Linux/容器等第二种 OS，必须先
   扩大 P1 runtime baseline 和字体安装矩阵，再重跑 G6，不能沿用本报告冒充已验证。

## 6. 最终结论

P6.1～P6.5 和 Gate G6 全部通过。SharedPdfKernel 已形成可复用的机械事实、受控字体、唯一 Patch
解释器、硬约束、有限 Repair、工作区与 Preservation 合同；不能证明安全的 PDF 特征在开工前
整文透传。P6 完成后可以进入 P7 Toolbox 生产合同与迁移骨架，但本阶段没有提前实现 P7 叶子。
