$ErrorActionPreference = 'Stop'
$mutex = New-Object System.Threading.Mutex($false, 'Local\CardBotBotSingleInstance')
$held = $false
$exitCode = 23

try {
    try {
        $held = $mutex.WaitOne(0)
    } catch [System.Threading.AbandonedMutexException] {
        $held = $true
    }

    if ($held) {
        $exitCode = 0
        $names = @('TG_TOKEN', 'STEAM_ID', 'STEAM_LOGIN', 'STEAM_PASSWORD')
        foreach ($name in $names) {
            $value = [Environment]::GetEnvironmentVariable($name, 'User')
            if ([string]::IsNullOrWhiteSpace($value)) {
                throw "$name is not configured in the Windows user environment."
            }
            [Environment]::SetEnvironmentVariable($name, $value, 'Process')
        }

        # Playerok settings are optional. A missing token keeps the monitor disabled.
        $optionalNames = @('PLAYEROK_TOKEN', 'PLAYEROK_USER_AGENT', 'PLAYEROK_ITEM_1_SET_ID', 'PLAYEROK_ITEM_5_SET_ID')
        foreach ($name in $optionalNames) {
            $value = [Environment]::GetEnvironmentVariable($name, 'User')
            if (-not [string]::IsNullOrWhiteSpace($value)) {
                [Environment]::SetEnvironmentVariable($name, $value, 'Process')
            }
        }

        Set-Location 'C:\CardBot'
        & py -3 bot.py
        $exitCode = $LASTEXITCODE
    } else {
        Write-Output 'Bot already running; duplicate launch skipped.'
    }
} finally {
    if ($held) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}

exit $exitCode
