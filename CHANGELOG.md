# Changelog

本项目遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

## [0.1.0] - 2026-10-06

首个公开版本。经两本真实中文扫描书（263 页 / 200 页）全流程实测，期间修复的问题全部收录如下。

### Added
- `pdf-to-epub.ps1` 交互式一条龙：MinerU 解析 → 三级校验 → 人工审暂停 → pandoc 封装
- `epub_verify.py` 三级校验漏斗：规则启发式 → Qwen3-8B 裁判 → Qwen2.5-VL 定向复核
- 双后端：`--backend ollama`（默认，本机 Ollama 服务）/ `--backend transformers`（HF 权重 4bit）
- 双确认铁律：裁判与 VLM 规范化后一致才自动修，其余进 needs_review
- 重名 EPUB 自动加序号不覆盖；已有解析 zip 可复用跳过解析；手改 md 自动备份
- ollama 未运行时自动拉起 serve；模型缺失自动补拉（可选 `-Proxy`）

### Fixed（实测暴露）
- ollama serve 冷启动超时 45s → 150s，并检测进程早退给出可操作报错
- MinerU 拒绝扩展名前含空格的 PDF 文件名 → 自动复制为干净名再解析
- `load_blocks` 按文件大小选中间文件会误选更大的 `model_output.json`（其 pages 元素是 list）→ 改为按文件名优先匹配 `middle_json`
- **自动修正的位置无关替换风险**：可疑文本在全文出现多次时 `replace` 会命中第一处改错位置 → 加唯一性护栏，多处出现一律转人工审
- `--max-flags` 超限静默截断 → 超限打印警告
- **VLM 转录 prompt 繁体偏置**：「繁体字按原样输出」一句导致 qwen2.5vl 对简体页面系统性输出繁体（张炜→張煣、胡适→胡適）→ 改为字形中立 prompt（经 vision ground truth + 双 prompt 对照实验确认）
- 用户级 PYTHONPATH 污染环境时依赖被"走私"掩盖 → 调用外部进程前隔离 PYTHONPATH；文档加入 `pip check` 自检
- ollama 0.35.1 `/api/chat` 的 `content` 只接受字符串，图片改走消息级 `images: [base64]`
- 视觉模型官方库名 `qwen2.5vl:7b`（无连字符），写错报 404 而非"模型不存在"
- 缺 bbox 可疑点静默跳过 → 计数并打印；临时裁图用后清理
- 重跑时手改的 `-fixed.md` 会被静默删除 → 删除前自动备份 `.bak`

[0.1.0]: https://github.com/Onemanmountain/pdf-to-epub/releases/tag/v0.1.0
