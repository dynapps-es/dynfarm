<#
.SYNOPSIS
Install dynfarm on Windows:   .\install.ps1 [-Version 0.1.0]      (without -Version: the latest)

.DESCRIPTION
Installs uv (https://docs.astral.sh/uv/) when it is missing, then installs dynfarm from PyPI as a tool of its own, in
its own environment (uv tool install dynfarm, or dynfarm==VERSION), and runs dynfarm --version. Nothing here needs a
credential: the package is public. Piped from the web it is

  powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/dynapps-es/dynfarm/main/install.ps1 | iex"

.PARAMETER Version
The release to install, 0.1.0 or v0.1.0. Default: the latest.
#>
[CmdletBinding()]
param(
    [string]$Version = ""
)

$ErrorActionPreference = "Stop"

$Name = "dynfarm"
$UvInstallerUrl = "https://astral.sh/uv/install.ps1"

# `throw`, not `exit`: this script is often run as `& ([scriptblock]::Create(...))` from an open terminal, where `exit`
# would close the person's session.
function Fail([string]$Message) {
    throw "install.ps1: $Message"
}

# Run a native command; stop with a message when it exits non-zero. PowerShell does not do that by itself.
function Invoke-Native([string]$FailureMessage, [string]$Command, [string[]]$Arguments) {
    & $Command @Arguments
    if ($LASTEXITCODE -ne 0) { Fail $FailureMessage }
}

if ($Version.StartsWith("v")) {
    $Given = $Version
    $Version = $Version.Substring(1)
    if ($Version -eq "") { Fail "'$Given' is not a version like 0.1.0" }
}
# The version ends up in a requirement: accept what a release is called and nothing else.
if ($Version -ne "" -and $Version -notmatch '^[0-9][0-9A-Za-z.+!_-]*$') {
    Fail "'$Version' is not a version like 0.1.0"
}

# -- uv ------------------------------------------------------------------------------------------------------------

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "uv is not installed; installing it from $UvInstallerUrl"
    try {
        Invoke-RestMethod $UvInstallerUrl | Invoke-Expression
    } catch {
        Fail "the uv installer failed: $($_.Exception.Message)"
    }
    # The installer updates the PATH of new shells; this one needs it too.
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        Fail "uv was installed but is not on PATH; open a new terminal and run this again"
    }
}

# -- dynfarm -------------------------------------------------------------------------------------------------------

$Requirement = $Name
if ($Version -ne "") { $Requirement = "$Name==$Version" }
Write-Host "Installing $Requirement from PyPI"
Invoke-Native "uv could not install $Requirement from PyPI (is the version published?)" `
    "uv" @("tool", "install", "--reinstall", $Requirement)

# -- check ---------------------------------------------------------------------------------------------------------

$BinDir = (& uv tool dir --bin).Trim()
if (Get-Command $Name -ErrorAction SilentlyContinue) {
    $CommandPath = $Name
} else {
    $CommandPath = Join-Path $BinDir "$Name.exe"
    Write-Host "$BinDir is not on your PATH yet: run 'uv tool update-shell' and open a new terminal."
}
Invoke-Native "$Name was installed but does not run" $CommandPath @("--version")

Write-Host ""
Write-Host "Next: $Name auth login --hostname https://<your farm>"
