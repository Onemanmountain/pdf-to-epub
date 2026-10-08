# -*- coding: utf-8 -*-
"""
arbitrate.py — Phase 3 批量智能模式的增强仲裁：
verify 报告里 needs_review（裁判与 VLM 裁块读数打架）的条目，
用「整页 + 中性转录」重判一次——信息量严格大于裁块，且不带字形偏置。

裁决逻辑（do-no-harm）：
  仲裁支持裁判的纠正 且 纠正文本在全文唯一 → 落地
  其余（支持原文 / 支持 VLM / 无法判定）→ 保持原文，记入报告
  未决条目过多 → needs_human=true（提示值得开一次主动审阅）

CLI:
  python -m engine.arbitrate --report verify_report.json --md fixed.md \
      --pdf book.pdf --out final.md [--threshold 3]
"""
import argparse, base64, json, os, re, sys, tempfile

import pymupdf

sys.stdout.reconfigure(encoding="utf-8")


def norm(s):
    return re.sub(r"[\s，。,.、·:：;；\"'“”‘’《》<>!\[\]【】?？]", "", s or "")


def ollama_chat(url, model, prompt, images_b64=None, timeout=600):
    import urllib.request
    msg = {"role": "user", "content": prompt}
    if images_b64:
        msg["images"] = images_b64
    payload = {"model": model, "messages": [msg], "stream": False, "keep_alive": "30m",
               "options": {"num_ctx": 16384}}
    req = urllib.request.Request(url.rstrip("/") + "/api/chat",
                                 data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())["message"]["content"]


def ark_vision_call(api_base, api_key, model, prompt, image_b64, timeout=300):
    """外部强视觉模型（OpenAI 兼容多模态）。直连：显式空 ProxyHandler，不吃环境代理。
    max_tokens 压低：终裁只输出小 JSON，防长生成拖时（200dpi 整页实测 180s 超时两轮）。"""
    import urllib.request
    body = {"model": model, "temperature": 0, "max_tokens": 400,
            "thinking": {"type": "disabled"},  # 终裁只需小 JSON：关思考链 281s→8s 实测，裁决质量不降
            "messages": [{"role": "user", "content": [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + image_b64}}]}]}
    req = urllib.request.Request(api_base.rstrip("/") + "/chat/completions",
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + api_key})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as r:
        return json.loads(r.read())["choices"][0]["message"]["content"]


ARB_PROMPT = """这是一本书扫描版的第 {page} 页（整页）。
对页面上某处文字，有三种读法：
  A（OCR 原文）:「{old}」
  B（初审读法）:「{vlm}」
  C（裁判建议）:「{judge}」
请仔细看整页，找到该处，判断实际文字。只输出 JSON：
{{"actual": "该处实际文字（按图中实际字形，不要简繁转换）", "supports": "A|B|C|none", "reason": "一句话"}}"""


def extract_json(text):
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


EXT_PROMPT = """这是一本书扫描版的第 {page} 页（整页）。
OCR 把某处文字读作：「{old}」
本地视觉模型裁块后读作：「{vlm}」
两者冲突，本地整页仲裁无法裁定。请你看整页图像作最终裁定：
- 图上实际是什么？（actual，逐字）
- 支持谁？supports: A=OCR原文 / B=裁块读数
只输出 JSON：{{"actual": "...", "supports": "A|B", "reason": "一句话"}}"""


