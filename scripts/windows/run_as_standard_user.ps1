param(
    [Parameter(Mandatory = $true)]
    [string]$Script,
    [string]$Python = (Join-Path $env:GITHUB_WORKSPACE ".venv\Scripts\python.exe")
)

$ErrorActionPreference = "Stop"
$workspace = [IO.Path]::GetFullPath($env:GITHUB_WORKSPACE)
$scriptPath = [IO.Path]::GetFullPath((Join-Path $workspace $Script))
if (-not $scriptPath.StartsWith($workspace + [IO.Path]::DirectorySeparatorChar) -or
    -not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
    throw "standard-user script must be a repository file"
}
if ([IO.Path]::GetExtension($scriptPath) -notin ".ps1", ".py") {
    throw "standard-user script must be PowerShell or Python"
}

$userName = "pony_ci_standard"
$userRoot = Join-Path $env:RUNNER_TEMP $userName
$stdout = Join-Path $userRoot "stdout.txt"
$stderr = Join-Path $userRoot "stderr.txt"
$wrapper = Join-Path $userRoot "run-script.ps1"
$passwordBytes = New-Object byte[] 32
[Security.Cryptography.RandomNumberGenerator]::Fill($passwordBytes)
$passwordText = [Convert]::ToBase64String($passwordBytes) + "aA1!"
$password = ConvertTo-SecureString $passwordText -AsPlainText -Force
$principal = "$env:COMPUTERNAME\$userName"
$currentPrincipal = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$credential = [PSCredential]::new($principal, $password)

try {
    New-LocalUser -Name $userName -Password $password -AccountNeverExpires |
        Out-Null
    $usersGroup = Get-LocalGroup -SID "S-1-5-32-545"
    Add-LocalGroupMember -Group $usersGroup -Member $userName

    New-Item -ItemType Directory -Path $userRoot | Out-Null
    & icacls.exe $userRoot /inheritance:r /grant:r `
        "${principal}:(OI)(CI)F" "${currentPrincipal}:(OI)(CI)F" /q
    if ($LASTEXITCODE -ne 0) {
        throw "failed to secure the standard user's temporary directory"
    }
    & icacls.exe $workspace /grant "${principal}:(OI)(CI)M" /t /c /q
    if ($LASTEXITCODE -ne 0) {
        throw "failed to grant the standard user repository access"
    }
    if ($env:UV_CACHE_DIR) {
        & icacls.exe $env:UV_CACHE_DIR /grant "${principal}:(OI)(CI)M" /t /c /q
        if ($LASTEXITCODE -ne 0) {
            throw "failed to grant the standard user uv cache access"
        }
    }

    $homePath = Join-Path $userRoot "home"
    New-Item -ItemType Directory -Path $homePath | Out-Null
    $tempLiteral = $userRoot.Replace("'", "''")
    $homeLiteral = $homePath.Replace("'", "''")
    $workspaceLiteral = $workspace.Replace("'", "''")
    $pythonLiteral = $Python.Replace("'", "''")
    $scriptLiteral = $scriptPath.Replace("'", "''")
    $uvLiteral = (Get-Command uv).Source.Replace("'", "''")
    $invocation = if ([IO.Path]::GetExtension($scriptPath) -eq ".py") {
        "& '$pythonLiteral' '$scriptLiteral'"
    }
    else {
        "& '$scriptLiteral'"
    }
    @"
`$ErrorActionPreference = "Stop"
`$env:TEMP = '$tempLiteral'
`$env:TMP = '$tempLiteral'
`$env:HOME = '$homeLiteral'
`$env:USERPROFILE = '$homeLiteral'
`$env:RUNNER_TEMP = '$tempLiteral'
`$env:GITHUB_WORKSPACE = '$workspaceLiteral'
`$env:PONY_CI_UV = '$uvLiteral'
Set-Location '$workspaceLiteral'
$invocation
exit `$LASTEXITCODE
"@ | Set-Content -Path $wrapper -Encoding UTF8

    $systemPowerShell = Join-Path $env:SystemRoot `
        "System32\WindowsPowerShell\v1.0\powershell.exe"
    $process = Start-Process -FilePath $systemPowerShell `
        -Credential $credential `
        -ArgumentList @(
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy", "Bypass",
            "-File", $wrapper
        ) `
        -WorkingDirectory $workspace `
        -RedirectStandardOutput $stdout `
        -RedirectStandardError $stderr `
        -Wait `
        -PassThru

    Get-Content $stdout
    Get-Content $stderr
    if ($process.ExitCode -ne 0) {
        throw "standard-user script failed with exit code $($process.ExitCode)"
    }
}
finally {
    if (Get-LocalUser -Name $userName -ErrorAction SilentlyContinue) {
        Remove-LocalUser -Name $userName
    }
    Remove-Item -LiteralPath $userRoot -Recurse -Force -ErrorAction SilentlyContinue
}
