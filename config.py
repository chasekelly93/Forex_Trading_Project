import os
from dotenv import load_dotenv

load_dotenv()

# OANDA
OANDA_API_KEY = os.getenv("OANDA_API_KEY")
OANDA_ACCOUNT_ID = os.getenv("OANDA_ACCOUNT_ID")
OANDA_ENVIRONMENT = os.getenv("OANDA_ENVIRONMENT", "practice")

# Anthropic
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Trading pairs
PAIRS = [
    "GBP_USD", "NZD_USD", "EUR_USD", "USD_JPY",
    "USD_CHF", "AUD_USD", "USD_CAD"
]

# Timeframes to analyze
TIMEFRAMES = ["H1", "H4", "D"]

# Risk management
MAX_RISK_PER_TRADE_PCT = 1.0   # % of account balance per trade
MAX_OPEN_POSITIONS = 3
MAX_DAILY_LOSS_PCT = 3.0       # Kill switch: halt if daily loss exceeds this
