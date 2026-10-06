# -*- coding: utf-8 -*-
"""
epub_verify.py — MinerU 产物（markdown）的可疑点三级校验漏斗

数据流：
  Stage A: 从 middle_json 逐块扫描可疑点（块自带 page + bbox）
    - isolated_short: 1-2 字孤立块（手写题字被拆块等）
    - digit_in_word: 汉字夹数字（方法5技巧）
    - heading_pagenum: 标题带页码尾缀（" 002"）
    - rare_chars_in_heading: 标题含多个生僻字
    - trad_mix: 简体段落混入多个繁体字
  Stage A3: Qwen3-8B 文本裁判（--no-judge 关闭）：判 wrong + 给 corrected
  Stage B:  Qwen2.5-VL 定向复核：按 bbox 裁块区域（300dpi），VLM 只读小图
  Stage C:  VLM 读数与原文不同则修正 md，输出报告 JSON

用法:
  python epub_verify.py --md xxx.md --pdf xxx.pdf --mid <middle_json目录> \
      --out 修正后.md --report report.json [--no-judge] [--max-flags 30]

模型默认在 C:/Users/29698/models/，--qwen3/--qwen2vl 可覆盖。
两模型串行加载，用完卸载，避免爆显存。
"""
import argparse, io, json, os, re, sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
MODELS_ROOT = os.environ.get("MODELS_ROOT", "models")  # transformers 后端的权重根目录，可用环境变量覆盖

TRAD_CHARS = set("證據適體學經國關問間這還進遠運過選遺鄉麗龜龍龔實臺曆歷")

# ---------------- middle_json 装载（块级） ----------------

def load_blocks(mid_dir):
    """返回 [{page, type, bbox, text}]，page 为 1 起。"""
    cands = []
    for root, _, files in os.walk(mid_dir):
        for f in files:
            if f.endswith(".json"):
                cands.append(os.path.join(root, f))
    if not cands:
        return []
    # 优先按文件名找 middle_json.json / *_middle.json：纯按大小选会误选更大的 model_output.json（其 pages 元素是 list）
    mid_named = [p for p in cands if "middle" in os.path.basename(p).lower()]
    if mid_named:
        cands = sorted(mid_named, key=lambda p: -os.path.getsize(p))
    else:
        cands.sort(key=lambda p: -os.path.getsize(p))
    data = json.load(open(cands[0], encoding="utf-8"))
    pages = data.get("pages") if isinstance(data, dict) else data
    blocks = []
    for pg in pages:
        pno = pg.get("page_idx", 0) + 1
        for b in pg.get("blocks", []):
            text = b.get("content")
            if isinstance(text, list):  # 兼容嵌套 schema（content 里可能再套 list）
                def flatten(o):
                    if isinstance(o, str):
                        return o
                    if isinstance(o, list):
                        return "".join(flatten(x) for x in o)
                    if isinstance(o, dict):
                        return flatten(o.get("content", ""))
                    return ""
                text = flatten(text)
            if isinstance(text, str) and text.strip():
                blocks.append({"page": pno, "type": b.get("type", "text"),
                               "bbox": b.get("bbox"), "text": text.strip()})
    return blocks

# ---------------- Stage A：块级启发式 ----------------

def flag_blocks(blocks):
    flags = []
    for i, b in enumerate(blocks):
        t = b["text"]
        kind = None
        detail = ""
        if b["type"] in ("text", "interline_equation") and len(t) <= 2 and not re.fullmatch(r"[0-9一二三四五六七八九十]+[.、]?|[*\-—·]", t):
            kind, detail = "isolated_short", f"孤立短块({len(t)}字)"
        elif re.search(r"[\u4e00-\u9fff]\d[\u4e00-\u9fff]", t):
            kind, detail = "digit_in_word", "数字混入词中"
        elif b["type"] in ("paragraph_title", "doc_title") and re.search(r"\s[0-9IilLoO]{1,4}$", t):
            kind, detail = "heading_pagenum", "标题带页码尾缀"
        elif b["type"] in ("paragraph_title", "doc_title") and len(t) >= 2:
            rare = [c for c in t if c in RARE_SET]
            if len(rare) >= 2:
                kind, detail = "rare_chars_in_heading", f"标题生僻字: {''.join(rare)}"
        trad = [c for c in t if c in TRAD_CHARS]
        if kind is None and len(trad) >= 2:
            kind, detail = "trad_mix", f"繁体混入: {''.join(trad[:6])}"
        if kind:
            flags.append({**b, "idx": i, "kind": kind, "detail": detail})
    return flags

