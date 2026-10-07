# pdf-to-epub.ps1 — v0.2.0 引擎驱动版
# 扫描版 PDF → 可重排 EPUB：一条命令走完 Phase 0-4（引擎在 engine.pipeline）
# 本脚本只负责：交互询问、ollama 起床检查、调用引擎。转换逻辑全在引擎仓。
#
# 用法:
#   .\pdf-to-epub.ps1                 # 交互模式：逐项询问
#   .\pdf-to-epub.ps1 -Pdf book.pdf -Title 书名 -Author 某人 -Mode auto
#   .\pdf-to-epub.ps1 -Help           # 显示本帮助
#
# 参数:
#   -Pdf        PDF 路径（可省略进入交互；支持拖拽带引号路径）
#   -OutDir     工作输出目录（默认 <PDF旁>\epub_out；zip/profile 自动复用）
#   -Title      书名（默认取 PDF 文件名）
#   -Author     作者（可空）
#   -Mode       auto=批量智能（默认，零干预）| guided=主动审阅（校验后暂停等人）
#   -SkipScout  跳过 Phase 0 侦察（无结构规则，退回 MinerU 原生标题）
#   -EngineDir  引擎仓目录（默认 D:\Documents\pdf-to-epub，或环境变量 PDF2EPUB_ENGINE）
#   -VenvDir    mineru venv 目录，默认 $env:MINERU_VENV 或 ~\mineru-venv
#   -OllamaExe  ollama.exe 路径，默认查 PATH 再查 %LOCALAPPDATA%\Programs\Ollama
#   -Proxy      仅用于 ollama pull 补拉模型；默认空=不设置
#
# 双模式说明:
#   auto   批量智能：仲裁/裁决全自动，不一致项 do-no-harm 保持原文并记报告
#   guided 主动审阅：校验后打印报告路径并暂停，人工改 md 后回车继续封装
# 前提: ollama 模型 qwen3:8b + qwen2.5vl:7b（缺了自动补拉）；pandoc 在 PATH
param(
  [switch]$Help,
  [string]$Pdf,
  [string]$OutDir,
  [string]$Title,
  [string]$Author = '',
  [ValidateSet('auto','guided')]
  [string]$Mode = 'auto',
  [switch]$SkipScout,
  [string]$EngineDir = '',
  [string]$VenvDir = '',
  [string]$OllamaExe = '',
  [string]$Proxy = ''
)

if ($Help) { Get-Help $PSCommandPath -Detailed; exit 0 }
$ErrorActionPreference = 'Stop'

