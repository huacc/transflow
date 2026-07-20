# Trace Card - R27_AIA_ZH_TO_EN_pages_001_020 - text_fit_overflow

1. 问题是什么：`text_fit_overflow`。
2. 问题域：`text-loading`。
3. 严重度：`P1`。
4. 证据来源：`reports/quality_signal_ledger.json`、`reports/problem_domain_buckets.json`。
5. 为什么这么判：先由 Triage 按因果优先级选出 text_fit_overflow，再由静态 Dispatch 表映射到 expand_or_reflow_slot。
6. 派了什么修复：`expand_or_reflow_slot`。
7. 修复结果：`REJECTED_ROLLBACK`。
8. 回滚/失败原因：{'cross_slot_overlap': {'after': 268, 'before': 140}}
