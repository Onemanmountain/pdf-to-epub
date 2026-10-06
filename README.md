# pdf-to-epub

把扫描版 PDF 转成**可重排（reflowable）EPUB** 的本地流水线：MinerU 解析 → 本地双模型三级校验 → pandoc 封装。全程离线，零 API 费用，为你的 Kindle/阅读器而生。

> Scanned-PDF → reflowable EPUB pipeline. MinerU parsing → local dual-LLM triple verification (Qwen3 judge + Qwen2.5-VL page-level recheck) → pandoc. Fully offline, zero API cost. 目前针对现代简体中文图书调优。

## 为什么需要它

Calibre 直接转 PDF 常常切错章、丢版式；纯 OCR 工具转出来的文字带着满身错字就封装了。本项目的增量是**校验层**：转换完不直接交付，而是让两个本地模型对 OCR 结果做三级漏斗审查，拿不准的一律留给人工——宁可多审，绝不改错。

## 流水线

```
PDF ──► MinerU 解析 ──► markdown + 插图 + 块坐标(middle_json)
                          │
                          ▼ 三级校验漏斗
              Stage A  规则启发式扫描（全书 → 十余个可疑点）
                ▼
              Stage A3 Qwen3-8B 裁判逐条裁决（wrong / corrected）
                ▼
              Stage B  Qwen2.5-VL 按 bbox 裁块、300dpi 定向复核
                ▼
              双确认铁律：裁判与 VLM 规范化后一致 + 全文唯一 → 自动修
                          其余一律进 needs_review 人工审
                          │
                          ▼
                    人工终审 → pandoc → EPUB
```

## 实测效果

两本现代中文扫描书（263 页 / 200 页）：

| 指标 | 结果 |
|---|---|
| MinerU standard 档解析后可疑点 | 13-15 处（basic 档为大量错字） |
| 自动修改落地 | 0-1 处（双确认+唯一性护栏，宁缺毋滥） |
| 进人工审 | 2-5 处（含竖排题字等真·盲区） |
| 单本耗时（RTX 级 GPU） | 解析 ~5-10 min + 校验 ~20-25 min |

护栏有效性实录：裁判模型曾把"胡适"判错并给出合并下文式的幻觉纠正（"读书 胡适"），双确认+唯一性护栏将其全部拦下，0 处错误落地。

## 安装

**前置**：Windows + NVIDIA GPU（验证环境：16GB 显存；CPU 未测试）、Python 3.11+、PowerShell 5.1+

```powershell
# 1. MinerU 环境（按其官方文档，或用下面的精简路径）
python -m venv mineru-venv
mineru-venv\Scripts\pip install mineru[core]==4.0.10 -i https://pypi.org/simple
mineru-venv\Scripts\pip install -r requirements.txt

# 2. Ollama（默认校验后端）—— https://ollama.com 下载
ollama pull qwen3:8b
ollama pull qwen2.5vl:7b     # 注意官方库名无连字符

# 3. pandoc —— https://pandoc.org
```

**自检**：依赖完整性建议跑一遍（曾有环境因 PYTHONPATH 污染掩盖缺包）：

```powershell
$env:PYTHONPATH=$null; mineru-venv\Scripts\python -m pip check
```

## 使用

`epub_verify.py` 必须与 `pdf-to-epub.ps1` 放在同一目录。

```powershell
# 交互模式（逐项询问 PDF 路径 / 书名 / 作者 / 输出目录）
.\pdf-to-epub.ps1

# 全自动
.\pdf-to-epub.ps1 -Pdf "D:\books\某书.pdf" -Title "某书" -Author "某某"

# 帮助与全部参数
.\pdf-to-epub.ps1 -Help
```

流程走到 [3/4] 会暂停：打开 `verify_report.json` 看 `needs_review`，对照原 PDF 手改 `<书名>-fixed.md`，回车继续，自动封装 EPUB（与 PDF 同目录，重名自动加序号）。

**transformers 后端**（不依赖 Ollama 服务，直连 HF 权重，4bit 量化）：

```powershell
$env:MODELS_ROOT = "D:\models"   # 下含 Qwen3-8B 与 Qwen2.5-VL-7B-Instruct
.\pdf-to-epub.ps1 -Backend transformers
```

## 已知边界（重要，使用前请读）

- **"校验通过"≠"全书无误"**：Stage A 启发式只覆盖几类错误模式（数字嵌词/繁简混杂/孤立短块/标题页码尾缀），无语义级查错能力，无召回率保证
- **竖排文字是公认盲区**：OCR 与 VLM 对竖排题字/古籍都不可靠，靠人工审兜底
- **低清书法字的裁块级 VLM 转录仍可能出错**：护栏（双确认+人工）是承重墙，不是 VLM 准确率
- 复杂版式（多栏/脚注混排）的章节切分未充分测试
- 启发式字符集为手写小集合，目前只针对现代简体中文图书调优

详见 [docs/limitations.md](docs/limitations.md) 与 [docs/pipeline.md](docs/pipeline.md)。

## 目录结构

```
scripts/
  pdf-to-epub.ps1   # 交互式一条龙入口
  epub_verify.py    # 三级校验核心（--backend ollama|transformers）
docs/
  pipeline.md       # 技术路线与设计决策
  limitations.md    # 已知边界与失败案例
CHANGELOG.md
requirements.txt
```

## 贡献

Issue / PR 都欢迎：新的错误模式启发式、其他语种字符集、CPU 适配、批量模式都是好方向。提交前请用一本真书实测并在 PR 里贴 `verify_report.json` 摘要。

## License

MIT — 详见 [LICENSE](LICENSE)。

**版权提醒**：请只转换你有权使用的 PDF。本仓库不包含也不接受任何受版权保护的书籍内容，PR 中附带书籍文本将被拒绝。
