# Round27 Batch Summary

Round27 runs the obstacle-aware memory workflow against two 20-page zh-to-en samples.

- Stage A seed verdict: `PASS`
- Stage A seed counts are read from round25 evidence and are not fixed gates.

| Case | Process | Decision graph | Product | Terminal | Loop1 | Loop2 | Repair1 accepted | Repair2 accepted | Candidate |
|---|---|---|---|---|---|---|---|---|---|
| `R27_00005_ZH_TO_EN_pages_001_020` | `PASS` | `PASS` | `FAIL` | `S_FAIL_QUALITY` | `REJECTED_ROLLBACK` | `IMPROVED` | `False` | `True` | `output/R27_00005_ZH_TO_EN_pages_001_020_repair0002_candidate.pdf` |
| `R27_AIA_ZH_TO_EN_pages_001_020` | `PASS` | `PASS` | `FAIL` | `S_FAIL_QUALITY` | `REJECTED_ROLLBACK` | `IMPROVED` | `False` | `True` | `output/R27_AIA_ZH_TO_EN_pages_001_020_repair0002_candidate.pdf` |