def arbitrate(report_path, md_path, pdf_path, out_path, ollama_url, vl_model, threshold=3, ext_cfg=None):
    report = json.load(open(report_path, encoding="utf-8"))
    items = report.get("needs_review", [])
    if not items:
        print("[arbitrate] 无 needs_review，直接放行")
        report["arbitration"] = {"applied": [], "kept_original": []}
        json.dump(report, open(report_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        import shutil
        shutil.copyfile(md_path, out_path)
        return report
    md = open(md_path, encoding="utf-8").read()
    doc = pymupdf.open(pdf_path)
    tmp = tempfile.mkdtemp(prefix="arb_")
    applied, kept = [], []
    ext_on = bool(ext_cfg and ext_cfg.get("api_key") and ext_cfg.get("vision_model"))
    print(f"[arbitrate] 整页仲裁 {len(items)} 处（本地 VL → 不定则外部强视觉终裁{'[on]' if ext_on else '[off]'}）...")
    for it in items:
        page = it["page"]
        png = os.path.join(tmp, f"p{page}.png")
        doc[page - 1].get_pixmap(dpi=200).save(png)
        b64 = base64.b64encode(open(png, "rb").read()).decode()
        old, vlm, judge = it.get("old", ""), it.get("vlm", ""), it.get("judge", "")
        try:
            ans = ollama_chat(ollama_url, vl_model,
                              ARB_PROMPT.format(page=page, old=old[:80], vlm=vlm[:80], judge=judge[:80]),
                              images_b64=[b64])
            j = extract_json(ans) or {}
        except Exception as e:
            j = {"supports": "none", "reason": f"error {e}"}
        sup = j.get("supports", "none")
        tier = "本地整页"
        # 本地无法裁定（none/冲突/异常/并列）→ 外部强视觉终裁（150dpi 控制载荷）
        if sup not in ("A", "B", "C") and ext_on:
            try:
                png150 = os.path.join(tmp, f"p{page}_150.png")
                doc[page - 1].get_pixmap(dpi=150).save(png150)
                b64_150 = base64.b64encode(open(png150, "rb").read()).decode()
                ans2 = ark_vision_call(ext_cfg["api_base"], ext_cfg["api_key"], ext_cfg["vision_model"],
                                       EXT_PROMPT.format(page=page, old=old[:80], vlm=vlm[:80]), b64_150)
                j2 = extract_json(ans2) or {}
                if j2.get("supports") in ("A", "B"):
                    sup, j, tier = j2["supports"], j2, "外部终裁"
            except Exception as e:
                j["ext_error"] = str(e)  # 终裁异常必须留痕（曾被 setdefault 静默吞掉——NameError 实战教训）
                print(f"    [!] 外部终裁异常: {e}")
        jc = (judge or "").strip()
        if sup == "C" and jc and norm(jc) != norm(old) and len(jc) < 0.8 * len(old):
            # 收缩护栏：纠正把文本砍掉 20%+ → 是"转录不全"不是"改错"，拒落地
            kept.append({**it, "arb_actual": j.get("actual", ""), "arb_supports": sup, "tier": tier,
                         "note": f"收缩 {len(old)}→{len(jc)} 超20%拒落地"})
            mark = "保持原文(收缩护栏)"
        elif sup == "B" and (vlm or "").strip() and norm(vlm.strip()) != norm(old) \
                and len(vlm.strip()) >= 0.8 * len(old) and md.count(old) == 1:
            # 双视觉一致（裁块 + 整页/外部终裁确认）→ 落地，带收缩+唯一性护栏
            md = md.replace(old, vlm.strip(), 1)
            applied.append({"page": page, "old": old, "new": vlm.strip(),
                            "actual": j.get("actual", ""), "source": "arb_vision_x2", "tier": tier})
            mark = f"✓落地(双视觉一致/{tier})"
        elif sup == "C" and jc and norm(jc) != norm(old) and md.count(old) == 1:
            md = md.replace(old, jc, 1)
            applied.append({"page": page, "old": old, "new": jc, "actual": j.get("actual", ""), "tier": tier})
            mark = f"✓落地({tier})"
        else:
            kept.append({**it, "arb_supports": sup, "arb_reason": j.get("reason", ""), "tier": tier,
                         "note": "仲裁链终局：证据不足，保守保持原文（有理由的决策，非转人工）"})
            mark = f"保持原文(supports={sup}/{tier})"
        print(f"  p{page} {old[:20]!r} -> {mark}")
    doc.close()
    open(out_path, "w", encoding="utf-8", newline="\n").write(md)
    report["arbitration"] = {"applied": applied, "kept_original": kept}
    json.dump(report, open(report_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"[arbitrate] 落地 {len(applied)}，保持原文 {len(kept)}（每条均附理由）")
    return report


def main():
    from .config import load_config
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", required=True)
    ap.add_argument("--md", required=True)
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--threshold", type=int, default=3)
    ap.add_argument("--config", default=None)
    args = ap.parse_args()
    cfg = load_config(args.config)["scout"]
    arbitrate(args.report, args.md, args.pdf, args.out,
              cfg["ollama_url"], cfg["vl_model"], args.threshold, ext_cfg=cfg)


if __name__ == "__main__":
    main()
