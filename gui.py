# -*- coding: utf-8 -*-
"""
gui.py — 最简图形化界面（与 CLI 共用同一引擎 engine.pipeline）。
当前版本只暴露「批量智能 auto」模式（GUI 无控制台，guided 的暂停交互请用 ps1）。

启动: python gui.py   然后浏览器打开 http://127.0.0.1:7861
依赖: gradio（mineru-venv 已装）
"""
import os, subprocess, sys, threading

REPO = os.path.dirname(os.path.abspath(__file__))
VENV_PY = os.path.join(os.environ.get("MINERU_VENV", os.path.join(os.path.expanduser("~"), "mineru-venv")),
                       "Scripts", "python.exe")

import gradio as gr


def run_pipeline(pdf, title, author, outdir, skip_scout):
    if not pdf or not os.path.exists(pdf):
        yield "[X] PDF 路径不存在", None, None
        return
    if not title:
        title = os.path.splitext(os.path.basename(pdf))[0]
    cmd = [VENV_PY, "-m", "engine.pipeline", "--pdf", pdf, "--title", title,
           "--author", author or "", "--mode", "auto"]
    if outdir:
        cmd += ["--outdir", outdir]
    if skip_scout:
        cmd += ["--skip-scout"]
    log = f"$ {' '.join(cmd)}\n"
    yield log, None, None
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    proc = subprocess.Popen(cmd, cwd=REPO, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    epub, report = None, None
    for line in proc.stdout:
        log += line
        yield log, None, None
        if line.startswith("[done] EPUB -> "):
            epub = line.split("->", 1)[1].strip()
        if "转换报告 -> " in line:
            report = line.split("->", 1)[1].split("   ")[0].strip()
    proc.wait()
    log += f"\n[exit] {proc.returncode}\n"
    yield log, epub if epub and os.path.exists(epub) else None, \
        report if report and os.path.exists(report) else None


with gr.Blocks(title="pdf-to-epub") as app:
    gr.Markdown("# pdf-to-epub\n扫描版 PDF → 可重排 EPUB（批量智能模式 · 零人工干预）")
    with gr.Row():
        pdf_in = gr.Textbox(label="PDF 路径", placeholder=r"D:\Documents\Local-Books\xxx.pdf", scale=3)
        outdir_in = gr.Textbox(label="输出目录（留空=PDF 旁 epub_out）", scale=2)
    with gr.Row():
        title_in = gr.Textbox(label="书名（留空=取文件名）", scale=2)
        author_in = gr.Textbox(label="作者", scale=2)
        skip_chk = gr.Checkbox(label="跳过侦察（无结构规则）", value=False, scale=1)
    run_btn = gr.Button("开始转换", variant="primary")
    log_out = gr.Textbox(label="运行日志", lines=22, max_lines=40, interactive=False)
    with gr.Row():
        epub_out = gr.File(label="成品 EPUB")
        report_out = gr.File(label="转换报告")
    run_btn.click(run_pipeline, [pdf_in, title_in, author_in, outdir_in, skip_chk],
                  [log_out, epub_out, report_out])

if __name__ == "__main__":
    app.launch(server_name="127.0.0.1", server_port=7861, inbrowser=os.environ.get("GUI_NO_BROWSER") != "1", show_error=True,
               css=".gradio-container {max-width: 860px; margin: auto}", theme=gr.themes.Soft())
