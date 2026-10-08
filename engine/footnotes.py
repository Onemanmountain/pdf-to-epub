# -*- coding: utf-8 -*-
"""
footnotes.py — Phase 2.6 脚注规范化（v0.3.0）
纸书脚注按页从①重置，流式 EPUB 里多个①互相歧义、脚注块卡在页边界会截断段落。
本模块把 page_footnote 块与同页正文①②③标记按序配对，改写为 pandoc 原生 [^n] 脚注：
  - 全书连续编号，永不重置（彻底消歧）
  - pandoc 转 EPUB3 时自动生成标准 aside + epub:type=noteref（Kindle 弹窗脚注，可过 KFX）
  - 保守原则：某页 标记数≠注释数、或 md 定位不唯一 → 该页整页跳过记报告，绝不硬改

CLI: python -m engine.footnotes --mid <extracted> --md <in.md> --out <out.md> --report <json>
"""
import argparse
import json
import os
import re

MARKERS = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"
MARKER_RE = re.compile("([" + MARKERS + "])")
# page_footnote 块通常以圈号开头："① 大意，不是确切的翻译。"
FOOTNOTE_MARK_RE = re.compile("^\\s*([" + MARKERS + "])\\s*")


def norm(s):
    return re.sub(r"\s+", "", s or "")


def collect_page_footnotes(pages):
    """每页：正文圈号标记（含上下文）与 page_footnote 块（按块序）。
    返回 [(page, [(标记字符, 前文, 后文)...], [(注释字符, 注释全文)...]), ...]。"""
    out = []
    for p in sorted(pages):
        blocks = pages[p]
        marks, notes = [], []
        for b in blocks:
            if b["type"] == "page_footnote" and b["text"].strip():
                m = FOOTNOTE_MARK_RE.match(b["text"])
                notes.append((m.group(1) if m else "", b["text"].strip()))
            elif b["type"] in ("text", "paragraph_title"):
                t = b["text"]
                for mm in MARKER_RE.finditer(t):
                    s = mm.start()
                    marks.append((mm.group(1), t[max(0, s - 10):s], t[s + 1:s + 11]))
        if marks and notes:
            out.append((p, marks, notes))
    return out


def _flex(ctx):
    """把上下文字符串编译成容许任意空白穿插的 regex（md 换行可能切开块内文本）。"""
    return r"\s*".join(re.escape(ch) for ch in ctx if not ch.isspace())


def normalize_footnotes(pages, md_text, report):
    """主入口。返回新 md 文本。report dict 会被填充（converted/pages_skipped/details）。
    算法（单趟重建，无原地偏移簿记）：
      页按序处理；每页先定位全部注释行的 (start,end) 区间，
      输出 = 已处理前缀 + 替换过标记的正文窗口 + 定义块 + 注释区间之外的原文，
      游标前进到最后一条注释行尾。任何一步不满足唯一性/配对性 → 整页原样保留。"""
    n_counter, converted, skipped, details = 0, 0, [], []
    out, cursor = [], 0

    for page, marks, notes in collect_page_footnotes(pages):
        mark_chars = [m for m, _, _ in marks]
        if len(marks) != len(notes):
            skipped.append({"page": page, "reason": f"标记{len(marks)}个≠注释{len(notes)}条"})
            continue
        if all(c for c, _ in notes) and [c for c, _ in notes] != mark_chars[:len(notes)]:
            skipped.append({"page": page, "reason": "标记与注释圈号序列不一致"})
            continue

        # 从游标起依次定位各注释行（norm 匹配，行级）
        note_spans, ok, probe = [], True, cursor
        for c, ntext in notes:
            nn = norm(ntext)
            found, found_end = -1, -1
            pos = probe
            while pos < len(md_text):
                idx = md_text.find("\n", pos)
                line_end = idx if idx >= 0 else len(md_text)
                if nn and nn in norm(md_text[pos:line_end]):
                    found, found_end = pos, line_end
                    break
                if idx < 0:
                    break
                pos = idx + 1
            if found < 0:
                ok = False
                break
            note_spans.append((found, found_end))
            probe = found_end + 1
        if not ok:
            skipped.append({"page": page, "reason": "注释文本在 md 中定位失败"})
            continue

        window = md_text[cursor:note_spans[0][0]]
        # 上下文锚定：逐标记用「块内前后各 ~10 字」在窗口内唯一定位（与别处①枚举无关）
        page_pairs, tmp_n, mark_spans = [], n_counter, []
        ok = True
        for mk, before, after in marks:
            pat = _flex(before) + r"\s*(" + re.escape(mk) + r")\s*" + _flex(after)
            hits = list(re.finditer(pat, window))
            if len(hits) != 1:
                ok = False
                skipped.append({"page": page, "reason": f"标记{mk}上下文定位到{len(hits)}处（要求唯一）"})
                break
            h = hits[0]
            mark_spans.append((h.start(1), h.end(1)))
            tmp_n += 1
            page_pairs.append((mk, tmp_n))
        if not ok:
            continue

        # 按 span 从后往前替换标记（防位置前移）
        for (st, en), (mk, n) in sorted(zip(mark_spans, page_pairs), reverse=True):
            window = window[:st] + f"[^{n}]" + window[en:]
        defs = [(n, FOOTNOTE_MARK_RE.sub("", ntext).strip()) for (mk, n), (c, ntext) in zip(page_pairs, notes)]

        out.append(window)
        out.append("\n\n" + "\n\n".join(f"[^{n}]: {body}" for n, body in defs) + "\n\n")
        # 复制注释区间中除注释行以外的内容（防注释行间夹带正文被误删）
        seg_pos, last_end = note_spans[0][0], note_spans[-1][1]
        for st, en in note_spans:
            out.append(md_text[seg_pos:st])
            seg_pos = en
        out.append(md_text[seg_pos:last_end])
        cursor = last_end + 1
        n_counter = tmp_n
        converted += len(page_pairs)
        details.append({"page": page, "count": len(page_pairs), "to": [n for _, n in page_pairs]})

    out.append(md_text[cursor:])
    report["footnotes"] = {
        "converted": converted,
        "pages_skipped": skipped,
        "details": details,
    }
    print(f"[footnotes] 转换 {converted} 条（全书连续编号），跳过 {len(skipped)} 页（保守保持）")
    for s in skipped:
        print(f"  [skip] p{s['page']}: {s['reason']}")
    return "".join(out)


def main():
    from .structure import load_pages
    ap = argparse.ArgumentParser()
    ap.add_argument("--mid", required=True)
    ap.add_argument("--md", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--report", default=None)
    args = ap.parse_args()
    pages = load_pages(args.mid)
    md = open(args.md, encoding="utf-8").read()
    report = {}
    new_md = normalize_footnotes(pages, md, report)
    open(args.out, "w", encoding="utf-8", newline="\n").write(new_md)
    if args.report:
        json.dump(report, open(args.report, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print(f"[footnotes] 报告 -> {args.report}")


if __name__ == "__main__":
    main()
