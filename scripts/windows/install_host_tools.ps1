param()

$ErrorActionPreference = "Stop"

if ($args.Count -ne 0) {
    [Console]::Error.WriteLine("usage: install_host_tools.ps1")
    exit 2
}

$principal = [Security.Principal.WindowsPrincipal]::new(
    [Security.Principal.WindowsIdentity]::GetCurrent()
)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Windows host-tool installation requires an elevated PowerShell"
}
if (-not [Environment]::Is64BitOperatingSystem) {
    throw "Pony Windows host tools require x64 Windows"
}

$ripgrepVersion = "15.2.0"
$ripgrepExecutableHash = (
    "14231169855EC5205CF5A1B6F1DB358FF4AED4247C86B69CE8AAE647C77F6680"
)
$packageRoot = Join-Path $env:ProgramFiles "ripgrep"
$sourceRoot = Join-Path $packageRoot (
    "ripgrep-$ripgrepVersion-x86_64-pc-windows-msvc"
)
$sourceExecutable = Join-Path $sourceRoot "rg.exe"
$toolRoot = Join-Path $env:ProgramFiles "Pony Host Tools"
$trustedExecutable = Join-Path $toolRoot "rg.exe"
$noticeNames = @("COPYING", "LICENSE-MIT", "UNLICENSE")
$maximumNoticeBytes = 65536

function Assert-NoReparsePoint {
    param([Parameter(Mandatory = $true)][string]$Path)

    $item = Get-Item -LiteralPath $Path -Force
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "host-tool path must not be a reparse point: $Path"
    }
    return $item
}

function Set-TrustedDirectoryAcl {
    param([Parameter(Mandatory = $true)][string]$Path)

    $administrators = [Security.Principal.SecurityIdentifier]::new("S-1-5-32-544")
    $system = [Security.Principal.SecurityIdentifier]::new("S-1-5-18")
    $users = [Security.Principal.SecurityIdentifier]::new("S-1-5-32-545")
    $inheritance = (
        [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
        [Security.AccessControl.InheritanceFlags]::ObjectInherit
    )
    $security = [Security.AccessControl.DirectorySecurity]::new()
    $security.SetAccessRuleProtection($true, $false)
    $security.SetOwner($administrators)
    foreach ($entry in @(
        @($administrators, [Security.AccessControl.FileSystemRights]::FullControl),
        @($system, [Security.AccessControl.FileSystemRights]::FullControl),
        @($users, [Security.AccessControl.FileSystemRights]::ReadAndExecute)
    )) {
        $security.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
            $entry[0],
            $entry[1],
            $inheritance,
            [Security.AccessControl.PropagationFlags]::None,
            [Security.AccessControl.AccessControlType]::Allow
        ))
    }
    Set-Acl -LiteralPath $Path -AclObject $security
}

function Set-TrustedFileAcl {
    param([Parameter(Mandatory = $true)][string]$Path)

    $administrators = [Security.Principal.SecurityIdentifier]::new("S-1-5-32-544")
    $system = [Security.Principal.SecurityIdentifier]::new("S-1-5-18")
    $users = [Security.Principal.SecurityIdentifier]::new("S-1-5-32-545")
    $security = [Security.AccessControl.FileSecurity]::new()
    $security.SetAccessRuleProtection($true, $false)
    $security.SetOwner($administrators)
    foreach ($entry in @(
        @($administrators, [Security.AccessControl.FileSystemRights]::FullControl),
        @($system, [Security.AccessControl.FileSystemRights]::FullControl),
        @($users, [Security.AccessControl.FileSystemRights]::ReadAndExecute)
    )) {
        $security.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
            $entry[0],
            $entry[1],
            [Security.AccessControl.AccessControlType]::Allow
        ))
    }
    Set-Acl -LiteralPath $Path -AclObject $security
}

function Assert-TrustedAcl {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][bool]$Directory
    )

    $administratorsSid = "S-1-5-32-544"
    $expectedRights = @{
        $administratorsSid = [Security.AccessControl.FileSystemRights]::FullControl
        "S-1-5-18" = [Security.AccessControl.FileSystemRights]::FullControl
        "S-1-5-32-545" = (
            [Security.AccessControl.FileSystemRights]::ReadAndExecute -bor
            [Security.AccessControl.FileSystemRights]::Synchronize
        )
    }
    $security = Get-Acl -LiteralPath $Path
    $ownerSid = $security.GetOwner(
        [Security.Principal.SecurityIdentifier]
    ).Value
    $rules = @($security.GetAccessRules(
        $true,
        $true,
        [Security.Principal.SecurityIdentifier]
    ))
    if (-not $security.AreAccessRulesProtected -or $ownerSid -ne $administratorsSid) {
        throw "trusted host-tool ACL owner or inheritance mismatch"
    }
    if ($rules.Count -ne $expectedRights.Count) {
        throw "trusted host-tool ACL rule count mismatch"
    }
    foreach ($rule in $rules) {
        $sid = $rule.IdentityReference.Value
        if (
            -not $expectedRights.ContainsKey($sid) -or
            $rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow -or
            $rule.IsInherited -or
            [int]$rule.FileSystemRights -ne [int]$expectedRights[$sid] -or
            $rule.PropagationFlags -ne [Security.AccessControl.PropagationFlags]::None
        ) {
            throw "trusted host-tool ACL rule mismatch"
        }
        $expectedInheritance = if ($Directory) {
            [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
            [Security.AccessControl.InheritanceFlags]::ObjectInherit
        }
        else {
            [Security.AccessControl.InheritanceFlags]::None
        }
        if ($rule.InheritanceFlags -ne $expectedInheritance) {
            throw "trusted host-tool ACL inheritance mismatch"
        }
    }
}

