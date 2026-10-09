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


# ---------------- 结构调和（v0.3.0：唯一章节树权威）----------------
CHAPTER_RE = re.compile(r"^\d{1,2}[.、]\s*\S{2,30}$")

# v0.3.1 修复六：序数形态家族。同族（"其N"/"第N"）被字体投票 ≥2 次 → 族合法化，
# 此后整行同形态文本一律提升为标题（其一 不再因文本型块沉底）。
ORD_FAMILIES = [("其", re.compile(r"^其[一二三四五六七八九十]{1,2}$")),
                ("第", re.compile(r"^第[一二三四五六七八九十]{1,3}$"))]

def legit_ordinal_families(pages):
    from collections import Counter
    votes = Counter()
    for p in sorted(pages):
        for b in pages[p]:
            if b["type"] != "paragraph_title":
                continue
            t = re.sub(r"\s+", "", b["text"].strip())
            for fam, pat in ORD_FAMILIES:
                if pat.match(t):
                    votes[fam] += 1
    return {fam for fam, c in votes.items() if c >= 2}

def aggregate_numbered_chapters(pages):
    """编号章题模式聚合：全书范围内 ^数字. 标题 型块出现 ≥3 次 → 模式成立，全部合法化为二级标题。
    证据驱动：不预设书有编号章；模式由数据自己说话。"""
    cands = {}
    for p in sorted(pages):
        for b in pages[p]:
            if b["type"] in ("text", "paragraph_title") and b["text"]:
                t = re.sub(r"\s+", "", b["text"].strip())  # 空白不敏感（OCR 会插入空格：19. 起 点）
                if CHAPTER_RE.match(t):
                    cands[norm(t)] = t
    return set(cands.keys()) if len(cands) >= 3 else set()

def toc_line_range(md_lines, boundaries, applied):
    """目录区行范围：『目录』标题行 → 其后第一个落位边界行。
    目录可以是边界（漫长），也可以只是字体投票标题（艺术：目录不在篇目序列里）——
    两种证据都接受。终点取落位记录中最近的下一个（幻影标题不在落位记录里，天然免疫）。"""
    toc_norm = {norm(b["title"]) for b in boundaries if "目录" in b["title"]}
    start = None
    for i, ln in enumerate(md_lines):
        if not ln.lstrip().startswith("#"):
            continue
        nt = norm(re.sub(r"^#+\s*", "", ln))
        if (toc_norm and nt in toc_norm) or (not toc_norm and "目录" in nt):
            start = i
            break
    if start is None:
        return None
    later = [a["line"] for a in applied if a["line"] > start]
    end = min(later) if later else len(md_lines)
    return (start, end)

