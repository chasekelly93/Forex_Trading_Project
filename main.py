"""
Forex Trading Agent — Entry Point
Run this to start the agent.
"""
import sys
from config import OANDA_API_KEY, OANDA_ACCOUNT_ID, ANTHROPIC_API_KEY


def check_config():
    missing = []
    if not OANDA_API_KEY or OANDA_API_KEY == "your_oanda_api_key_here":
        missing.append("OANDA_API_KEY")
    if not OANDA_ACCOUNT_ID or OANDA_ACCOUNT_ID == "your_oanda_account_id_here":
        missing.append("OANDA_ACCOUNT_ID")
    if not ANTHROPIC_API_KEY or ANTHROPIC_API_KEY == "your_anthropic_api_key_here":
        missing.append("ANTHROPIC_API_KEY")

    if missing:
        print(f"[ERROR] Missing credentials in .env: {', '.join(missing)}")
        print("Copy .env.example to .env and fill in your keys.")
        sys.exit(1)

    print("[OK] Config loaded.")


if __name__ == "__main__":
    check_config()
    print("Forex agent starting... (pipeline not yet connected)")
