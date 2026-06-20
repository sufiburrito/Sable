# Sable's Discord I/O

Single source of truth for how Sable talks to the user. Open this first when working
on alert delivery, the command interface, or reaction feedback. Replaced Telegram in
2026-06 (bean algotrading-c46y).

## Channels

| Channel | Who posts | What |
|---|---|---|
| `#sable-broadcast` | Sable (initiated) | alerts, morning digest, calendar reminders, pinned MMI, out-of-process loop output |
| `#sable-chat` | the user (initiated) | commands + conversation |
| `#dalal-digest`, `#insider-info`, `#general-intel` | the user (forwards) | ingest — saved to files by `discord_ingest.route_message` |

Commands work in **both** Sable channels (`_SABLE_CHANNELS` in `discord_client.py`) — the
user runs portfolio/other queries directly in `#sable-broadcast`, not only `#sable-chat`.
Never restrict commands to chat-only.

**Channel-local reply rule:** Sable answers in the channel she was addressed in. A
command or alert-reply in `#sable-broadcast` is answered there; one in `#sable-chat` is
answered there. The routing token is the Discord channel id (it flows through the
listener handlers where Telegram's `chat_id` used to).

## Commands

Text-parsed (not native slash commands — freeform args like `analyze BBOX full 5y
--no-update` don't fit Discord's structured-param model). The dispatcher accepts **both**
the configured `COMMAND_PREFIX` (default `!`, in `alert_bot/config.py`) **and** a literal
`/` — so existing help text that shows `/portfolio …` stays accurate while the user's
chosen prefix also works. `command_text()` in `discord_client.py` does the normalisation.

Commands, reactions, and replies live in `alert_bot/listener.py` (`_handle`,
`_handle_reply`, `log_reaction`); the Discord gateway calls them — there is no polling
loop. The `/portfolio archive` confirm is **reaction-based** (✅/❌), not buttons: the
prompt registers via `discord_client.register_pending_archive`, and a ✅ reaction runs
`listener.perform_archive()` (`_handle_archive_reaction`). Buttons (`discord.ui.View`)
were tried first but failed — the click reached the bot yet discord.py never dispatched
it to the View callback (its in-memory view registry doesn't survive our cross-thread
send bridge), so the interaction was never acknowledged ("interaction failed").
Reactions use the same reliable gateway path as feedback logging.

## Transport: one token = one gateway

A single bot token permits exactly **one** live gateway connection. `alert_bot/discord_client.py`
owns it (ingest + commands + reactions + alert posting over that one connection).

- **In-process** (the running bot: alerts, MMI pin/edit, reminders, command replies) →
  `DiscordNotifier` (`alert_bot/discord_notifier.py`). It bridges sync calls into the
  client's event loop via `run_coroutine_threadsafe`. Mirrors the old `TelegramNotifier`
  API (`send`/`send_many`/`send_document`/`reply`/`pin_message`/`edit_message`). Fails
  silently (returns `None`/`False`) so a transport hiccup never blocks the alert poll.
  Command handlers run via `asyncio.to_thread` so the loop stays free to service these
  bridged sends (running a sync handler on the loop thread would deadlock).
- **Out-of-process** (separate processes that can't share the gateway: `send_message.py`,
  `send_report.py`, `backtest_levels.py`, `process_insider_trades.py`, `run_forever.sh`)
  → a **channel webhook** (`alert_bot/discord_webhook.py`, `DISCORD_BROADCAST_WEBHOOK`).
  Stateless HTTP POST, `?wait=true` returns the message id. Reactions on webhook-posted
  messages are still seen by the in-channel bot.
- **Out-of-process, channel-targeted** (the loop answering a note/reply in `#sable-chat`, which a
  broadcast-only webhook can't reach) → the **outbox relay**. The producer calls
  `discord_webhook.enqueue(channel_id, text)` (or `send_message.py --channel <id> "…"`), which
  drops a `{channel_id, content}` JSON in `DISCORD_OUTBOX_DIR` (`data/discord_outbox/`); the
  running bot's `_outbox_loop` (`discord_client.py`) polls it every ~3s and posts each to its
  channel over the gateway, then deletes it. Requires the bot to be running (unlike the webhook),
  so true broadcasts / crash notices keep using the webhook. The loop passes the originating
  `chat_id` (stored in every chat/note request JSON) so replies land in the channel addressed.

## Formatting

The codebase emits Telegram HTML (`<b>`, `<i>`, `<code>`, `<a>`). `html_to_markdown()`
(in `discord_notifier.py`) translates it to Discord Markdown **once at the send boundary**
— both the notifier and the webhook apply it. The ~211 HTML source strings are left
untouched by design (no per-string rewrite).

## Reaction feedback (unchanged contract)

`on_raw_reaction_add/_remove` → `listener.log_reaction` → `data/feedback.jsonl`. The alert
is looked up by message id in `data/sent_alerts.json` (`SentAlertsRegistry`, keyed by
`str(message_id)` — Discord snowflakes slot in like Telegram ids). Six-emoji vocabulary
(👍 action_taken · 👎 disagree · ⏳ watching · ✅ profitable · ❌ not_profitable · 🎯
perfect_call) is identical on Discord.

## Config / env

`alert_bot/config.py` (+ `.env`):
- `DISCORD_BOT_TOKEN` — the bot (also used by ingest). Needs the MESSAGE CONTENT intent.
- `DISCORD_BROADCAST_CHANNEL`, `DISCORD_CHAT_CHANNEL` — channel ids.
- `DISCORD_BROADCAST_WEBHOOK` — webhook URL for `#sable-broadcast` (out-of-process senders).
- `COMMAND_PREFIX` — default `!`.

## Operational note

Discord per-channel notifications default to "mentions only". Set `#sable-broadcast` to
**All Messages** on mobile or alerts will arrive silently.

## Revert (transition only)

`alert_bot/notifier.py` (Telegram) is kept temporarily, unwired, for an easy revert window.
Delete it + the `TELEGRAM_*` config once Discord is proven (follow-up bean).