# --- 路径解析 ---
if (-not $EngineDir) { $EngineDir = if ($env:PDF2EPUB_ENGINE) { $env:PDF2EPUB_ENGINE } else { 'D:\Documents\pdf-to-epub' } }
if (-not (Test-Path (Join-Path $EngineDir 'engine\pipeline.py'))) { Write-Host "[X] 引擎仓不存在: $EngineDir（用 -EngineDir 指定）" -ForegroundColor Red; exit 1 }
if (-not $VenvDir) { $VenvDir = if ($env:MINERU_VENV) { $env:MINERU_VENV } else { Join-Path $env:USERPROFILE 'mineru-venv' } }
$VenvPy = Join-Path $VenvDir 'Scripts\python.exe'
if (-not (Test-Path $VenvPy)) { Write-Host "[X] venv python 不存在: $VenvPy" -ForegroundColor Red; exit 1 }
if (-not $OllamaExe) {
  $cmd = Get-Command ollama -ErrorAction SilentlyContinue
  $OllamaExe = if ($cmd) { $cmd.Source } else { Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe' }
}
$pandoc = Get-Command pandoc -ErrorAction SilentlyContinue
if (-not $pandoc) { Write-Host "[X] pandoc 不在 PATH" -ForegroundColor Red; exit 1 }

# --- 交互询问 ---
if (-not $Pdf) { $Pdf = (Read-Host 'PDF path (drag file here)').Trim('"').Trim("'") }
if (-not (Test-Path $Pdf)) { Write-Host "[X] PDF 不存在: $Pdf" -ForegroundColor Red; exit 1 }
$Pdf = (Resolve-Path $Pdf).Path
if (-not $Title) {
  $def = [IO.Path]::GetFileNameWithoutExtension($Pdf)
  $t = Read-Host "Book title [$def]"
  $Title = if ($t) { $t } else { $def }
}
if (-not $Author) { $Author = Read-Host 'Author (blank ok)' }
if (-not $OutDir) {
  $defOut = Join-Path (Split-Path $Pdf) 'epub_out'
  $o = Read-Host "Work output dir [$defOut]"
  $OutDir = if ($o) { $o } else { $defOut }
}
if (-not $PSBoundParameters.ContainsKey('Mode')) {
  $m = Read-Host 'Mode: auto=批量智能(默认) / guided=主动审阅，输入 g 选 guided'
  if ($m -eq 'g') { $Mode = 'guided' }
}
Write-Host "[i] Pdf=$Pdf`n[i] Title=$Title Author=$Author Mode=$Mode SkipScout=$SkipScout`n[i] OutDir=$OutDir Engine=$EngineDir"

# --- ollama 起床检查（本地看页/裁判/复核都靠它） ---
function Test-Ollama { try { Invoke-RestMethod -Uri 'http://localhost:11434/api/tags' -TimeoutSec 3 | Out-Null; $true } catch { $false } }
if (-not (Test-Ollama)) {
  Write-Host '[i] ollama serve 未运行，启动中（冷启动约 1-2 分钟，别急）...' -ForegroundColor Yellow
  Start-Process $OllamaExe -ArgumentList 'serve' -WindowStyle Hidden -WorkingDirectory (Split-Path $OllamaExe)
  $ok = $false
  foreach ($i in 1..75) {
    Start-Sleep -Seconds 2
    if (Test-Ollama) { $ok = $true; break }
    if (-not (Get-Process ollama -ErrorAction SilentlyContinue)) {
      Write-Host '[X] ollama serve 进程已退出。请手动开终端跑 ollama serve 看报错。' -ForegroundColor Red; exit 1
    }
  }
  if (-not $ok) { Write-Host '[X] ollama serve 启动超时(150s)。请手动跑 ollama serve 诊断。' -ForegroundColor Red; exit 1 }
}
Write-Host '[ok] ollama up'
$tags = (Invoke-RestMethod -Uri 'http://localhost:11434/api/tags').models.name
foreach ($m in @('qwen3:8b','qwen2.5vl:7b')) {
  if ($tags -notcontains $m) {
    Write-Host "[i] pulling $m ..." -ForegroundColor Yellow
    if ($Proxy) { $env:HTTPS_PROXY = $Proxy; $env:HTTP_PROXY = $Proxy }
    & $OllamaExe pull $m
    Remove-Item Env:HTTPS_PROXY,Env:HTTP_PROXY -ErrorAction SilentlyContinue
  } else { Write-Host "[ok] model present: $m" }
}

# --- 调用引擎（剥 PYTHONPATH 防依赖走私） ---
$savedPP = $env:PYTHONPATH
Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
$argvList = @('-m','engine.pipeline','--pdf',$Pdf,'--title',$Title,'--author',$Author,'--mode',$Mode,'--outdir',$OutDir)
if ($SkipScout) { $argvList += '--skip-scout' }
Push-Location $EngineDir
try {
  & $VenvPy @argvList
  $rc = $LASTEXITCODE
} finally {
  Pop-Location
  if ($savedPP) { $env:PYTHONPATH = $savedPP }
}
if ($rc -eq 0) {
  Write-Host "`n[done] 转换完成。EPUB 在 PDF 同目录（重名自动加序号）。" -ForegroundColor Green
  Write-Host "[i] 转换报告: $(Join-Path $OutDir 'conversion_report.json')"
} else {
  Write-Host "`n[X] 引擎退出码 $rc，向上翻日志定位。" -ForegroundColor Red
}
exit $rc
