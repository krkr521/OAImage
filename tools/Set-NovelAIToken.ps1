param([switch]$MakeDefault)
$ErrorActionPreference = 'Stop'
$OutputEncoding = [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$token = Read-Host 'NovelAI Persistent API Token (hidden input)' -AsSecureString
$plainToken = [System.Net.NetworkCredential]::new('', $token).Password.Trim()
if ([string]::IsNullOrWhiteSpace($plainToken)) { throw 'Token is empty; configuration was not changed.' }
try {
    $payload = @{token=$plainToken; make_default=[bool]$MakeDefault} | ConvertTo-Json -Compress
    $payload | & 'C:\qqbot\MaiBot\.venv\Scripts\python.exe' (Join-Path $PSScriptRoot 'set_novelai_token.py')
    if ($LASTEXITCODE -ne 0) { throw 'Could not save NovelAI Token.' }
} finally {
    $plainToken = $null
    $payload = $null
    $token.Dispose()
}
