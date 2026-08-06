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
$runId = [Guid]::NewGuid().ToString("N")
$controlRoot = Join-Path $env:RUNNER_TEMP "$userName-$runId"
$toolRoot = Join-Path $env:ProgramFiles "pony-ci-tools-$runId"
$userRoot = Join-Path $controlRoot "profile"
$stdout = Join-Path $controlRoot "stdout.txt"
$stderr = Join-Path $controlRoot "stderr.txt"
$wrapper = Join-Path $controlRoot "run-script.ps1"
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

    New-Item -ItemType Directory -Path $controlRoot | Out-Null
    & icacls.exe $controlRoot /inheritance:r /grant:r `
        "${principal}:(OI)(CI)F" "${currentPrincipal}:(OI)(CI)F" /q
    if ($LASTEXITCODE -ne 0) {
        throw "failed to secure the standard-user control directory"
    }
    New-Item -ItemType Directory -Path $userRoot | Out-Null
    & icacls.exe $userRoot /inheritance:r /grant:r `
        "${principal}:(OI)(CI)F" "${currentPrincipal}:F" /q
    if ($LASTEXITCODE -ne 0) {
        throw "failed to secure the standard user's private directory"
    }
    & icacls.exe $userRoot /setowner $principal /q
    if ($LASTEXITCODE -ne 0) {
        throw "failed to assign the standard user's private directory"
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

    $chocolateyRoot = if ($env:ChocolateyInstall) {
        $env:ChocolateyInstall
    }
    else {
        Join-Path $env:ProgramData "chocolatey"
    }
    $rgSource = Get-ChildItem -LiteralPath (Join-Path $chocolateyRoot "lib") `
        -Filter "rg.exe" -File -Recurse -ErrorAction SilentlyContinue |
        Select-Object -First 1 -ExpandProperty FullName
    if (-not $rgSource) {
        $rgSource = (Get-Command rg.exe -CommandType Application).Source
    }
    New-Item -ItemType Directory -Path $toolRoot | Out-Null
    Copy-Item -LiteralPath $rgSource -Destination (Join-Path $toolRoot "rg.exe")
    $basePython = & $Python -c "import sys; print(sys.base_prefix)"
    if ($LASTEXITCODE -ne 0 -or -not $basePython) {
        throw "failed to locate the base Python runtime"
    }
    $pythonToolRoot = Join-Path $toolRoot "python"
    New-Item -ItemType Directory -Path $pythonToolRoot | Out-Null
    Copy-Item -LiteralPath (Join-Path $basePython "python.exe") `
        -Destination $pythonToolRoot
    Get-ChildItem -LiteralPath $basePython -Filter "*.dll" -File |
        Copy-Item -Destination $pythonToolRoot
    Copy-Item -LiteralPath (Join-Path $basePython "DLLs") `
        -Destination $pythonToolRoot -Recurse
    Copy-Item -LiteralPath (Join-Path $basePython "Lib") `
        -Destination $pythonToolRoot -Recurse
    & icacls.exe $toolRoot /inheritance:r /grant:r `
        "${principal}:(OI)(CI)RX" "${currentPrincipal}:(OI)(CI)F" `
        "*S-1-5-18:(OI)(CI)F" /t /c /q
    if ($LASTEXITCODE -ne 0) {
        throw "failed to secure the standard-user native tool directory"
    }

    $homePath = Join-Path $userRoot "home"
    $appDataPath = Join-Path $homePath "AppData\Roaming"
    $localAppDataPath = Join-Path $homePath "AppData\Local"
    $tempLiteral = $userRoot.Replace("'", "''")
    $homeLiteral = $homePath.Replace("'", "''")
    $appDataLiteral = $appDataPath.Replace("'", "''")
    $localAppDataLiteral = $localAppDataPath.Replace("'", "''")
    $workspaceLiteral = $workspace.Replace("'", "''")
    $pythonLiteral = $Python.Replace("'", "''")
    $scriptLiteral = $scriptPath.Replace("'", "''")
    $uvLiteral = (Get-Command uv).Source.Replace("'", "''")
    $trustedPath = $pythonToolRoot + [IO.Path]::PathSeparator + `
        $toolRoot + [IO.Path]::PathSeparator + $env:PATH
    $pathLiteral = $trustedPath.Replace("'", "''")
    $invocation = if ([IO.Path]::GetExtension($scriptPath) -eq ".py") {
        "& '$pythonLiteral' '$scriptLiteral'"
    }
    else {
        "& '$scriptLiteral'"
    }
    @"
`$ErrorActionPreference = "Stop"
`$env:APPDATA = '$appDataLiteral'
`$env:TEMP = '$tempLiteral'
`$env:TMP = '$tempLiteral'
`$env:HOME = '$homeLiteral'
`$env:LOCALAPPDATA = '$localAppDataLiteral'
`$env:USERPROFILE = '$homeLiteral'
`$env:RUNNER_TEMP = '$tempLiteral'
`$env:GITHUB_WORKSPACE = '$workspaceLiteral'
`$env:PONY_CI_UV = '$uvLiteral'
`$env:PATH = '$pathLiteral'
New-Item -ItemType Directory -Path '$appDataLiteral' -Force | Out-Null
New-Item -ItemType Directory -Path '$localAppDataLiteral' -Force | Out-Null
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
        -PassThru

    $stdoutLines = 0
    $stderrLines = 0
    while (-not $process.WaitForExit(5000)) {
        $stdoutContent = @(Get-Content $stdout -ErrorAction SilentlyContinue)
        $stderrContent = @(Get-Content $stderr -ErrorAction SilentlyContinue)
        if ($stdoutContent.Count -gt $stdoutLines) {
            $stdoutContent[$stdoutLines..($stdoutContent.Count - 1)]
            $stdoutLines = $stdoutContent.Count
        }
        if ($stderrContent.Count -gt $stderrLines) {
            $stderrContent[$stderrLines..($stderrContent.Count - 1)] |
                ForEach-Object { [Console]::Error.WriteLine($_) }
            $stderrLines = $stderrContent.Count
        }
    }

    @(Get-Content $stdout -ErrorAction SilentlyContinue) |
        Select-Object -Skip $stdoutLines
    @(Get-Content $stderr -ErrorAction SilentlyContinue) |
        Select-Object -Skip $stderrLines |
        ForEach-Object { [Console]::Error.WriteLine($_) }
    if ($process.ExitCode -ne 0) {
        throw "standard-user script failed with exit code $($process.ExitCode)"
    }
}
finally {
    if (Get-LocalUser -Name $userName -ErrorAction SilentlyContinue) {
        Remove-LocalUser -Name $userName
    }
    Remove-Item -LiteralPath $controlRoot -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $toolRoot -Recurse -Force -ErrorAction SilentlyContinue
}
