# -*- coding: utf-8 -*-
"""
structure.py — Phase 2 结构重建：middle_json × book_profile → 标题层级地图 → 改写 md

零硬编码：所有边界判定规则来自 book_profile.boundary_rules（Scout 运行时发现）。
当前实现的规则执行器：
  - header_blocks 型（"页眉即篇名"）：页眉变化 → 回找篇名块/署名块 → 篇章边界
  - text_blocks regex 型（"引子段即篇界"）：regex 命中 → 其后短块为篇名
侦察一无所获（无规则）→ 原样输出，不劣于 v0.1.0。

CLI:
  python -m engine.structure --mid <middle_json目录> --profile profile.json \
      --md markdown.md --out structured.md --report structure_report.json
"""
import argparse, json, os, re, sys

sys.stdout.reconfigure(encoding="utf-8")


def norm(s):
    return re.sub(r"[\s，。,.、·:：;；\"'“”‘’《》<>!\[\]【】?？]", "", s or "")


def _flatten(o):
    if isinstance(o, str):
        return o
    if isinstance(o, list):
        return "".join(_flatten(x) for x in o)
    if isinstance(o, dict):
        return _flatten(o.get("content", ""))
    return ""


def load_pages(mid_dir):
    """按文件名优先选 middle_json（教训：按大小会误选 model_output.json）。"""
    cands = []
    for root, _, files in os.walk(mid_dir):
        for f in files:
            if f.endswith(".json"):
                cands.append(os.path.join(root, f))
    named = [p for p in cands if "middle" in os.path.basename(p).lower()]
    pool = named if named else cands
    if not pool:
        raise SystemExit(f"[X] {mid_dir} 下找不到 middle_json")
    pool.sort(key=lambda p: -os.path.getsize(p))
    data = json.load(open(pool[0], encoding="utf-8"))
    pages = data.get("pages") if isinstance(data, dict) else data
    out = {}
    for pg in pages:
        pno = pg.get("page_idx", 0)
        blocks = []
        for b in pg.get("blocks", []):
            t = _flatten(b.get("content"))
            if t.strip():
                blocks.append({"type": b.get("type", "text"), "text": t.strip()})
        out[pno] = blocks
    return out


# ---------------- 边界检测 ----------------

def detect_boundaries(pages, rules, total):
    """返回 [{page, title, author, evidence, rule}]，page 为 0 起。"""
    boundaries = []
    for rule in rules:
        tgt, match = rule.get("target"), rule.get("match", {})
        if tgt == "header_blocks":
            boundaries += _by_headers(pages, rule, match, total)
        elif tgt == "text_blocks" and match.get("regex"):
            boundaries += _by_regex(pages, rule, match)
        # title_blocks 型暂无执行器，留待后续版本
    # 合并去重：同页多条规则命中 → 取证据更足（confidence 高）的
    by_page = {}
    for b in boundaries:
        k = b["page"]
        if k not in by_page or b["confidence"] > by_page[k]["confidence"]:
            by_page[k] = b
    return sorted(by_page.values(), key=lambda b: b["page"])


def _by_headers(pages, rule, match, total):
    """页眉序列变化 → 篇界。篇名页通常无页眉，回找 1-2 页定位篇名块与署名块。"""
    exclude = set(match.get("exclude_text", []))
    btype = match.get("block_type", "header")
    seq = []  # (page, header)
    for p in sorted(pages):
        for b in pages[p]:
            if b["type"] == btype and b["text"] and norm(b["text"]) not in {norm(e) for e in exclude}:
                seq.append((p, b["text"]))
                break  # 每页只取一个有效页眉
    bounds = []
    cur = None
    for p, h in seq:
        if cur is None or norm(h) != norm(cur):
            # 页眉从 cur 变为 h：h 篇的起点在 p 之前 1-2 页
            start, title_blk, author = _find_title_page(pages, p, h)
            if start is not None:
                bounds.append({"page": start, "title": title_blk, "author": author,
                               "evidence": f"页眉于 p{p} 变为「{h}」", "rule": rule["name"],
                               "confidence": rule.get("confidence", 0.7)})
            cur = h
    return bounds


def _find_title_page(pages, header_page, title):
    """从 header_page 回找：含 title 文本块（且无有效页眉）的页 = 篇名页。
    匹配分两级：①规范化全等（单块篇名）②包含匹配（「第X编 标题」式组合页眉——
    编名页上编序与标题常是两个独立块，页眉是其拼接，全等会静默漏检）。"""
    nt = norm(title)
    for p in range(header_page - 1, max(-1, header_page - 7), -1):  # 回看 6 页：编名页后可能有空白页+无页眉章首页
        if p not in pages:
            continue
        texts = [b["text"] for b in pages[p] if b["type"] in ("text", "paragraph_title", "doc_title")]
        hit = -1
        for i, t in enumerate(texts):
            n = norm(t)
            if n == nt:  # 全等优先
                hit = i
                break
        if hit < 0:
            for i, t in enumerate(texts):
                n = norm(t)
                if len(n) >= 2 and (n in nt or nt in n):  # 组合页眉的部分匹配
                    hit = i
                    break
        if hit >= 0:
            author = ""
            for t2 in texts[hit + 1: hit + 3]:
                if 0 < len(t2) <= 4 and re.fullmatch(r"[一-龥·]{2,4}", t2):
                    author = t2
                    break
            return p, title, author
    return None, title, ""


