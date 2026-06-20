"""
Robust target / stop-loss derivation for alert levels — emits the `TRADE:` clause
that ends every actionable BUY/SELL message in stocks/*.md (bean algotrading-4eon).

Why this module exists
----------------------
Targets and stops used to be hand-authored per /analyze run, so a level's "R:R 8.4"
was an eyeballed number with no grounding. This module derives them from the data the
repo already computes, following the standard stop/target literature (Sweeney's
MAE/MFE; Van Tharp R-multiples):

  STOP   — the wider/safer of {ATR volatility floor, nearest structural support}.
           The stop is NOT sample-limited: ATR comes from full price history, so every
           level gets a real stop. The deepest rung (no support, no ATR) → "daily close
           below" exit. Empirical p75_dd is a sanity check only.

  TARGET — the level's own Maximum-Favorable-Excursion (mfe_6m from the backtest sidecar)
           SHRUNK toward the stock's pooled floor-run-up by weight w = n/(n+k). Thin
           samples can't produce a wild target; they get pulled back to a stable prior.
           The result is then CAPPED at the regime Monte-Carlo p75 cone — no target above
           what volatility plausibly supports in-horizon.

  R:R    — (target − entry)/(entry − stop), computed here. The TRADE: clause is a TACTICAL
           SWING overlay and is emitted ONLY when R:R >= RR_FLOOR (a genuine clean swing).
           A weak-swing level keeps no TRADE: line — it stays a pure long-term INVESTMENT
           alert whose body carries the conviction. The presence of a TRADE: line is itself
           the "this is also a clean tradeable swing" signal.

Routing (decided per level, not by a static stock list):
  - Binary-phase PENDING  → scenario sentence (a single target is the wrong OBJECT for a
                            bimodal trial/verdict payoff). Self-expires when Sable marks
                            the catalyst COMPLETED.
  - ETF / passive basket  → mean-reversion band (no tactical stop / R:R).
  - clean swing (R:R>=floor) → numeric swing line.
  - weak swing / no R:R   → no TRADE: line (long-term investment add only).

No network calls. Reuses floor_context._compute_atr, regime_context (MC p75),
retrospective_analysis.find_floors (pooled run-up), and the backtest JSON sidecars.
"""
from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# --- tunable constants (documented; never tuned per-stock to flatter R:R) ---
K_SHRINK = 10.0          # shrinkage half-weight: level's own MFE counts n/(n+K_SHRINK)
K_ATR_BULL = 2.5         # ATR multiplier for the stop in a Bull regime (tighter)
K_ATR_VOL = 3.0          # ATR multiplier in a Volatile regime (wider)
K_ATR_DEFAULT = 2.75     # Bear / Sideways / unknown regime
ATR_WINDOW = 14
RR_FLOOR = 2.0      # a TRADE: swing line is emitted only at/above this R:R; weaker = no line
ETF_FALLBACK_STOP_PCT = 0.08   # ETF-only last-resort stop when no ATR + no support below

_STOCKS_DIR = Path("stocks")

# Passive baskets — index/sector ETFs get mean-reversion band framing, not conviction R:R.
ETF_TICKERS = {"ITBEES", "NIF100BEES", "GROWWEV", "GOLDBEES", "NIFTYBEES", "BANKBEES"}


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class TradeLevel:
    price_str: str
    entry: float
    atype: str                       # BUY | SELL
    fmt: str                         # swing | etf_band | scenario | trim
    target: Optional[float] = None
    stop: Optional[float] = None
    rr: Optional[float] = None
    rew_pct: Optional[float] = None
    rsk_pct: Optional[float] = None
    n: int = 0
    w: float = 0.0
    reload_to: Optional[float] = None  # SELL: nearest BUY rung below (the reload anchor)
    notes: str = ""
    clause: str = ""                 # the rendered "TRADE: ..." sentence


# ---------------------------------------------------------------------------
# Pure helpers — the methodology, unit-tested in isolation
# ---------------------------------------------------------------------------

def shrink_weight(n: int, k: float = K_SHRINK) -> float:
    """Empirical-Bayes weight on a level's own sample: w = n/(n+k). 0 when n<=0."""
    if not n or n <= 0:
        return 0.0
    return n / (n + k)