RARE_SET = set("否登涉粱汾渭浚芮晁扈戛轼鹑骅羁囡殇烨骞虢澹踵隼蹙鲸嚼淼焱垚骉猋")

# ---------------- 模型 ----------------

def load_qwen3(path):
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
    tok = AutoTokenizer.from_pretrained(path)
    model = AutoModelForCausalLM.from_pretrained(
        path, quantization_config=BitsAndBytesConfig(load_in_4bit=True),
        device_map="cuda", dtype=torch.bfloat16)
    return model, tok

def qwen3_chat(model, tok, prompt, max_new=400):
    msgs = [{"role": "user", "content": prompt}]
    try:
        text = tok.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True,
                                       enable_thinking=False)
    except TypeError:
        text = tok.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True)
    ids = tok(text, return_tensors="pt").to(model.device)
    out = model.generate(**ids, max_new_tokens=max_new, temperature=0.1,
                         do_sample=True, pad_token_id=tok.eos_token_id)
    return tok.decode(out[0][ids["input_ids"].shape[1]:], skip_special_tokens=True)

JUDGE_PROMPT = """你是中文图书校对专家。图书《{title}》经 OCR 后有一可疑处，请判断是否真错。

可疑类型：{kind}（{detail}）
OCR 文本：「{span}」
同页相邻文本：「{context}」

只输出 JSON：{{"wrong": true/false, "corrected": "正确文本或原文", "reason": "一句话"}}"""

def load_qwen2vl(path):
    import torch
    from transformers import AutoProcessor, Qwen2_5_VLForConditionalGeneration, BitsAndBytesConfig
    from qwen_vl_utils import process_vision_info
    proc = AutoProcessor.from_pretrained(path)
    # 限制视觉 token，防止整页大图拖死 4bit 视觉塔
    proc.image_processor.max_pixels = 768 * 28 * 28
    proc.image_processor.min_pixels = 32 * 28 * 28
    model = Qwen2_5_VLForConditionalGeneration.from_pretrained(
        path, quantization_config=BitsAndBytesConfig(load_in_4bit=True),
        device_map="cuda", dtype=torch.bfloat16, attn_implementation="sdpa")
    return model, proc, process_vision_info

def qwen2vl_read(model, proc, pvi, image_path, hint, max_new=120):
    prompt = (f"这是图书《{hint}》扫描页中的一个文字区域。请逐字转录图中文字，"
              f"不要解释，不要加空格。按图中实际字形原样输出，"
              f"不要把简体转换成繁体，也不要把繁体转换成简体。")
    msgs = [{"role": "user", "content": [
        {"type": "image", "image": image_path},
        {"type": "text", "text": prompt}]}]
    text = proc.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True)
    images, vids = pvi(msgs)
    inputs = proc(text=[text], images=images, videos=vids, padding=True, return_tensors="pt")
    inputs = inputs.to(model.device)
    out = model.generate(**inputs, max_new_tokens=max_new, temperature=0.1,
                         do_sample=True, pad_token_id=proc.tokenizer.eos_token_id)
    return proc.tokenizer.decode(out[0][inputs["input_ids"].shape[1]:], skip_special_tokens=True).strip()

# ---------------- bbox 裁图 ----------------

def render_region(doc, page, bbox, out_png, expand=0.15, dpi=300):
    """bbox 为归一化 [x0,y0,x1,y1]；按比例外扩后裁剪渲染。"""
    import fitz
    pg = doc[page - 1]
    r = pg.rect
    x0, y0, x1, y1 = bbox
    w, h = x1 - x0, y1 - y0
    x0 = max(0, x0 - w * expand); y0 = max(0, y0 - h * expand)
    x1 = min(1, x1 + w * expand); y1 = min(1, y1 + h * expand)
    clip = fitz.Rect(x0 * r.width, y0 * r.height, x1 * r.width, y1 * r.height)
    pix = pg.get_pixmap(dpi=dpi, clip=clip)
    pix.save(out_png)

# ---------------- Ollama 后端 ----------------

def ollama_chat(url, model, prompt, images_b64=None, think=None, timeout=900):
    """走 Ollama /api/chat。images_b64: [base64str]（注意：该版本 content 只接受 str，图片走 images 字段）。"""
    import urllib.request
    msg = {"role": "user", "content": prompt}
    if images_b64:
        msg["images"] = images_b64
    payload = {"model": model, "messages": [msg],
               "stream": False, "keep_alive": "30m"}
    if think is not None:
        payload["think"] = think
    req = urllib.request.Request(url + "/api/chat",
                                 data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())["message"]["content"]