def reconcile_headings(md_text, boundaries, pages, applied):
    """调和器：四方证据 → 唯一权威 → 现存标题行逐个审判。
    判决：# = 边界佐证且位置就是落位行（同标题的其他标题行=目录页幻影/重复，降级）；
    ## = 编号章题/目录条目佐证；### = 仅字体投票（保留强调，toc-depth=2 下不入大纲）；
    目录区内标题行 → 降为正文。返回 (新md, 降级清单, 保留清单)。"""
    justified_l1 = {norm(b["title"]) for b in boundaries if b.get("title")}
    l1_line = {}
    for a in applied:  # 同名篇允许：落位行号集合（单值映射会把同名篇的第一个落位误杀——实测教训）
        l1_line.setdefault(norm(a["title"]), set()).add(a["line"])
    justified_l2 = aggregate_numbered_chapters(pages)
    legit_fams = legit_ordinal_families(pages)
    lines = md_text.split("\n")
    toc_range = toc_line_range(lines, boundaries, applied)
    if toc_range:
        for ln in lines[toc_range[0]:toc_range[1]]:
            t = re.sub(r"^[-*+]\s*", "", ln.strip())
            t = re.sub(r"^#+\s*", "", t)
            if CHAPTER_RE.match(re.sub(r"\s+", "", t)):
                justified_l2.add(norm(t))
    demoted, keptn = [], []
    for i, ln in enumerate(lines):
        if not ln.lstrip().startswith("#"):
            # v0.3.1 修复六：序数形态提升——整行就是已合法化的序数形态（短行、非特殊行）
            t = re.sub(r"\s+", "", ln.strip())
            if (ln.strip() and len(t) <= 6
                    and not ln.strip().startswith(("- ", "* ", "+ ", "[^", "!"))
                    and not (toc_range and toc_range[0] <= i < toc_range[1])
                    and any(pat.match(t) for fam, pat in ORD_FAMILIES if fam in legit_fams)):
                lines[i] = "### " + ln.strip()
                keptn.append({"line": i + 1, "title": ln.strip(), "level": 3,
                              "reason": "序数形态家族合法化提升"})
            continue
        title = re.sub(r"^#+\s*", "", ln).strip()
        nt = norm(title)
        if not title:
            continue
        if toc_range and toc_range[0] < i < toc_range[1]:
            lines[i] = title
            demoted.append({"line": i + 1, "title": title, "reason": "目录区条目非标题"})
        elif nt in justified_l1 and i in l1_line.get(nt, set()):
            lines[i] = "# " + title
            keptn.append({"line": i + 1, "title": title, "level": 1})
        elif nt in justified_l2:
            lines[i] = "## " + title
            keptn.append({"line": i + 1, "title": title, "level": 2})
        elif nt in justified_l1:
            # 标题匹配边界但位置非落位行 → 目录页幻影/重复
            lines[i] = title
            demoted.append({"line": i + 1, "title": title, "reason": "边界标题的非落位重复（幻影）"})
        else:
            lines[i] = "### " + title
            keptn.append({"line": i + 1, "title": title, "level": 3,
                          "reason": "仅字体投票：保留强调不入大纲"})
    # v0.3.1 修复三：目录区条目统一为列表格式（MinerU 对跨页目录会给两种排版）
    if toc_range:
        for i in range(toc_range[0] + 1, toc_range[1]):
            s = lines[i].strip()
            if s and not s.startswith(("- ", "* ", "+ ", "#")):
                lines[i] = "- " + s
        # v0.3.2 问题3：目录区标题恒为一级（目录是书籍结构性独立部分；艺术书原为 ### 会归并入前言麾下）
        ts = lines[toc_range[0]]
        if ts.lstrip().startswith("#"):
            tt = re.sub(r"^#+\s*", "", ts).strip()
            lines[toc_range[0]] = "# " + tt
        # v0.3.2 问题5b：目录条目补全边界标题——条目剥页码后以边界标题为前缀且更长 → 取全式
        toc_entries = []
        for i in range(toc_range[0] + 1, toc_range[1]):
            e = re.sub(r"^[-*+]\s*", "", lines[i].strip())
            e = re.sub(r"\s*[-—–….·]*\s*\d{1,4}\s*$", "", e).strip()  # 剥页码尾缀
            if e:
                toc_entries.append(e)
        for a in applied:
            ln_idx = a["line"]
            if ln_idx >= len(lines) or not lines[ln_idx].lstrip().startswith("#"):
                continue
            cur = norm(re.sub(r"^#+\s*", "", lines[ln_idx]))
            for e in toc_entries:
                ne = norm(e)
                if ne.startswith(cur) and len(ne) > len(cur) + 1:  # 前缀且显著更长
                    lines[ln_idx] = "# " + e
                    a["title_completed"] = e
                    break
    # v0.3.2 问题4：组合篇名残留块从降级改为删除——落位行后第一个非空行的 norm
    # 是（补全后）标题 norm 的真子串 → 其内容已被组合标题完整覆盖，原书该页并无重复文字。
    # 保护清单：非紧邻行、非子串内容一律不动（附录一内"西藏和神"等真实小标题安全）。
    applied_lines = {a["line"]: (a.get("title_completed") or a["title"]) for a in applied}
    for ln_idx, ttl in applied_lines.items():
        nt = norm(ttl)
        j = ln_idx + 1
        while j < min(ln_idx + 6, len(lines)):  # 连续吸收：组合标题跨多块时残留是多行
            s = lines[j].strip()
            if not s:
                j += 1
                continue  # 空行跳过
            cand = norm(re.sub(r"^#+\s*", "", s))
            if len(cand) >= 2 and cand in nt and cand != nt:
                demoted.append({"line": j + 1, "title": s, "reason": "组合篇名残留块删除"})
                lines[j] = ""
                j += 1
                continue
            break  # 遇到非子串行即停（保护"西藏和神"等真实小标题）
    return "\n".join(lines), demoted, keptn

