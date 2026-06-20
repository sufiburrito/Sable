#!/usr/bin/env python3
"""
Post a message to one of Sable's Discord channels.

Out-of-process (a single bot token allows only one gateway connection, held by the
running bot). Two delivery paths:
  - default → #sable-broadcast via the broadcast webhook (resilient; works even if
    the bot is down — e.g. crash notices).
  - --channel <id> → the outbox relay, which the running bot posts to that exact
    channel. Use this so the loop answers a note/reply IN the channel it came from.
HTML is translated to Discord Markdown automatically.

Usage:
    python3 send_message.py "Message text here"
    python3 send_message.py --channel 123456789 "Reply text"
    python3 send_message.py --file message.txt
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from alert_bot.discord_webhook import post, enqueue


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("text", nargs="?", help="message text")
    ap.add_argument("--file", help="read message text from this file")
    ap.add_argument("--channel", type=int, default=None,
                    help="target channel id (routes via the bot outbox relay)")
    args = ap.parse_args()

    if args.file:
        text = Path(args.file).read_text(encoding="utf-8").strip()
    elif args.text:
        text = args.text
    else:
        print("Usage: python3 send_message.py [--channel ID] 'text'  |  --file msg.txt")
        sys.exit(1)

    if not text:
        print("Error: empty message")
        sys.exit(1)

    ok = enqueue(args.channel, text) if args.channel else post(text)
    if ok:
        print("Message queued." if args.channel else "Message sent.")
    else:
        print("Failed to send message.")
        sys.exit(1)


if __name__ == "__main__":
    main()
