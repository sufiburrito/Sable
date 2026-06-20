#!/usr/bin/env python3
"""
journal/tax_reminders.py — push tax nudges out of the journal (idempotent).

Two channels, per the user's split:
  • General tax-calendar dates (FY-end harvest, advance tax, ITR) → Discord #sable-broadcast,
    fired only inside a lead-time window, deduped via a small state file so a date posts once.
  • Portfolio-specific (a holding's near-term LTCG-crossing) → CalDAV, written into a SEPARATE
    `algotrading-tax` collection. The bot's stock-alert `sync_calendar` full-reconcile only
    touches `algotrading-events`, so it never wipes these; we run our own reconcile here.

No LLM. Network = the Discord webhook only. Run nightly from journal/build.py.
"""
import datetime as dt
import json
from pathlib import Path

from journal import tax

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "data" / "journal" / "tax_reminders.json"   # Discord dedupe (gitignored)
DISCORD_WINDOW = 14          # post a general-date nudge once it is this many days out
TAX_CALENDAR = "algotrading-tax"


def _today() -> dt.date:
    return dt.date.today()


# ── Discord: general tax-calendar dates ──────────────────────────────────────

def _load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_state(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=2))


def _due_general(data: dict, window: int) -> list[tuple[str, str]]:
    """(dedupe_key, message) for general dates now inside the lead-time window."""
    k, fy = data["key_dates"], data["fy"]
    t = data["tax"]
    out = []
    if k["days_to_harvest"] is not None and 0 <= k["days_to_harvest"] <= window:
        out.append((f"{fy}:harvest", (
            f"🧾 **Tax-harvest deadline {k['harvest_by']}** ({k['days_to_harvest']}d). "
            f"Book sells by ~28 Mar (T+1) to land in {fy}. "
            f"LTCG headroom left: ₹{round(t['exemption_left']):,} tax-free.")))
    d = k["days_to_advance_tax"]
    if d is not None and 0 <= d <= window:
        out.append((f"{fy}:advtax:{k['next_advance_tax']}", (
            f"🧾 **Advance-tax due {k['next_advance_tax']}** ({d}d). "
            f"Est. CG tax so far this FY: ₹{round(t['total_tax']):,}.")))
    if k["days_to_itr"] is not None and 0 <= k["days_to_itr"] <= window:
        out.append((f"{k['itr_fy']}:itr", (
            f"🧾 **ITR for {k['itr_fy']} due {k['itr_due']}** ({k['days_to_itr']}d). "
            "File by the due date to carry losses forward (8 yrs).")))
    return out


def push_discord(data: dict, window: int = DISCORD_WINDOW, dry_run: bool = False) -> list[str]:
    """Post any general-date nudges now due that haven't been posted before. Returns keys posted."""
    state = _load_state()
    posted = []
    for key, msg in _due_general(data, window):
        if state.get(key):
            continue
        if not dry_run:
            from alert_bot import discord_webhook
            discord_webhook.post(msg)
        state[key] = _today().isoformat()
        posted.append(key)
    if posted and not dry_run:
        _save_state(state)
    return posted


# ── CalDAV: portfolio-specific LTCG-crossing reminders ───────────────────────

def _tax_vevent(uid: str, summary: str, anchor: dt.date) -> bytes:
    from datetime import datetime, timedelta, timezone
    from icalendar import Calendar, Event, vText
    cal = Calendar()
    cal.add("prodid", "-//algotrading//tax//EN")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    ev = Event()
    ev.add("uid", vText(uid))
    ev.add("summary", vText(summary))
    ev.add("categories", vText("TAX"))
    ev.add("dtstart", anchor)
    ev.add("dtend", anchor + timedelta(days=1))
    ev.add("dtstamp", datetime.now(tz=timezone.utc))
    ev.add("description", vText("Portfolio tax reminder (planning aid, not tax advice)"))
    cal.add_component(ev)
    return cal.to_ical()


def _expected_caldav(data: dict) -> dict[str, bytes]:
    """One VEVENT per near-term LTCG-crossing holding, anchored to its crossing date."""
    from alert_bot import caldav_sync
    exp = {}
    for w in data["ltcg_watch"]:
        anchor = dt.date.fromisoformat(w["ltcg_date"])
        summary = (f"{w['symbol']} → LTCG today — hold to pay 12.5% not 20% "
                   f"(save ~₹{round(w['tax_saving']):,})")
        uid = caldav_sync._make_uid(w["symbol"], f"ltcg-cross:{w['ltcg_date']}")
        exp[uid] = _tax_vevent(uid, summary, anchor)
    return exp


def sync_caldav(data: dict) -> int:
    """Full-reconcile the `algotrading-tax` collection (write current, delete stale)."""
    from alert_bot import caldav_sync
    from alert_bot.config import CALDAV_STORAGE_DIR
    coll = caldav_sync._ensure_collection(CALDAV_STORAGE_DIR, TAX_CALENDAR)
    exp = _expected_caldav(data)
    for f in coll.glob("*.ics"):
        if f.stem not in exp:
            f.unlink()
    for uid, ics in exp.items():
        (coll / f"{uid}.ics").write_bytes(ics)
    return len(exp)


def main():
    data = tax.build_tax_data()
    posted = push_discord(data)
    n = sync_caldav(data)
    print(f"tax reminders → Discord posted {len(posted)} {posted or ''} · CalDAV {n} LTCG-crossing event(s)")


if __name__ == "__main__":
    main()