def shrunk_mfe(mfe_level: Optional[float], n: int, pooled_mfe: Optional[float]) -> Optional[float]:
    """
    Blend the level's own run-up toward the stock's pooled prior by sample weight.
    Degrades gracefully: returns whichever side exists, or None if neither does.
    """
    if mfe_level is None and pooled_mfe is None:
        return None
    if mfe_level is None:
        return pooled_mfe
    if pooled_mfe is None:
        return mfe_level
    w = shrink_weight(n)
    return w * mfe_level + (1.0 - w) * pooled_mfe


def buy_target(entry: float, mfe_level: Optional[float], n: int,
               pooled_mfe: Optional[float], mc_p75: Optional[float]) -> tuple[Optional[float], float]:
    """
    Target = entry projected by the shrunk MFE%, then capped at the MC p75 cone.
    Returns (target, shrink_weight). target is None when there is no run-up data at all.
    """
    w = shrink_weight(n)
    sm = shrunk_mfe(mfe_level, n, pooled_mfe)
    if sm is None:
        return None, w
    target = entry * (1.0 + sm / 100.0)
    if mc_p75 is not None and mc_p75 > entry:
        target = min(target, mc_p75)
    return target, w


def buy_stop(entry: float, support_below: Optional[float],
             atr: Optional[float], k_atr: float) -> Optional[float]:
    """
    The wider/safer stop for a BUY = the LOWEST of the available candidates
    (ATR volatility floor and the nearest structural support below). Safety beats
    tightness; position size is reduced to hold rupee-risk constant. Returns None
    only when neither candidate exists → caller renders a "daily close below" exit.
    """
    candidates: list[float] = []
    if atr is not None:
        candidates.append(entry - k_atr * atr)
    if support_below is not None:
        candidates.append(support_below)
    if not candidates:
        return None
    return min(candidates)


def atr_k_for_regime(regime_current: Optional[str]) -> float:
    """Bull → tight, Volatile → wide, everything else → default."""
    r = (regime_current or "").lower()
    if r == "bull":
        return K_ATR_BULL
    if r == "volatile":
        return K_ATR_VOL
    return K_ATR_DEFAULT


def rr_triplet(entry: float, target: float, stop: float) -> tuple[float, float, float]:
    """Returns (rr, reward_pct, risk_pct). Caller guarantees entry > stop and target > entry."""
    rew_pct = (target - entry) / entry * 100.0
    rsk_pct = (entry - stop) / entry * 100.0
    rr = (target - entry) / (entry - stop)
    return rr, rew_pct, rsk_pct


def nearest_below(price: float, candidates: list[float]) -> Optional[float]:
    """Largest candidate strictly below `price`, or None."""
    below = [c for c in candidates if c < price]
    return max(below) if below else None


def nearest_above(price: float, candidates: list[float]) -> Optional[float]:
    """Smallest candidate strictly above `price`, or None."""
    above = [c for c in candidates if c > price]
    return min(above) if above else None


# ---------------------------------------------------------------------------
# Binary-phase field
# ---------------------------------------------------------------------------

def parse_binary_phase(content: str) -> Optional[dict]:
    """
    Read the optional `Binary-phase:` config field. Returns parsed anchors only while
    the catalyst is unresolved (status PENDING); None for COMPLETED or absent.

    Format:
      Binary-phase: PENDING | catalyst: <text> | expected: <date> | positive: ₹A-B | negative-stop: ₹C
    """
    for line in content.splitlines():
        if "Binary-phase:" not in line:
            continue
        rest = line.split("Binary-phase:", 1)[1].strip()
        segs = [s.strip() for s in rest.split("|")]
        if not segs:
            return None
        status = segs[0].split()[0].upper() if segs[0] else ""
        if status != "PENDING":
            return None
        d: dict = {"status": "PENDING"}
        for seg in segs[1:]:
            if ":" in seg:
                key, val = seg.split(":", 1)
                d[key.strip()] = val.strip()
        return d
    return None


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _fmt_price(x: float) -> str:
    """₹-free price token: integer with comma grouping when whole, else 1 dp."""
    if abs(x - round(x)) < 0.05:
        return f"{int(round(x)):,}"
    return f"{x:,.1f}"