# ---------------- Phase 3.7 后处理（v0.3.1 修复二/五：页眉剥离 + 中文重排）----------------
def detect_running_heads(pages, min_pages=3):
    """页眉检测（证据驱动）：同一字符串出现在 ≥min_pages 个不同页的首块或尾块 → 页眉。
    返回 {norm串: 原始串}。"""
    from collections import Counter
    top, bot, raw = Counter(), Counter(), {}
    for p in sorted(pages):
        bl = [b for b in pages[p] if b["text"].strip()]
        if not bl:
            continue
        for cnt, blk in ((top, bl[0]), (bot, bl[-1])):
            key = norm(blk["text"])
            cnt[key] += 1
            raw.setdefault(key, blk["text"].strip())
    return {k: raw[k] for k in set(top) | set(bot) if top[k] + bot[k] >= min_pages and len(k) >= 2}

SENT_END = set("。！？!?:：;；…””’」》》）)]")
_CJK = lambda ch: "\u4e00" <= ch <= "\u9fff"
_SPECIAL = ("#", "- ", "* ", "+ ", "[^", "!", "<", "|")
_FOOTMARK = re.compile(r"^[①-⑳]")

def _plain(s):
    return s and not s.startswith(_SPECIAL) and not _FOOTMARK.match(s)

def build_cross_page_pairs(pages, head_keys):
    """跨页断点证据集：页 p 最后一个正文块（非页眉、非脚注/页码）原文结尾无句末标点，
    且页 p+1 有首块 → 记 (尾块末20字norm, 首块首20字norm)。这就是"同一段话被页边界
    截断"的充要语义证据——同页内的块间空行是 MinerU 判定的段落边界，一律不动。"""
    body_types = ("text", "paragraph_title", "doc_title")
    ordered = []
    for p in sorted(pages):
        blks = [b for b in pages[p]
                if b["type"] in body_types and b["text"].strip()
                and norm(b["text"]) not in head_keys]
        if blks:
            ordered.append((p, blks[0]["text"].strip(), blks[-1]["text"].strip()))
    pairs = set()
    for (p, _, last), (p2, first2, _) in zip(ordered, ordered[1:]):
        if p2 == p + 1 and last.rstrip()[-1:] not in SENT_END:
            pairs.add((norm(last)[-20:], norm(first2)[:20]))
    return pairs

def reflow_paragraphs(md_text, cross_pairs=frozenset()):
    """中文重排：①段内视觉换行并接（md 语义：无空行连续行本属同段）；
    ②跨段合并——仅当相邻两段落在跨页断点证据集中（页尾块无句末标点+页首块）。
    圈号行/标题/列表/图片/脚注定义一律不动。返回 (新md, 并接次数)。"""
    lines = md_text.split("\n")
    out, buf, joins = [], None, 0
    def flush():
        nonlocal buf
        if buf is not None:
            out.append(buf); buf = None
    for ln in lines:
        s = ln.strip()
        if not _plain(s):
            flush(); out.append(ln); continue
        if buf is None:
            buf = s
        else:
            joiner = "" if _CJK(buf[-1]) and _CJK(s[0]) else " "
            buf += joiner + s; joins += 1
    flush()
    res = []
    for ln in out:
        s = ln.strip()
        if res and _plain(s):
            j = len(res) - 1
            while j >= 0 and not res[j].strip():
                j -= 1
            if j >= 0 and _plain(res[j].strip()):
                prev = res[j].strip()
                # v0.3.2：跨段合并充要条件 = 跨页断点证据（不再是"前段无句末标点"单条件）
                if (norm(prev)[-20:], norm(s)[:20]) in cross_pairs:
                    joiner = "" if _CJK(res[j].rstrip()[-1]) and _CJK(s[0]) else " "
                    res[j] = res[j].rstrip() + joiner + s; joins += 1
                    del res[j + 1:]
                    continue
        res.append(ln)
    return "\n".join(res), joins

