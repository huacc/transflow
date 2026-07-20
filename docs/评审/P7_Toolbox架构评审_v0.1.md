# P7 Toolbox 架构评审 v0.1

- 评审范围：P7.1～P7.5 Toolbox 生产合同与迁移骨架
- 评审日期：2026-07-20
- 设计基线：`Transflow_PDF翻译排版引擎_总体设计_v0.1.md` §8.3、§9～§10、§16、§20
- 计划基线：`Transflow_PDF翻译排版引擎_详细开发计划_v0.1.md` P7 / G7

## 结论

评审结论：PASS

开放问题：0

## 边界决议

1. `PageToolbox` 固定为 prepare、build translation request、consume translation bundle、render、judge、repair 六阶段；`PageOutcome` 由外部统一归一，叶不得返回自由字典。
2. `TranslationPort` 只由 `ToolboxPageCoordinator` 持有；Toolbox 不接触 Provider 地址、密钥、HTTP、重试、并发或下一页调度。
3. Legacy 单页 PDF 仅是 run/page 私有、可重建、非权威兼容物；不得成为 DocumentFinalizer 输入、Checkpoint 权威引用或最终 Artifact 来源。
4. Catalog 使用显式静态 v2 资源；禁止目录扫描、entry point、运行时注册、模型或 Agent 选择。P7 的 17 条 Route 全部保持 disabled，并各自收敛到确定性页级透传。
5. Margin 公共处理只使用跨页重复、归一化几何、对象类型和跨叶证据；页码、Logo、装饰受保护，正文、表注、图标签和不确定文本交回具体 Toolbox。
6. 叶级 Gate 仅允许 `PASS_ENABLE`、`PASS_DISABLED_WITH_FALLBACK`、`FAIL`；Catalog enabled 必须与叶版本、证据哈希和证明哈希一致。
7. 本阶段不迁移、不启用 P8 及以后任何真实叶算法；`PASS_NON_BLIND`、`NOT_EVALUATED`、`FAIL` 原状态均未升级。

## 验证结论

- P7.1～P7.5 共 30 个编号测试全部通过。
- v2 Catalog 覆盖设计中的 17 条 Route，重复、悬空和无 fallback 数为 0。
- Toolbox/core 的 DB、API、lease、Provider、动态发现和自调度直接依赖数为 0。
- P2 架构扫描通过，新增 `toolboxes` 层保持 `application -> toolboxes -> domain/pdf_kernel` 单向依赖。
- P7 静态资源可确定性重建，资源 manifest 漂移数为 0。

正式命令、原始输出和逐项“通过/失败 + 证据”统一收录在 P7 阶段执行报告中。
