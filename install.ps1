<#
.SYNOPSIS
Install dynfarm on Windows:   .\install.ps1 [-Version 0.1.0]      (without -Version: the latest)

.DESCRIPTION
Installs uv (https://docs.astral.sh/uv/) when it is missing, then installs dynfarm as a tool of its own, in its own
environment, from one of two places:

  the project's package registry   when UV_INDEX_DYNFARM_USERNAME and UV_INDEX_DYNFARM_PASSWORD are set (a GitLab user
                                   name and a personal access token with the read_api scope).
  git                              otherwise, with the git credentials you already have for gitlab.dynapps.be.

A token never travels in a URL: uv reads the registry's credentials from those two variables. The project is private,
so one of the two ways of signing in is needed; see "Install" in README.md.

uv syntax this relies on (uv 0.5 and later; docs.astral.sh/uv/concepts/indexes/):
  --index NAME=URL                      an extra package index with a name (uv tool install --help);
  UV_INDEX_<NAME>_USERNAME / _PASSWORD  the credentials of the index called NAME, NAME upper-cased with - as _
                                        ("Providing credentials" in the indexes page). The index is searched before
                                        PyPI, which still serves the dependencies.

.PARAMETER Version
The release to install, 0.1.0 or v0.1.0. Default: the latest.
#>
[CmdletBinding()]
param(
    [string]$Version = ""
)

$ErrorActionPreference = "Stop"

$Name = "dynfarm"
# The index's name decides the variables above: dynfarm -> UV_INDEX_DYNFARM_USERNAME / UV_INDEX_DYNFARM_PASSWORD.
$IndexName = "dynfarm"
$RegistryUrl = "https://gitlab.dynapps.be/api/v4/projects/4378/packages/pypi/simple"
$GitUrl = "https://gitlab.dynapps.be/infra-es/farm-ai/cli.git"
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
# The version ends up in a requirement and in a git ref: accept what a release is called and nothing else.
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

if ($env:UV_INDEX_DYNFARM_USERNAME -and $env:UV_INDEX_DYNFARM_PASSWORD) {
    $Requirement = $Name
    if ($Version -ne "") { $Requirement = "$Name==$Version" }
    Write-Host "Installing $Requirement from the project's package registry"
    Invoke-Native "uv could not install $Requirement from the registry: check the user name and the token (read_api scope)" `
        "uv" @("tool", "install", "--reinstall", "--index", "$IndexName=$RegistryUrl", $Requirement)
} else {
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        Fail "git is needed to install without registry credentials (see README.md)"
    }
    if ($Version -eq "") {
        # The newest tag by version number, not by date. Asking also proves the person's git credentials work here.
        $Tags = & git ls-remote --tags --refs --sort=-v:refname $GitUrl "v*"
        if ($LASTEXITCODE -ne 0) {
            Fail "git could not read ${GitUrl}: sign in to gitlab.dynapps.be with git, or set the registry variables"
        }
        # Final releases only (v1.2.3): a pre-release such as v1.3.0rc1 sorts above v1.3.0 and must not be "the latest".
        $Tag = $Tags | ForEach-Object { if ($_ -match 'refs/tags/(v[0-9]+\.[0-9]+\.[0-9]+)$') { $Matches[1] } } |
            Select-Object -First 1
        if (-not $Tag) { Fail "no release tag found in $GitUrl" }
    } else {
        $Tag = "v$Version"
        & git ls-remote --exit-code --tags --refs $GitUrl "refs/tags/$Tag" | Out-Null
        if ($LASTEXITCODE -ne 0) {
            Fail "git could not find the tag $Tag in $GitUrl (no access, or no such release)"
        }
    }
    Write-Host "Installing $Name $Tag from git ($GitUrl)"
    Invoke-Native "uv could not install $Name from git" "uv" @("tool", "install", "--reinstall", "git+$GitUrl@$Tag")
}

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