PUNCT_MAP = {",": "，", ":": "：", ";": "；", "!": "！", "?": "？"}

def localize_punctuation(md_text):
    """v0.3.2 问题2：中文语境标点本地化——两侧都是中文字符的半角 , ; : ! ? 转全角。
    保护清单：一侧是英文或数字即不动（索引条目里的英文姓名安全）。"""
    return re.sub(r"(?<=[一-鿿])([,;:!?])(?=[一-鿿])", lambda m: PUNCT_MAP[m.group(1)], md_text)

def postprocess(md_text, pages):
    """Phase 3.7 主入口：页眉剥离（第一篇正文落位之后；独立行删、句首/句尾黏连剥）
    → 中文重排（段内并接 + 跨页断点合并）→ 中文语境标点本地化。返回 (新md, 报告)。"""
    heads = detect_running_heads(pages)
    lines = md_text.split("\n")
    first_h = next((i for i, ln in enumerate(lines) if ln.startswith("# ")), len(lines))
    removed = []
    for i in range(first_h + 1, len(lines)):
        s = lines[i].strip()
        if not s or s.startswith(("#", "[^", "!")):
            continue
        s2 = re.sub(r"^[-*+]\s+", "", s)  # 列表前缀不妨碍整行页眉判定（目录区统一列表化会把页眉行变成列表项）
        ns = norm(s2)
        if ns in heads:
            removed.append({"line": i + 1, "text": s, "how": "独立行"})
            lines[i] = ""
            continue
        if not _plain(s):
            continue
        for hk, hv in heads.items():
            if len(ns) <= len(hk) + 4:
                continue
            if s.startswith(hv):
                lines[i] = s[len(hv):].strip()
                removed.append({"line": i + 1, "text": hv, "how": "句首黏连"})
                break
            if s.endswith(hv):
                lines[i] = s[:-len(hv)].strip()
                removed.append({"line": i + 1, "text": hv, "how": "句尾黏连"})
                break
    cross_pairs = build_cross_page_pairs(pages, set(heads.keys()))
    md2, joins = reflow_paragraphs("\n".join(lines), cross_pairs)
    md2 = localize_punctuation(md2)
    return md2, {"head_set": sorted(heads.values()), "cross_page_pairs": len(cross_pairs),
                 "heads_removed": removed, "reflow_joins": joins}

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
    # v0.3.0 调和：插入边界后，对全部现存标题行做证据审判（含 MinerU 原生投票与刚插入的边界）
    pages_all = load_pages(args.mid)
    new_md, demoted, keptn = reconcile_headings(new_md, boundaries, pages_all, applied)
    open(args.out, "w", encoding="utf-8", newline="\n").write(new_md)
    if demoted:
        print(f"[structure] 目录区降级 {len(demoted)} 个条目（条目非标题）")
        for d in demoted[:8]:
            print(f"    ✂ 行{d['line']} {d['title'][:30]!r} ({d['reason']})")

    report = {"rules_used": [r["name"] for r in rules], "boundaries": boundaries,
              "applied": applied, "misses": misses, "warns": warns,
              "reconcile": {"demoted": demoted, "kept": keptn}}
    json.dump(report, open(args.report, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"[structure] 标题落位 {len(applied)}/{len(applied)+len(misses)} -> {args.out}")
    print(f"[structure] 报告 -> {args.report}")


if __name__ == "__main__":
    main()
