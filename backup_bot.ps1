param(
    [string]$BotRoot = 'C:\CardBot'
)

$ErrorActionPreference = 'Stop'
$backupRoot = Join-Path $BotRoot 'резервные'
$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$destination = Join-Path $backupRoot ("backup_" + $stamp)
if (Test-Path -LiteralPath $destination) {
    $destination = Join-Path $backupRoot ("backup_" + $stamp + "_" + [guid]::NewGuid().ToString('N').Substring(0, 8))
}

$files = @(
    'bot.py',
    'order_parser.py',
    'funpay_orders.py',
    'funpay_orders.json',
    'start.bat',
    'update_bot.ps1',
    'run_bot.ps1',
    'backup_bot.ps1',
    'funpay_config.json',
    'funpay_config.paused.json',
    'inventory_cache.json',
    'auto_update.log',
    'bot_errors.log',
    'steam_session.json',
    'vouchers.json'
)

try {
    New-Item -ItemType Directory -Path $destination -Force | Out-Null
    $copied = @()
    foreach ($name in $files) {
        $source = Join-Path $BotRoot $name
        if (Test-Path -LiteralPath $source -PathType Leaf) {
            Copy-Item -LiteralPath $source -Destination (Join-Path $destination $name) -ErrorAction Stop
            $copied += $name
        }
    }

    if ($copied.Count -eq 0) {
        throw 'No bot or runtime files were found to back up.'
    }

    $manifest = @(
        "Created: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
        "Files copied: $($copied.Count)"
        'This backup is local and must not be committed to GitHub.'
        ''
        $copied
    ) -join [Environment]::NewLine
    $utf8Bom = New-Object System.Text.UTF8Encoding -ArgumentList $true
    [System.IO.File]::WriteAllText((Join-Path $destination 'backup_manifest.txt'), $manifest, $utf8Bom)
    Write-Output "Локальная резервная копия создана: $destination"
} catch {
    Write-Error ("Backup failed: " + $_.Exception.Message)
    exit 1
}