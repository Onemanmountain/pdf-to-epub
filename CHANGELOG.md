# Changelog

本项目遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

## [0.2.0] - 2026-10-08

单一引擎重构。核心主张：**规则零硬编码**——每本书的结构规律由系统运行时发现、验证、生成、用完即弃。经两本类型迥异的真实图书（文集 200p / 编章纪实 250p）auto 模式零人工实测。

### Added
- `engine/` 引擎层：scout（侦察）/ signals（信号聚合）/ structure（结构重建）/ arbitrate（整页仲裁）/ qc（EPUB 质检）/ pipeline（编排）
- Phase 0 Scout：本地 VL 抽样看 16 页 → 确定性信号聚合出候选规则 → `book_profile.json`
- 外部强模型仲裁层（可插拔）：审核候选规则 adopt/reject/adjust/add；实测否决过拟合规则（把纪实书正文里的名人生平段误当篇界）
- Phase 2 结构重建：页眉规则 × middle_json → 篇/编边界检测 → 标题层级修复（文集 23/23、纪实 12/12）
- Phase 3+ 批量智能仲裁：needs_review 整页重判；do-no-harm 保持原文；`needs_human` 旗标
- Phase 4 EPUB 质检：zip 结构 / manifest 完整性 / TOC 落点对账（40/40、66/66）
- `conversion_report.json` 全程审计；GUI（gui.py，Gradio 最简界面）；ps1 改写为引擎薄驱动
- 双模式：`--mode auto`（默认）/ `guided`；多书安全：profile/zip 复用前校验归属

### Fixed（泛化实测暴露）
- **破坏性落地事故**：段落级报警的"纠正"实为转录不全（165 字→66 字），长文本天然过唯一性检查 → 收缩护栏（纠正砍掉原文 20%+ 拒落地）
- 组合页眉（"第X编 标题"分两个块）全等匹配静默漏检 → 包含匹配
- 编名页回看窗口 3 页不足（空白页+无页眉章首页）→ 6 页；md 落位固定 200 行窗在编距 30+ 页失效 → 单调游标
- ollama 整页视觉默认 n_ctx=4096 超限报 400 → num_ctx=16384
- 本地 8B 文本模型自由立法会内容过拟合 → 假设生成改为确定性数据聚合，LLM 降为仲裁角色
- curl/schannel 吊销检查离线致 HTTPS 假死（CRYPT_E_REVOCATION_OFFLINE）→ 引擎一律走 python urllib

### Known issues（v0.3.0 已立项）
- 文本裁判无视觉，长段落报警输出噪声碎片（收缩护栏是兜底，根源未除）→ 视觉裁判重构
- MinerU 字体投票与文档级证据无调和层，目录页条目/编名页残留/正文加粗误标仍漏进成品 → 结构调和器重构
- 页脚注按页①重置 → 全书连续编号 + pandoc 原生脚注
- MinerU 解析时长书间差异大（5min~26min/250p）；Scout 本地 16 页约 27min

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

[0.2.0]: https://github.com/Onemanmountain/pdf-to-epub/releases/tag/v0.2.0
[0.1.0]: https://github.com/Onemanmountain/pdf-to-epub/releases/tag/v0.1.0
