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
# page_footnote 块通常以圈号开头："① 大意，不是确切的翻译。"；星号译注："* 应为 1964 年——译注。"
FOOTNOTE_MARK_RE = re.compile("^\\s*([" + MARKERS + "*])\\s*")
# 星号译注的正文标记：句末标点后紧跟的孤立 *（排除 ** 强调语法）。
# 注：本书另有「字*。」标前式星标（p118/131/134/175），其 md 侧标记形态残缺，
# 位置唯一性无法保证——宁可不配对（HTML 注释行原样保留渲染），也不错配。
STAR_MARK_RE = re.compile(r"(?<=[。！？!?”’\"』」）)])\*(?!\*)")


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
                t = re.sub(r"\$?\^\{([" + MARKERS + "*])\}\$?", r"\1", b["text"])  # $^{①}$ 上标壳剥除
                for mm in MARKER_RE.finditer(t):
                    s = mm.start()
                    marks.append((mm.group(1), t[max(0, s - 10):s], t[s + 1:s + 11]))
                # v0.3.2：星号译注的标记（句末标点后孤立 *）
                for mm in STAR_MARK_RE.finditer(t):
                    s = mm.start()
                    marks.append(("*", t[max(0, s - 10):s], t[s + 1:s + 11]))
        if marks and notes:
            out.append((p, marks, notes))
    return out


def _flex(ctx):
    """把上下文字符串编译成容许任意空白穿插的 regex（md 换行可能切开块内文本）。"""
    return r"\s*".join(re.escape(ch) for ch in ctx if not ch.isspace())


def normalize_footnotes(pages, md_text, report):
    """主入口。返回新 md 文本。report dict 会被填充（converted/pages_skipped/details）。
    v0.3.2 重写：放弃全局游标——MinerU 的 md 里页脚注释行不一定紧跟本页正文
    （实测 p69 注释行排在 p70 正文之后），游标单调假设让后续页标记定位全灭。
    改为每页独立配对：标记在全文范围做上下文唯一性定位，注释从最后一个标记之后
    按行 norm 子串匹配（兼容 <small><span> HTML 包装行——行级删除天然剥掉标签）。
    全部操作用 span 收集，最后一次重建（无偏移簿记）。
    标记形态预处理：^{①} 上标壳剥除（MinerU 上标写法）；句末标点后的 \* 转义还原。"""
    md_text = re.sub(r"\$?\^\{([" + MARKERS + "*])\}\$?", r"\1", md_text)
    md_text = re.sub(r"(?<=[。！？!?”’\"』」）)])\\\*", "*", md_text)
    n_counter, converted, skipped, details = 0, 0, [], []
    ops = []  # (start, end, replacement)

    for page, marks, notes in collect_page_footnotes(pages):
        mark_chars = [m for m, _, _ in marks]
        # 最大可配对前缀：标记与注释的圈号序列从头对齐，能配几对配几对
        # （混合页常见 ①②+* 形态：圈号照常转换，多出的译注保持原样渲染）
        k = 0
        while k < len(marks) and k < len(notes) and \
                (not notes[k][0] or notes[k][0] == mark_chars[k]):
            k += 1
        if k == 0:
            skipped.append({"page": page, "reason": "标记与注释序列无法对齐"})
            continue
        if k < len(marks) or k < len(notes):
            details.append({"page": page, "note": f"部分配对 {k}/{max(len(marks), len(notes))}"})
        marks, notes = marks[:k], notes[:k]

        # 标记定位：全文范围上下文锚定（前后各 ~10 字，天然唯一；不依赖任何游标窗口）
        page_ok, mark_spans = True, []
        for mk, before, after in marks:
            pat = _flex(before) + r"\s*(" + re.escape(mk) + r")\s*" + _flex(after)
            hits = list(re.finditer(pat, md_text))
            if len(hits) != 1:
                page_ok = False
                skipped.append({"page": page, "reason": f"标记{mk}上下文定位到{len(hits)}处（要求唯一）"})
                break
            mark_spans.append((hits[0].start(1), hits[0].end(1)))
        if not page_ok:
            continue

        # 注释定位：从最后一个标记之后按行找（norm 子串）
        probe = mark_spans[-1][1]
        note_spans = []
        for c, ntext in notes:
            nn = norm(ntext)
            found, found_end, pos = -1, -1, probe
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
                page_ok = False
                skipped.append({"page": page, "reason": "注释文本在 md 中定位失败"})
                break
            note_spans.append((found, found_end))
            probe = found_end + 1
        if not page_ok:
            continue

        # 生成操作：标记→[^n]；首条注释行→定义块，其余注释行→删除
        base = n_counter + 1
        for k, (st, en) in enumerate(mark_spans):
            ops.append((st, en, f"[^{base + k}]"))
        defs = [(base + k, FOOTNOTE_MARK_RE.sub("", nt).strip()) for k, (c, nt) in enumerate(notes)]
        def_block = "\n\n" + "\n\n".join(f"[^{n}]: {body}" for n, body in defs) + "\n\n"
        for k, (ns, ne) in enumerate(note_spans):
            ops.append((ns, ne, def_block if k == 0 else ""))
        n_counter += len(mark_spans)
        converted += len(mark_spans)
        details.append({"page": page, "count": len(mark_spans), "to": [n for n, _ in defs]})

    out = md_text
    for st, en, rep in sorted(ops, key=lambda x: -x[0]):
        out = out[:st] + rep + out[en:]

    report["footnotes"] = {
        "converted": converted,
        "pages_skipped": skipped,
        "details": details,
    }
    print(f"[footnotes] 转换 {converted} 条（全书连续编号），跳过 {len(skipped)} 页（保守保持）")
    for s in skipped:
        print(f"  [skip] p{s['page']}: {s['reason']}")
    return out


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
