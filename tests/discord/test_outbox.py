"""
Outbox relay test: enqueue() writes a well-formed {channel_id, content} file that
the bot's _outbox_loop can pick up. This is the out-of-process entry point that
lets the loop answer a note/reply IN the channel it came from (a broadcast-only
webhook can't reach #sable-chat).
"""
import json

import alert_bot.discord_webhook as dw


def test_enqueue_writes_channel_and_content(tmp_path, monkeypatch):
    monkeypatch.setattr(dw, "DISCORD_OUTBOX_DIR", tmp_path)
    ok = dw.enqueue(1234567890123456789, "<b>Reply</b> in #sable-chat")
    assert ok is True
    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1
    data = json.loads(files[0].read_text(encoding="utf-8"))
    assert data == {"channel_id": 1234567890123456789, "content": "<b>Reply</b> in #sable-chat"}


def test_enqueue_each_call_is_a_separate_file(tmp_path, monkeypatch):
    monkeypatch.setattr(dw, "DISCORD_OUTBOX_DIR", tmp_path)
    dw.enqueue(111, "a")
    dw.enqueue(222, "b")
    assert len(list(tmp_path.glob("*.json"))) == 2


def test_enqueue_coerces_channel_to_int(tmp_path, monkeypatch):
    monkeypatch.setattr(dw, "DISCORD_OUTBOX_DIR", tmp_path)
    dw.enqueue("12345", "x")
    data = json.loads(next(tmp_path.glob("*.json")).read_text(encoding="utf-8"))
    assert data["channel_id"] == 12345 and isinstance(data["channel_id"], int)
