# Page Toolbox Engine Puncture v1

当前完成范围：P0、P1。

P1 只提供运行基建：合同、中文状态机、样本快照、固定译文 Provider、千问页级 Provider、Run Artifact 和聚焦测试。它的成功终点是“译文已就绪”，不生成排版候选 PDF。

运行聚焦测试：

```powershell
$env:PYTHONPATH = "$PWD\src"
python -m unittest discover -s tests -v
```

运行固定译文纵向切片：

```powershell
python scripts/run_p1_vertical_slice.py --provider fixed
```

真实千问调用只从环境变量读取密钥：

```powershell
$env:PAGE_TOOLBOX_QWEN_API_KEY = "<local-secret>"
python scripts/run_p1_vertical_slice.py --provider qwen
```

不得把密钥、输入 PDF 或 `artifacts/runs/` 提交到版本控制。

