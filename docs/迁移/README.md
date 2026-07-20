# Transflow 迁移治理目录

本目录保存从分类穿刺、工具箱穿刺和 MerqFin 参考基线迁入生产引擎时使用的只读
台账。生产代码不得 import 本目录，也不得把历史 run、报告或样本当作运行时规则。

P0 的权威机器可读文件：

- `baseline_manifest.json`：设计、计划、两个 spike 和 MerqFin 参考提交；
- `migration_ledger.json`：逐迁移单元来源、哈希、目标、改造策略和真实证据状态；
- `traceability_matrix.json`：设计、任务、交付接口、测试和 Gate 的双向追溯；
- `governance_registry.json`：阶段状态、待决策、行为变化和风险登记规则。

前三个文件由 `scripts/build_p0_assets.py` 可重复生成。人工不得直接修改生成内容；
来源发生变化时必须形成新基线并重新通过受影响 Gate。
