"""
Reaction-based /portfolio archive confirm (replaced the unreliable buttons).
register_pending_archive records message_id → (ticker, channel_id) so that a later
✅ reaction can be matched and run the archive. (The full async reaction → edit flow
is exercised live; here we pin the registration contract.)
"""
import alert_bot.discord_client as dc


def test_register_pending_archive_records_entry():
    dc._pending_archive.clear()
    # The seed coroutine bridges onto the client loop, which isn't running in a test;
    # register_pending_archive must still record the pending entry (seed failure is
    # caught + logged), so the ✅ reaction can be matched later.
    dc.register_pending_archive(123456789, "SUVEN", 1234567890123456789)
    assert dc._pending_archive.get(123456789) == ("SUVEN", 1234567890123456789)


def test_pending_archive_is_keyed_by_message():
    dc._pending_archive.clear()
    dc.register_pending_archive(111, "AAA", 999)
    dc.register_pending_archive(222, "BBB", 999)
    assert dc._pending_archive[111] == ("AAA", 999)
    assert dc._pending_archive[222] == ("BBB", 999)