def _fmt_pct(x: float) -> str:
    return f"{x:.1f}"


def _fmt_rr(rr: float) -> str:
    """Up to 2 dp, trailing zeros stripped (2.0→'2', 1.75→'1.75')."""
    s = f"{rr:.2f}".rstrip("0").rstrip(".")
    return s


def _trim_pct(message: str) -> Optional[str]:
    """Pull an existing 'Trim NN%' / 'Exit' intent out of the level's own message."""
    m = re.search(r"[Tt]rim\s+(\d+)\s*%", message)
    if m:
        return m.group(1)
    if re.search(r"\b[Ee]xit\b|[Ff]ull", message):
        return "exit"
    return None


def _trim_head(message: str, at_price: str) -> str:
    """Render the trim verb, reusing the level's own percentage when it states one."""
    pct = _trim_pct(message)
    if pct == "exit":
        return f"Exit remaining swing at ₹{at_price}"
    if pct:
        return f"Trim {pct}% at ₹{at_price}"
    return f"Trim swing at ₹{at_price}"


# Visual separator for the TRADE: line. A LOOK-ALIKE vertical bar (U+2502 "│"), NOT
# the ASCII "|": the clause lives in a markdown table cell whose columns are split on
# ASCII "|" (parser.py + _apply_to_file), so a real pipe would corrupt row parsing.
_SEP = " │ "


def _fallback_comment(tl: "TradeLevel", regime_current: Optional[str]) -> str:
    """Deterministic, Sable-voiced one-liner for the note slot, used until a real
    intuition is hand-authored during /analyze. Keyed on route + regime."""
    r = (regime_current or "").lower()
    if tl.atype == "SELL":
        return "Bank the swing; the core rides on."
    if tl.fmt == "etf_band":
        return "Patient accumulation — mean reversion favours the buyer."
    if r == "bull":
        return "Trend's with you — add with conviction."
    if r in ("bear", "volatile"):
        return "Counter-trend add — size small, respect the stop."
    return "Constructive setup — accumulate into weakness."


