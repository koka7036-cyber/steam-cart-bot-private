# Steam Card Telegram Bot

Приватный репозиторий исходного кода Telegram-бота для продажи Steam trading cards.

## GitHub-проверки
- История коммитов позволяет просматривать изменения и откатывать неудачные обновления.
- GitHub Actions проверяет синтаксис Python при push в `main`, pull request и ручном запуске.
- Проверка только компилирует `bot.py`; она не запускает бота и не проверяет реальные подключения к Telegram, Steam или FunPay.

## Безопасность
- Не загружайте `vouchers.json`, `inventory_cache.json`, `steam_session.json`, `funpay_config.json`, логи, коды или сессии.
- Секреты задаются через переменные окружения, а не хранятся в коде.
- Если токен Telegram или пароль Steam ранее были раскрыты, перевыпустите токен через BotFather и смените пароль Steam. Удаление секрета из текущего файла не удаляет его из старых коммитов.

## Запуск в Windows
Установите зависимости:
```powershell
python -m pip install -r requirements.txt
```

В PowerShell задайте переменные окружения в текущем окне (подставьте свои значения локально, не публикуйте их):
```powershell
$env:TG_TOKEN="НОВЫЙ_ТОКЕН_БОТА"
$env:STEAM_ID="ВАШ_STEAM_ID64"
$env:STEAM_LOGIN="ВАШ_STEAM_ЛОГИН"
$env:STEAM_PASSWORD="ВАШ_STEAM_ПАРОЛЬ"
python bot.py
```

Код использует Windows-специфичный модуль `winreg`; запуск на Linux не настроен.