def ollama_unload(url, model):
    import urllib.request
    payload = json.dumps({"model": model, "keep_alive": 0}).encode("utf-8")
    try:
        urllib.request.urlopen(urllib.request.Request(
            url + "/api/generate", data=payload,
            headers={"Content-Type": "application/json"}), timeout=60)
    except Exception:
        pass

# ---------------- 主流程 ----------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--md", required=True)
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--mid", required=True, help="middle_json 输出目录（standard 档）")
    ap.add_argument("--out", required=True)
    ap.add_argument("--report", required=True)
    ap.add_argument("--title", default="")
    ap.add_argument("--qwen3", default=os.path.join(MODELS_ROOT, "Qwen3-8B"))
    ap.add_argument("--qwen2vl", default=os.path.join(MODELS_ROOT, "Qwen2.5-VL-7B-Instruct"))
    ap.add_argument("--max-flags", type=int, default=30)
    ap.add_argument("--no-judge", action="store_true")
    ap.add_argument("--no-vlm", action="store_true")
    ap.add_argument("--backend", choices=["transformers", "ollama"], default="transformers")
    ap.add_argument("--ollama-url", default="http://localhost:11434")
    ap.add_argument("--ollama-text-model", default="qwen3:8b")
    ap.add_argument("--ollama-vl-model", default="qwen2.5vl:7b")
    args = ap.parse_args()

    md_text = open(args.md, encoding="utf-8").read()
    title = args.title or os.path.splitext(os.path.basename(args.md))[0].strip()
    blocks = load_blocks(args.mid)
    print(f"[load] middle_json 块数 {len(blocks)}")

    flags = flag_blocks(blocks)
    # 同文本去重（OCR 重复块只查一次）
    seen, uniq = set(), []
    for f in flags:
        if f["text"][:50] not in seen:
            seen.add(f["text"][:50]); uniq.append(f)
    if len(uniq) > args.max_flags:
        print(f"[!] 可疑点 {len(uniq)} 处超过 --max-flags {args.max_flags}，只校验前 {args.max_flags} 处（其余未校验）")
    flags = uniq[: args.max_flags]
    print(f"[Stage A] 可疑点 {len(flags)} 处")
    for f in flags:
        print(f"  - [{f['kind']}] p{f['page']} {f['text'][:40]!r}")

    report = {"title": title, "flags": [{k: f[k] for k in ('page','type','kind','detail','text')} for f in flags],
              "judged": [], "vlm": [], "applied": []}
    corrected = md_text

    # ---- Stage A3: Qwen3 裁判 ----
    judged = []
    if flags and not args.no_judge:
        print(f"[Stage A3] Qwen3 裁判中... (backend={args.backend})")
        norm0 = lambda s: re.sub(r"[\s，。,.、·:：;；\"'“”‘’《》<>!\[\]【】]", "", s or "")
        use_ollama = args.backend == "ollama"
        if not use_ollama:
            model, tok = load_qwen3(args.qwen3)
        for f in flags:
            ctx = " ".join(b["text"] for b in blocks
                           if b["page"] == f["page"])[:400]
            prompt = JUDGE_PROMPT.format(title=title, kind=f["kind"],
                                         detail=f["detail"], span=f["text"][:150], context=ctx)
            try:
                if use_ollama:
                    ans = ollama_chat(args.ollama_url, args.ollama_text_model,
                                      prompt, think=False)
                else:
                    ans = qwen3_chat(model, tok, prompt)
                m = re.search(r"\{.*\}", ans, re.S)
                j = json.loads(m.group(0)) if m else {}
            except Exception as e:
                j = {"error": str(e)}
            # 幻觉护栏：判 wrong 但 corrected 与原文相同 → 视为误报
            if j.get("wrong") and norm0(j.get("corrected")) == norm0(f["text"]):
                j["wrong"] = False
                j["note"] = "corrected==original, treated as false positive"
            judged.append({**f, "judge": j})
            mark = "✗错" if j.get("wrong") else "✓对"
            print(f"  {mark} p{f['page']} {f['text'][:32]!r} -> {j.get('corrected','')[:28]!r}")
        report["judged"] = [{k: v for k, v in j.items() if k != "bbox"} for j in judged]
        if use_ollama:
            ollama_unload(args.ollama_url, args.ollama_text_model)
        else:
            del model
            import torch; torch.cuda.empty_cache()
    else:
        judged = [{**f, "judge": {}} for f in flags]

    # 机械修复：标题页码尾缀无需模型裁决，直接剥除
    norm = lambda s: re.sub(r"[\s，。,.、·:：;；\"'“”‘’《》<>!\[\]【】]", "", s or "")
    mech_fixed = set()
    for f in flags:
        if f["kind"] == "heading_pagenum":
            stripped = re.sub(r"\s[0-9IilLoO]{1,4}$", "", f["text"])
            if stripped != f["text"] and corrected.count(f["text"]) == 1:
                corrected = corrected.replace(f["text"], stripped, 1)
                mech_fixed.add(f["text"])
                report["applied"].append({"old": f["text"], "new": stripped,
                                          "page": f["page"], "source": "mechanical"})
            elif stripped != f["text"]:
                # 全文多次出现时不自动替换（replace 会命中第一处，可能改错位置）
                report.setdefault("needs_review", []).append(
                    {"page": f["page"], "old": f["text"], "vlm": "", "judge": stripped,
                     "note": "mechanical strip but text occurs multiple times - manual"})

    # 待视觉复核：裁判判错（机械修复过的除外）
    targets = [j for j in judged if j["judge"].get("wrong") and j["text"] not in mech_fixed]
    print(f"[Stage B] 送视觉复核 {len(targets)} 处")

    if targets and not args.no_vlm:
        import fitz, tempfile, base64
        use_ollama = args.backend == "ollama"
        if use_ollama:
            model = proc = pvi = None
        else:
            model, proc, pvi = load_qwen2vl(args.qwen2vl)
        doc = fitz.open(args.pdf)
        tmp = tempfile.mkdtemp(prefix="verify_")
        skipped_nobox = 0
        for j in targets:
            if not j.get("bbox"):
                skipped_nobox += 1
                continue
            img = os.path.join(tmp, f"p{j['page']}_b{j['idx']}.png")
            try:
                render_region(doc, j["page"], j["bbox"], img)
                if use_ollama:
                    with open(img, "rb") as fh:
                        truth = ollama_chat(
                            args.ollama_url, args.ollama_vl_model,
                            f"这是图书《{title}》扫描页中的一个文字区域。请逐字转录图中文字，"
                            f"不要解释，不要加空格。按图中实际字形原样输出，"
                            f"不要把简体转换成繁体，也不要把繁体转换成简体。",
                            images_b64=[base64.b64encode(fh.read()).decode()]).strip()
                else:
                    truth = qwen2vl_read(model, proc, pvi, img, title)
            except Exception as e:
                truth = f"<error {e}>"
            norm = lambda s: re.sub(r"[\s，。,.、·:：;；\"'“”‘’《》<>!\[\]【】]", "", s)
            rec = {"page": j["page"], "suspicion": j["text"], "vlm_read": truth,
                   "judge_corrected": j["judge"].get("corrected", "")}
            report["vlm"].append(rec)
            differ = norm(truth) and norm(truth) != norm(j["text"])
            print(f"  p{j['page']} {j['text'][:26]!r} -> VLM {truth[:26]!r} {'⚠不同' if differ else ''}")
            # 自动修正铁律：裁判与 VLM 规范化后一致才落地；其余进 needs_review
            jc = (j["judge"].get("corrected") or "").strip()
            agree = jc and norm(jc) == norm(truth) and norm(jc) != norm(j["text"])
            if agree and 0.5 <= len(jc) / max(1, len(j["text"])) <= 2.0:
                if corrected.count(j["text"]) == 1:
                    corrected = corrected.replace(j["text"], jc, 1)
                    report["applied"].append({"old": j["text"], "new": jc, "page": j["page"],
                                              "source": "judge+vlm_agree"})
                else:
                    # 多处出现时不自动替换（避免命中错误位置）
                    report.setdefault("needs_review", []).append(
                        {"page": j["page"], "old": j["text"], "vlm": truth, "judge": jc,
                         "note": "judge+vlm agree but text not unique in md - manual"})
            elif differ:
                report.setdefault("needs_review", []).append(
                    {"page": j["page"], "old": j["text"], "vlm": truth, "judge": jc})
        doc.close()
        import shutil; shutil.rmtree(tmp, ignore_errors=True)
        if skipped_nobox:
            print(f"[!] {skipped_nobox} 处缺 bbox，已跳过视觉复核")
        if use_ollama:
            ollama_unload(args.ollama_url, args.ollama_vl_model)
        else:
            del model
            import torch; torch.cuda.empty_cache()

    open(args.out, "w", encoding="utf-8", newline="\n").write(corrected)
    json.dump(report, open(args.report, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"\n[Stage C] 已修正 {len(report['applied'])} 处 -> {args.out}\n  报告: {args.report}")

if __name__ == "__main__":
    main()
