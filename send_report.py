#!/usr/bin/env python3
"""
Post a PDF report to Sable's #sable-broadcast Discord channel.

Out-of-process: uploads via the broadcast webhook (not the bot gateway).

Usage:
    python3 send_report.py <path/to/report.pdf> [caption]

Reads DISCORD_BROADCAST_WEBHOOK from .env.
"""
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from alert_bot.config import DISCORD_BROADCAST_WEBHOOK
from alert_bot.discord_webhook import post_document


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 send_report.py <report.pdf> [caption]")
        sys.exit(1)

    pdf_path = Path(sys.argv[1])
    if not pdf_path.exists():
        print(f"Error: {pdf_path} not found")
        sys.exit(1)

    caption = sys.argv[2] if len(sys.argv) >= 3 else f"TradeCentral report: {pdf_path.stem}"

    if not DISCORD_BROADCAST_WEBHOOK:
        print("Error: DISCORD_BROADCAST_WEBHOOK not set in .env")
        sys.exit(1)

    if post_document(pdf_path, caption=caption):
        print(f"Sent: {pdf_path.name}")
    else:
        print("Failed to send — check logs.")
        sys.exit(1)


if __name__ == "__main__":
    main()
