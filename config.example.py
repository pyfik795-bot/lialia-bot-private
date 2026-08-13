"""Безопасный шаблон конфигурации.

Скопируйте этот файл в ``config.py`` и заполните только своими данными.
Настоящий ``config.py`` исключён из Git через .gitignore.
"""

# Telegram user account (https://my.telegram.org/)
API_ID = 0
API_HASH = "CHANGE_ME"

# Local MTProxy used by the Docker deployment.
PROXY_ADDRESS = "127.0.0.1"
PROXY_PORT = 443
PROXY_SECRET = "CHANGE_ME_32_HEXADECIMAL_CHARS"

# Optional channel seeded on the first start. Channels can also be added in UI.
CHANNEL = ""

# Bybit API. Start with demo credentials and keep DEMO = True.
BYBIT_API_KEY = "CHANGE_ME"
BYBIT_API_SECRET = "CHANGE_ME"
DEMO = True
TESTNET = False
RECV_WINDOW = 10000

CATEGORY = "linear"
MARGIN_USDT = 20
LEVERAGE = 20
TP_PERCENTS = [30, 30, 20, 20]

# Telegram control bot (create it via the official @BotFather).
# Фиктивный токен корректного формата: он нужен, чтобы импорт и unit-тесты
# работали до настройки. Перед запуском обязательно замените его настоящим.
BOT_TOKEN = "123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
ALLOWED_USERNAMES = ["your_telegram_username"]

# Runtime state files. They stay outside the Docker image.
ACTIVE_TRADES_FILE = "active_trades.json"
TRADE_HISTORY_FILE = "trade_history.json"
AUTHORIZED_CHATS_FILE = "authorized_chats.json"
PROCESSED_SIGNALS_FILE = "processed_signals.json"
PARSED_SIGNALS_LOG_FILE = "parsed_signals.json"
SETTINGS_FILE = "settings.json"
CHANNELS_FILE = "channels.json"
PARSERS_FILE = "parsers.json"
RISK_FILE = "risk.json"
RISK_STATE_FILE = "risk_state.json"

# Local web dashboard.
WEB_HOST = "0.0.0.0"
WEB_PORT = 8080
WEB_PASSWORD = "CHANGE_ME_TO_A_LONG_UNIQUE_PASSWORD"

LOG_DIR = "logs"
LOG_FILE = "bot.log"
