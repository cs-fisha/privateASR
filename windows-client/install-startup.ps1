$ErrorActionPreference = 'Stop'
$clientDir = (Resolve-Path $PSScriptRoot).Path
$python = Join-Path $clientDir '.venv\Scripts\pythonw.exe'
$script = Join-Path $clientDir 'asr_client.py'
if (-not (Test-Path $python)) { throw 'Create .venv and install requirements.txt first.' }
$startup = [Environment]::GetFolderPath('Startup')
$shortcutPath = Join-Path $startup 'Private ASR.lnk'
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $python
$shortcut.Arguments = "`"$script`""
$shortcut.WorkingDirectory = $clientDir
$shortcut.Save()
Write-Host "Installed startup shortcut: $shortcutPath"
