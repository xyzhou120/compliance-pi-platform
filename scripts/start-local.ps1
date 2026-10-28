param(
    [Parameter(Mandatory = $true)][ValidateSet('api', 'worker', 'pi')][string]$Role,
    [string]$ConfigPath = '',
    [switch]$Once
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'local-env.ps1') -ConfigPath $ConfigPath
$pythonPath = Join-Path (Split-Path -Parent $PSScriptRoot) '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) { throw 'Run scripts/setup-local.ps1 first.' }
switch ($Role) {
    'api' {
        Write-Output 'API http://127.0.0.1:8092 ; Swagger http://127.0.0.1:8092/docs'
        & $pythonPath -m uvicorn apps.control_api.main:app --host 127.0.0.1 --port 8092
    }
    'worker' {
        Write-Output ('Control worker; PI_ENABLED=' + $env:PI_ENABLED)
        if ($Once) { & $pythonPath -m apps.control_api.worker --once }
        else { & $pythonPath -m apps.control_api.worker }
    }
    'pi' {
        if ($env:PI_ENABLED -ne 'true') { throw 'Set PI_ENABLED=true in .env.local to start real Pi.' }
        if (-not $env:PI_MODEL_BASE_URL -or -not $env:PI_MODEL_ID) { throw 'Configure PI_MODEL_BASE_URL and PI_MODEL_ID in .env.local.' }
        & node apps/pi-worker/src/server.mjs
    }
}
if ($LASTEXITCODE -ne 0) { throw ($Role + ' process exited with code ' + $LASTEXITCODE) }
