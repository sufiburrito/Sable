"""
Portfolio management — add, archive, and restore tickers in stocks/.

Shared backend used by both Telegram (/portfolio) and Web UI (/api/portfolio).

Responsibilities:
- add_ticker(): validate symbol, fetch yfinance metadata, fill template, write stocks/{TICKER}.md
- archive_ticker(): full sweep — move stocks/.md + KB dossier + analysis sidecars + report PDFs
  into archive/{TICKER}/, register the ticker in archive/_index.json, and prune the lightweight
  attention surfaces (stock_sectors.json, discovery_watchlist.json, state.json). Recoverable.
- restore_ticker(): reverse an archive from its meta.json, then re-queue a fresh analysis.
- remove_ticker(): backwards-compatible alias of archive_ticker (kept so /portfolio remove works).
- list_archived() / load_archived_set(): read the archive registry — the latter is the single
  source of truth the autonomous routines (discovery/digest/convergence) consult to keep an
  archived ticker from re-surfacing when those files regenerate.
- sync_active_stocks_table(): regenerate the Active stocks table in README.md from current
  stocks/*.md (so the table stops drifting from reality).
- queue_full_analysis(): drop a request file for the autonomous loop to pick up.

Add/archive/restore always trigger sync_active_stocks_table() so the docs stay accurate.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from alert_bot.config import REQUESTS_DIR, STOCKS_DIR

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).parent.parent
# The Active stocks table lives in README.md (it was moved out of CLAUDE.md during the
# docs restructure). sync_active_stocks_table() rewrites the table block here.
README_MD = REPO_ROOT / "README.md"
TEMPLATE_FILE = STOCKS_DIR / "_TEMPLATE.md"
BACKUPS_DIR = STOCKS_DIR / "backups"

# Per-ticker artifact locations swept up by archive_ticker().
KB_TICKERS_DIR = REPO_ROOT / "KNOWLEDGE_BASE" / "tickers"
ANALYSIS_DIR = REPO_ROOT / "analysis"
REPORTS_DIR = REPO_ROOT / "reports"
DATA_DIR = REPO_ROOT / "data"

# Archive: one folder per archived ticker + a registry index.
ARCHIVE_DIR = REPO_ROOT / "archive"
ARCHIVE_INDEX = ARCHIVE_DIR / "_index.json"

# Lightweight "attention surface" JSON files pruned on archive (so the ticker stops
# appearing in digests / discovery). DB rows are left intact as historical audit.
STOCK_SECTORS_FILE = DATA_DIR / "stock_sectors.json"
DISCOVERY_WATCHLIST_FILE = DATA_DIR / "discovery_watchlist.json"
STATE_FILE = DATA_DIR / "state.json"

# Default analysis mode queued after add (matches nightly refresh mode).
DEFAULT_ANALYSIS_MODE = "chart-news-community-retro-backtest-forecast"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _normalize_ticker(raw: str) -> str:
    """Uppercase + strip whitespace. NSE tickers are alphanumeric (no .NS suffix here)."""
    return raw.strip().upper()


def _is_valid_ticker_format(ticker: str) -> bool:
    """NSE tickers are 1-15 chars, alphanumeric only (some have & but we'll allow)."""
    return bool(re.fullmatch(r"[A-Z0-9&]{1,15}", ticker))


def is_in_portfolio(ticker: str) -> bool:
    """Return True if stocks/{TICKER}.md exists (regardless of whether it parses)."""
    return (STOCKS_DIR / f"{_normalize_ticker(ticker)}.md").exists()


# ---------------------------------------------------------------------------
# yfinance metadata fetch (best-effort, fast-path)
# ---------------------------------------------------------------------------

def _fetch_metadata(ticker: str) -> dict[str, Any]:
    """
    Fetch sector, market cap, 52W range, current price, long name from yfinance.
    Best-effort — if any field fails, leave it as None and the caller substitutes
    a placeholder in the template.

    Returns: {"current_price": float|None, "low_52w": ..., "high_52w": ...,
              "market_cap_cr": float|None, "sector": str|None, "long_name": str|None}
    """
    out: dict[str, Any] = {
        "current_price": None,
        "low_52w": None,
        "high_52w": None,
        "market_cap_cr": None,
        "sector": None,
        "long_name": None,
    }

    try:
        import yfinance as yf
    except ImportError:
        logger.warning("yfinance not installed — metadata fetch skipped")
        return out

    yf_symbol = f"{ticker}.NS"
    try:
        t = yf.Ticker(yf_symbol)
        info = t.info or {}

        # Best-effort field extraction. yfinance.info is flaky and field names drift.
        out["current_price"] = info.get("currentPrice") or info.get("regularMarketPrice")
        out["low_52w"] = info.get("fiftyTwoWeekLow")
        out["high_52w"] = info.get("fiftyTwoWeekHigh")
        out["sector"] = info.get("sector") or info.get("industry")
        out["long_name"] = info.get("longName") or info.get("shortName")

        # Market cap is in raw rupees from yfinance — convert to Cr (1 Cr = 1e7).
        mc_raw = info.get("marketCap")
        if mc_raw:
            out["market_cap_cr"] = round(mc_raw / 1e7, 0)
    except Exception as exc:
        logger.warning("yfinance metadata fetch failed for %s: %s", ticker, exc)

    return out


# ---------------------------------------------------------------------------
# Template rendering
# ---------------------------------------------------------------------------

def _render_stub(ticker: str, meta: dict[str, Any]) -> str:
    """
    Read _TEMPLATE.md and substitute placeholders with available metadata.
    Fields with no metadata keep their bracketed placeholders so the user/Claude
    knows to fill them in during the first analysis pass.
    """
    template = TEMPLATE_FILE.read_text(encoding="utf-8")
    today = datetime.now().strftime("%Y-%m-%d")

    # Header: "# [TICKER] — [Company Full Name] | NSE: [TICKER]"
    name = meta.get("long_name") or "[Company Full Name]"
    template = template.replace(
        "# [TICKER] — [Company Full Name] | NSE: [TICKER]",
        f"# {ticker} — {name} | NSE: {ticker}",
    )

    # Identity block
    sector = meta.get("sector") or "[NEEDS REVIEW — fetched no sector from yfinance]"
    price = meta.get("current_price")
    price_str = f"₹{price:.2f} (as of {today})" if price else f"₹[price] (as of {today})"

    low = meta.get("low_52w")
    high = meta.get("high_52w")
    range_str = f"₹{low:.0f} — ₹{high:.0f}" if (low and high) else "₹[low] — ₹[high]"

    cap = meta.get("market_cap_cr")
    cap_str = f"₹{cap:,.0f} Cr" if cap else "₹[X] Cr"

    template = template.replace("[e.g. Optical fiber / Data centers]", sector)
    template = template.replace("₹[price] (as of [date])", price_str)
    template = template.replace("₹[low] — ₹[high]", range_str)
    template = template.replace("₹[X] Cr", cap_str)

    # New stocks default to 0% core (matches user's add-then-research workflow)
    template = template.replace(
        "**Core Position:** [X]% of invested value — NEVER sell this",
        "**Core Position:** 0% of invested value — fresh watchlist add, no position yet",
    )

    return template


# ---------------------------------------------------------------------------
# Analysis request queueing
# ---------------------------------------------------------------------------

def queue_full_analysis(ticker: str, mode: str = DEFAULT_ANALYSIS_MODE) -> Path:
    """Drop a request JSON for the autonomous loop to process."""
    REQUESTS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    request_path = REQUESTS_DIR / f"{ticker}_portfolio_add_{timestamp}.json"

    # Decode the mode flags that the autonomous loop expects.
    payload = {
        "ticker": ticker,
        "mode": mode,
        "retro": "retro" in mode,
        "retro_period": "2y",
        "backtest": "backtest" in mode,
        "backtest_period": "5y",
        "forecast": "forecast" in mode,
        "update": True,
        "chat": False,
        "react": False,
        "requested_at": datetime.now().astimezone().isoformat(),
    }
    request_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return request_path


# ---------------------------------------------------------------------------
# Add
# ---------------------------------------------------------------------------

def add_ticker(raw_ticker: str, queue_analysis: bool = True) -> dict[str, Any]:
    """
    Add a ticker to the portfolio. Creates stocks/{TICKER}.md from the template
    with any yfinance metadata pre-filled, optionally queues a full analysis,
    and re-syncs the Active Stocks table in CLAUDE.md.

    Returns a status dict the caller can format for Telegram or the web UI.
    """
    ticker = _normalize_ticker(raw_ticker)

    if not _is_valid_ticker_format(ticker):
        return {"ok": False, "ticker": ticker, "error": f"Invalid ticker format: {raw_ticker!r}"}

    if is_in_portfolio(ticker):
        return {"ok": False, "ticker": ticker, "error": f"{ticker} is already in the portfolio"}

    if not TEMPLATE_FILE.exists():
        return {"ok": False, "ticker": ticker, "error": f"Template file missing: {TEMPLATE_FILE}"}

    # Best-effort metadata fetch (network call — slow but bounded).
    meta = _fetch_metadata(ticker)

    # Render and write the stub.
    stub = _render_stub(ticker, meta)
    stock_path = STOCKS_DIR / f"{ticker}.md"
    stock_path.write_text(stub, encoding="utf-8")
    logger.info("Created stock stub: %s (sector=%s, price=%s)",
                stock_path, meta.get("sector"), meta.get("current_price"))

    request_path = None
    if queue_analysis:
        try:
            request_path = queue_full_analysis(ticker)
            logger.info("Queued analysis request: %s", request_path)
        except Exception as exc:
            # Non-fatal — the stock file is in place; user can /analyze manually.
            logger.warning("Failed to queue analysis for %s: %s", ticker, exc)

    sync_result = sync_active_stocks_table()

    return {
        "ok": True,
        "ticker": ticker,
        "stock_file": str(stock_path),
        "metadata": meta,
        "analysis_queued": request_path is not None,
        "request_file": str(request_path) if request_path else None,
        "claude_md_synced": sync_result.get("ok", False),
        "claude_md_count": sync_result.get("count"),
    }


# ---------------------------------------------------------------------------
# Archive — full sweep + registry
# ---------------------------------------------------------------------------

def _snapshot_conviction(ticker: str) -> str | None:
    """
    Best-effort one-line conviction snapshot pulled from the ticker's KB Current
    State block (Weinstein Stage + Convergence). Returns None if the KB file or the
    fields are missing — purely informational, stored in the registry for context.
    """
    kb_path = KB_TICKERS_DIR / f"{ticker}.md"
    try:
        text = kb_path.read_text(encoding="utf-8")
    except OSError:
        return None

    stage_m = re.search(r"\*\*Weinstein Stage:\*\*\s+(.+?)(?:\s+—|\.|$)", text, re.MULTILINE)
    conv_m = re.search(r"\*\*Convergence:\*\*\s+(.+?)(?:\s+—|\||$)", text, re.MULTILINE)
    bits = []
    if stage_m:
        bits.append(f"Stage {stage_m.group(1).strip()}")
    if conv_m:
        bits.append(f"Convergence {conv_m.group(1).strip()}")
    return " · ".join(bits) if bits else None


def _prune_state_file(ticker: str) -> bool:
    """
    Remove a ticker's transient runtime state from data/state.json so a future
    restore starts clean (no stale cooldowns or cached regime). Best-effort —
    returns True if anything was changed.
    """
    if not STATE_FILE.exists():
        return False
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not read state.json to prune %s: %s", ticker, exc)
        return False

    changed = False

    # Keys shaped "{TICKER}:..." — the ticker is the first colon-delimited token.
    for dict_key in ("level_cooldowns", "disarmed_levels"):
        d = state.get(dict_key)
        if isinstance(d, dict):
            for k in [k for k in d if k.split(":", 1)[0] == ticker]:
                del d[k]
                changed = True

    # calendar_cooldowns mixes "{TICKER}:m:y" and "forecast:{TICKER}:..." — match any token.
    cal = state.get("calendar_cooldowns")
    if isinstance(cal, dict):
        for k in [k for k in cal if ticker in k.split(":")]:
            del cal[k]
            changed = True

    # stock_regimes nests the per-ticker map under "regimes".
    regimes = state.get("stock_regimes")
    if isinstance(regimes, dict):
        inner = regimes.get("regimes")
        if isinstance(inner, dict) and ticker in inner:
            del inner[ticker]
            changed = True

    # regime_prob_history is keyed directly by ticker.
    rph = state.get("regime_prob_history")
    if isinstance(rph, dict) and ticker in rph:
        del rph[ticker]
        changed = True

    if changed:
        STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
        logger.info("Pruned state.json entries for %s", ticker)
    return changed


def _prune_attention_json(ticker: str) -> list[str]:
    """
    Drop the ticker from the lightweight JSON files that feed digests/discovery, so
    it stops surfacing there. Returns the list of files touched.
    """
    touched: list[str] = []

    # stock_sectors.json — flat {ticker: sector}
    if STOCK_SECTORS_FILE.exists():
        try:
            sectors = json.loads(STOCK_SECTORS_FILE.read_text(encoding="utf-8"))
            if isinstance(sectors, dict) and ticker in sectors:
                del sectors[ticker]
                STOCK_SECTORS_FILE.write_text(json.dumps(sectors, indent=2), encoding="utf-8")
                touched.append(str(STOCK_SECTORS_FILE))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Could not prune stock_sectors.json for %s: %s", ticker, exc)

    # discovery_watchlist.json — {scan_date, candidates: [{ticker, ...}]}
    if DISCOVERY_WATCHLIST_FILE.exists():
        try:
            disc = json.loads(DISCOVERY_WATCHLIST_FILE.read_text(encoding="utf-8"))
            cands = disc.get("candidates") if isinstance(disc, dict) else None
            if isinstance(cands, list):
                kept = [c for c in cands if (c.get("ticker") or "").upper() != ticker]
                if len(kept) != len(cands):
                    disc["candidates"] = kept
                    DISCOVERY_WATCHLIST_FILE.write_text(json.dumps(disc, indent=2), encoding="utf-8")
                    touched.append(str(DISCOVERY_WATCHLIST_FILE))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Could not prune discovery_watchlist.json for %s: %s", ticker, exc)

    return touched


# Per-ticker artifacts to sweep: (source path or glob dir, archive subfolder).
def _collect_artifacts(ticker: str) -> list[tuple[Path, str]]:
    """
    Build the list of (source_file, archive_relative_path) pairs to move.
    Globs analysis/ and reports/ so every sidecar variant is captured.
    """
    pairs: list[tuple[Path, str]] = []

    stock_md = STOCKS_DIR / f"{ticker}.md"
    if stock_md.exists():
        pairs.append((stock_md, "stocks.md"))

    kb_md = KB_TICKERS_DIR / f"{ticker}.md"
    if kb_md.exists():
        pairs.append((kb_md, "kb.md"))

    for f in sorted(ANALYSIS_DIR.glob(f"{ticker}_*")):
        if f.is_file():
            pairs.append((f, f"analysis/{f.name}"))

    for f in sorted(REPORTS_DIR.glob(f"{ticker}_*")):
        if f.is_file():
            pairs.append((f, f"reports/{f.name}"))

    return pairs


def archive_ticker(raw_ticker: str, reason: str | None = None) -> dict[str, Any]:
    """
    Archive a ticker: full sweep of its footprint into archive/{TICKER}/, register it
    in archive/_index.json, prune the lightweight attention surfaces, and re-sync the
    Active stocks table. Recoverable via restore_ticker().

    The autonomous loop's next stocks-dir-mtime check picks up the absence (hot-reload).
    """
    ticker = _normalize_ticker(raw_ticker)

    if not _is_valid_ticker_format(ticker):
        return {"ok": False, "ticker": ticker, "error": f"Invalid ticker format: {raw_ticker!r}"}

    stock_path = STOCKS_DIR / f"{ticker}.md"
    if not stock_path.exists():
        return {"ok": False, "ticker": ticker, "error": f"{ticker} is not in the portfolio"}

    conviction = _snapshot_conviction(ticker)
    artifacts = _collect_artifacts(ticker)

    # Move every artifact into archive/{TICKER}/, recording orig→stored for exact restore.
    dest_root = ARCHIVE_DIR / ticker
    dest_root.mkdir(parents=True, exist_ok=True)
    moved: list[dict[str, str]] = []
    for src, stored_rel in artifacts:
        dest = dest_root / stored_rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest))
        moved.append({"orig": str(src.relative_to(REPO_ROOT)), "stored": stored_rel})
    logger.info("Archived %d artifact(s) for %s → %s", len(moved), ticker, dest_root)

    # Prune attention surfaces + transient state.
    pruned_json = _prune_attention_json(ticker)
    _prune_state_file(ticker)

    # Drop any pending requests so the loop doesn't recreate the .md from a stale add.
    cleaned_requests: list[str] = []
    if REQUESTS_DIR.exists():
        for req in REQUESTS_DIR.glob(f"{ticker}_*.json"):
            try:
                req.unlink()
                cleaned_requests.append(str(req))
            except OSError as exc:
                logger.warning("Could not clean stale request %s: %s", req, exc)

    archived_at = datetime.now().astimezone().isoformat()

    # Write the per-ticker meta.json (drives exact restore).
    meta = {
        "ticker": ticker,
        "archived_at": archived_at,
        "reason": reason,
        "conviction_at_archive": conviction,
        "files": moved,
    }
    (dest_root / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    # Update the registry (source of truth for suppression + list/restore).
    registry = _read_registry()
    registry[ticker] = {
        "archived_at": archived_at,
        "reason": reason,
        "conviction_at_archive": conviction,
    }
    _write_registry(registry)

    sync_result = sync_active_stocks_table()

    return {
        "ok": True,
        "ticker": ticker,
        "archive_dir": str(dest_root),
        "artifacts_moved": len(moved),
        "pruned_json": pruned_json,
        "cleaned_requests": cleaned_requests,
        "conviction_at_archive": conviction,
        "table_synced": sync_result.get("ok", False),
        "table_count": sync_result.get("count"),
    }


def remove_ticker(raw_ticker: str) -> dict[str, Any]:
    """Backwards-compatible alias of archive_ticker() (keeps /portfolio remove working)."""
    result = archive_ticker(raw_ticker, reason="removed via /portfolio remove")
    # Preserve the legacy key the web UI / older callers may read.
    if result.get("ok"):
        result["archived_to"] = result.get("archive_dir")
        result["claude_md_synced"] = result.get("table_synced")
        result["claude_md_count"] = result.get("table_count")
    return result


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------

def restore_ticker(raw_ticker: str, queue_analysis: bool = True) -> dict[str, Any]:
    """
    Restore an archived ticker: move every file back to its original path from the
    archive's meta.json, drop the registry entry, re-sync the table, and (by default)
    queue a fresh analysis since the dossier will be stale.
    """
    ticker = _normalize_ticker(raw_ticker)

    if not _is_valid_ticker_format(ticker):
        return {"ok": False, "ticker": ticker, "error": f"Invalid ticker format: {raw_ticker!r}"}

    dest_root = ARCHIVE_DIR / ticker
    meta_path = dest_root / "meta.json"
    if not meta_path.exists():
        return {"ok": False, "ticker": ticker, "error": f"{ticker} is not archived"}

    if (STOCKS_DIR / f"{ticker}.md").exists():
        return {"ok": False, "ticker": ticker, "error": f"{ticker} is already active in the portfolio"}

    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "ticker": ticker, "error": f"Corrupt archive meta for {ticker}: {exc}"}

    restored: list[str] = []
    for entry in meta.get("files", []):
        stored = dest_root / entry["stored"]
        orig = REPO_ROOT / entry["orig"]
        if not stored.exists():
            logger.warning("Archived file missing on restore: %s", stored)
            continue
        orig.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(stored), str(orig))
        restored.append(entry["orig"])
    logger.info("Restored %d artifact(s) for %s", len(restored), ticker)

    # Remove the registry entry and clean up the (now-empty) archive folder.
    registry = _read_registry()
    registry.pop(ticker, None)
    _write_registry(registry)
    meta_path.unlink(missing_ok=True)
    shutil.rmtree(dest_root, ignore_errors=True)

    request_path = None
    if queue_analysis:
        try:
            request_path = queue_full_analysis(ticker)
        except Exception as exc:
            logger.warning("Failed to queue analysis on restore of %s: %s", ticker, exc)

    sync_result = sync_active_stocks_table()

    return {
        "ok": True,
        "ticker": ticker,
        "restored": restored,
        "analysis_queued": request_path is not None,
        "table_synced": sync_result.get("ok", False),
        "table_count": sync_result.get("count"),
    }


