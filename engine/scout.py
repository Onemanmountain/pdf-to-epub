# -*- coding: utf-8 -*-
"""
scout.py — Phase 0 侦察：视觉抽样阅读 → 结构规则假设 → book_profile.json

设计要点（v0.2.0 架构）：
- 零硬编码：所有本书专属规则由模型在运行时发现，写入 profile，用完即弃
- Scout 大脑可插拔：LocalOllama（默认离线）/ ExternalAPI（OpenAI 兼容端点）
- 每条 boundary_rule 带置信度；规则的有效性在 Phase 2 用 middle_json 数据复核

CLI:
  python -m engine.scout --pdf book.pdf --out profile.json [--dpi 150] [--samples 16]
"""
import argparse, base64, json, os, re, sys, tempfile

import pymupdf  # PyMuPDF

sys.stdout.reconfigure(encoding="utf-8")


# ---------------- 可插拔后端 ----------------

class LocalOllama:
    """本机 ollama /api/chat。content 只接受 str，图片走消息级 images: [base64]。"""

    def __init__(self, url, vl_model, text_model):
        self.url, self.vl, self.text = url.rstrip("/"), vl_model, text_model

    def _chat(self, model, prompt, images=None, think=None, timeout=600):
        import urllib.request
        msg = {"role": "user", "content": prompt}
        if images:
            msg["images"] = images
        payload = {"model": model, "messages": [msg], "stream": False, "keep_alive": "30m",
                   "options": {"num_ctx": 16384}}  # 整页视觉 token 远超 ollama 默认 4096，必须显式放大
        if think is not None:
            payload["think"] = think
        req = urllib.request.Request(self.url + "/api/chat",
                                     data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())["message"]["content"]

    def describe(self, png, prompt):
        b64 = base64.b64encode(open(png, "rb").read()).decode()
        return self._chat(self.vl, prompt, images=[b64])

    def reason(self, prompt):
        return self._chat(self.text, prompt, think=False)


class ExternalAPI:
    """OpenAI 兼容端点（/v1/chat/completions），视觉走 image_url data URI。"""

    def __init__(self, base, key, model):
        self.base, self.key, self.model = base.rstrip("/"), key, model

    def _chat(self, content, timeout=600):
        import urllib.request
        payload = {"model": self.model, "messages": [{"role": "user", "content": content}]}
        req = urllib.request.Request(self.base + "/chat/completions",
                                     data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json",
                                              "Authorization": f"Bearer {self.key}"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())["choices"][0]["message"]["content"]

    def describe(self, png, prompt):
        b64 = base64.b64encode(open(png, "rb").read()).decode()
        return self._chat([{"type": "text", "text": prompt},
                           {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}])

    def reason(self, prompt):
        return self._chat(prompt)


def make_backend(cfg):
    s = cfg["scout"]
    if s["backend"] == "api":
        if not (s["api_base"] and s["api_key"] and s["api_model"]):
            raise SystemExit("[X] scout.backend=api 需要 config.yaml 配齐 api_base/api_key/api_model")
        return ExternalAPI(s["api_base"], s["api_key"], s["api_model"])
    return LocalOllama(s["ollama_url"], s["vl_model"], s["text_model"])


# ---------------- 抽样与渲染 ----------------

def sample_pages(total, n):
    """前 8 页 + 中段等距 + 末 2 页，去重排序。"""
    head = list(range(min(8, total)))
    tail = [total - 2, total - 1] if total > 10 else []
    mid_n = max(0, n - len(head) - len(tail))
    mid = []
    if mid_n and total > 12:
        step = (total - 10) / (mid_n + 1)
        mid = [int(9 + step * (i + 1)) for i in range(mid_n)]
    pages = sorted(set(head + mid + tail))
    return [p for p in pages if 0 <= p < total]


def render(pdf_path, pages, dpi, tmp):
    doc = pymupdf.open(pdf_path)
    out = {}
    for p in pages:
        pix = doc[p].get_pixmap(dpi=dpi)
        fp = os.path.join(tmp, f"p{p:04d}.png")
        pix.save(fp)
        out[p] = fp
    total = doc.page_count
    doc.close()
    return out, total


# ---------------- Prompt ----------------

DESCRIBE_PROMPT = """这是一本书扫描版 PDF 的第 {idx} 页（0 起，全书 {total} 页）。请仔细观察页面，只输出 JSON：
{{"page_type": "封面|版权页|目录|正文|篇名页|插图页|空白|其他",
 "header_text": "页眉/书眉文字（无则空串）",
 "printed_page_no": 印刷页码数字（无则 null),
 "title_like_text": "页面上最像标题的文字（无则空串）",
 "author_like_text": "页面上像作者署名的文字（无则空串）",
 "bio_like_text": "页面上像作者生平简介的段落开头（无则空串）",
 "summary": "一句话内容概括",
 "font_notes": "简体/繁体/竖排/美术字/手写等版式特征"}}"""

