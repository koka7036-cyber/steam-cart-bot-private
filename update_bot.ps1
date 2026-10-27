$ErrorActionPreference = 'Stop'
$repo = 'C:\CardBot_repo'
$live = 'C:\CardBot'
$log = Join-Path $live 'auto_update.log'
$marker = Join-Path $live 'auto_update_commit.txt'
$requirementsSnapshot = Join-Path $live 'requirements.installed.txt'
$deployFiles = @('bot.py', 'order_parser.py', 'update_bot.ps1', 'run_bot.ps1', 'backup_bot.ps1')

function Write-Log([string]$Message) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Message"
    Add-Content -LiteralPath $log -Value $line -Encoding UTF8
}

try {
    if (-not (Test-Path -LiteralPath (Join-Path $repo '.git'))) { throw 'Клон GitHub не найден.' }
    if (-not (Test-Path -LiteralPath (Join-Path $live 'bot.py'))) { throw 'Рабочий bot.py не найден.' }

    foreach ($name in $deployFiles) {
        if (-not (Test-Path -LiteralPath (Join-Path $repo $name))) { throw "В репозитории нет $name." }
    }
    if (-not (Test-Path -LiteralPath (Join-Path $repo 'requirements.txt'))) { throw 'В репозитории нет requirements.txt.' }

    $dirty = @(& git -C $repo status --porcelain 2>$null)
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось проверить клон GitHub.' }
    if ($dirty.Count -gt 0) { throw 'В клоне есть локальные изменения; автоматическое обновление остановлено.' }

    $null = & git -C $repo pull --ff-only --quiet 2>&1
    $pullCode = $LASTEXITCODE
    if ($pullCode -ne 0) { throw 'Не удалось получить код с GitHub. Рабочая версия оставлена без изменений.' }

    $commit = (& git -C $repo rev-parse HEAD 2>$null).Trim()
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($commit)) { throw 'Не удалось определить версию GitHub.' }

    foreach ($name in $deployFiles) {
        if (-not (Test-Path -LiteralPath (Join-Path $repo $name))) { throw "После обновления отсутствует $name." }
    }

    $newRequirements = [System.IO.File]::ReadAllText((Join-Path $repo 'requirements.txt'))
    if (Test-Path -LiteralPath $requirementsSnapshot) {
        $installedRequirements = [System.IO.File]::ReadAllText($requirementsSnapshot)
        if ($newRequirements -ne $installedRequirements) {
            throw 'Список библиотек изменился. Код не обновлялся; требуется отдельное обновление библиотек.'
        }
    } else {
        $utf8 = New-Object System.Text.UTF8Encoding -ArgumentList $false
        [System.IO.File]::WriteAllText($requirementsSnapshot, $newRequirements, $utf8)
    }

    $deployedCommit = ''
    if (Test-Path -LiteralPath $marker) {
        $deployedCommit = [System.IO.File]::ReadAllText($marker).Trim()
    }
    if ($deployedCommit -eq $commit) {
        Write-Log "Без изменений; активная версия $commit."
        exit 0
    }

    $botText = [System.IO.File]::ReadAllText((Join-Path $repo 'bot.py'))
    if ($botText -notmatch '(?m)^\s*import\s+threading\b' -or
        $botText -notmatch '(?m)^\s*def\s+funpay_background_worker\s*\(' -or
        $botText -notmatch '(?m)^\s*bot\s*=\s*telebot\.TeleBot\s*\(') {
        throw 'Структура bot.py изменилась; обновление остановлено для ручной проверки.'
    }

    if ($botText -notmatch '(?m)^\s*threading\.Thread\s*\(\s*target\s*=\s*funpay_background_worker') {
        $botText = $botText.TrimEnd() + [Environment]::NewLine +
            'threading.Thread(target=funpay_background_worker, daemon=True).start()' + [Environment]::NewLine
    }
    if ($botText -notmatch '(?m)^\s*bot\.(infinity_polling|polling)\s*\(') {
        $botText = $botText.TrimEnd() + [Environment]::NewLine + 'bot.infinity_polling()' + [Environment]::NewLine
    }

    $backupScript = Join-Path $live 'backup_bot.ps1'
    if (-not (Test-Path -LiteralPath $backupScript)) { throw 'Локальный backup_bot.ps1 не найден; обновление остановлено.' }
    & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $backupScript -BotRoot $live | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось создать локальную резервную копию; обновление отменено.' }

    $stage = Join-Path $env:TEMP ('CardBotUpdate_' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $stage | Out-Null
    $utf8 = New-Object System.Text.UTF8Encoding -ArgumentList $false
    $utf8Bom = New-Object System.Text.UTF8Encoding -ArgumentList $true

    foreach ($name in $deployFiles) {
        $source = Join-Path $repo $name
        $target = Join-Path $stage $name
        if ($name -eq 'bot.py') {
            [System.IO.File]::WriteAllText($target, $botText, $utf8)
        } elseif ($name.EndsWith('.ps1')) {
            $scriptText = [System.IO.File]::ReadAllText($source)
            [System.IO.File]::WriteAllText($target, $scriptText, $utf8Bom)
        } else {
            Copy-Item -LiteralPath $source -Destination $target
        }
    }

    $backupRoot = Join-Path $live 'update_backups'
    New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null
    $backup = Join-Path $backupRoot ((Get-Date -Format 'yyyyMMdd_HHmmss') + '_' + $commit.Substring(0, 8))
    New-Item -ItemType Directory -Path $backup | Out-Null

    $rollbackFiles = @($deployFiles) + @('start.bat')
    foreach ($name in $rollbackFiles) {
        $source = Join-Path $live $name
        if (Test-Path -LiteralPath $source -PathType Leaf) {
            Copy-Item -LiteralPath $source -Destination (Join-Path $backup $name)
        }
    }

    try {
        foreach ($name in $deployFiles) {
            Copy-Item -LiteralPath (Join-Path $stage $name) -Destination (Join-Path $live $name) -Force
        }
        [System.IO.File]::WriteAllText($marker, $commit, $utf8)
    } catch {
        foreach ($name in $rollbackFiles) {
            $saved = Join-Path $backup $name
            if (Test-Path -LiteralPath $saved -PathType Leaf) {
                Copy-Item -LiteralPath $saved -Destination (Join-Path $live $name) -Force
            }
        }
        throw
    }

    Write-Log "Обновлено до $commit; код сохранён в $backup, данные — в резервной копии."
    Write-Output "Бот обновлён до версии $commit."
    exit 0
} catch {
    Write-Log "ОБНОВЛЕНИЕ ОСТАНОВЛЕНО: $($_.Exception.Message)"
    Write-Error $_.Exception.Message
    exit 1
}