[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$SshTarget,
    [string]$IdentityFile
)

$ErrorActionPreference = 'Stop'
$RemotePath = '/srv/personal-vault/architecture/architecture-rules.md'

try {
    $sshArguments = @()
    if ($IdentityFile) {
        $sshArguments += @('-i', $IdentityFile)
    }
    $sshArguments += @($SshTarget, "cat -- '$RemotePath'")
    $rules = & ssh @sshArguments
    if ($LASTEXITCODE -ne 0) {
        throw 'the server-hosted Architecture Rules mirror could not be read'
    }
} catch {
    Write-Error "Architecture Rules operational mirror is unavailable: $($_.Exception.Message)"
    exit 1
}

$document = ($rules -join "`n").Trim()
if (
    [string]::IsNullOrWhiteSpace($document) -or
    $document -notmatch '(?m)^# Personal Vault Architecture Rules\s*$' -or
    $document -notmatch '(?m)^Canonical source: Google Doc\s*$' -or
    $document -notmatch '(?m)^Operational mirror:' -or
    $document -notmatch '(?m)^AR-[A-Z]{2}-\d{3}' -or
    $document -notmatch '(?m)^AR-ST-007\b' -or
    $document -notmatch '(?m)^AR-VC-005\b' -or
    $document -match '(?i)<html|sign in to continue'
) {
    Write-Error 'Architecture Rules operational mirror is empty or obviously malformed.'
    exit 1
}

$document
