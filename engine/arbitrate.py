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


def arbitrate(report_path, md_path, pdf_path, out_path, ollama_url, vl_model, threshold=3):
    report = json.load(open(report_path, encoding="utf-8"))
    items = report.get("needs_review", [])
    if not items:
        print("[arbitrate] 无 needs_review，直接放行")
        report["arbitration"] = []
        report["needs_human"] = False
        json.dump(report, open(report_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        import shutil
        shutil.copyfile(md_path, out_path)
        return report
    md = open(md_path, encoding="utf-8").read()
    doc = pymupdf.open(pdf_path)
    tmp = tempfile.mkdtemp(prefix="arb_")
    applied, unresolved = [], []
    print(f"[arbitrate] 整页仲裁 {len(items)} 处（信息增量：整页+上下文）...")
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
        jc = (judge or "").strip()
        if sup == "C" and jc and norm(jc) != norm(old) and len(jc) < 0.8 * len(old):
            # 收缩护栏：纠正把文本砍掉 20%+ → 是"转录不全"不是"改错"，拒落地
            # （长段落天然过唯一性检查，曾致 3 处整段被截断替换——《漫长的革命》实测事故）
            unresolved.append({**it, "arb_actual": j.get("actual", ""), "arb_supports": sup,
                               "note": f"收缩 {len(old)}→{len(jc)} 超20%拒落地"})
            mark = "保持原文(收缩护栏)"
        elif sup == "B" and (vlm or "").strip() and norm(vlm.strip()) != norm(old) \
                and len(vlm.strip()) >= 0.8 * len(old) and md.count(old) == 1:
            # v0.3.0：整页仲裁支持裁块读数 → 两次独立视觉读数一致（新双确认），带收缩+唯一性护栏
            md = md.replace(old, vlm.strip(), 1)
            applied.append({"page": page, "old": old, "new": vlm.strip(),
                            "actual": j.get("actual", ""), "source": "arb_vision_x2"})
            mark = "✓落地(双视觉一致)"
        elif sup == "C" and jc and norm(jc) != norm(old) and md.count(old) == 1:
            md = md.replace(old, jc, 1)
            applied.append({"page": page, "old": old, "new": jc, "actual": j.get("actual", "")})
            mark = "✓落地"
        else:
            unresolved.append({**it, "arb_supports": sup, "arb_reason": j.get("reason", "")})
            mark = f"保持原文(supports={sup})"
        print(f"  p{page} {old[:20]!r} -> {mark}")
    doc.close()
    open(out_path, "w", encoding="utf-8", newline="\n").write(md)
    report["arbitration"] = {"applied": applied, "unresolved": unresolved}
    report["needs_human"] = len(unresolved) > threshold
    json.dump(report, open(report_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"[arbitrate] 落地 {len(applied)}，保持原文 {len(unresolved)}，needs_human={report['needs_human']}")
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
              cfg["ollama_url"], cfg["vl_model"], args.threshold)


if __name__ == "__main__":
    main()
