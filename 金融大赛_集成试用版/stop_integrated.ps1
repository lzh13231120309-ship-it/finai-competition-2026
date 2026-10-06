$ErrorActionPreference='Stop'
$record=Join-Path $PSScriptRoot 'data/logs/integrated.pid'
$serviceId=$null
if(Test-Path -LiteralPath $record){
    $serviceId=[int](Get-Content -LiteralPath $record)
    $process=Get-CimInstance Win32_Process -Filter "ProcessId = $serviceId"
    if($process){
        if($process.CommandLine -notmatch 'server.app:app' -or $process.CommandLine -notmatch '17904'){throw '服务进程身份不匹配，拒绝关闭其他程序'}
        $health=Invoke-RestMethod -Uri 'http://127.0.0.1:17904/api/health' -TimeoutSec 4
        if($health.running_tasks -gt 0){throw 'Agent 对话仍在执行，请结束后再停止'}
    }else{$serviceId=$null}
}
$jobs=Join-Path (Split-Path $PSScriptRoot -Parent) '第一阶段_金融提取/local_data/jobs'
foreach($file in Get-ChildItem -LiteralPath $jobs -Filter state.json -Recurse){
    $state=Get-Content -LiteralPath $file.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
    if($state.status -in @('running','queued')){throw '有提取任务尚未结束，请完成后再停止'}
}
if($serviceId){& "$env:SystemRoot\System32\taskkill.exe" /PID $serviceId /T /F | Out-Null}
$modelRecord=Join-Path $PSScriptRoot 'data/logs/reasoning.pid'
if(Test-Path -LiteralPath $modelRecord){
    $modelId=[int](Get-Content -LiteralPath $modelRecord)
    $modelProcess=Get-CimInstance Win32_Process -Filter "ProcessId = $modelId"
    if($modelProcess){
        if($modelProcess.Name -ne 'llama-server.exe' -or $modelProcess.CommandLine -notmatch '17907' -or $modelProcess.CommandLine -notmatch 'finai-qwen3-4b'){throw '推理进程身份不匹配，拒绝关闭其他模型'}
        & "$env:SystemRoot\System32\taskkill.exe" /PID $modelId /T /F | Out-Null
    }
}
Write-Host '集成版与本地推理服务已停止。'
