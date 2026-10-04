@echo off
setlocal
set "AILAB_PWSH="
where.exe pwsh.exe >nul 2>&1
if errorlevel 1 goto installed
pwsh.exe -NoProfile -NonInteractive -Command "if ($PSVersionTable.PSVersion.Major -lt 7) { exit 1 }" >nul 2>&1
if errorlevel 1 goto installed
set "AILAB_PWSH=pwsh.exe"
goto launch

:installed
if not exist "%ProgramFiles%\PowerShell\7\pwsh.exe" goto bundled
set "AILAB_PWSH=%ProgramFiles%\PowerShell\7\pwsh.exe"
goto launch

:bundled
if not exist "%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\native\powershell\pwsh.exe" goto missing
set "AILAB_PWSH=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\native\powershell\pwsh.exe"
goto launch

:missing
echo PowerShell 7 is required. Install it separately or run Start-AILab.ps1 with an existing PowerShell 7.
exit /b 1

:launch
"%AILAB_PWSH%" -NoProfile -File "%~dp0Start-AILab.ps1" %*
set "AILAB_EXIT=%ERRORLEVEL%"
if not "%AILAB_EXIT%"=="0" pause
exit /b %AILAB_EXIT%
