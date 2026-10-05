$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

if (-not (Test-Path '.venv\Scripts\python.exe')) {
  py -3 -m venv .venv
}

& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt

$pyinstallerArgs = @(
  '--noconfirm',
  '--clean',
  '--windowed',
  '--name', 'Musorka',
  '--collect-all', 'googleapiclient',
  '--collect-all', 'google_auth_oauthlib'
)

if (Test-Path 'assets\musorka.ico') {
  $pyinstallerArgs += @('--icon', 'assets\musorka.ico', '--add-data', 'assets\musorka.ico;assets')
}

$pyinstallerArgs += 'main.py'
& .\.venv\Scripts\python.exe -m PyInstaller @pyinstallerArgs

Write-Host ''
Write-Host 'Ready: dist\Musorka\Musorka.exe'