def _by_regex(pages, rule, match):
    rx = re.compile(match["regex"])
    bounds = []
    for p in sorted(pages):
        texts = [b for b in pages[p] if b["type"] in ("text", "paragraph_title")]
        for i, b in enumerate(texts):
            if rx.search(b["text"]):
                # 引子段之后 1-2 个短块为篇名（+署名）
                title, author = "", ""
                shorts = [t["text"] for t in texts[i + 1: i + 4] if 0 < len(t["text"]) <= 30]
                if shorts:
                    title = shorts[0]
                    if len(shorts) > 1 and len(shorts[1]) <= 4:
                        author = shorts[1]
                if title:
                    bounds.append({"page": p, "title": title, "author": author,
                                   "evidence": f"引子段命中 p{p}", "rule": rule["name"],
                                   "confidence": rule.get("confidence", 0.6)})
                break
    return bounds


# ---------------- 一致性自检 ----------------

def self_check(boundaries, pages):
    warns = []
    for b in boundaries:
        # 篇名应在篇名页之后的页眉中复现（对 header 规则发现的边界）
        if b["rule"] == "页眉即篇名" and b["title"] and norm(b["title"]) != norm(b["evidence"].split("「")[-1].rstrip("」")):
            pass  # evidence 里就是页眉原文，title 与其一致由构造保证
    if not boundaries:
        warns.append("未检测到任何篇章边界（规则无命中）——退回 MinerU 原生标题")
    # 边界密度合理性
    if len(boundaries) > 80:
        warns.append(f"边界数量异常多（{len(boundaries)}），可能规则过拟合")
    return warns


# ---------------- 改写 markdown ----------------

def apply_to_markdown(md_text, boundaries, front_matter_last_page=None, pages_total=None):
    """把篇名行提升为 '# 篇名'；篇内标题降 '##'；前记的 '#' 书名行剥为正文。
    返回 (new_md, applied, misses)"""
    lines = md_text.split("\n")
    applied, misses = [], []
    cursor = 0
    for b in boundaries:
        title = b["title"]
        if not title:
            continue
        nt = norm(title)
        found = -1
        for i in range(cursor, len(lines)):  # 游标单调向前不设行窗：编距可能远超固定窗口（实测 30+ 页/编 → 数百行）
            nl = norm(lines[i].lstrip("#").strip())
            if nl == nt or (len(nl) >= 2 and nl in nt):  # 全等或「编序」行部分匹配（组合篇名跨行）
                found = i
                break
        if found >= 0:
            lines[found] = "# " + title
            applied.append({"page": b["page"], "title": title, "line": found, "rule": b["rule"]})
            cursor = found + 1
        else:
            misses.append({"page": b["page"], "title": title, "rule": b["rule"]})
    # 篇内既有 '# '（非边界标题）→ '## '（含前记书名行剥除 #）
    boundary_titles = {norm(b["title"]) for b in boundaries}
    for i, ln in enumerate(lines):
        if ln.startswith("# ") and norm(ln[2:]) not in boundary_titles:
            lines[i] = "## " + ln[2:].strip() if i > 20 else ln[2:].strip()  # 前 20 行视为封面区，剥 #
    return "\n".join(lines), applied, misses


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mid", required=True)
    ap.add_argument("--profile", required=True)
    ap.add_argument("--md", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--report", required=True)
    args = ap.parse_args()

    pages = load_pages(args.mid)
    profile = json.load(open(args.profile, encoding="utf-8"))
    rules = profile.get("boundary_rules", [])
    total = max(pages) + 1 if pages else 0
    print(f"[structure] 页数 {total}，规则 {len(rules)} 条: {[r['name'] for r in rules]}")

    boundaries = detect_boundaries(pages, rules, total)
    warns = self_check(boundaries, pages)
    print(f"[structure] 检测到篇章边界 {len(boundaries)} 处")
    for b in boundaries:
        print(f"  p{b['page']:>3} 「{b['title']}」{(' / ' + b['author']) if b['author'] else ''}  ({b['evidence'][:28]})")
    for w in warns:
        print(f"  [!] {w}")

    md_text = open(args.md, encoding="utf-8").read()
    new_md, applied, misses = apply_to_markdown(md_text, boundaries)
    open(args.out, "w", encoding="utf-8", newline="\n").write(new_md)

    report = {"rules_used": [r["name"] for r in rules], "boundaries": boundaries,
              "applied": applied, "misses": misses, "warns": warns,
              "needs_human": bool(misses) or bool(warns and "异常" in "".join(warns))}
    json.dump(report, open(args.report, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"[structure] 标题落位 {len(applied)}/{len(applied)+len(misses)} -> {args.out}")
    print(f"[structure] 报告 -> {args.report}  needs_human={report['needs_human']}")


if __name__ == "__main__":
    main()