def format_clause(tl: TradeLevel, *, catalyst: str = "", reload_to: Optional[float] = None,
                  positive: str = "", negative_stop: str = "", comment: str = "") -> str:
    """Render the TRADE: sentence for the level's chosen format.

    BUY trades (single-name swing + ETF band) → "Buy at ₹X │ Target: ₹Y │ SL: ₹Z │ note".
    SELL trims (single-name + ETF band SELL)  → "Trim … at ₹X │ Reload: ₹Y │ note".
    Binary-phase scenarios keep their prose (a single Buy/Target/SL misrepresents a
    bimodal catalyst). `comment` is the resolved (authored-or-fallback) Sable note.
    """
    e = _fmt_price(tl.entry)
    note = f"{_SEP}{comment}" if comment else ""

    if tl.fmt == "scenario":
        cat = catalyst or "the binary event"
        pos = (positive or "re-rate").lstrip("₹").strip()
        neg = (negative_stop or "").lstrip("₹").strip()
        # Rungs the file itself marks as reachable only after a positive readout must
        # not invite pre-data accumulation — honour that intent verbatim.
        if tl.atype == "BUY" and re.search(r"POST-POSITIVE-DATA|post-data", tl.notes, re.I):
            return "TRADE: POST-POSITIVE-DATA reload only — no entry pre-binary."
        if tl.atype == "SELL":
            return (f"TRADE: Post-{cat} only — trim into a positive re-rate (₹{pos}); "
                    f"pre-data hold, core never sold.")
        neg_clause = (f"negative → structural stop daily close below ₹{neg}."
                      if neg else "negative → exit on structural breakdown.")
        return (f"TRADE: Accumulate ₹{e} (core add) → positive {cat} ₹{pos}; {neg_clause}")

    # SELL trims (single-name trim + ETF band SELL) — Trim at │ Reload │ note
    if tl.atype == "SELL" and tl.fmt in ("trim", "etf_band"):
        rl = f"{_SEP}Reload: ₹{_fmt_price(reload_to)}" if reload_to is not None else ""
        return f"TRADE: {_trim_head(tl.notes, e)}{rl}{note}"

    # BUY trades (single-name swing + ETF band) —
    #   Buy at ₹X │ Target: ₹Y (+rew%) │ SL: ₹Z │ R:R N, risk −P% │ {note}
    # reward% sits on the target; R:R + risk% are their own metrics segment; the
    # Sable note is the final segment (own segment → robust to any note text). A
    # swing only reaches here when it earned a line (rr >= RR_FLOOR, gated in
    # derive_levels); missing target/stop → no line (investment add only).
    if tl.atype == "BUY" and tl.fmt in ("swing", "etf_band"):
        if tl.target is None or tl.stop is None:
            return ""
        rew = tl.rew_pct if tl.rew_pct is not None else (tl.target - tl.entry) / tl.entry * 100.0
        segs = [f"Buy at ₹{e}",
                f"Target: ₹{_fmt_price(tl.target)} (+{_fmt_pct(rew)}%)",
                f"SL: ₹{_fmt_price(tl.stop)}"]
        meta = []
        if tl.rr is not None:
            meta.append(f"R:R {_fmt_rr(tl.rr)}")
        if tl.rsk_pct is not None:
            meta.append(f"risk −{_fmt_pct(tl.rsk_pct)}%")
        if meta:
            segs.append(", ".join(meta))
        if comment:
            segs.append(comment)
        return "TRADE: " + _SEP.join(segs)

    return ""


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def derive_levels(stock, *, atr: Optional[float] = None, backtest: Optional[dict] = None,
                  regime: Optional[dict] = None, binary: Optional[dict] = None,
                  pooled_mfe: Optional[float] = None,
                  is_etf: Optional[bool] = None,
                  comments: Optional[dict] = None) -> list[TradeLevel]:
    """
    Produce a TradeLevel (with rendered clause) for every BUY/SELL rung on `stock`.
    All data is injected so this is pure/testable; the CLI wires the real sources.
    `comments` maps price_str → hand-authored Sable note (preserved across sweeps);
    levels without an authored note fall back to a deterministic phrase.
    """
    backtest = backtest or {}
    regime = regime or {}
    comments = comments or {}
    bt_levels = backtest.get("levels", {})
    if is_etf is None:
        is_etf = stock.ticker in ETF_TICKERS

    regime_current = regime.get("current")
    mc_p75 = regime.get("mc_p75_30d")
    k_atr = atr_k_for_regime(regime_current)

    buy_entries = [lv.upper for lv in stock.levels if lv.alert_type == "BUY"]
    sell_entries = [lv.lower for lv in stock.levels if lv.alert_type == "SELL"]

    def _note(t: TradeLevel) -> str:
        return comments.get(t.price_str) or _fallback_comment(t, regime_current)

    out: list[TradeLevel] = []
    for lv in stock.levels:
        if lv.alert_type not in ("BUY", "SELL"):
            continue
        entry = lv.upper if lv.alert_type == "BUY" else lv.lower

        # --- binary phase: scenario framing for the whole stock (no pipe note) ---
        if binary is not None:
            t = TradeLevel(price_str=lv.price_str, entry=entry, atype=lv.alert_type,
                           fmt="scenario", notes=lv.message)
            t.clause = format_clause(
                t, catalyst=binary.get("catalyst", ""),
                positive=binary.get("positive", ""),
                negative_stop=binary.get("negative-stop", ""),
            )
            out.append(t)
            continue

        # --- ETF band framing ---
        if is_etf:
            if lv.alert_type == "BUY":
                resist = nearest_above(entry, sell_entries)
                support_below = nearest_below(entry, [b for b in buy_entries if b < entry])
                stop = buy_stop(entry, support_below, atr, k_atr)
                if stop is None:   # ETF deepest rung w/o ATR or support → flat fallback
                    stop = round(entry * (1 - ETF_FALLBACK_STOP_PCT), 1)
                t = TradeLevel(price_str=lv.price_str, entry=entry, atype="BUY",
                               fmt="etf_band", target=resist, stop=stop, notes=lv.message)
                if resist is not None:
                    t.rew_pct = (resist - entry) / entry * 100.0
                    if stop is not None and entry > stop:
                        t.rr, _, t.rsk_pct = rr_triplet(entry, resist, stop)
                t.clause = format_clause(t, comment=_note(t))
            else:
                reload_to = nearest_below(entry, buy_entries)
                t = TradeLevel(price_str=lv.price_str, entry=entry, atype="SELL",
                               fmt="etf_band", reload_to=reload_to, notes=lv.message)
                t.clause = format_clause(t, reload_to=reload_to, comment=_note(t))
            out.append(t)
            continue

        # --- SELL trim (single names) ---
        if lv.alert_type == "SELL":
            reload_to = nearest_below(entry, buy_entries)
            t = TradeLevel(price_str=lv.price_str, entry=entry, atype="SELL",
                           fmt="trim", reload_to=reload_to, notes=lv.message)
            t.clause = format_clause(t, reload_to=reload_to, comment=_note(t))
            out.append(t)
            continue

        # --- numeric BUY swing ---
        bt = bt_levels.get(lv.price_str, {})
        n = int(bt.get("n", 0) or 0)
        mfe_level = bt.get("mfe_6m")
        target, w = buy_target(entry, mfe_level, n, pooled_mfe, mc_p75)
        if target is None:
            # fall back to nearest structural resistance as a sanity reference
            target = nearest_above(entry, sell_entries)
        support_below = nearest_below(entry, [b for b in buy_entries if b < entry])
        stop = buy_stop(entry, support_below, atr, k_atr)

        t = TradeLevel(price_str=lv.price_str, entry=entry, atype="BUY",
                       fmt="swing", target=target, stop=stop, n=n, w=w, notes=lv.message)
        if target is not None:
            t.rew_pct = (target - entry) / entry * 100.0
            if stop is not None and entry > stop:
                t.rr, _, t.rsk_pct = rr_triplet(entry, target, stop)
        # TRADE: overlay only for a genuine clean swing (good R:R). A weak swing is still a
        # valid long-term investment add — it keeps its conviction body and gets NO TRADE: line.
        t.clause = (format_clause(t, comment=_note(t))
                    if (t.rr is not None and t.rr >= RR_FLOOR) else "")
        out.append(t)

    return out


