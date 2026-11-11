$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $PSScriptRoot
$local = Join-Path $project '.local'
$stateFile = Join-Path $local 'state.json'
$state = Get-Content -LiteralPath $stateFile -Raw -Encoding UTF8 | ConvertFrom-Json
$dbPort = [int]$state.dbPort
$appPort = [int]$state.appPort
$mysqld = 'D:\Tools\mysql\mysql-9.7.0-winx64\bin\mysqld.exe'
$dbConfig = 'D:\graduationProject\runtime\juxia-data\mysql.ini'
$java = 'C:\Program Files\Java\jdk-17.0.2\bin\java.exe'
$jar = Join-Path $project 'backend\target\juxia-1.0.0.jar'

function Test-Free([int]$port) {
    $listener = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, $port)
    try { $listener.Start(); return $true } catch { return $false } finally { $listener.Stop() }
}

function Test-Listening([int]$port) {
    $conn = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    return [bool]$conn
}

Write-Host "[1/3] database port $dbPort ..." -NoNewline
if (Test-Listening $dbPort) {
    Write-Host ' already running'
} else {
    Start-Process -FilePath $mysqld -ArgumentList ('--defaults-file="' + $dbConfig + '"') -WindowStyle Hidden `
        -RedirectStandardOutput 'D:\graduationProject\runtime\juxia-data\mysqld.out.log' `
        -RedirectStandardError 'D:\graduationProject\runtime\juxia-data\mysqld.err.log' | Out-Null
    $ok = $false
    for ($i = 0; $i -lt 60; $i++) { Start-Sleep -Milliseconds 700; if (Test-Listening $dbPort) { $ok = $true; break } }
    if (-not $ok) { Write-Host ' failed'; Read-Host 'Press Enter to exit'; exit 1 }
    Write-Host ' started'
}

Write-Host "[2/3] application port $appPort ..." -NoNewline
if (Test-Listening $appPort) {
    Write-Host ' already running'
} else {
    $app = Start-Process -FilePath $java -ArgumentList @('-Dfile.encoding=UTF-8', '-jar', "`"$jar`"") `
        -WorkingDirectory $project -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $local 'app.log') -RedirectStandardError (Join-Path $local 'app.err.log')
    $state.appPort = $appPort
    $state.appPid = $app.Id
    $state.initialized = $true
    $state | ConvertTo-Json | Set-Content -LiteralPath $stateFile -Encoding UTF8
    Write-Host ' starting (about 15s)'
}

Write-Host '[3/3] waiting for health check ...'
$ready = $false
for ($i = 0; $i -lt 90; $i++) {
    try {
        $r = Invoke-RestMethod -Uri "http://127.0.0.1:$appPort/api/health" -TimeoutSec 3
        if ($r.databaseReady) { $ready = $true; break }
    } catch { }
    Start-Sleep -Seconds 1
}
if (-not $ready) {
    Write-Host ' health check failed, see .local\app.log'
    Read-Host 'Press Enter to exit'
    exit 1
}

$url = "http://127.0.0.1:$appPort"
Write-Host ''
Write-Host "Juxia is ready: $url"
Write-Host 'Accounts (password 123456): reader / author / author2 / reviewer / admin'
Start-Process $url
Start-Sleep -Seconds 2
