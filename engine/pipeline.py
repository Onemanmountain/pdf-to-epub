# -*- coding: utf-8 -*-
"""
pipeline.py — v0.2.0 单引擎编排（CLI 与未来的 GUI 共用同一套 Phase 函数）

  Phase 0  scout       侦察 → book_profile.json（可跳过：已存在则复用）
  Phase 1  mineru      解析 → zip → extracted/
  Phase 2  structure   结构重建 → structured.md + structure_report.json
  Phase 3  verify      三级校验 → verified.md + verify_report.json
           mode=auto   → arbitrate 整页仲裁 → final.md（零干预）
           mode=guided → 打印指引并暂停，人工改 verified.md → final.md
  Phase 4  package     pandoc → epub（重名自动加序号）+ qc 自动质检
  输出     conversion_report.json（各阶段决策与理由全程留痕）

CLI: python -m engine.pipeline --pdf book.pdf --title 书名 [--author 某人] \
       [--outdir epub_out] [--mode auto|guided] [--skip-scout]
"""
import argparse, json, os, re, shutil, subprocess, sys

sys.stdout.reconfigure(encoding="utf-8")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def sh(cmd, env_extra=None, cwd=None):
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    if env_extra:
        env.update(env_extra)
    r = subprocess.run(cmd, env=env, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise SystemExit(f"[X] 命令失败 ({r.returncode}): {' '.join(cmd)}\n{r.stdout[-800:]}\n{r.stderr[-800:]}")
    return r


def unique_path(path):
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    n = 2
    while os.path.exists(f"{stem}-{n}{ext}"):
        n += 1
    return f"{stem}-{n}{ext}"


def run(pdf, title, author, outdir, mode, config_path=None, skip_scout=False, no_arbitrate=False):
    from . import config, scout, structure, arbitrate, qc
    cfg = config.load_config(config_path)
    os.makedirs(outdir, exist_ok=True)
    pdf = os.path.abspath(pdf)
    venv = os.environ.get("MINERU_VENV", os.path.join(os.path.expanduser("~"), "mineru-venv"))
    venv_py = os.path.join(venv, "Scripts", "python.exe")
    mineru = os.path.join(venv, "Scripts", "mineru-kit.exe")
    verify_py = os.path.join(REPO, "scripts", "epub_verify.py")
    report = {"book": title, "pdf": pdf, "mode": mode, "phases": {}}

    # ---- Phase 0: scout ----
    profile_p = os.path.join(outdir, "book_profile.json")
    profile_ok = os.path.exists(profile_p)
    if profile_ok:  # 复用前校验归属：profile 必须属于本 PDF，防多书共用 outdir 串味
        try:
            _p = json.load(open(profile_p, encoding="utf-8"))
            profile_ok = _p.get("_meta", {}).get("pdf") == os.path.basename(pdf)
        except Exception:
            profile_ok = False
    if skip_scout or profile_ok:
        print(f"[Phase 0] 复用已有 profile: {profile_p}" if profile_ok else "[Phase 0] 跳过 scout")
        if not os.path.exists(profile_p):
            json.dump({"boundary_rules": []}, open(profile_p, "w", encoding="utf-8"))
    else:
        print("[Phase 0] Scout 侦察中（抽样视觉阅读）...")
        scout.scout(pdf, profile_p, scout.make_backend(cfg))
    profile = json.load(open(profile_p, encoding="utf-8"))
    # 仲裁层（默认开，凭证在位即启用；profile 复用时同样执行）：
    # 聚合层也会过拟合（如把正文里的名人生平段误判为篇界），外部仲裁是设计内的刹车
    if not no_arbitrate and profile.get("_descs") and not profile.get("arbitration"):
        s = cfg["scout"]
        if s["api_base"] and s["api_key"] and s["api_model"]:
            print("[Phase 0+] 外部强模型仲裁候选规则...")
            arb = scout.ExternalAPI(s["api_base"], s["api_key"], s["api_model"])
            final_rules, verdicts = scout.arbitrate_rules(profile["_descs"],
                                                          profile.get("boundary_rules", []), arb)
            profile["boundary_rules"] = final_rules
            profile["arbitration"] = verdicts
            json.dump(profile, open(profile_p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
            print(f"[Phase 0+] 仲裁后规则 {len(final_rules)} 条: {[r['name'] for r in final_rules]}")
    report["phases"]["scout"] = {"rules": [r["name"] for r in profile.get("boundary_rules", [])]}

    # ---- Phase 1: mineru ----
    pdf_stem = os.path.splitext(os.path.basename(pdf))[0]
    # 只复用名字与本书匹配的 zip（防多书共用 outdir 时错配他书解析产物）
    zips = [f for f in os.listdir(outdir) if f.endswith(".zip")
            and (os.path.splitext(f)[0] in (pdf_stem, title) or pdf_stem in os.path.splitext(f)[0])]
    if zips:
        zip_p = os.path.join(outdir, sorted(zips, key=lambda f: -os.path.getmtime(os.path.join(outdir, f)))[0])
        print(f"[Phase 1] 复用解析产物: {zip_p}")
    else:
        print("[Phase 1] MinerU 解析中（GPU，耐心）...")
        sh([mineru, "parse", pdf, "-o", outdir, "-f", "zip", "--tier", "standard"],
           env_extra={"MINERU_MODEL_SOURCE": "modelscope"})
        zips = [f for f in os.listdir(outdir) if f.endswith(".zip")
                and (os.path.splitext(f)[0] in (pdf_stem, title) or pdf_stem in os.path.splitext(f)[0])]
        if not zips:  # mineru 产物名不含书名时退回最新 zip
            zips = sorted((f for f in os.listdir(outdir) if f.endswith(".zip")),
                          key=lambda f: -os.path.getmtime(os.path.join(outdir, f)))
        if not zips:
            raise SystemExit("[X] MinerU 解析后未找到产物 zip（解析可能失败）")
        zip_p = os.path.join(outdir, zips[0])
    extracted = os.path.join(outdir, "extracted")
    if not os.path.exists(extracted):
        import zipfile
        with zipfile.ZipFile(zip_p) as z:
            z.extractall(extracted)
    md_src = None
    for root, _, files in os.walk(extracted):
        if "markdown.md" in files:
            md_src = os.path.join(root, "markdown.md")
            break
    if not md_src:
        raise SystemExit("[X] zip 内未找到 markdown.md")
    report["phases"]["parse"] = {"zip": zip_p}

    # ---- Phase 2: structure ----
    structured_md = os.path.join(extracted, f"{title}-structured.md")
    struct_report_p = os.path.join(extracted, "structure_report.json")
    print("[Phase 2] 结构重建...")
    sh([venv_py, "-m", "engine.structure", "--mid", extracted, "--profile", profile_p,
        "--md", md_src, "--out", structured_md, "--report", struct_report_p], cwd=REPO)
    srep = json.load(open(struct_report_p, encoding="utf-8"))
    report["phases"]["structure"] = {"boundaries": len(srep["boundaries"]),
                                     "misses": len(srep["misses"]), "warns": srep.get("warns", [])}

    # ---- Phase 3: verify ----
    verified_md = os.path.join(extracted, f"{title}-verified.md")
    verify_report_p = os.path.join(extracted, "verify_report.json")
    print("[Phase 3] 三级校验（启发式 → 裁判 → VLM 复核）...")
    sh([venv_py, verify_py, "--md", structured_md, "--pdf", pdf, "--mid", extracted,
        "--out", verified_md, "--report", verify_report_p, "--backend", "ollama", "--title", title,
        "--max-flags", "100"])  # 检测器变多后 30 上限会把真实报警截掉（v0.3.1 实测：漫长 124 条 flags）
    vrep = json.load(open(verify_report_p, encoding="utf-8"))
    report["phases"]["verify"] = {"flags": len(vrep.get("flags", [])),
                                  "applied": len(vrep.get("applied", [])),
                                  "needs_review": len(vrep.get("needs_review", []))}

    final_md = os.path.join(extracted, f"{title}-final.md")
    if mode == "auto":
        print("[Phase 3+] 批量智能：整页仲裁 needs_review ...")
        arbitrate.arbitrate(verify_report_p, verified_md, pdf, final_md,
                            cfg["scout"]["ollama_url"], cfg["scout"]["vl_model"],
                            ext_cfg=cfg["scout"])
    else:
        print("=" * 60)
        print(f"[主动审阅] 1. 打开报告: {verify_report_p}")
        print(f"           2. 手改 md:   {verified_md}")
        print("           3. 改完回到这里按回车继续 pandoc")
        input("按回车继续（Ctrl+C 中止）...")
        shutil.copyfile(verified_md, final_md)
    vrep = json.load(open(verify_report_p, encoding="utf-8"))
    arb = vrep.get("arbitration", {})
    if isinstance(arb, dict):
        report["phases"]["verify"]["arb_applied"] = len(arb.get("applied", []))
        report["phases"]["verify"]["arb_kept"] = len(arb.get("kept_original", []))

    # ---- Phase 3.6: 脚注规范化（全书连续编号 + pandoc [^n]；保守跳过记报告）----
    print("[Phase 3.6] 脚注规范化（按页配对 → 全书连续 [^n]）...")
    from . import footnotes
    pages = structure.load_pages(extracted)
    fn_report = {}
    fn_md = footnotes.normalize_footnotes(pages, open(final_md, encoding="utf-8").read(), fn_report)
    fn_info = fn_report.get("footnotes", {})
    epub_md_p = final_md.replace("-final.md", "-epub.md")
    if fn_info.get("converted", 0) > 0:
        open(epub_md_p, "w", encoding="utf-8", newline="\n").write(fn_md)
        pandoc_src = epub_md_p
    else:
        pandoc_src = final_md
    report["phases"]["footnotes"] = fn_info

    # ---- Phase 3.7: 页眉剥离 + 中文重排（v0.3.1 修复二/五）----
    print("[Phase 3.7] 页眉剥离 + 中文重排...")
    pp_src_text = open(pandoc_src, encoding="utf-8").read()
    md_pp, pp_rep = structure.postprocess(pp_src_text, pages)
    open(epub_md_p, "w", encoding="utf-8", newline="\n").write(md_pp)
    pandoc_src = epub_md_p
    report["phases"]["postprocess"] = {"head_set": pp_rep["head_set"],
                                       "heads_removed": len(pp_rep["heads_removed"]),
                                       "reflow_joins": pp_rep["reflow_joins"]}
    print(f"  页眉剥离 {len(pp_rep['heads_removed'])} 处（{pp_rep['head_set']}），重排并接 {pp_rep['reflow_joins']} 行")

    # ---- Phase 4: pandoc + QC ----
    pandoc = shutil.which("pandoc")
    if not pandoc:
        raise SystemExit("[X] pandoc 不在 PATH")
    epub = unique_path(os.path.join(os.path.dirname(pdf), f"{title}.epub"))
    print("[Phase 4] pandoc 封装...")
    sh([pandoc, os.path.basename(pandoc_src), "-o", epub, "-s",
        "--metadata", f"title={title}", "--metadata", f"author={author}",
        "--metadata", "lang=zh-CN", "--toc", "--toc-depth=2"], cwd=os.path.dirname(final_md))
    print("[Phase 4] EPUB 自动质检...")
    qc_rep = qc.qc(epub, os.path.join(outdir, "qc_report.json"))
    report["phases"]["qc"] = {"passed": qc_rep["passed"], "epub": epub}

    rep_p = os.path.join(outdir, "conversion_report.json")
    json.dump(report, open(rep_p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print("=" * 60)
    print(f"[done] EPUB -> {epub}")
    print(f"[done] 转换报告 -> {rep_p}")
    return report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--title", required=True)
    ap.add_argument("--author", default="")
    ap.add_argument("--outdir", default=None)
    ap.add_argument("--mode", choices=["auto", "guided"], default="auto")
    ap.add_argument("--skip-scout", action="store_true")
    ap.add_argument("--no-arbitrate", action="store_true", help="关闭外部仲裁层（默认开，需 config.local.yaml 凭证）")
    ap.add_argument("--config", default=None)
    args = ap.parse_args()
    outdir = args.outdir or os.path.join(os.path.dirname(os.path.abspath(args.pdf)), "epub_out")
    run(args.pdf, args.title, args.author, outdir, args.mode, args.config, args.skip_scout, args.no_arbitrate)


if __name__ == "__main__":
    main()
