# Ляля Бот

Приватный Telegram/Bybit-бот с веб-панелью, Telegram-панелью для просмотра,
настраиваемыми парсерами сигналов, риск-менеджером и Docker-развёртыванием.

> Сначала используйте только DEMO-счёт. Автор проекта не гарантирует прибыль;
> торговля криптовалютой связана с риском полной потери средств.

## Безопасная настройка

В репозитории намеренно отсутствуют ключи, Telegram-сессия, журналы, история,
каналы и активные сделки. Создайте локальную конфигурацию:

```powershell
Copy-Item .\config.example.py .\config.py
```

Заполните в `config.py` только собственные `API_ID`, `API_HASH`, `BOT_TOKEN`,
ключи Bybit, разрешённые Telegram usernames, пароль панели и новый
32-символьный `PROXY_SECRET`. Настоящий `config.py` исключён из Git.

Подробная пошаговая инструкция для Windows находится в
[`FRIEND_SETUP.html`](FRIEND_SETUP.html). Она открывается локально двойным
нажатием и содержит установку WSL 2, Git, Docker Desktop, авторизацию Telegram
и команды запуска.

## Краткий запуск Docker

После заполнения `config.py`:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\prepare-docker.ps1"
docker compose build
docker compose up -d mtproxy
docker compose run --rm app python -c "from main import build_akk_client; c=build_akk_client(); c.start(); print('Telegram authorization OK:', c.is_user_authorized()); c.disconnect()"
docker compose up -d
docker compose ps
```

Панель доступна только на этом компьютере: <http://localhost:8080>.

## Проверка

```powershell
python -m unittest discover -v
```

Для локального запуска тестов без Docker потребуется Python и зависимости из
`requirements.txt`. Для обычной работы бота Python на Windows не требуется.

## Секреты запрещено коммитить

Никогда не добавляйте в Git:

- `config.py` и `.env`;
- `BOT.session` и другие `*.session`;
- журналы и JSON-файлы состояния;
- ключи Bybit, токены Telegram, коды входа и пароль 2FA.

API-ключ Bybit для торговли не должен иметь разрешения на вывод средств.
