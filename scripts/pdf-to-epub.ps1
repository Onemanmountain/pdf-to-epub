#Requires -Version 5.1
<#
.SYNOPSIS
  PDF -> EPUB one-stop pipeline: MinerU parse -> triple-verify (LLM) -> pandoc.
.DESCRIPTION
  Interactive by default; every prompt can be pre-empted via parameters.
  Use -Help for usage and examples.
#>
param(
  [switch]$Help,
  [string]$Pdf,
  [string]$OutDir,
  [string]$Title,
  [string]$Author = '',
  [ValidateSet('ollama','transformers')]
  [string]$Backend = 'ollama',
  [string]$VenvDir = '',     # mineru venv dir; default: $env:MINERU_VENV or ~\mineru-venv
  [string]$OllamaExe = '',   # default: ollama in PATH or %LOCALAPPDATA%\Programs\Ollama\ollama.exe
  [string]$Proxy = ''        # e.g. http://127.0.0.1:7890 - only used for ollama pull
)

# ---------- tool resolution: param > env > autodetect ----------
if (-not $VenvDir) { $VenvDir = if ($env:MINERU_VENV) { $env:MINERU_VENV } else { Join-Path $env:USERPROFILE 'mineru-venv' } }
$VenvPy    = Join-Path $VenvDir 'Scripts\python.exe'
$MineruKit = Join-Path $VenvDir 'Scripts\mineru-kit.exe'
$VerifyPy  = Join-Path $PSScriptRoot 'epub_verify.py'   # must sit next to this script
if (-not $OllamaExe) {
  $oc = Get-Command ollama -ErrorAction SilentlyContinue
  $OllamaExe = if ($oc) { $oc.Source } else { Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe' }
}

function Show-Help {
  @'
============================================================
 pdf-to-epub.ps1 - PDF转EPUB一条龙（解析->三级校验->封装）
============================================================
用法:
  pdf-to-epub.ps1                  交互模式（逐项询问）
  pdf-to-epub.ps1 -Pdf "D:\books\某书.pdf" [-Title "某书"] [-Author "某人"]
  pdf-to-epub.ps1 -Help            显示本帮助

参数:
  -Pdf        PDF 文件路径（建议不要含尾空格）
  -OutDir     中间产物输出目录，默认 <PDF同目录>\epub_out
  -Title      书名，默认取 PDF 文件名；EPUB 用它命名
  -Author     作者，写进 EPUB metadata
  -Backend    校验用 LLM 后端：ollama（默认，走本机 Ollama 服务）
              或 transformers（走 MODELS_ROOT 环境变量下的权重，默认 ./models）
  -VenvDir    mineru venv 目录，默认 $env:MINERU_VENV 或 ~\mineru-venv
  -OllamaExe  ollama.exe 路径，默认从 PATH 探测
  -Proxy      仅 ollama pull 时使用的代理，如 http://127.0.0.1:7890
  -Help       显示帮助与示例

流程:
  1. MinerU 解析 PDF（standard 档，GPU 约 10 分钟/263 页）
  2. 解包 zip（markdown.md + images/ + middle_json.json）
  3. 三级校验：规则启发式 -> Qwen3 裁判 -> Qwen2.5-VL 定向复核
     （自动修"双确认一致"的；其余进 report 人工审）
  4. 暂停，等你审查并手改 <书名>-fixed.md（看 verify_report.json）
  5. pandoc 封装成 <书名>.epub（与 PDF 同目录；重名自动加序号）

前提:
  - epub_verify.py 与本脚本同目录；mineru-venv 可用；pandoc 在 PATH；GPU 驱动正常
  - Backend=ollama 时: ollama serve 在跑（没在跑本脚本会自动拉起），
    且已 pull qwen3:8b 与 qwen2.5vl:7b（缺了会自动补拉，可用 -Proxy 指定代理）
  - Backend=transformers 时: MODELS_ROOT 下有 Qwen3-8B 与 Qwen2.5-VL-7B-Instruct 权重

示例:
  交互跑一本新书:
    .\pdf-to-epub.ps1
  全自动（全部参数给齐就不发问）:
    .\pdf-to-epub.ps1 -Pdf "D:\Documents\Local-Books\新书.pdf" -Title "新书" -Author "某某" 
  换 transformers 后端（不依赖 Ollama 服务）:
    .\pdf-to-epub.ps1 -Backend transformers
  只看帮助:
    .\pdf-to-epub.ps1 -Help

参考耗时: 解析 10 分钟 + 校验 25 分钟（首次加载模型另计 1-2 分钟）。
'@
  exit 0
}

function Ask($prompt, $default) {
  $suffix = if ($default) { " [$default]" } else { '' }
  $ans = Read-Host "$prompt$suffix"
  if ([string]::IsNullOrWhiteSpace($ans)) { return $default } else { return $ans.Trim() }
}

function Fail($msg) { Write-Host "[X] $msg" -ForegroundColor Red; exit 1 }

if ($Help) { Show-Help }

Write-Host '========== PDF -> EPUB pipeline ==========' -ForegroundColor Cyan

# ---------- 0. preflight ----------
if (-not (Test-Path $VenvPy))    { Fail "venv python not found: $VenvPy" }
if (-not (Test-Path $MineruKit)) { Fail "mineru-kit not found: $MineruKit" }
if (-not (Test-Path $VerifyPy))  { Fail "epub_verify.py not found: $VerifyPy" }
if (-not (Get-Command pandoc -ErrorAction SilentlyContinue)) { Fail 'pandoc not in PATH' }

# ---------- 1. inputs ----------
if (-not $Pdf)    { $Pdf    = Ask 'PDF path' '' }
$Pdf = $Pdf.Trim('"').Trim("'")
if (-not (Test-Path $Pdf)) { Fail "PDF not found: $Pdf" }
if ($Pdf -ne $Pdf.Trim()) { Write-Host '[!] PDF filename has trailing space(s) - MinerU hates that; please rename first.' -ForegroundColor Yellow; exit 1 }

$pdfDir  = Split-Path $Pdf -Parent
$stem    = [IO.Path]::GetFileNameWithoutExtension($Pdf)

if (-not $Title)  { $Title  = Ask 'Book title' $stem }
if (-not $Author) { $Author = Ask 'Author (blank ok)' '' }
if (-not $OutDir) { $OutDir = Ask 'Work output dir' (Join-Path $pdfDir 'epub_out') }
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

Write-Host "[i] Pdf=$Pdf" -ForegroundColor DarkGray
Write-Host "[i] Title=$Title  Author=$Author  Backend=$Backend" -ForegroundColor DarkGray
Write-Host "[i] OutDir=$OutDir" -ForegroundColor DarkGray

# ---------- 2. ollama backend readiness ----------
if ($Backend -eq 'ollama') {
  $ollamaUp = $false
  try {
    $null = Invoke-RestMethod -Uri 'http://localhost:11434/api/tags' -TimeoutSec 3
    $ollamaUp = $true
  } catch {}
  if (-not $ollamaUp) {
    Write-Host '[i] ollama serve not running - starting it (cold start can take 1-2 min)...' -ForegroundColor Yellow
    if (-not (Test-Path $OllamaExe)) { Fail "ollama.exe not found: $OllamaExe" }
    $srv = Start-Process -FilePath $OllamaExe -ArgumentList 'serve' `
             -WorkingDirectory (Split-Path $OllamaExe -Parent) `
             -WindowStyle Hidden -PassThru
    $deadline = (Get-Date).AddSeconds(150)
    while ((Get-Date) -lt $deadline) {
      Start-Sleep -Seconds 3
      if ($srv.HasExited) { Fail "ollama serve process exited early (code $($srv.ExitCode)) - run 'ollama serve' manually to see the error" }
      try { $null = Invoke-RestMethod -Uri 'http://localhost:11434/api/tags' -TimeoutSec 2; $ollamaUp = $true; break } catch {}
    }
    if (-not $ollamaUp) { Fail 'ollama serve not answering after 150s - check Task Manager (ollama.exe) or run "ollama serve" in another window to see the error' }
  }
  Write-Host '[ok] ollama up' -ForegroundColor Green

  # model presence; pull if missing (needs proxy)
  $tags = (Invoke-RestMethod -Uri 'http://localhost:11434/api/tags').models.name
  foreach ($m in @('qwen3:8b','qwen2.5vl:7b')) {
    if ($tags -notcontains $m) {
      Write-Host "[i] pulling $m ..." -ForegroundColor Yellow
      if ($Proxy) { $env:HTTPS_PROXY = $Proxy; $env:HTTP_PROXY = $Proxy }
      & $OllamaExe pull $m
      if ($LASTEXITCODE -ne 0) { Fail "ollama pull $m failed (if network is blocked, rerun with -Proxy http://127.0.0.1:7890)" }
    } else {
      Write-Host "[ok] model present: $m" -ForegroundColor Green
    }
  }
}

# ---------- 3. mineru parse ----------
# MinerU chokes on PDF filenames with space before extension (e.g. "书名 .pdf") -> copy to a clean name first
$parsePdf = $Pdf
if ($Pdf -match '\s+\.(pdf|PDF)$' -or $Pdf -ne $Pdf.Trim()) {
  $cleanName = (($Title -replace '[\\/:*?"<>|]', '').Trim() -replace '\s+$', '')
  if (-not $cleanName) { $cleanName = ([IO.Path]::GetFileNameWithoutExtension($Pdf) -replace '\s+', '') }
  $parsePdf = Join-Path $OutDir ($cleanName + '.pdf')
  Write-Host "[i] filename may break MinerU - copying to: $parsePdf" -ForegroundColor Yellow
  Copy-Item -LiteralPath $Pdf -Destination $parsePdf -Force
}
Write-Host '========== [1/4] MinerU parsing (GPU, be patient) ==========' -ForegroundColor Cyan
$existingZip = Get-ChildItem -Path $OutDir -Filter '*.zip' | Sort-Object LastWriteTime -Descending | Select-Object -First 1
$skipParse = $false
if ($existingZip) {
  $reuse = Read-Host "[?] existing parse result found: $($existingZip.FullName) - reuse and skip parsing? [Y/n]"
  if ($reuse -notmatch '^(n|N)') { $skipParse = $true }
}
if (-not $skipParse) {
  $env:MINERU_MODEL_SOURCE = 'modelscope'
  $savedPyPath = $env:PYTHONPATH; $env:PYTHONPATH = $null   # user-level PYTHONPATH (hermes) pollutes imports
  & $MineruKit parse $parsePdf -o $OutDir -f zip --tier standard
  $env:PYTHONPATH = $savedPyPath
  if ($LASTEXITCODE -ne 0) { Fail 'mineru parse failed' }
} else {
  Write-Host '[ok] reusing existing zip - parse skipped' -ForegroundColor Green
}

$zip = Get-ChildItem -Path $OutDir -Recurse -Filter '*.zip' | Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $zip) { Fail "no zip produced under $OutDir" }
Write-Host "[ok] parse output: $($zip.FullName)" -ForegroundColor Green

