# -*- coding: utf-8 -*-
"""
qc.py — Phase 4 EPUB 自动质检：
  ① zip 结构：mimetype 第一且不压缩；container.xml / content.opf 可解析
  ② manifest 引用的文件（图片/样式）全部存在
  ③ TOC 落点对账：每个目录项的目标文件里能找到（规范化后的）目录文字
CLI: python -m engine.qc --epub book.epub [--report qc.json]
"""
import argparse, json, re, sys, zipfile
import xml.etree.ElementTree as ET

sys.stdout.reconfigure(encoding="utf-8")


def norm(s):
    return re.sub(r"[\s，。,.、·:：;；\"'“”‘’《》<>!\[\]【】?？#*]", "", s or "")


def strip_tags(html):
    return re.sub(r"<[^>]+>", "", html)


def qc(epub_path, report_path=None):
    checks = []
    def ck(name, okv, detail=""):
        checks.append({"name": name, "ok": bool(okv), "detail": detail})
        print(f"  {'✓' if okv else '✗'} {name} {detail}")

    z = zipfile.ZipFile(epub_path)
    names = z.namelist()
    first = z.infolist()[0]
    ck("mimetype 为第一项", first.filename == "mimetype", first.filename)
    ck("mimetype 不压缩", first.compress_type == zipfile.ZIP_STORED,
       f"compress_type={first.compress_type}")

    # container -> opf
    try:
        cont = ET.fromstring(z.read("META-INF/container.xml"))
        opf_path = cont.iter("{urn:oasis:names:tc:opendocument:xmlns:container}rootfile")
        opf_path = next(r.get("full-path") for r in opf_path)
        ck("container.xml -> opf", True, opf_path)
    except Exception as e:
        ck("container.xml -> opf", False, str(e)[:80])
        return _finish(checks, report_path)

    base = opf_path.rsplit("/", 1)[0] + "/" if "/" in opf_path else ""
    opf = ET.fromstring(z.read(opf_path))
    ns = {"opf": "http://www.idpf.org/2007/opf", "dc": "http://purl.org/dc/elements/1.1/"}
    manifest = {m.get("id"): m.get("href") for m in opf.iter("{http://www.idpf.org/2007/opf}item")}
    missing = [h for h in manifest.values() if h and (base + h) not in names]
    ck("manifest 引用文件全部存在", not missing, f"缺失 {len(missing)} 个" if missing else f"共 {len(manifest)} 项")

    # 找 nav（EPUB3 properties=nav 或 NCX）
    nav_href = next((h for m in opf.iter("{http://www.idpf.org/2007/opf}item")
                     if m.get("properties") == "nav" for h in [m.get("href")]), None)
    toc_entries = []
    if nav_href and (base + nav_href) in names:
        html = z.read(base + nav_href).decode("utf-8", "replace")
        toc_entries = re.findall(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', html, re.S)
    ncx = next((base + h for h in manifest.values() if h and h.endswith(".ncx")), None)
    if not toc_entries and ncx and ncx in names:
        xml = z.read(ncx).decode("utf-8", "replace")
        toc_entries = re.findall(r'<content[^>]+src="([^"]+)"[^>]*/?>.*?<text>(.*?)</text>', xml, re.S)
        toc_entries = [(h, t) for h, t in toc_entries]
    ck("找到目录条目", len(toc_entries) > 0, f"{len(toc_entries)} 条")

    # TOC 落点对账（剔除 pandoc 自动生成的样板条目：Title Page / Table of Contents 及自指项）
    BOILER = {"titlepage", "tableofcontents", "标题页", "目录"}
    mism = []
    file_cache = {}
    nav_self = (base + nav_href) if nav_href else None
    for href, label in toc_entries:
        if norm(strip_tags(label)).lower() in BOILER:
            continue
        fpath = href.split("#")[0]
        full = base + fpath if not fpath.startswith(base) else fpath
        if not fpath or full == nav_self:
            continue  # 自指/空锚（TOC 页指向自身）
        if full not in names:
            mism.append({"label": label, "reason": f"目标文件不存在 {fpath}"})
            continue
        if full not in file_cache:
            file_cache[full] = norm(strip_tags(z.read(full).decode("utf-8", "replace")))
        if norm(strip_tags(label)) and norm(strip_tags(label)) not in file_cache[full]:
            mism.append({"label": strip_tags(label).strip()[:30], "reason": "目标文件内找不到目录文字"})
    ck("TOC 落点对账", not mism, f"不符 {len(mism)} 条" if mism else f"{len(toc_entries)} 条全部命中")
    z.close()
    return _finish(checks, report_path, mism)


def _finish(checks, report_path, mism=None):
    passed = all(c["ok"] for c in checks)
    result = {"passed": passed, "checks": checks, "toc_mismatches": mism or []}
    print(f"[qc] {'通过' if passed else '未通过'}")
    if report_path:
        json.dump(result, open(report_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print(f"[qc] 报告 -> {report_path}")
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--epub", required=True)
    ap.add_argument("--report", default=None)
    args = ap.parse_args()
    r = qc(args.epub, args.report)
    sys.exit(0 if r["passed"] else 1)


if __name__ == "__main__":
    main()
