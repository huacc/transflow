# Round28 Batch Summary

Round28 runs page-classification and column-width-invariant vertical-flow workflow against AIA 20-page zh-to-en and en-to-zh samples.

- Stage A seed verdict: `PASS`
- Stage A seed counts are read from round25 evidence and are not fixed gates.

| Case | Process | Decision graph | Product | Terminal | Loop1 | Loop2 | Repair1 accepted | Repair2 accepted | Candidate |
|---|---|---|---|---|---|---|---|---|---|
| `R28_AIA_ZH_TO_EN_pages_001_020` | `PASS` | `PASS` | `FAIL` | `S_FAIL_QUALITY` | `REJECTED_ROLLBACK` | `IMPROVED` | `False` | `True` | `output/R28_AIA_ZH_TO_EN_pages_001_020_repair0002_candidate.pdf` |
| `R28_AIA_EN_TO_ZH_pages_001_020` | `PASS` | `PASS` | `FAIL` | `S_FAIL_QUALITY` | `REJECTED_ROLLBACK` | `IMPROVED` | `False` | `True` | `output/R28_AIA_EN_TO_ZH_pages_001_020_repair0002_candidate.pdf` |