# ---------------------------------------------------------------------------
# Archive registry (source of truth for suppression + list/restore)
# ---------------------------------------------------------------------------

def _read_registry() -> dict[str, Any]:
    """Load archive/_index.json, or {} if absent/corrupt."""
    if not ARCHIVE_INDEX.exists():
        return {}
    try:
        data = json.loads(ARCHIVE_INDEX.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not read archive registry: %s", exc)
        return {}


def _write_registry(registry: dict[str, Any]) -> None:
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    ARCHIVE_INDEX.write_text(json.dumps(registry, indent=2, sort_keys=True), encoding="utf-8")


def list_archived() -> dict[str, Any]:
    """Return the archive registry (ticker → {archived_at, reason, conviction})."""
    return _read_registry()


def load_archived_set() -> set[str]:
    """
    Set of archived tickers. THE suppression hook: discovery/digest/convergence
    routines import this and filter it out so an archived ticker never re-surfaces.
    """
    return set(_read_registry().keys())


# ---------------------------------------------------------------------------
# README.md "Active stocks" table auto-sync
# ---------------------------------------------------------------------------

# Regex anchors for the Active stocks block in README.md. We replace the table
# only; the heading and the "To add a stock:" trailer are preserved. Any intro
# lines between the heading and the table are matched permissively (there are none
# today) so editorial wording changes don't break the anchor. No DOTALL — `.` is a
# per-line wildcard, which keeps the engine from catastrophic backtracking.
_ACTIVE_STOCKS_BLOCK_RE = re.compile(
    r"(## Active stocks\n"        # heading (note: lowercase 'stocks' in README)
    r"(?:[^|\n].*\n)*?"           # optional intro lines (none currently)
    r"\n)"                        # blank line before the table
    r"(\| *Ticker.*\n)"           # header row
    r"(\|[-: |]+\n)"              # separator row
    r"((?:\|.*\n)+)"              # data rows
    r"(\nTo add a stock:.*)",
    re.MULTILINE,
)


def _read_identity(md_path: Path) -> tuple[str | None, int]:
    """
    Pull (sector, core_pct) from a stock .md file's Identity section.
    Returns (None, 0) if either field can't be parsed.
    """
    try:
        text = md_path.read_text(encoding="utf-8")
    except OSError:
        return (None, 0)

    sector_m = re.search(r"\*\*Sector:\*\*\s+(.+?)$", text, re.MULTILINE)
    sector = sector_m.group(1).strip() if sector_m else None

    core_m = re.search(r"\*\*Core Position:\*\*\s+(\d+)%", text)
    core_pct = int(core_m.group(1)) if core_m else 0

    return (sector, core_pct)


def sync_active_stocks_table() -> dict[str, Any]:
    """
    Regenerate the Active stocks table in README.md from the current stocks/*.md
    files. Replaces only the table block — leaves the rest of README.md alone.

    Sorted alphabetically by ticker; columns padded so the table stays tidy.
    """
    if not README_MD.exists():
        return {"ok": False, "error": f"README.md not found at {README_MD}"}

    rows: list[tuple[str, str, int]] = []
    for md_file in sorted(STOCKS_DIR.glob("*.md")):
        if md_file.name in ("_TEMPLATE.md",):
            continue
        ticker = md_file.stem.upper()
        sector, core_pct = _read_identity(md_file)
        if sector is None:
            # Skip files that don't look like stock configs (e.g. malformed)
            logger.debug("Skipping %s — no Sector found in Identity section", md_file.name)
            continue
        rows.append((ticker, sector, core_pct))

    if not rows:
        return {"ok": False, "error": "No stock files with parseable Identity found"}

    rows.sort(key=lambda r: r[0])

    # Pad columns to the widest cell so the rendered table stays aligned.
    tw = max([len("Ticker")] + [len(t) for t, _, _ in rows])
    sw = max([len("Sector")] + [len(s) for _, s, _ in rows])
    new_table_lines = [
        f"| {'Ticker':<{tw}} | {'Sector':<{sw}} | Core % |\n",
        f"|{'-' * (tw + 2)}|{'-' * (sw + 2)}|--------|\n",
    ]
    for ticker, sector, core_pct in rows:
        core_cell = f"{core_pct}%"
        new_table_lines.append(f"| {ticker:<{tw}} | {sector:<{sw}} | {core_cell:<6} |\n")

    new_block_body = "".join(new_table_lines)

    # Splice it back into README.md.
    text = README_MD.read_text(encoding="utf-8")
    match = _ACTIVE_STOCKS_BLOCK_RE.search(text)
    if not match:
        return {
            "ok": False,
            "error": "Couldn't locate Active stocks block in README.md (regex anchors changed?)",
        }

    new_text = (
        text[: match.start()]
        + match.group(1)             # "## Active stocks\n\n"
        + new_block_body
        + match.group(5)             # "\nTo add a stock:..."
        + text[match.end() :]
    )

    if new_text != text:
        README_MD.write_text(new_text, encoding="utf-8")
        logger.info("Synced %d stocks into README.md Active stocks table", len(rows))
    else:
        logger.debug("README.md Active stocks table already up to date")

    return {"ok": True, "count": len(rows)}


# ---------------------------------------------------------------------------
# CLI for manual sync
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    usage = (
        "Usage: python3 -m alert_bot.portfolio "
        "<add TICKER | archive TICKER [reason] | restore TICKER | "
        "remove TICKER | archived | sync>"
    )
    if len(sys.argv) < 2:
        print(usage)
        sys.exit(1)

    cmd = sys.argv[1].lower()

    if cmd == "add" and len(sys.argv) >= 3:
        result = add_ticker(sys.argv[2])
    elif cmd == "archive" and len(sys.argv) >= 3:
        reason = " ".join(sys.argv[3:]) or None
        result = archive_ticker(sys.argv[2], reason=reason)
    elif cmd == "restore" and len(sys.argv) >= 3:
        result = restore_ticker(sys.argv[2])
    elif cmd == "remove" and len(sys.argv) >= 3:
        result = remove_ticker(sys.argv[2])
    elif cmd == "archived":
        result = {"ok": True, "archived": list_archived()}
    elif cmd == "sync":
        result = sync_active_stocks_table()
    else:
        print(usage)
        sys.exit(1)

    print(json.dumps(result, indent=2, default=str))
    sys.exit(0 if result.get("ok") else 1)