# ---------- 4. unzip ----------
$extracted = Join-Path $OutDir 'extracted'
if (Test-Path $extracted) {
  Get-ChildItem -Path $extracted -Filter '*-fixed.md' -ErrorAction SilentlyContinue | ForEach-Object {
    Copy-Item $_.FullName (Join-Path $OutDir ($_.Name + '.bak')) -Force
    Write-Host "[i] previous edited md backed up: $($_.Name).bak" -ForegroundColor Yellow
  }
  Remove-Item -Recurse -Force $extracted
}
New-Item -ItemType Directory -Force -Path $extracted | Out-Null
Expand-Archive -LiteralPath $zip.FullName -DestinationPath $extracted -Force
Write-Host "[ok] extracted -> $extracted" -ForegroundColor Green

$mdMd   = Get-ChildItem -Path $extracted -Recurse -Filter 'markdown.md'   | Select-Object -First 1
$midJs  = Get-ChildItem -Path $extracted -Recurse -Filter 'middle_json.json' | Select-Object -First 1
if (-not $mdMd)  { Fail 'markdown.md missing in zip' }
if (-not $midJs) { Fail 'middle_json.json missing in zip - without coordinates no verification is possible; do not ship raw OCR' }

# ---------- 5. triple verify ----------
Write-Host '========== [2/4] triple verify (scan -> judge -> VLM) ==========' -ForegroundColor Cyan
$fixedMd = Join-Path $extracted ($Title + '-fixed.md')
$report  = Join-Path $extracted 'verify_report.json'
$midArg  = if ($midJs) { Split-Path $midJs.FullName -Parent } else { $extracted }
$env:PYTHONPATH = $null
& $VenvPy $VerifyPy --md $mdMd.FullName --pdf $parsePdf --mid $midArg `
  --out $fixedMd --report $report --backend $Backend --title "$Title"
$env:PYTHONPATH = $savedPyPath
if ($LASTEXITCODE -ne 0) { Fail 'epub_verify.py failed' }
Write-Host "[ok] report -> $report" -ForegroundColor Green

# ---------- 6. human review pause ----------
Write-Host '========== [3/4] YOUR TURN ==========' -ForegroundColor Cyan
Write-Host "  1. open report:    $report"
Write-Host "     (applied = auto-fixed; needs_review = judge/VLM disagree - check the page in the PDF)"
Write-Host "  2. edit the md:    $fixedMd"
Write-Host '  3. come back here and press Enter to continue to pandoc.'
Read-Host 'Press Enter when the md is final (or Ctrl+C to abort)'

if (-not (Test-Path $fixedMd)) { Fail "edited md vanished: $fixedMd" }

# ---------- 7. pandoc ----------
Write-Host '========== [4/4] pandoc -> epub ==========' -ForegroundColor Cyan
Push-Location $extracted
try {
  $epubBase = Join-Path $pdfDir ($Title + '.epub')
  $epub = $epubBase
  $n = 2
  while (Test-Path $epub) { $epub = Join-Path $pdfDir ($Title + "-$n.epub"); $n++ }
  & pandoc (Split-Path $fixedMd -Leaf) -o $epub -s `
    --metadata title="$Title" --metadata author="$Author" `
    --metadata lang=zh-CN --toc --toc-depth=2
  if ($LASTEXITCODE -ne 0) { Fail 'pandoc failed' }
  Write-Host "[done] EPUB -> $epub" -ForegroundColor Green
  Invoke-Item $epub
} finally {
  Pop-Location
}
