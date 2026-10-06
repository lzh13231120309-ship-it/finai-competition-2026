param([switch]$SkipModels)
$ErrorActionPreference='Stop'
$root=$PSScriptRoot
if($root -match '[^\x00-\x7F]'){throw '请将仓库移到纯英文路径，例如 C:\FinAI\finai-competition-2026；本地模型不支持所有中文路径。'}
$stage=Join-Path $root '第一阶段_金融提取'
New-Item -ItemType Directory -Path (Join-Path $root 'runtime') -Force | Out-Null
Start-Transcript -Path (Join-Path $root 'runtime/install.log') -Append | Out-Null
function Invoke-Checked([string]$program,[string[]]$arguments){
    & $program @arguments
    if($LASTEXITCODE -ne 0){throw "执行失败，请查看 runtime/install.log，退出码 $LASTEXITCODE"}
}
try {
    $python=Join-Path $root 'runtime/python/Scripts/python.exe'
    if(-not (Test-Path -LiteralPath $python)){
        $launcher=Get-Command py -ErrorAction SilentlyContinue
        if(-not $launcher){throw '请先从 https://www.python.org/downloads/windows/ 安装 Python 3.13 64位，并勾选 Python Launcher，再重试。'}
        Invoke-Checked $launcher.Source @('-3.13','-m','venv',(Join-Path $root 'runtime/python'))
    }
    Invoke-Checked $python @('-c','import sys; assert sys.version_info[:2] == (3,13), "需要 Python 3.13"')
    Invoke-Checked $python @('-m','pip','install','-r',(Join-Path $stage 'requirements-lock.txt'))
    Invoke-Checked $python @('-m','pip','check')
    if(-not $SkipModels){
        $env:MINERU_HOME=Join-Path $root 'runtime/mineru'
        $env:MINERU_CONFIG=Join-Path $stage 'mineru-local.yaml'
        $env:MINERU_MODEL_BASE_DIR=Join-Path $root 'runtime/models'
        $env:MINERU_MODEL_SMALL_BACKEND='onnx'; $env:MINERU_MODEL_VLM_ENGINE='llama-cpp'
        Invoke-Checked (Join-Path $root 'runtime/python/Scripts/mineru-models-download.exe') @('--tier','standard','--small-backend','onnx','--vlm-engine','llama-cpp','--source','modelscope')
        Invoke-Checked $python @('-X','utf8',(Join-Path $root '金融大赛_集成试用版/download_reasoning.py'))
        Invoke-Checked $python @('-X','utf8',(Join-Path $root 'scripts/check_install.py'))
    }
    Write-Host '软件环境安装成功。完整模型检查通过后，可双击 启动衡知.bat。' -ForegroundColor Green
} finally {Stop-Transcript | Out-Null}
