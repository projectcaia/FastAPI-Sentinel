import os
VERSION = "2025.08.25-threadless"
TZ = "Asia/Seoul"
CONNECTOR_SECRET = os.getenv("CONNECTOR_SECRET", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
BASE_URL = os.getenv("BASE_URL", "http://localhost:8080")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
DATA_DIR = os.getenv("DATA_DIR", "data")
DB_PATH = os.path.join(DATA_DIR, "hub.sqlite3")
CAIA_PUSH_MODE = os.getenv("CAIA_PUSH_MODE", "telegram")
PUSH_SIMULATE_429 = os.getenv("PUSH_SIMULATE_429", "0").lower() in ("1","true","yes")
