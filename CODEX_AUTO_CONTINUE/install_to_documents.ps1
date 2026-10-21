# Copies this project to <Documents>\1_<gae><bal>\CODEX_AUTO_CONTINUE (ASCII-only script; Korean folder name built from code points)
$docs = [Environment]::GetFolderPath('MyDocuments')
$dev  = '1_' + [char]0xAC1C + [char]0xBC1C
$dest = Join-Path (Join-Path $docs $dev) 'CODEX_AUTO_CONTINUE'
$src  = Split-Path -Parent $MyInvocation.MyCommand.Path
if ((Resolve-Path $src).Path -eq $dest) { Write-Host "Already in $dest"; exit 0 }
New-Item -ItemType Directory -Force -Path $dest | Out-Null
robocopy $src $dest /E /XD .venv __pycache__ data build dist .git /XF *.pyc | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $dest 'data') | Out-Null
Write-Host "Copied to: $dest"
Write-Host "Run start.cmd in that folder."
