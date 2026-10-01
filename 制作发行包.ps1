param(
    [string]$Version = "2.0.0-preview",
    [switch]$SkipZip
)

$ErrorActionPreference = "Stop"
$project = Split-Path -Parent $MyInvocation.MyCommand.Path
$output = Join-Path $project "发布输出"
$stage = Join-Path $output "本地通话录音工具-$Version-win64"
$zip = "$stage.zip"

if ($Version -notmatch '^[0-9A-Za-z][0-9A-Za-z._-]{0,39}$') {
    throw "版本号只能包含英文字母、数字、点、下划线和连字符。"
}
$outputFull = [System.IO.Path]::GetFullPath($output)
$stageFull = [System.IO.Path]::GetFullPath($stage)
if (-not $stageFull.StartsWith($outputFull + [System.IO.Path]::DirectorySeparatorChar,
                              [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "发行目录不在指定输出目录内。"
}

# Only known distributable files are copied. Never stage an existing user profile,
# recording, log, backup, development script, or local config.
$sourceFiles = @(
    "app.py", "manager_gui.py", "analyzer.py", "api_client.py",
    "transcript_polish.py", "local_asr.py", "acoustic_curve.py",
    "wechat_loopback.py", "audio_capture.py", "contact_name.py",
    "ocr_name.py", "ocr_worker.py", "converter.py", "audio_player.py",
    "autostart.py", "requirements.txt", "启动录音工具-升级版.vbs",
    "README.md", "第三方组件说明.md"
)

if (Test-Path -LiteralPath $stage) {
    throw "发行目录已存在：$stage。为保护现有成果，请换一个版本号。"
}
if (-not $SkipZip -and (Test-Path -LiteralPath $zip)) {
    throw "发行 ZIP 已存在：$zip。请换一个版本号。"
}

New-Item -ItemType Directory -Path $stage -Force | Out-Null
try {
    foreach ($name in $sourceFiles) {
        $from = Join-Path $project $name
        if (-not (Test-Path -LiteralPath $from -PathType Leaf)) {
            throw "缺少发行文件：$name"
        }
        Copy-Item -LiteralPath $from -Destination (Join-Path $stage $name)
    }
    foreach ($name in @("runtime", "models", "assets", "licenses")) {
        $from = Join-Path $project $name
        if (-not (Test-Path -LiteralPath $from -PathType Container)) {
            throw "缺少发行目录：$name"
        }
        Copy-Item -LiteralPath $from -Destination (Join-Path $stage $name) -Recurse
    }
    $icons = Join-Path $stage "assets\icons"
    Get-ChildItem -LiteralPath $icons -Filter "_检查_*" -File |
        Remove-Item -Force

    $required = @(
        "runtime\Python312\pythonw.exe",
        "runtime\Python312\python.exe",
        "models\sense-voice\model.int8.onnx",
        "models\sense-voice\tokens.txt",
        "models\funasr-nano\model.int8.onnx",
        "models\silero_vad.onnx",
        "assets\icons\app.ico",
        "licenses\FunASR_MODEL_LICENSE.txt",
        "licenses\Apache-2.0.txt",
        "licenses\GPL-3.0.txt"
    )
    foreach ($name in $required) {
        if (-not (Test-Path -LiteralPath (Join-Path $stage $name) -PathType Leaf)) {
            throw "发行包不完整：$name"
        }
    }
    foreach ($name in @("config.json", "durations.json", "logs", "测试录音", "备份")) {
        if (Test-Path -LiteralPath (Join-Path $stage $name)) {
            throw "检测到私人文件或目录：$name"
        }
    }
    $py = Join-Path $stage "runtime\Python312\python.exe"
    & $py -c "import tkinter, PIL, numpy, pystray, sherpa_onnx, pyaudiowpatch, comtypes, winsdk, pycaw; print('依赖导入成功')"
    if ($LASTEXITCODE -ne 0) { throw "发行包的内置运行时依赖导入失败" }
    & $py -m py_compile (Join-Path $stage "app.py") (Join-Path $stage "manager_gui.py") (Join-Path $stage "analyzer.py")
    if ($LASTEXITCODE -ne 0) { throw "发行包代码编译失败" }
    # py_compile creates cache; the downloadable package should not contain it.
    Get-ChildItem -LiteralPath $stage -Directory -Filter "__pycache__" -Recurse |
        ForEach-Object {
            $cacheFull = [System.IO.Path]::GetFullPath($_.FullName)
            if (-not $cacheFull.StartsWith($stageFull + [System.IO.Path]::DirectorySeparatorChar,
                                           [System.StringComparison]::OrdinalIgnoreCase)) {
                throw "缓存路径越界：$cacheFull"
            }
            Remove-Item -LiteralPath $cacheFull -Recurse -Force
        }

    if (-not $SkipZip) {
        Add-Type -AssemblyName System.IO.Compression
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        [System.IO.Compression.ZipFile]::CreateFromDirectory(
            $stage, $zip, [System.IO.Compression.CompressionLevel]::Optimal, $true)
        $sha = (Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash.ToLowerInvariant()
        Set-Content -LiteralPath "$zip.sha256" -Value "$sha  $(Split-Path -Leaf $zip)" -Encoding ascii
        Write-Output "ZIP: $zip"
        Write-Output "SHA256: $sha"
    }
    Write-Output "发行目录：$stage"
} catch {
    throw
}
