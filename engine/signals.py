# -*- coding: utf-8 -*-
"""
signals.py — 从抽样页结构化描述中确定性提取结构信号，组装 book_profile。

设计动机（实测证据）：本地 8B 文本模型自由"立法"能力不足——给它 16 页优质描述，
它仍会把书名/内容高频词当成结构规律（内容过拟合）。改为：
  假设由数据聚合生成（证据驱动，可复核）→ LLM 只保留可选的仲裁/命名角色。
每条规则带 evidence_pages 与置信度；证据不足的信号直接不产生规则（宁缺毋滥）。
"""
from collections import Counter

FRONT_TYPES = {"封面", "版权页", "编者言", "目录", "插图页", "空白", "扉页", "前言", "序言", "跋"}


def guess_book_title(descs):
    titles = [(d.get("title_like_text") or "").strip() for d in descs]
    titles = [t for t in titles if t]
    return Counter(titles).most_common(1)[0][0] if titles else ""


def aggregate_signals(descs, book_title=None):
    """descs -> [boundary_rule]。每个信号都是一次独立的'假设+证据计数'。"""
    bt = book_title or guess_book_title(descs)
    rules = []

    # --- 信号 1：页眉即篇名（页眉方差且异于书名） ---
    hdr = [(d["idx"], (d.get("header_text") or "").strip()) for d in descs]
    non_book = sorted({h for _, h in hdr if h and h != bt})
    if bt and len(non_book) >= 2:
        ev = [i for i, h in hdr if h and h != bt]
        rules.append({
            "name": "页眉即篇名",
            "description": f"正文页页眉随篇章变化（抽样见 {len(non_book)} 个非书名页眉），"
                           f"页眉变化处即篇章边界；排除书名「{bt}」页眉",
            "target": "header_blocks",
            "match": {"block_type": "header", "exclude_text": [bt]},
            "signals_boundary": "article",
            "confidence": min(0.95, 0.55 + 0.1 * len(non_book)),
            "evidence_pages": ev[:8]})

    # --- 信号 2：引子段即篇界（作者生平段格式） ---
    bios = [d["idx"] for d in descs if (d.get("bio_like_text") or "").strip()]
    if len(bios) >= 2:
        rules.append({
            "name": "引子段即篇界",
            "description": f"抽样见 {len(bios)} 处作者生平式段落（姓名+生卒/籍贯+著有《》），其后紧跟新篇篇名与署名",
            "target": "text_blocks",
            "match": {"regex": r"^[一-龥]{2,4}(（\d{4}[—–-]\d{0,4}）|[12]\d{3}年生|，原名)"},
            "signals_boundary": "article",
            "confidence": min(0.9, 0.5 + 0.15 * len(bios)),
            "evidence_pages": bios[:8]})

    # --- 信号 3：篇名页版式（篇名页在抽样中重复出现） ---
    tps = [d["idx"] for d in descs if d.get("page_type") == "篇名页"]
    if len(tps) >= 2:
        rules.append({
            "name": "篇名页即篇界",
            "description": f"抽样见 {len(tps)} 个独立篇名页（大标题+署名版式），篇名页即新篇起点",
            "target": "title_blocks",
            "match": {"block_type": "doc_title", "adjacent_short_block": True},
            "signals_boundary": "article",
            "confidence": min(0.85, 0.5 + 0.15 * len(tps)),
            "evidence_pages": tps[:8]})

    return rules


def build_profile(descs, total, book_title=None):
    bt = book_title or guess_book_title(descs)
    rules = aggregate_signals(descs, bt)
    # 前记只计"正文开始前"的引导段（空白/插图页出现在书中段不算前记）
    first_body = min((d["idx"] for d in descs if d.get("page_type") == "正文"), default=total)
    fm = sorted(d["idx"] for d in descs if d.get("page_type") in FRONT_TYPES and d["idx"] < first_body)
    notes = "；".join(sorted({(d.get("font_notes") or "") for d in descs if d.get("font_notes")}))
    vertical = sorted(d["idx"] for d in descs if "竖排" in (d.get("font_notes") or ""))
    calli = sorted(d["idx"] for d in descs
                   if any(k in (d.get("font_notes") or "") for k in ("美术字", "手写", "书法")))
    kind = "文集选本" if any(r["signals_boundary"] == "article" for r in rules) else "单作者专著"
    return {
        "meta": {"title": bt, "book_kind": kind, "script": "zh-Hans"},
        "page_map": {"printed_page_offset": None, "front_matter_pages": fm},
        "boundary_rules": rules,
        "quirks": {"vertical_pages": vertical, "calligraphy_pages": calli, "notes": notes[:200]},
        "ocr_strategy": {"special_pages": vertical + calli, "note": "竖排/书法页建议人工或整页 VLM 复核"},
    }
