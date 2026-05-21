# llm-relay one-command installer for Windows native.
#
# Usage from PowerShell:
#   irm https://raw.githubusercontent.com/ArkNill/llm-relay/main/scripts/install.ps1 | iex
#
# What this does:
#   1. Verifies Python 3.9+ is on PATH.
#   2. Detects an active virtual environment (uses it if present).
#   3. pip install --upgrade "llm-relay[all]".
#   4. Calls `llm-relay init` -- atomic: starts the Windows daemon, waits
#      for /_health, then writes ANTHROPIC_BASE_URL into ~/.claude/settings.json.
#      Routing is NEVER activated unless the proxy actually responds.
#   5. Prints the dashboard URL.
#
# What this does NOT do:
#   - Install Python (we don't bundle a runtime).
#   - Install Claude Code / Codex / Gemini (vendor responsibility).
#   - Modify your shell profile or PATH (we surface a hint if --user install
#     puts the entry point somewhere PATH doesn't see).
#
# Prerequisites and venv guidance: see README.md.

$ErrorActionPreference = 'Stop'

function Write-Step($msg) {
    Write-Host ""
    Write-Host "==> $msg" -ForegroundColor Cyan
}

function Write-OK($msg) {
    Write-Host "    [OK] $msg" -ForegroundColor Green
}

function Write-Warn($msg) {
    Write-Host "    [WARN] $msg" -ForegroundColor Yellow
}

function Write-Err($msg) {
    Write-Host "    [FAIL] $msg" -ForegroundColor Red
}


# ── 1. Python ──────────────────────────────────────────────────────────────

Write-Step "Checking Python"

$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) {
    Write-Err "python not found on PATH."
    Write-Host ""
    Write-Host "Install Python 3.9 or newer first. Quick options:" -ForegroundColor White
    Write-Host "  winget install Python.Python.3.12" -ForegroundColor Gray
    Write-Host "  or download from https://www.python.org/downloads/" -ForegroundColor Gray
    Write-Host ""
    Write-Host "Full prerequisites and venv guidance:" -ForegroundColor White
    Write-Host "  https://github.com/ArkNill/llm-relay#prerequisites" -ForegroundColor Gray
    exit 1
}

$pyVer = (& python --version 2>&1)
Write-OK "$pyVer at $($py.Source)"

# Verify >= 3.9
$verMatch = [regex]::Match($pyVer, 'Python\s+(\d+)\.(\d+)')
if ($verMatch.Success) {
    $major = [int]$verMatch.Groups[1].Value
    $minor = [int]$verMatch.Groups[2].Value
    if ($major -lt 3 -or ($major -eq 3 -and $minor -lt 9)) {
        Write-Err "Python $major.$minor is older than the required 3.9."
        Write-Host "  Upgrade with: winget install Python.Python.3.12" -ForegroundColor Gray
        exit 1
    }
}


# ── 2. venv detection ──────────────────────────────────────────────────────

Write-Step "Checking for active virtual environment"

$pipUserFlag = @()
if ($env:VIRTUAL_ENV) {
    Write-OK "venv active at $env:VIRTUAL_ENV (installing there)"
} else {
    Write-Warn "No venv active. Falling back to --user install."
    Write-Host "      (A venv is recommended for clean uninstall. See README.)" -ForegroundColor Gray
    $pipUserFlag = @('--user')
}


# ── 3. pip install ─────────────────────────────────────────────────────────

Write-Step "Installing llm-relay[all] from PyPI"

$pipArgs = @('-m', 'pip', 'install') + $pipUserFlag + @('--upgrade', 'llm-relay[all]')
& python @pipArgs

if ($LASTEXITCODE -ne 0) {
    Write-Err "pip install failed (exit code $LASTEXITCODE)."
    exit $LASTEXITCODE
}
Write-OK "Installed."


# ── 4. PATH sanity for --user installs ─────────────────────────────────────

if (-not $env:VIRTUAL_ENV) {
    $userScripts = & python -c "import sysconfig; print(sysconfig.get_path('scripts', f'{sysconfig.get_default_scheme()}_user'))"
    if ($userScripts -and (Test-Path $userScripts)) {
        $pathParts = $env:PATH -split ';'
        if ($pathParts -notcontains $userScripts) {
            Write-Warn "Your --user Scripts dir is not on PATH:"
            Write-Host "        $userScripts" -ForegroundColor Gray
            Write-Host "      Add it once via:" -ForegroundColor Gray
            Write-Host "        [Environment]::SetEnvironmentVariable('Path', `"`$env:Path;$userScripts`", 'User')" -ForegroundColor Gray
            Write-Host "      then reopen this terminal. Continuing with the absolute path for this run." -ForegroundColor Gray
            $env:PATH = "$env:PATH;$userScripts"
        }
    }
}


# ── 5. llm-relay init ──────────────────────────────────────────────────────

Write-Step "Running llm-relay init (atomic: server + health-gate + routing)"

& llm-relay init
$initExit = $LASTEXITCODE

if ($initExit -ne 0) {
    Write-Err "llm-relay init failed (exit code $initExit)."
    Write-Host "      Server may not be running. Inspect:" -ForegroundColor Gray
    Write-Host "        Get-Content `$env:USERPROFILE\.llm-relay\service-error.log -Tail 30" -ForegroundColor Gray
    exit $initExit
}


# ── 6. Done ────────────────────────────────────────────────────────────────

Write-Step "Install complete"
Write-Host "    Dashboard:  http://localhost:8083/dashboard/" -ForegroundColor White
Write-Host "    Display:    http://localhost:8083/display/" -ForegroundColor White
Write-Host ""
Write-Host "    Verify everything:" -ForegroundColor Gray
Write-Host "      llm-relay verify all" -ForegroundColor Gray
Write-Host ""
Write-Host "    Roll back (turn proxy off, leave package installed):" -ForegroundColor Gray
Write-Host "      llm-relay service stop" -ForegroundColor Gray
Write-Host "      llm-relay service uninstall" -ForegroundColor Gray
