# P3 文件副作用与测试 AI 评审 v0.1

评审结论：PASS

开放问题：0

## 冻结边界

1. `StandaloneRunAdapter` 只接受允许根内一份可打开的完整 PDF 绝对路径；所有校验通过前不得创建 Run 或 workspace。
2. `FilesystemCheckpointAdapter` 以更高版本为权威，相同版本相同内容幂等，同版本分叉和版本倒退均冲突。
3. Checkpoint 和 Artifact 都按 partial、fsync、同文件系统 rename、manifest 原子替换的顺序提交；manifest 是 standalone 权威。
4. 恢复只删除 pending journal 明确登记的 partial，不递归删除未知、跨 Run 或重解析目标。
5. Artifact 不可变；Checkpoint 只能引用已登记且重新验证哈希成功的 Artifact。
6. 审计日志固定必填上下文，秘密字段直接丢弃，疑似 Bearer/密钥值脱敏，长文本按硬上限截断。
7. Translation 与 ModelDecision 保持两个 Port；同一 `HttpAiCapabilityAdapter` 仅共享 HTTP 传输，不合并领域合同。
8. fake AI Service 位于工程脚本而非 production wheel；真实 Provider、Qwen 接线和 LiteLLM 依赖不进入 Transflow wheel。

## 设计取舍

- P3 不建设 CLI，也不创建产品 Job；测试直接调用 Standalone Adapter。
- Windows 环境无文件符号链接权限时，验收使用真实目录 Junction 覆盖同一“允许根内入口解析到根外目标”的重解析攻击，不跳过安全测试。
- P3 使用 Fixed/Deterministic 与本机 fake HTTP 完成合同验收，不调用真实模型。这样避免把密钥写入配置或将外部模型波动混入文件原子性 Gate。
- 文件系统与未来数据库之间不声明分布式原子性；P3 只证明 standalone manifest 的文件权威语义。
