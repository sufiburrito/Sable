#!/usr/bin/env python3
"""
journal/pnl_statement.py — infer the cost/tax model from the broker P&L statement.

The broker P&L statement (slow to regenerate) is parsed ONCE to extract the all-in
charge total and the round-trip turnover it applies to, yielding a blended
`charge_rate` (charges as a fraction of turnover). That rate + statutory capital-gains
tax rates are stored in journal/charge_model.json and applied going forward — re-run
this only when a fresh statement is dropped in `stock portfolio/`.

This NEVER touches the gross journal. It only produces the model the separate
"Effective P&L" view consumes. Pure Python, no LLM, no network.

Usage:  python3 -m journal.pnl_statement
"""
import glob
import json
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STMT_GLOB = str(ROOT / "stock portfolio" / "Stocks_PnL_*.xlsx")
MODEL = Path(__file__).resolve().parent / "charge_model.json"

# Statutory capital-gains params (listed equity, current). Editable in charge_model.json.
DEFAULT_STCG = 0.20
DEFAULT_LTCG = 0.125
DEFAULT_LTCG_EXEMPTION = 125000   # ₹1.25L/FY aggregate LTCG exemption (§112A)
# Fallback blended charge rate when no statement is present (≈ delivery cost on turnover).
DEFAULT_CHARGE_RATE = 0.0004


def _latest_statement() -> str | None:
    files = [f for f in glob.glob(STMT_GLOB) if "~" not in f]
    return max(files) if files else None


def parse_statement(path: str) -> dict:
    """Pull the all-in charge total + round-trip turnover from a broker P&L xlsx."""
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)

    # Trade Level: a "Charges" section (label/value rows) ending in a "Total" row,
    # then a "Realised trades" table with Buy value (col F) + Sell value (col I).
    ws = wb["Trade Level"]
    total_charges, turnover = 0.0, 0.0
    in_charges, in_trades = False, False
    rows = list(ws.iter_rows(values_only=True))
    for r in rows:
        label = (str(r[0]).strip() if r and r[0] is not None else "")
        if label == "Charges":
            in_charges, in_trades = True, False
            continue
        if label == "Realised trades":
            in_charges, in_trades = False, True
            continue
        if in_charges and label == "Total" and r[1] is not None:
            total_charges = float(r[1]); in_charges = False
        elif in_trades and label and label != "Stock name":
            # realised-trades data row: Buy value = col index 5, Sell value = col index 8
            try:
                turnover += float(r[5]) + float(r[8])
            except (TypeError, ValueError, IndexError):
                pass
    return {"total_charges": round(total_charges, 2), "turnover": round(turnover, 2)}


def build_model(path: str | None) -> dict:
    if path:
        p = parse_statement(path)
        rate = (p["total_charges"] / p["turnover"]) if p["turnover"] > 0 else DEFAULT_CHARGE_RATE
        return {
            "charge_rate": round(rate, 6), "total_charges": p["total_charges"],
            "turnover": p["turnover"], "stcg_rate": DEFAULT_STCG, "ltcg_rate": DEFAULT_LTCG,
            "ltcg_exemption": DEFAULT_LTCG_EXEMPTION,
            "source": Path(path).name, "derived_at": datetime.now().isoformat(timespec="seconds"),
        }
    return {
        "charge_rate": DEFAULT_CHARGE_RATE, "total_charges": None, "turnover": None,
        "stcg_rate": DEFAULT_STCG, "ltcg_rate": DEFAULT_LTCG,
        "ltcg_exemption": DEFAULT_LTCG_EXEMPTION,
        "source": "defaults (no statement found)", "derived_at": datetime.now().isoformat(timespec="seconds"),
    }


def load_model() -> dict:
    """The charge model the Effective-P&L view uses. Reads charge_model.json, else defaults."""
    if MODEL.exists():
        try:
            return json.loads(MODEL.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return build_model(None)


# ── effective-P&L math (pure; gross is never mutated) ────────────────────────

def effective_for_lot(lot: dict, model: dict) -> dict:
    """Layer a gross closed lot into charges → after-charges → CG tax → take-home."""
    qty = lot.get("quantity", 0) or 0
    turnover = qty * ((lot.get("buy_price") or 0) + (lot.get("sell_price") or 0))
    gross = lot.get("realized_pnl", 0.0) or 0.0
    charges = model["charge_rate"] * turnover
    after = gross - charges
    rate = model["ltcg_rate"] if lot.get("gain_type") == "LTCG" else model["stcg_rate"]
    cg_tax = rate * max(0.0, after)            # losses are not taxed (offsets ignored)
    return {"gross": round(gross, 2), "charges": round(charges, 2),
            "after_charges": round(after, 2), "cg_tax": round(cg_tax, 2),
            "effective": round(after - cg_tax, 2)}


def main():
    stmt = _latest_statement()
    model = build_model(stmt)
    MODEL.write_text(json.dumps(model, indent=2))
    src = model["source"]
    print(f"charge model → {MODEL}")
    print(f"  source: {src}")
    if model["total_charges"] is not None:
        print(f"  charges ₹{model['total_charges']:,} / turnover ₹{model['turnover']:,.0f} "
              f"→ rate {model['charge_rate']*100:.4f}%")
    print(f"  CG tax: STCG {model['stcg_rate']*100:.0f}% · LTCG {model['ltcg_rate']*100:.1f}% "
          "(edit in charge_model.json)")


if __name__ == "__main__":
    main()
