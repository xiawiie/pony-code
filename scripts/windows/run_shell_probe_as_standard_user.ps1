param(
    [string]$Python = (Join-Path $env:GITHUB_WORKSPACE ".venv\Scripts\python.exe")
)

$ErrorActionPreference = "Stop"
$userName = "pony_ci_standard"
$userRoot = Join-Path $env:RUNNER_TEMP $userName
$stdout = Join-Path $userRoot "stdout.txt"
$stderr = Join-Path $userRoot "stderr.txt"
$wrapper = Join-Path $userRoot "run-probe.ps1"
$passwordBytes = New-Object byte[] 32
[Security.Cryptography.RandomNumberGenerator]::Fill($passwordBytes)
$passwordText = [Convert]::ToBase64String($passwordBytes) + "aA1!"
$password = ConvertTo-SecureString $passwordText -AsPlainText -Force
$principal = "$env:COMPUTERNAME\$userName"
$credential = [PSCredential]::new($principal, $password)

try {
    New-LocalUser -Name $userName -Password $password -AccountNeverExpires |
        Out-Null
    $usersGroup = Get-LocalGroup -SID "S-1-5-32-545"
    Add-LocalGroupMember -Group $usersGroup -Member $userName

    New-Item -ItemType Directory -Path $userRoot | Out-Null
    & icacls.exe $userRoot /inheritance:r /grant:r "${principal}:(OI)(CI)F" /q
    if ($LASTEXITCODE -ne 0) {
        throw "failed to grant the standard user a private temporary directory"
    }
    & icacls.exe $env:GITHUB_WORKSPACE /grant "${principal}:(OI)(CI)RX" /q
    if ($LASTEXITCODE -ne 0) {
        throw "failed to grant the standard user repository access"
    }

    $tempLiteral = $userRoot.Replace("'", "''")
    $workspaceLiteral = $env:GITHUB_WORKSPACE.Replace("'", "''")
    $pythonLiteral = $Python.Replace("'", "''")
    $probeLiteral = (Join-Path $env:GITHUB_WORKSPACE `
        "scripts\windows\probe_shell_backend.py").Replace("'", "''")
    @"
`$ErrorActionPreference = "Stop"
`$env:TEMP = '$tempLiteral'
`$env:TMP = '$tempLiteral'
Set-Location '$workspaceLiteral'
& '$pythonLiteral' '$probeLiteral'
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
        -WorkingDirectory $env:GITHUB_WORKSPACE `
        -RedirectStandardOutput $stdout `
        -RedirectStandardError $stderr `
        -Wait `
        -PassThru

    Get-Content $stdout
    Get-Content $stderr
    if ($process.ExitCode -ne 0) {
        throw "standard-user Windows shell probe failed with exit code $($process.ExitCode)"
    }
}
finally {
    if (Get-LocalUser -Name $userName -ErrorAction SilentlyContinue) {
        Remove-LocalUser -Name $userName
    }
    Remove-Item -LiteralPath $userRoot -Recurse -Force -ErrorAction SilentlyContinue
}