SYNTH_PROMPT = """你在为「扫描PDF→EPUB」转换流水线做结构侦察。视觉模型对抽样页的描述如下（JSON 数组，idx 为 0 起页码，全书 {total} 页）：

{descs}

下游用 MinerU 解析全书得到 middle_json：每页 {{page_idx, blocks[]}}；block 有 type（text/paragraph_title/doc_title/header/footer/page_number/image/table）与 content（文字）。

请推断**这本书的结构语法**，只输出 JSON：
{{"meta": {{"title": "书名", "book_kind": "单作者专著|文集选本|其他", "script": "zh-Hans|zh-Hant"}},
 "page_map": {{"printed_page_offset": 整数或null, "front_matter_pages": [页码]}},
 "boundary_rules": [
   {{"name": "规则名", "description": "人类可读描述",
     "target": "header_blocks|text_blocks|title_blocks",
     "match": {{"regex": "可选", "block_type": "可选", "max_len": 可选整数}},
     "signals_boundary": "article|chapter|section",
     "confidence": 0.0, "evidence_pages": [页码]}}],
 "quirks": {{"vertical_pages": [], "calligraphy_pages": [], "notes": ""}},
 "ocr_strategy": {{"special_pages": [], "note": ""}}}}

铁律：
1. 规则必须在抽样描述中**重复出现至少 2 次**（如：页眉随篇变化且不同于书名；篇末有固定格式引子段；篇名页版式特殊）
2. 没看出规律就留空数组——宁可少，不可编
3. regex 若给出，必须能对 block 的 content 直接 re.search（Python 语法）
4. confidence 按证据强度给 0.3-0.95"""


# ---------------- 主流程 ----------------

def extract_json(text):
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def scout(pdf_path, out_path, backend, dpi=150, n_samples=16, llm_synth=False):
    tmp = tempfile.mkdtemp(prefix="scout_")
    doc = pymupdf.open(pdf_path)
    total = doc.page_count
    doc.close()
    pages = sample_pages(total, n_samples)
    print(f"[scout] 全书 {total} 页，抽样 {len(pages)} 页: {pages}")
    imgs, _ = render(pdf_path, pages, dpi, tmp)

    descs = []
    n_bad = 0
    for p in pages:
        try:
            raw = backend.describe(imgs[p], DESCRIBE_PROMPT.format(idx=p, total=total))
            d = extract_json(raw) or {"_raw": raw[:200]}
        except Exception as e:
            d = {"_error": str(e)[:120]}
        d["idx"] = p
        descs.append(d)
        bad = "_error" in d or "_raw" in d
        n_bad += bad
        if bad:
            print(f"  p{p}: [ERR] {d.get('_error', d.get('_raw',''))[:80]}")
        else:
            print(f"  p{p}: [{d.get('page_type','?')}] header={d.get('header_text','')[:14]!r} "
                  f"title={d.get('title_like_text','')[:14]!r} author={d.get('author_like_text','')[:10]!r}")

    if n_bad > len(descs) / 2:
        raise SystemExit(f"[X] {n_bad}/{len(descs)} 页描述失败，拒绝基于残缺侦察做结构推理（垃圾进垃圾出）")

    print("[scout] 确定性信号聚合（证据驱动，替代 LLM 自由立法）...")
    from . import signals
    profile = signals.build_profile(descs, total)
    if llm_synth:
        # 可选：LLM 仲裁层（默认关闭——实测 8B 自由立法会内容过拟合）
        syn = backend.reason(SYNTH_PROMPT.format(total=total, descs=json.dumps(descs, ensure_ascii=False, indent=1)))
        profile["llm_commentary"] = extract_json(syn) or {"_unparsed": syn[:500]}
    profile["_meta"] = {"pdf": os.path.basename(pdf_path), "total_pages": total,
                        "sampled": pages, "dpi": dpi, "backend": type(backend).__name__,
                        "lawmaker": "deterministic-signals" + ("+llm" if llm_synth else "")}
    profile["_descs"] = descs  # 留档：便于人工核查规则是否真的来自证据
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    json.dump(profile, open(out_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"[scout] profile -> {out_path}")
    rules = profile.get("boundary_rules", [])
    print(f"[scout] 发现边界规则 {len(rules)} 条:")
    for r in rules:
        print(f"  - [{r.get('signals_boundary')}|{r.get('confidence')}] {r.get('name')}: {r.get('description','')[:60]}")
    return profile


def main():
    from .config import load_config
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--dpi", type=int, default=150)
    ap.add_argument("--samples", type=int, default=16)
    ap.add_argument("--config", default=None)
    ap.add_argument("--llm-synth", action="store_true", help="额外让 LLM 对聚合结果做仲裁注释（默认关闭）")
    args = ap.parse_args()
    cfg = load_config(args.config)
    backend = make_backend(cfg)
    scout(args.pdf, args.out, backend, dpi=args.dpi, n_samples=args.samples, llm_synth=args.llm_synth)


if __name__ == "__main__":
    main()
