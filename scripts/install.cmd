@echo off
REM ============================================================================
REM Mason Agent Installer for Windows (CMD wrapper)
REM ============================================================================
REM This batch file launches the PowerShell installer for users running CMD.
REM That PowerShell installer is the real thing: it installs every required
REM dependency (uv, Python, Git, C toolchain, Node.js, ripgrep, ffmpeg,
REM Chromium, cua-driver) BEFORE it installs the framework, then clones the
REM repo and installs it editable.
REM
REM Usage:
REM   curl -fsSL https://raw.githubusercontent.com/Mochi-Sora/Mason-Agent/MasonAgent/scripts/install.cmd -o install.cmd && install.cmd && del install.cmd
REM
REM Or if you're already in PowerShell, use the direct command instead:
REM   iex (irm https://raw.githubusercontent.com/Mochi-Sora/Mason-Agent/MasonAgent/scripts/install.ps1)
REM ============================================================================

echo.
echo  Mason Agent Installer
echo  Launching PowerShell installer...
echo.

powershell -ExecutionPolicy ByPass -NoProfile -Command "iex (irm https://raw.githubusercontent.com/Mochi-Sora/Mason-Agent/MasonAgent/scripts/install.ps1)"

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo  Installation failed. Please try running PowerShell directly:
    echo    powershell -ExecutionPolicy ByPass -c "iex (irm https://raw.githubusercontent.com/Mochi-Sora/Mason-Agent/MasonAgent/scripts/install.ps1)"
    echo.
    pause
    exit /b 1
)