# ---------------------------------------------------------------------------
# Data wiring (CLI side — file reads, no network)
# ---------------------------------------------------------------------------

def _pooled_floor_mfe(df) -> Optional[float]:
    """
    Prior for the target: the stock's median run-up from its historical floors.
    Reuses retrospective_analysis.find_floors (local minima that recovered ≥ threshold)
    and measures forward run-up over the same recovery window. Larger n than any single
    level. (Mildly upward-biased — it only counts floors that bounced — which the MC p75
    cap then reins in.)
    """
    try:
        from retrospective_analysis import find_floors, RECOVERY_BARS
    except Exception:
        return None
    floors = find_floors(df)
    if not floors:
        return None
    import statistics
    n = len(df)
    runups: list[float] = []
    for idx in floors:
        low = float(df["Low"].iloc[idx])
        fwd = min(idx + RECOVERY_BARS, n)
        if idx + 1 >= fwd or low <= 0:
            continue
        hi = float(df["High"].iloc[idx + 1: fwd].max())
        runups.append((hi - low) / low * 100.0)
    return statistics.median(runups) if runups else None


def _compute_for_ticker(ticker: str, *, regime: Optional[dict] = None) -> dict:
    """
    Gather the injected inputs for one ticker from local files (no network).

    When *regime* is supplied (e.g. the live alert path passing the cached daily
    regime), the expensive HMM + Monte-Carlo recompute is skipped — keeping the
    hot path network- and compute-light. When omitted (the CLI bake), it computes
    the regime live.
    """
    from .floor_context import _compute_atr, _load_ohlc, _load_backtest

    df = _load_ohlc(ticker)
    atr = _compute_atr(df) if df is not None and len(df) > ATR_WINDOW else None
    pooled = _pooled_floor_mfe(df) if df is not None else None
    backtest = _load_backtest(ticker)

    # Regime + MC p75 cone. Use the injected cache when given; else compute live.
    if regime is None:
        regime = {}
        if df is not None and len(df) >= 60:
            try:
                from .regime_context import run_regime_detection, run_simulation
                closes = df["Close"].values.astype(float).tolist()
                volumes = df["Volume"].values.astype(float).tolist()
                rd = run_regime_detection(closes, volumes, lookback_days=504, n_states=4)
                mc = run_simulation(closes, days_forward=30, n_sims=10_000,
                                    lookback_days=252, regime_data=rd)
                regime = {"current": rd["current"], "mc_p75_30d": float(mc["fan"][75][-1])}
            except Exception:
                regime = {}

    # Binary-phase flag from the config file itself.
    binary = None
    cfg = _STOCKS_DIR / f"{ticker}.md"
    if cfg.exists():
        binary = parse_binary_phase(cfg.read_text(encoding="utf-8"))

    return {"atr": atr, "backtest": backtest, "regime": regime,
            "pooled_mfe": pooled, "binary": binary}


