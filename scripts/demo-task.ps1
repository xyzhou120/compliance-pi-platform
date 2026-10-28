param([string]$ConfigPath = '', [int]$WaitSeconds = 60)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'local-env.ps1') -ConfigPath $ConfigPath
$payload = @{
    mineId = 'demo-mine'
    checkCycle = '2026-10'
    executionId = ('windows-demo-' + [Guid]::NewGuid().ToString('N'))
    status = 'running'
    items = @(@{ itemCode = 'stop_work_unload' })
}
$body = [Text.Encoding]::UTF8.GetBytes(($payload | ConvertTo-Json -Depth 8))
$response = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8092/inspection/tasks' -Method Post -ContentType 'application/json; charset=utf-8' -Body $body
$taskId = [string]$response.Headers['X-Inspection-Task']
if (-not $taskId) { throw 'API did not return X-Inspection-Task.' }
$taskUrl = 'http://127.0.0.1:8092/inspection/tasks/' + $taskId
$task = Get-LocalJson -Uri $taskUrl
$runId = [string]$task.runs[0].run_id
$identity = @{ taskId = $taskId; runId = $runId; taskUrl = $taskUrl; runUrl = ('http://127.0.0.1:8092/v1/runs/' + $runId) }
$identity | ConvertTo-Json | Set-Content -LiteralPath '.local\last-demo-task.json' -Encoding UTF8
Write-Output ('Accepted task: ' + $taskId)
Write-Output ('Run: ' + $runId)
$deadline = [DateTime]::UtcNow.AddSeconds($WaitSeconds)
while ($true) {
    $run = Get-LocalJson -Uri $identity.runUrl
    if ($run.state -in @('completed', 'failed')) {
        $run.result | ConvertTo-Json -Depth 30 | Set-Content -LiteralPath '.local\last-demo-result.json' -Encoding UTF8
        @{ executionState = $run.state; status = $run.result.status; description = $run.result.description; evidenceCount = @($run.snapshot.evidence).Count; coverageGaps = $run.result.coverage_gaps; resultFile = '.local/last-demo-result.json' } | ConvertTo-Json -Depth 6
        Write-Output ('Evidence: ' + $identity.runUrl + '/evidence')
        Write-Output ('Navigation: ' + $identity.runUrl + '/navigation')
        break
    }
    if ([DateTime]::UtcNow -ge $deadline) {
        Write-Output ('Still ' + $run.state + '. Check worker terminal or use the saved run URL.')
        break
    }
    Start-Sleep -Seconds 1
}
