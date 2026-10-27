$ErrorActionPreference = 'Stop'
Write-Host ''
Write-Host 'Playerok monitoring setup' -ForegroundColor Cyan
Write-Host 'This uses an UNOFFICIAL third-party API client, not an official Playerok SDK.' -ForegroundColor Yellow
Write-Host 'Playerok warns sellers about scripts that handle profile data. Use at your own risk.' -ForegroundColor Yellow
Write-Host 'This integration is read-only: it will not send buyer messages, change deal statuses, create/delete listings, or send Steam offers.'
Write-Host 'If Playerok blocks or challenges the connection, stop using the integration; do not add proxy/anti-bot workarounds.'
Write-Host ''

$consent = Read-Host 'Do you accept this risk and want to install the optional monitor? Type YES'
if ($consent -cne 'YES') {
    Write-Host 'Cancelled. No token was saved and no packages were installed.'
    exit 0
}

$versionText = & py -3 -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
if ($LASTEXITCODE -ne 0 -or -not $versionText) {
    throw 'Python was not found. The base bot was not changed.'
}
$versionParts = $versionText.Trim().Split('.')
$major = [int]$versionParts[0]
$minor = [int]$versionParts[1]
if ($major -lt 3 -or ($major -eq 3 -and $minor -lt 12)) {
    throw "Optional Playerok SDK requires Python 3.12+. Found Python $versionText. Existing bot dependencies were not changed."
}

$requirements = Join-Path $PSScriptRoot 'requirements-playerok.txt'
if (-not (Test-Path -LiteralPath $requirements -PathType Leaf)) {
    throw 'requirements-playerok.txt is missing. Update the bot first.'
}
Write-Host 'Installing the pinned optional SDK. The existing requirements.txt is unchanged.'
& py -3 -m pip install --disable-pip-version-check --requirement $requirements
if ($LASTEXITCODE -ne 0) {
    throw 'Optional SDK installation failed. No Playerok token was saved.'
}

$secure = Read-Host 'Paste the Playerok session token (input is hidden)' -AsSecureString
$ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try {
    $token = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr)
    $secure.Dispose()
}
$token = $token.Trim()
if ([string]::IsNullOrWhiteSpace($token)) {
    throw 'Empty token. Nothing was saved.'
}

$oneSetId = Read-Host 'Playerok listing ID for the 1-set lot (blank = manual review only)'
$fiveSetId = Read-Host 'Playerok listing ID for the 5-set lot (blank = manual review only)'
if ($oneSetId -and $fiveSetId -and $oneSetId.Trim() -eq $fiveSetId.Trim()) {
    Write-Warning 'Both listing IDs are identical. Deals will be sent to manual review until IDs are corrected.'
}

[Environment]::SetEnvironmentVariable('PLAYEROK_TOKEN', $token, 'User')
[Environment]::SetEnvironmentVariable('PLAYEROK_ITEM_1_SET_ID', $oneSetId.Trim(), 'User')
[Environment]::SetEnvironmentVariable('PLAYEROK_ITEM_5_SET_ID', $fiveSetId.Trim(), 'User')
$token = $null

Write-Host ''
Write-Host 'Configuration saved in the current Windows user environment.' -ForegroundColor Green
Write-Host 'The token was not sent to Telegram and was not written to the repository.'
Write-Host 'Playerok automatic delivery remains OFF; this only enables read-only deal/chat monitoring.'
Write-Host 'Close old bot windows, then start C:\CardBot\start.bat to load the new user environment.'