# ---------------------------------------------------------------------------
# Live alert path — regime-gated tactical overlay (bean algotrading-96ic)
# ---------------------------------------------------------------------------

def regime_blocks_overlay(regime_current: Optional[str], atype: str) -> bool:
    """
    True when the current HMM regime is hostile to the alert direction, so the
    tactical TRADE: overlay should be WITHHELD (the alert still fires; this only
    gates the overlay). Mirrors the signs in confidence.py:_score_regime:
      BUY/WATCH hostile in bear (and volatile for BUY); SELL hostile in bull.
    An unknown/empty regime never blocks.
    """
    r = (regime_current or "").lower()
    if not r:
        return False
    if atype == "SELL":
        return r == "bull"
    # BUY / WATCH
    if atype == "WATCH":
        return r == "bear"
    return r in ("bear", "volatile")


def select_overlay(derived: list[TradeLevel], price_str: str,
                   regime_current: Optional[str]) -> Optional[TradeLevel]:
    """
    Pure picker+gate (no IO): return the level matching *price_str* only if it is a
    numeric tactical overlay worth showing. Returns None when:
      - the level has no clause (weak swing → investment-only), or
      - it's a binary `scenario` level (no numeric target/stop; the scenario is
        investment context, not a tactical swing overlay), or
      - it's a genuine swing/trim AND the regime is hostile to the direction.
    ETF bands are passive and not regime-gated.
    """
    tl = next((d for d in derived if d.price_str == price_str), None)
    if tl is None or not tl.clause or tl.fmt == "scenario":
        return None
    if tl.fmt in ("swing", "trim") and regime_blocks_overlay(regime_current, tl.atype):
        return None
    return tl


def live_overlay(ticker: str, price_str: str, *, regime_cache: Optional[dict] = None,
                 stock=None) -> Optional[TradeLevel]:
    """
    Fire-time tactical overlay for one alert level, or None when it should be
    withheld (weak swing, hostile regime, binary scenario, or no data). Network-free:
    reuses the cached daily regime (regime_cache[ticker]) — no HMM/MC in the poll loop.
    Pass the already-parsed *stock* to avoid re-reading the .md on the hot path.
    """
    if stock is None:
        from .parser import parse_stock_file
        stock = parse_stock_file(_STOCKS_DIR / f"{ticker}.md")
        if stock is None:
            return None
    regime = (regime_cache or {}).get(ticker)
    inputs = _compute_for_ticker(ticker, regime=regime)
    derived = derive_levels(stock, **inputs)
    return select_overlay(derived, price_str, (regime or {}).get("current"))


def parse_existing_comments(md_text: str) -> dict:
    """Pull each level's hand-authored Sable note (the final `│`-separated segment of
    its current TRADE: line) out of a stock .md, keyed by price_str.

    Lets `--apply` regenerate the numeric metrics every sweep while preserving the
    authored note verbatim. Old prose-format TRADE: lines (no `│`) yield no note, so
    they fall back to a deterministic phrase. The note slot itself never contains a
    raw ASCII `|`, so table-column splitting is unaffected.
    """
    sep = _SEP.strip()  # the look-alike "│"
    out: dict = {}
    for line in md_text.splitlines():
        s = line.rstrip()
        if not (s.startswith("|") and "₹" in s and "TRADE:" in s):
            continue
        cols = [c.strip() for c in s.split("|")[1:-1]]
        if len(cols) < 4:
            continue
        price_str, msg = cols[1], cols[3]
        trade = msg[msg.index("TRADE:"):].rstrip().rstrip('"').rstrip()
        if sep not in trade:
            continue
        last = trade.split(sep)[-1].strip()
        # The note is always the final segment. Skip only a trailing *structural* or
        # *numeric-metric* segment (a note-less line): "Buy at ₹…/Target:/SL:/Reload:"
        # or a metric "R:R <number>…". A note that merely starts with those letters
        # (e.g. "R:R thins out up here…") is still captured.
        if last and not re.match(r"^(Buy at ₹|Target:|SL:|Reload:|R:R\s+[\d.])", last):
            out[price_str] = last
    return out


