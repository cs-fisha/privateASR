$ErrorActionPreference = 'Stop'
$clientDir = (Resolve-Path $PSScriptRoot).Path
$exe = Join-Path $clientDir 'dist\PrivateASR.exe'
if (-not (Test-Path $exe)) { throw 'Run build.ps1 first.' }
$startup = [Environment]::GetFolderPath('Startup')
$shortcutPath = Join-Path $startup 'Private ASR.lnk'
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $exe
$shortcut.WorkingDirectory = $clientDir
$shortcut.Save()
Write-Host "Installed startup shortcut: $shortcutPath"
