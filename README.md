# pdf-to-epub

把扫描版 PDF 转成**可重排（reflowable）EPUB** 的本地流水线：视觉侦察 → MinerU 解析 → 结构重建 → 三级校验+整页仲裁 → pandoc 封装 → 自动质检。目标阅读器是 Kindle——成品以能顺利经亚马逊服务器转 KFX 为准绳。

> Scanned-PDF → reflowable EPUB pipeline. v0.2.0：单一引擎 × 双模式（批量智能 auto / 主动审阅 guided）× 双接口（CLI/GUI）。**规则零硬编码**：每本书的结构规律由系统在运行时发现、验证、生成、用完即弃。目前针对现代简体中文图书调优。

## 为什么需要它

Calibre 直接转 PDF 常常切错章、丢版式；纯 OCR 工具转出来的文字带着满身错字就封装了；MinerU 解析强大，但标题检测在美术字篇名页上会失灵、目录页条目会被误当标题。本项目的增量是**侦察层 + 结构层 + 校验层**：转换前先让视觉模型"翻一翻"这本书、现场总结出它的结构规律；转换后对 OCR 结果做三级漏斗审查，拿不准的一律保守保持原文并记入报告——宁可多审，绝不改错。

## 架构（v0.2.0）

```
PDF ─► Phase 0  Scout 侦察
        本地 VL 抽样看 16 页 → 确定性信号聚合出候选规则
        → 外部强模型仲裁（可插拔，可选）→ book_profile.json（本次运行的立法，用完即弃）
    ─► Phase 1  MinerU 解析（markdown + 插图 + 块坐标 middle_json）
    ─► Phase 2  结构重建（profile 规则 × middle_json → 篇/编边界 → 标题层级修复）
    ─► Phase 3  三级校验（启发式 → 裁判 → VLM 裁块复核；双确认 + 唯一性 + 收缩护栏）
    ─► Phase 3+ 批量智能仲裁（needs_review 整页重判；do-no-harm 保持原文）
    ─► Phase 4  pandoc 封装 → EPUB 自动质检（zip 结构 / manifest / TOC 落点对账）
    ─► conversion_report.json（全程审计 + needs_human 旗标）
```

架构图见 `docs/architecture-v0.2.0.html`（浏览器打开）。

## 实测效果（auto 模式，零人工干预）

| 书 | 页数/类型 | 结构重建 | 校验+仲裁 | 质检 |
|---|---|---|---|---|
| 文集（200p，23 篇选本） | MinerU 丢失篇名页 | **23/23 篇边界全部重建** | 13 可疑点：11 澄清 / 2 处保守保持 | TOC 40/40 命中 |
| 纪实（250p，编-章结构） | 8 编 + 2 附录 | **12/12 边界**（编名页跨块组合标题也命中） | 17 存疑：落地 3 / 保守保持 14 → needs_human | TOC 66/66 命中 |

**泛化验证亮点**：第二本书上，聚合层误把"正文中的名人生平段"当成篇界信号（过拟合），外部仲裁层按证据强度否决了这条规则——「本地 VL 看页（便宜）+ 聚合生成候选（免费）+ 外部强模型仲裁（质量）」的混合立法架构按设计工作了。

## 快速开始

前提：Windows + NVIDIA GPU（8GB+）、`pandoc` 在 PATH、MinerU venv（`~\mineru-venv` 或 `MINERU_VENV`）、Ollama 便携版（qwen3:8b + qwen2.5vl:7b）。

```powershell
# 交互式（推荐）：逐项询问，auto/guided 可选
.\scripts\pdf-to-epub.ps1

# 直接命令行
.\scripts\pdf-to-epub.ps1 -Pdf book.pdf -Title 书名 -Author 某人 -Mode auto

# GUI（最简图形界面，批量智能模式）
C:\Users\<你>\mineru-venv\Scripts\python.exe gui.py   # 然后开 http://127.0.0.1:7861

# 引擎裸 CLI
python -m engine.pipeline --pdf book.pdf --title 书名 --mode auto
```

双模式：`auto` 批量智能（零干预，不一致项保守保持原文并记报告）；`guided` 主动审阅（校验后暂停，人工改 md 后回车继续封装）。

## 配置

- `config.yaml`：入库模板（本地 ollama 为默认后端）
- `config.local.yaml：**gitignored**，放外部仲裁 API 凭证（OpenAI 兼容端点：`api_base` / `api_key` / `api_model`）。配上后 Phase 0 自动启用外部仲裁层；不配则纯本地运行（聚合层兜底）。
- 环境变量 `SCOUT_API_KEY` 可覆盖 key。

## 已知不足（诚实清单）

详见 `docs/limitations.md`。当前最重要的三条：

1. **文本裁判看不见页面**：校验漏斗中裁判环节无视觉，长段落报警时只能凭空猜（曾致 3 处破坏性落地，收缩护栏已堵住落地端，机制根源未除）
2. **标题体系无调和层**：MinerU 的页面级字体投票与文档级证据未调和——目录页条目、编名页残留块、正文加粗句仍会以标题身份漏进成品
3. **页脚注按页①重置**：流式 EPUB 中多个①互相歧义

## 路线图（已立项，v0.3.0 方向）

- **视觉裁判**：修改提案权只归看得见证据的环节；文本模型降为只能否决不能提案
- **结构调和器**：四方证据（页眉规则 / 目录页条目 / MinerU 字体投票 / 编号模式）→ 唯一章节树 → 标题全部由树重新生成（根治标题重复与多余，而非逐条打补丁）
- **脚注规范化**：全书连续编号 + pandoc 原生 `[^n]` 脚注，按页归组配对，Kindle KFX 转换为验收准绳

## 版权与合规

本仓库**不含任何书籍内容**（`.gitignore` 强制）。请仅对你有权处理的 PDF 使用本工具，转换产物遵守原书版权。

## License

MIT © 2026 Onemanmountain
