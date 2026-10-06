$ErrorActionPreference='Stop'
try {
    $python=Join-Path (Split-Path $PSScriptRoot -Parent) 'runtime/python/Scripts/python.exe'
    if (-not (Test-Path -LiteralPath $python)) {throw "未找到运行环境：$python"}
    & $python -X utf8 (Join-Path $PSScriptRoot 'start_integrated.py') @args
    exit $LASTEXITCODE
} catch {
    Write-Host ('启动失败：'+$_.Exception.Message) -ForegroundColor Red
    exit 1
}
