$ErrorActionPreference = 'Stop'
$repo = 'C:\CardBot_repo'
$live = 'C:\CardBot'
$log = Join-Path $live 'auto_update.log'
$marker = Join-Path $live 'auto_update_commit.txt'
$requirementsSnapshot = Join-Path $live 'requirements.installed.txt'
$deployFiles = @('bot.py', 'order_parser.py', 'funpay_orders.py', 'sales_analytics.py', 'playerok_orders.py', 'playerok_monitor.py', 'requirements-playerok.txt', 'configure_playerok.ps1', 'configure_playerok.bat', 'update_bot.ps1', 'run_bot.ps1', 'backup_bot.ps1')

function Write-Log([string]$Message) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Message"
    Add-Content -LiteralPath $log -Value $line -Encoding UTF8
}

try {
    if (-not (Test-Path -LiteralPath (Join-Path $repo '.git'))) { throw 'Клон GitHub не найден.' }
    if (-not (Test-Path -LiteralPath (Join-Path $live 'bot.py'))) { throw 'Рабочий bot.py не найден.' }

    # Обновляем только remote-tracking ref. Локальные изменения в клоне не затрагиваем.
    $null = & git -C $repo fetch --quiet origin '+refs/heads/main:refs/remotes/origin/main' 2>&1
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось получить обновления с GitHub. Рабочая версия оставлена без изменений.' }

    $commit = (& git -C $repo rev-parse 'origin/main^{commit}' 2>$null).Trim()
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($commit)) { throw 'Не удалось определить актуальную версию origin/main.' }

    $deployedCommit = ''
    if (Test-Path -LiteralPath $marker) {
        $deployedCommit = [System.IO.File]::ReadAllText($marker).Trim()
    }
    if ($deployedCommit -eq $commit) {
        $missingFiles = @($deployFiles | Where-Object {
            -not (Test-Path -LiteralPath (Join-Path $live $_) -PathType Leaf)
        })
        if ($missingFiles.Count -eq 0) {
            Write-Log "Без изменений; активная версия $commit."
            exit 0
        }
        Write-Log ("Восстановление неполного развёртывания версии " + $commit + "; отсутствуют: " + ($missingFiles -join ', '))
        # Continue through the normal backup/stage/deploy path to repair missing files.
    }

    # Архивируем файлы именно из удалённого коммита, а не из локальной рабочей папки.
    $sourceRoot = Join-Path $env:TEMP ('CardBotSource_' + [guid]::NewGuid().ToString('N'))
    $archive = Join-Path $env:TEMP ('CardBotSource_' + [guid]::NewGuid().ToString('N') + '.zip')
    $archivePaths = @($deployFiles) + @('requirements.txt')
    $archiveArgs = @('-C', $repo, 'archive', '--format=zip', "--output=$archive", $commit) + $archivePaths
    $null = & git @archiveArgs 2>&1
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $archive)) {
        throw 'Не удалось подготовить архив файлов GitHub. Рабочая версия оставлена без изменений.'
    }
    Expand-Archive -LiteralPath $archive -DestinationPath $sourceRoot -Force
    Remove-Item -LiteralPath $archive -Force

    foreach ($name in $archivePaths) {
        if (-not (Test-Path -LiteralPath (Join-Path $sourceRoot $name) -PathType Leaf)) {
            throw "В удалённой версии отсутствует $name. Рабочая версия оставлена без изменений."
        }
    }

    $newRequirements = [System.IO.File]::ReadAllText((Join-Path $sourceRoot 'requirements.txt'))
    if (Test-Path -LiteralPath $requirementsSnapshot) {
        $installedRequirements = [System.IO.File]::ReadAllText($requirementsSnapshot)
        if ($newRequirements -ne $installedRequirements) {
            throw 'Список библиотек изменился. Код не обновлялся; требуется отдельное обновление библиотек.'
        }
    } else {
        $utf8 = New-Object System.Text.UTF8Encoding -ArgumentList $false
        [System.IO.File]::WriteAllText($requirementsSnapshot, $newRequirements, $utf8)
    }

    $botText = [System.IO.File]::ReadAllText((Join-Path $sourceRoot 'bot.py'))
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
        $source = Join-Path $sourceRoot $name
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
