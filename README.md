# Steam Card Telegram Bot

Приватный репозиторий исходного кода Telegram-бота для продажи Steam trading cards.

## Безопасность
- Не загружайте `vouchers.json`, `inventory_cache.json`, `steam_session.json`, `funpay_config.json`, логи, коды или сессии.
- Секреты задаются через переменные окружения, а не хранятся в коде.
- Если токен Telegram или пароль Steam ранее были раскрыты, перевыпустите токен через BotFather и смените пароль Steam.

## Запуск в Windows
Установите зависимости: `python -m pip install -r requirements.txt`.

В PowerShell задайте переменные окружения в текущем окне (подставьте свои значения локально, не публикуйте их):
```powershell
$env:TG_TOKEN="НОВЫЙ_ТОКЕН_БОТА"
$env:STEAM_ID="ВАШ_STEAM_ID64"
$env:STEAM_LOGIN="ВАШ_STEAM_ЛОГИН"
$env:STEAM_PASSWORD="ВАШ_STEAM_ПАРОЛЬ"
python bot.py
```

Зависимость от Windows сохраняется (используется `winreg`).