$winget = (Get-Command winget.exe -CommandType Application -ErrorAction Stop).Source
& $winget @(
    "install",
    "--exact",
    "--id", "BurntSushi.ripgrep.MSVC",
    "--version", $ripgrepVersion,
    "--scope", "machine",
    "--location", $packageRoot,
    "--silent",
    "--accept-package-agreements",
    "--accept-source-agreements",
    "--disable-interactivity"
)
$noApplicableUpgrade = -1978335189  # 0x8A15002B
if ($LASTEXITCODE -notin @(0, 3010, $noApplicableUpgrade)) {
    throw "WinGet ripgrep installation failed with exit code $LASTEXITCODE"
}

Assert-NoReparsePoint -Path $packageRoot | Out-Null
Assert-NoReparsePoint -Path $sourceRoot | Out-Null
$source = Assert-NoReparsePoint -Path $sourceExecutable
if (-not $source.PSIsContainer -and $source.Length -gt 0) {
    $sourceHash = (Get-FileHash -LiteralPath $sourceExecutable -Algorithm SHA256).Hash
}
else {
    throw "WinGet ripgrep executable is not a regular non-empty file"
}
if ($sourceHash -ne $ripgrepExecutableHash) {
    throw "WinGet ripgrep executable hash mismatch"
}

New-Item -ItemType Directory -Path $toolRoot -Force | Out-Null
Assert-NoReparsePoint -Path $toolRoot | Out-Null
Set-TrustedDirectoryAcl -Path $toolRoot
Assert-TrustedAcl -Path $toolRoot -Directory $true
$allowedNames = @("rg.exe") + $noticeNames
$unexpected = @(
    Get-ChildItem -LiteralPath $toolRoot -Force |
        Where-Object { $_.Name -notin $allowedNames }
)
if ($unexpected.Count -ne 0) {
    throw "Pony host-tool directory contains unexpected entries"
}

foreach ($name in $allowedNames) {
    $destination = Join-Path $toolRoot $name
    if (Test-Path -LiteralPath $destination) {
        $existing = Assert-NoReparsePoint -Path $destination
        if ($existing.PSIsContainer) {
            throw "managed host-tool entry is not a regular file"
        }
        Remove-Item -LiteralPath $destination -Force
    }
    $sourcePath = if ($name -eq "rg.exe") {
        $sourceExecutable
    }
    else {
        Join-Path $sourceRoot $name
    }
    $sourceItem = Assert-NoReparsePoint -Path $sourcePath
    if (
        $sourceItem.PSIsContainer -or
        $sourceItem.Length -le 0 -or
        ($name -ne "rg.exe" -and $sourceItem.Length -gt $maximumNoticeBytes)
    ) {
        throw "host-tool source is not a bounded regular file"
    }
    Copy-Item -LiteralPath $sourcePath -Destination $destination -Force
    Set-TrustedFileAcl -Path $destination
    $installed = Assert-NoReparsePoint -Path $destination
    if (
        $installed.PSIsContainer -or
        $installed.Length -le 0 -or
        ($name -ne "rg.exe" -and $installed.Length -gt $maximumNoticeBytes)
    ) {
        throw "installed host tool is not a bounded regular file"
    }
    Assert-TrustedAcl -Path $destination -Directory $false
}

$trusted = Assert-NoReparsePoint -Path $trustedExecutable
if ($trusted.PSIsContainer -or $trusted.Length -le 0) {
    throw "trusted ripgrep executable is not a regular non-empty file"
}
if ((Get-FileHash -LiteralPath $trustedExecutable -Algorithm SHA256).Hash -ne (
    $ripgrepExecutableHash
)) {
    throw "trusted ripgrep executable hash mismatch"
}
$versionLine = @(& $trustedExecutable --version)[0]
if ($LASTEXITCODE -ne 0 -or $versionLine -notlike "ripgrep $ripgrepVersion *") {
    throw "trusted ripgrep version check failed"
}

$machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
$pathEntries = @($machinePath -split ";" | Where-Object { $_ })
if (-not ($pathEntries | Where-Object { $_.TrimEnd("\") -ieq $toolRoot })) {
    $updatedPath = (($machinePath.TrimEnd(";") + ";" + $toolRoot).Trim(";"))
    [Environment]::SetEnvironmentVariable("Path", $updatedPath, "Machine")
}

Write-Output "trusted_rg=$trustedExecutable"
Write-Output "ripgrep_version=$ripgrepVersion"
