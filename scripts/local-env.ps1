param([string]$ConfigPath = '')

$ErrorActionPreference = 'Stop'
$OutputEncoding = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
$dataRoot = Join-Path $projectRoot '.local'
New-Item -ItemType Directory -Path $dataRoot -Force | Out-Null
$sqliteFile = (Join-Path $dataRoot 'windows.sqlite3').Replace('\', '/')
$defaults = @{
    COMPLIANCE_ENVIRONMENT = 'local'
    COMPLIANCE_DATABASE_URL = ('sqlite:///' + $sqliteFile)
    COMPLIANCE_INPUT_ROOT = (Join-Path $projectRoot 'fixtures')
    COMPLIANCE_STORAGE_DIR = (Join-Path $dataRoot 'artifacts')
    COMPLIANCE_SOURCE_CONFIG = (Join-Path $projectRoot 'config\sources.example.json')
    COMPLIANCE_INTERNAL_TOKEN = 'local-demo-internal-token'
    INTERNAL_TOKEN = 'local-demo-internal-token'
    COMPLIANCE_ALLOWED_MINES = ''
    EVIDENCE_API_URL = 'http://127.0.0.1:8092'
    PI_WORKER_URL = 'http://127.0.0.1:8093'
    PI_HOST = '127.0.0.1'
    PI_PORT = '8093'
    PI_ENABLED = 'false'
    PI_MODEL_BASE_URL = ''
    PI_MODEL_ID = ''
    PI_MODEL_API_KEY = ''
    OCR_ENDPOINT = ''
    OCR_PROVIDER = 'http-json'
    OCR_AUTHORIZATION = ''
    VISION_ENDPOINT = ''
    VISION_MODEL = ''
    VISION_AUTHORIZATION = ''
    PI_MAX_TOOL_CALLS = '80'
    PI_MODEL_REQUEST_TIMEOUT_MS = '120000'
    MAX_MODEL_CONCURRENCY = '2'
    MAX_TOOL_CONCURRENCY = '4'
    MAX_OCR_CONCURRENCY = '2'
    CALLBACK_ALLOWED_HOSTS = ''
    COMPLIANCE_CALLBACK_AUTHORIZATION = ''
}
foreach ($name in $defaults.Keys) {
    [Environment]::SetEnvironmentVariable($name, $defaults[$name], 'Process')
}
if (-not $ConfigPath) { $ConfigPath = Join-Path $projectRoot '.env.local' }
if (Test-Path -LiteralPath $ConfigPath) {
    foreach ($line in Get-Content -LiteralPath $ConfigPath -Encoding UTF8) {
        $entry = $line.Trim()
        if (-not $entry -or $entry.StartsWith('#')) { continue }
        $pair = $entry -split '=', 2
        if ($pair.Length -ne 2 -or $pair[0] -notmatch '^[A-Z][A-Z0-9_]*$') {
            throw 'Invalid local configuration line; use KEY=value.'
        }
        $value = $pair[1].Trim()
        if ($value.Length -ge 2 -and (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'")))) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        [Environment]::SetEnvironmentVariable($pair[0], $value, 'Process')
    }
}
# This launcher is for local Windows development only.
$env:COMPLIANCE_ENVIRONMENT = 'local'
if ($env:COMPLIANCE_INTERNAL_TOKEN -ne $env:INTERNAL_TOKEN) {
    throw 'COMPLIANCE_INTERNAL_TOKEN and INTERNAL_TOKEN must match in .env.local.'
}

function Get-LocalJson {
    param([string]$Uri)
    $response = Invoke-WebRequest -UseBasicParsing -Uri $Uri
    # Windows PowerShell 5.1 guesses Latin-1 for application/json without a
    # charset. The API sends UTF-8 JSON; decode the bytes explicitly.
    $bytes = $response.RawContentStream.ToArray()
    return ([Text.Encoding]::UTF8.GetString($bytes) | ConvertFrom-Json)
}
