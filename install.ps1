<#
  L2Axis Control - installer
  Checks Python, installs the dependency and creates Desktop / Start Menu shortcuts
  (with the panel icon and "Run as administrator" enabled).

  Usage: double-click install.bat   (or: powershell -ExecutionPolicy Bypass -File install.ps1)
  -ShortcutDir lets you create the shortcut somewhere else (used for testing).
#>
param(
    [string]$ShortcutDir = [Environment]::GetFolderPath("Desktop"),
    [switch]$NoStartMenu
)

$ErrorActionPreference = "Stop"
$AppDir   = Split-Path -Parent $MyInvocation.MyCommand.Path
$Script   = Join-Path $AppDir "l2axis_control.pyw"
$Icon     = Join-Path $AppDir "assets\app.ico"
$Name     = "L2Axis Control"

function Step($text)  { Write-Host "`n> $text" -ForegroundColor Yellow }
function Ok($text)    { Write-Host "  OK  $text" -ForegroundColor Green }
function Warn($text)  { Write-Host "  !!  $text" -ForegroundColor DarkYellow }
function Fail($text)  { Write-Host "  XX  $text" -ForegroundColor Red; exit 1 }

Write-Host "=== L2Axis Control - installer ===" -ForegroundColor Cyan

# ---------------------------------------------------------------- 1. Python 3.11+
Step "Checking Python 3.11+"
function Find-Python {
    foreach ($cmd in @("python", "py")) {
        try {
            $out = & $cmd -c "import sys; print(sys.version_info >= (3, 11)); print(sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $out -and $out[0] -eq "True") { return $out[1].Trim() }
        } catch { }
    }
    return $null
}
$python = Find-Python
if (-not $python) {
    Warn "Python 3.11 or newer was not found."
    $answer = Read-Host "  Install Python 3.12 now with winget? (y/n)"
    if ($answer -match '^[yY]') {
        winget install --id Python.Python.3.12 --exact --accept-package-agreements --accept-source-agreements
        $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
        $python = Find-Python
    }
    if (-not $python) { Fail "Install Python 3.11+ from https://www.python.org/downloads/ (tick 'Add python.exe to PATH') and run this installer again." }
}
$pythonw = Join-Path (Split-Path -Parent $python) "pythonw.exe"
if (-not (Test-Path $pythonw)) { Fail "pythonw.exe not found next to $python" }
Ok "Python found"

# ---------------------------------------------------------------- 2. dependency
Step "Installing the dependency (pywebview)"
& $python -m pip install --user --disable-pip-version-check -q -r (Join-Path $AppDir "requirements.txt")
if ($LASTEXITCODE -ne 0) { Fail "pip could not install the requirements." }
& $python -c "import webview" 2>$null
if ($LASTEXITCODE -ne 0) { Fail "pywebview is not importable after installing." }
Ok "pywebview installed"

# ---------------------------------------------------------------- 3. folder placement
Step "Checking the folder placement"
$l2exe = Join-Path (Split-Path -Parent $AppDir) "system\l2.exe"
if (Test-Path $l2exe) {
    Ok "Game client found next to this folder"
} else {
    Warn "system\l2.exe was not found next to this folder."
    Warn "Move the L2AxisControl folder INSIDE your L2Axis client folder (next to 'system') so the panel can open the game."
}

# ---------------------------------------------------------------- 4. shortcuts
Step "Creating shortcuts"
function New-AdminShortcut($dir) {
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
    $lnk = Join-Path $dir "$Name.lnk"
    $shell = New-Object -ComObject WScript.Shell
    $s = $shell.CreateShortcut($lnk)
    $s.TargetPath       = $pythonw
    $s.Arguments        = "`"$Script`""
    $s.WorkingDirectory = $AppDir
    $s.IconLocation     = "$Icon,0"
    $s.Description      = "L2Axis Control - companion panel for L2Axis"
    $s.Save()
    # "Run as administrator": l2.exe requires it, so the panel needs it to open clients
    $bytes = [System.IO.File]::ReadAllBytes($lnk)
    $bytes[0x15] = $bytes[0x15] -bor 0x20
    [System.IO.File]::WriteAllBytes($lnk, $bytes)
    return $lnk
}
Ok ("Desktop:    " + (New-AdminShortcut $ShortcutDir))
if (-not $NoStartMenu) {
    Ok ("Start Menu: " + (New-AdminShortcut (Join-Path ([Environment]::GetFolderPath("Programs")) "L2Axis Control")))
}

Write-Host "`nDone! Open 'L2Axis Control' from your Desktop (Windows will ask for administrator permission)." -ForegroundColor Cyan
