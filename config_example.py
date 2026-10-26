# config.example.py
# Скопируйте этот файл под именем config.py и заполните своими данными:
# cp config.example.py config.py

# Токен Telegram-бота от @BotFather
BOT_TOKEN = "1234567890:AAH_ВАШ_ТОКЕН_БОТА"

# ID Telegram-канала для публикаций (начинается с -100...)
CHANNEL_ID = -1001234567890

# Список Telegram ID администраторов, которым доступна панель управления
ADMIN_IDS = [
    123456789,
    987654321,
]

# Данные приложения из https://dev.twitch.tv/console (для проверки Twitch)
TWITCH_CLIENT_ID = "ВАШ_TWITCH_CLIENT_ID"
TWITCH_CLIENT_SECRET = "ВАШ_TWITCH_CLIENT_SECRET"

# Опционально: Прокси для подключения к серверам Telegram (если доступ ограничен)
# Примеры: "socks5://127.0.0.1:10808" или "http://user:pass@ip:port"
PROXY_URL = None

# Интервал проверки стримов в секундах (по умолчанию 60 сек)
CHECK_INTERVAL = 60

# Задержка перед удалением поста после завершения всех стримов (5 минут = 300 сек)
OFFLINE_GRACE_PERIOD = 300

# Имя файла базы данных SQLite
DB_PATH = "streams.db"