$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
python -m PyInstaller --noconfirm --clean packaging/ResumeBuilderV3.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed" }
Copy-Item LICENSE, README.md, THIRD_PARTY_NOTICES.txt dist/ResumeBuilderV3/
# Preserve dependency-provided notices, rather than replacing them with this project's license.
python packaging/collect_licenses.py dist/ResumeBuilderV3/dependency-licenses
if ($LASTEXITCODE -ne 0) { throw "Dependency notice collection failed" }
$env:QT_QPA_PLATFORM = "offscreen"
$smoke = Join-Path ([System.IO.Path]::GetTempPath()) ("resume-builder-smoke-" + [guid]::NewGuid())
$process = Start-Process -FilePath "$PWD/dist/ResumeBuilderV3/ResumeBuilderV3.exe" -ArgumentList "--smoke-check", "`"$smoke`"" -Wait -PassThru
if ($process.ExitCode -ne 0) { throw "Frozen smoke check failed (exit $($process.ExitCode)). Preserve diagnostics in $smoke" }
$result = Get-Content (Join-Path $smoke "smoke-result.json") | ConvertFrom-Json
if (-not $result.ok) { throw "Frozen resource/database initialization failed" }
Remove-Item Env:QT_QPA_PLATFORM
Compress-Archive -Path dist/ResumeBuilderV3 -DestinationPath dist/ResumeBuilderV3-Windows.zip -Force
Write-Host "Built dist/ResumeBuilderV3-Windows.zip. Extract the entire folder before launching."
