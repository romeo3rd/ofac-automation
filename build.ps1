$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot

function Assert-LastExitCode {
    param([string]$StepName)

    if ($LASTEXITCODE -ne 0) {
        throw "$StepName failed with exit code $LASTEXITCODE."
    }
}

if (-not (Test-Path ".\venv\Scripts\python.exe")) {
    py -m venv venv
    Assert-LastExitCode "Creating virtual environment"
}

$Python = ".\venv\Scripts\python.exe"

& $Python -m pip install --upgrade pip
Assert-LastExitCode "Installing pip"

& $Python -m pip install -r requirements-build.txt
Assert-LastExitCode "Installing Python dependencies"

$env:PLAYWRIGHT_BROWSERS_PATH = "0"
$LocalBrowsersDir = ".\venv\Lib\site-packages\playwright\driver\package\.local-browsers"

if (Test-Path $LocalBrowsersDir) {
    Get-ChildItem -Path $LocalBrowsersDir -Directory -Filter "chromium-*" |
        Where-Object { $_.Name -notlike "chromium_headless_shell-*" } |
        Remove-Item -Recurse -Force
}

if (Test-Path (Join-Path $LocalBrowsersDir "chromium_headless_shell-*")) {
    Write-Host "Using existing Playwright Chromium headless shell files."
}
else {
    & $Python -m playwright install --only-shell chromium
    Assert-LastExitCode "Installing Playwright Chromium headless shell"
}

$BuildStamp = Get-Date -Format "yyyyMMddHHmmss"
$WorkPath = Join-Path $ProjectRoot "build\pyinstaller-$BuildStamp"
$SpecPath = Join-Path $ProjectRoot "build\spec"
$DistRoot = Join-Path $ProjectRoot "dist"
$DistDir = Join-Path $DistRoot "OFAC Automation"
$IconPath = Join-Path $ProjectRoot "OFAC.ico"
New-Item -ItemType Directory -Path $WorkPath, $SpecPath -Force | Out-Null

if (-not (Test-Path $IconPath)) {
    throw "Icon file not found: $IconPath"
}

if (Test-Path $DistDir) {
    Remove-Item $DistDir -Recurse -Force
}

$PyInstallerArgs = @(
    "--noconfirm",
    "--clean",
    "--onedir",
    "--windowed",
    "--name", "OFAC Automation",
    "--icon", $IconPath,
    "--collect-all", "customtkinter",
    "--collect-all", "playwright",
    "--workpath", $WorkPath,
    "--specpath", $SpecPath,
    "--distpath", $DistRoot,
    "app.py"
)

& $Python -m PyInstaller @PyInstallerArgs
Assert-LastExitCode "PyInstaller build"

$BundledBrowsersDir = Join-Path $DistDir "_internal\playwright\driver\package\.local-browsers"
if (Test-Path $BundledBrowsersDir) {
    Get-ChildItem -Path $BundledBrowsersDir -Directory -Filter "chromium-*" |
        Where-Object { $_.Name -notlike "chromium_headless_shell-*" } |
        Remove-Item -Recurse -Force
}

if (-not (Test-Path (Join-Path $BundledBrowsersDir "chromium_headless_shell-*"))) {
    throw "Build is missing the Playwright Chromium headless shell."
}

Copy-Item ".\README.txt" (Join-Path $DistDir "README.txt") -Force
New-Item -ItemType Directory -Path ".\installer" -Force | Out-Null

Write-Host ""
Write-Host "Build complete:"
Write-Host "  $DistDir\OFAC Automation.exe"
Write-Host ""
Write-Host "SHA256:"
Get-FileHash (Join-Path $DistDir "OFAC Automation.exe") -Algorithm SHA256

$IsccPath = $null
$Iscc = Get-Command "ISCC.exe" -ErrorAction SilentlyContinue

if ($Iscc) {
    $IsccPath = $Iscc.Source
}

if (-not $IsccPath) {
    $CandidatePaths = @(
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
    )

    foreach ($Path in $CandidatePaths) {
        if ($Path -and (Test-Path $Path)) {
            $IsccPath = (Get-Item $Path).FullName
            break
        }
    }
}

if ($IsccPath) {
    Write-Host ""
    Write-Host "Creating installer..."
    & $IsccPath ".\installer.iss"
    Assert-LastExitCode "Inno Setup installer build"

    $InstallerPath = Join-Path $ProjectRoot "installer\OFAC-Automation-Setup.exe"
    Write-Host ""
    Write-Host "Installer complete:"
    Write-Host "  $InstallerPath"
    Write-Host ""
    Write-Host "Installer SHA256:"
    Get-FileHash $InstallerPath -Algorithm SHA256
}
else {
    Write-Host ""
    Write-Host "Inno Setup was not found, so only the app folder was created."
    Write-Host "Install Inno Setup 6, then run this script again to create:"
    Write-Host "  installer\OFAC-Automation-Setup.exe"
}
