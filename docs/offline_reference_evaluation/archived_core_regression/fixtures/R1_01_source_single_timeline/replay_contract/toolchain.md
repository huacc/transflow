# Toolchain

| Tool | Use | Required |
|---|---|---|
| PowerShell | command runner | yes |
| Python 3.14.3 | replay script runtime | yes |
| PyMuPDF 1.27.2 | text extraction, redaction, text insertion, rendering | yes |
| `C:\Windows\Fonts\msyhl.ttc` | body font | yes |
| `C:\Windows\Fonts\msyhbd.ttc` | year and emphasis font | yes |
| `C:\Windows\Fonts\msyh.ttc` | title fallback/regular font | yes |
| Poppler `pdfinfo` | not used | no |
| ReportLab | not used | no |
| external translation API | not used | no |

## Path Rule

When scripting from PowerShell into Python, do not embed Chinese absolute paths inside Python source text. Use `Path.cwd()` and relative path joins. This avoids the path encoding failure observed during the first run.
