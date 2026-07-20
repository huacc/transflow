# P4 最小 PDF 纵向闭环评审 v0.1

评审结论：PASS

开放问题：0

## 冻结边界

1. `DocumentCoordinator` 只接受一份完整 PDF 的 `DocumentRunRequest`，由引擎只读打开并生成 1-based 原始页清单；调用方不能提交页列表。
2. P4 的 `PageFactsExtractor`、受控字体、`PagePatchInterpreter`、PyMuPDF PNG renderer、串行 replay 和 Preservation validator 全部位于最终 `src/transflow/pdf_kernel/`，P6 只能原位补全。
3. candidate 和 final replay 使用同一个 `PagePatchInterpreter`；Patch 应用前必须一次性通过 source/page/geometry/owner、目标对象、保护对象、字体和载荷哈希校验。
4. P4 只承诺页数、页序、MediaBox、CropBox、rotation 和未批准页面内容流不变；未在本阶段声明的完整 PDF 特性矩阵不冒充已经支持。
5. 页面流水线只实现 `body.flow_text.single`、`visual_only` 和透传的最小行为；其他 Route 不进入本阶段。
6. 固定 Route 只存在于 `tests/support/fixed_routes.py`，只按 `source_hash + page_no + geometry_hash` 派生的页面身份查表，未声明页透传；production wiring 不可导入该 fixture。
7. 页面预览固定为 PyMuPDF 直接生成的 144 DPI PNG；解码或原子发布失败时不得提交 preview 指针。
8. 最终 PDF 从源副本按原页序串行回放批准 Patch；不创建或拼接页级 PDF，不使用 HTML、浏览器或宿主机系统字体。
9. Patch 或 Preservation 失败时重新复制并校验源 PDF，以整本透传降级完成；只有源副本也无法发布才允许流程失败。
10. 页级 Checkpoint 是恢复权威；已提交页面重启后不重复翻译、渲染或发布预览，最终 Artifact 已发布时按哈希复用。

## 设计取舍

- P4 不接分类引擎。测试 fixture 负责显式 Route 装配，P5 必须用真实分类替换测试 resolver。
- `PageFacts` 继续保持纯领域最小合同；PyMuPDF 对象事实由 `ExtractedPageFacts` 留在正式 Kernel 内，不向 domain 泄漏打开的 PDF 对象。
- 受控字体只从 P1 `font_manifest.json` 和调用方注入的仓库根解析；未知 ID 直接失败，系统字体搜索次数固定为零。
- 完整年报 fixture 选择 `样本/年报` 中一份 38 页未拆分真实 PDF，以内容哈希、页数和页面身份登记；文件名只用于定位 fixture，不参与 Route 决策。
- P4 保持有界顺序执行以优先证明身份、恢复和最终化不变量；页面并发属于后续生产工程化阶段。