def _apply_to_file(ticker: str, derived: list[TradeLevel]) -> tuple[int, int]:
    """
    Sync each level's TRADE: clause into its matching Alert Message cell in stocks/TICKER.md,
    preserving the existing message verbatim. Idempotent: any prior TRADE: clause is stripped
    first, then the new one appended — or, when the level no longer warrants a TRADE: line
    (weak swing), it is left with none. Returns (levels_before, levels_after) so the caller
    can assert the parser still sees every row (guards the silent-drop in parser.py).
    """
    from .parser import parse_stock_file
    path = _STOCKS_DIR / f"{ticker}.md"
    parsed = parse_stock_file(path)
    if parsed is None:
        return 0, 0
    before = len(parsed.levels)
    by_price = {d.price_str: d for d in derived}

    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    out_lines: list[str] = []
    for line in lines:
        stripped = line.rstrip("\n")
        if stripped.startswith("|") and "₹" in stripped:
            cols = [c.strip() for c in stripped.split("|")[1:-1]]
            if len(cols) >= 4 and cols[1] in by_price:
                clause = by_price[cols[1]].clause
                raw = cols[3].strip()
                has_quote = raw.startswith('"') and raw.endswith('"')
                inner = raw[1:-1] if has_quote else raw
                # idempotent: strip any prior TRADE: clause, then re-append only if earned
                if "TRADE:" in inner:
                    inner = inner[:inner.index("TRADE:")].rstrip()
                inner = inner.rstrip()
                new_inner = f"{inner} {clause}" if clause else inner
                new_cell = f'"{new_inner}"' if has_quote else new_inner
                if new_cell != cols[3]:           # only rewrite a cell that actually changed
                    cols[3] = new_cell
                    line = "| " + " | ".join(cols) + " |\n"
        out_lines.append(line)

    path.write_text("".join(out_lines), encoding="utf-8")
    reparsed = parse_stock_file(path)
    after = len(reparsed.levels) if reparsed is not None else -1
    return before, after


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Derive TRADE: target/stop clauses for a ticker.")
    ap.add_argument("ticker")
    ap.add_argument("--apply", action="store_true",
                    help="append clauses to stocks/TICKER.md (default: print for review)")
    args = ap.parse_args(argv)

    ticker = args.ticker.upper()
    from .parser import parse_stock_file
    cfg_path = _STOCKS_DIR / f"{ticker}.md"
    stock = parse_stock_file(cfg_path)
    if stock is None:
        print(f"no config for {ticker}")
        return 1

    # Preserve any hand-authored Sable notes across the re-sweep (numbers refresh,
    # the authored note tail is kept; un-annotated levels get a fallback phrase).
    comments = parse_existing_comments(cfg_path.read_text(encoding="utf-8"))
    inputs = _compute_for_ticker(ticker)
    derived = derive_levels(stock, comments=comments, **inputs)

    if not args.apply:
        mode = ("scenario (binary PENDING)" if inputs["binary"]
                else "ETF band" if ticker in ETF_TICKERS else "numeric")
        print(f"# {ticker} — {mode}; atr={inputs['atr']}, "
              f"pooled_mfe={inputs['pooled_mfe']}, regime={inputs['regime'].get('current')}")
        for d in derived:
            print(f"  {d.price_str:<12} {d.atype:<4} {d.fmt:<9} {d.clause}")
        return 0

    before, after = _apply_to_file(ticker, derived)
    if before != after:
        print(f"!! {ticker}: level count changed {before}→{after} — REVERT AND INSPECT")
        return 2
    print(f"{ticker}: applied {len(derived)} clauses; level count stable ({after})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
