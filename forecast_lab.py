#!/usr/bin/env python3
"""
forecast_lab.py — empirical test of Prophet methods for the TA pipeline.

Backtests a panel of Prophet "methods" (plus naive baselines) on each active
stock's OHLC via expanding-window walk-forward, and scores each on:
  - directional accuracy  (did it call up vs down, vs the realized move)
  - point error           (MAPE / RMSE at the horizon)
  - skill vs random-walk  (1 - mape_method / mape_rw ; >0 means it beats RW)
  - trade-utility         (long-only timing rule's mean return vs buy-and-hold)
  - coverage              (secondary: does the 80% cone contain the outcome)

Every method is judged against a random-walk baseline — a method that doesn't
beat RW is reported as such. Research only; nothing here changes production.

The 7 methods from the plan, mapped to scorable variants:
  M0_current      incumbent config (raw price, weekly+yearly seasonality)  — the thing to beat
  M1_log          (1) log-space fit, seasonality kept
  M2_trendonly    (2) log-space, seasonality OFF (trend + changepoints)
  M3_regressors   (3) M2 + exogenous regressors (Nifty return, log-volume)
  M4_changepoint  (4) extrapolate the slope of the latest trend segment
  M5_trend        (5) extrapolate Prophet's fitted trend component (adaptive MA)
  M6_weekly       (6) fit on weekly bars (noise reduction for longer horizons)
  M7_garch_cone   (7) M2 mean + GARCH(1,1) cone (judged on coverage)
M2/M4/M5/M7 share ONE trend-only fit per origin, so the run stays bounded.

Usage:
  python3 forecast_lab.py --quick           # 3 stocks, recent origins (smoke test)
  python3 forecast_lab.py                    # all active stocks (slow — run in bg)
  python3 forecast_lab.py BBOX SUVEN         # specific tickers
Outputs results/prophet_lab/scorecard.json + a ranking table to stdout.
"""
from __future__ import annotations

import json
import logging
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
for noisy in ("prophet", "cmdstanpy"):
    logging.getLogger(noisy).setLevel(logging.CRITICAL)

ROOT = Path(__file__).parent
ANALYSIS = ROOT / "analysis"
STOCKS = ROOT / "stocks"
OUT_DIR = ROOT / "results" / "prophet_lab"

HORIZONS = [10, 21, 63]          # trading days
MAX_H = max(HORIZONS)
MIN_TRAIN = 252                  # ≥1y before an origin is eligible
STEP = 21                        # monthly walk-forward origins
MAX_ORIGINS = 36                 # cap to the most recent ~3y of origins
Z80 = 1.2815515594               # 80% two-sided normal quantile
BENCH = "NIF100BEES"             # Nifty-100 ETF as the market regressor
EXCLUDE = {"_TEMPLATE", BENCH}

_PROPHET = None


def _prophet():
    global _PROPHET
    if _PROPHET is None:
        from prophet import Prophet
        _PROPHET = Prophet
    return _PROPHET


# ── data ────────────────────────────────────────────────────────────────────

def load_ohlc(ticker: str) -> pd.DataFrame | None:
    p = ANALYSIS / f"{ticker}_ohlc_cache.csv"
    if not p.exists():
        return None
    df = pd.read_csv(p, parse_dates=["Date"], index_col="Date").sort_index()
    return df if len(df) else None


def active_tickers() -> list[str]:
    ts = sorted(p.stem for p in STOCKS.glob("*.md"))
    return [t for t in ts if t not in EXCLUDE and (ANALYSIS / f"{t}_ohlc_cache.csv").exists()]


# ── prophet helpers ─────────────────────────────────────────────────────────

def _fit(train: pd.DataFrame, *, seasonal: bool, cp: float = 0.05,
         regressors: dict[str, pd.Series] | None = None, mode: str = "additive"):
    """Fit Prophet on the provided y column; return (model, future_template)."""
    P = _prophet()
    m = P(growth="linear", changepoint_prior_scale=cp, seasonality_mode=mode,
          daily_seasonality=False, weekly_seasonality=seasonal,
          yearly_seasonality=seasonal, interval_width=0.80)
    df = train.reset_index().rename(columns={"index": "ds", train.index.name or "Date": "ds"})
    if regressors:
        for name in regressors:
            m.add_regressor(name)
    m.fit(df)
    return m


def _predict_on_dates(model, train_ds, future_dates, regressor_future=None) -> pd.DataFrame:
    rows = list(train_ds) + list(future_dates)
    fut = pd.DataFrame({"ds": rows})
    if regressor_future is not None:
        for name, series in regressor_future.items():
            fut[name] = series.values
    return model.predict(fut)


def _sample(fc: pd.DataFrame, future_dates, horizons, last_price, *, log: bool):
    """Read yhat/yhat_lower/upper at each horizon offset, back-transform if log."""
    fc = fc.set_index("ds")
    out = {}
    for h in horizons:
        if h > len(future_dates):
            continue
        d = future_dates[h - 1]
        if d not in fc.index:
            continue
        yhat, lo, hi = fc.loc[d, ["yhat", "yhat_lower", "yhat_upper"]]
        if log:
            yhat, lo, hi = np.exp(yhat), np.exp(lo), np.exp(hi)
        out[h] = (float(yhat), float(lo), float(hi))
    return out


# ── methods (each returns {h: (pred, lo, hi)}) ──────────────────────────────

def m_baseline_rw(train, fut_dates, horizons, last, **_):
    sd = float(np.std(np.diff(np.log(train["Close"].values[-252:]))))
    out = {}
    for h in horizons:
        band = last * Z80 * sd * np.sqrt(h)
        out[h] = (last, last - band, last + band)
    return out


def m_baseline_drift(train, fut_dates, horizons, last, **_):
    lr = np.diff(np.log(train["Close"].values))
    mu, sd = float(np.mean(lr[-252:])), float(np.std(lr[-252:]))
    out = {}
    for h in horizons:
        pred = last * np.exp(mu * h)
        band = pred * Z80 * sd * np.sqrt(h)
        out[h] = (pred, pred - band, pred + band)
    return out


def m0_current(train, fut_dates, horizons, last, **_):
    t = train[["Close"]].rename(columns={"Close": "y"})
    m = _fit(t, seasonal=True, cp=0.05, mode="multiplicative")
    fc = _predict_on_dates(m, train.index, fut_dates)
    return _sample(fc, fut_dates, horizons, last, log=False)


def m1_log(train, fut_dates, horizons, last, **_):
    t = pd.DataFrame({"y": np.log(train["Close"])}, index=train.index)
    m = _fit(t, seasonal=True, cp=0.05)
    fc = _predict_on_dates(m, train.index, fut_dates)
    return _sample(fc, fut_dates, horizons, last, log=True)


def fit_trendonly(train):
    """Shared log-space trend-only fit reused by M2/M4/M5/M7."""
    t = pd.DataFrame({"y": np.log(train["Close"])}, index=train.index)
    return _fit(t, seasonal=False, cp=0.05)


def m2_from(model, train, fut_dates, horizons, last):
    fc = _predict_on_dates(model, train.index, fut_dates)
    return _sample(fc, fut_dates, horizons, last, log=True), fc


def m4_changepoint(model, train, fut_dates, horizons, last):
    """Extrapolate the slope of the latest trend segment from the last changepoint."""
    cps = pd.to_datetime(model.changepoints)
    hist = _predict_on_dates(model, train.index, []).set_index("ds")["trend"]
    last_cp = cps[cps <= hist.index[-1]].max() if len(cps) else hist.index[max(0, len(hist) - 21)]
    seg = hist[hist.index >= last_cp]
    if len(seg) < 2:
        seg = hist[-21:]
    slope = (seg.iloc[-1] - seg.iloc[0]) / max(1, (len(seg) - 1))   # log per trading day
    out = {}
    for h in horizons:
        pred = float(np.exp(np.log(last) + slope * h))
        out[h] = (pred, np.nan, np.nan)
    return out


def m5_trend(fc_log, fut_dates, horizons, last):
    """Use Prophet's fitted trend component itself as the forecast (adaptive MA)."""
    f = fc_log.set_index("ds")["trend"]
    out = {}
    for h in horizons:
        if h <= len(fut_dates) and fut_dates[h - 1] in f.index:
            out[h] = (float(np.exp(f.loc[fut_dates[h - 1]])), np.nan, np.nan)
    return out


def m7_garch(m2_means, train, horizons, last):
    """M2 mean + GARCH(1,1) cumulative-variance cone (coverage-focused)."""
    from arch import arch_model
    lr = 100 * np.diff(np.log(train["Close"].values))
    try:
        res = arch_model(lr, mean="Zero", vol="GARCH", p=1, q=1).fit(disp="off")
        var = res.forecast(horizon=MAX_H, reindex=False).variance.values[-1]  # per-step, %^2
    except Exception:
        return {}
    out = {}
    for h in horizons:
        if h not in m2_means:
            continue
        mean = m2_means[h][0]
        cum_sd = np.sqrt(np.sum(var[:h])) / 100.0          # log-return sd over h
        out[h] = (mean, float(mean * np.exp(-Z80 * cum_sd)), float(mean * np.exp(Z80 * cum_sd)))
    return out


def m3_regressors(train, fut_dates, horizons, last, bench=None, **_):
    t = pd.DataFrame({"y": np.log(train["Close"])}, index=train.index)
    regs = {}
    regs["logvol"] = np.log(train["Volume"].replace(0, np.nan)).ffill().fillna(0).values
    if bench is not None:
        b = bench["Close"].reindex(train.index).ffill().bfill()
        regs["benchret"] = np.log(b).diff().fillna(0).values
    for name, vals in regs.items():
        t[name] = vals
    m = _fit(t, seasonal=False, cp=0.05, regressors=regs)
    # Persistence: future regressor values = last observed.
    fut_reg = {name: pd.Series([vals[-1]] * (len(train) + len(fut_dates))) for name, vals in regs.items()}
    fc = _predict_on_dates(m, train.index, fut_dates, regressor_future=fut_reg)
    return _sample(fc, fut_dates, horizons, last, log=True)


def m6_weekly(train, fut_dates, horizons, last, **_):
    wk = train["Close"].resample("W-FRI").last().dropna()
    if len(wk) < 60:
        return {}
    t = pd.DataFrame({"y": np.log(wk)}, index=wk.index)
    m = _fit(t, seasonal=False, cp=0.05)
    wfut = [wk.index[-1] + pd.Timedelta(weeks=i) for i in range(1, MAX_H // 5 + 3)]
    fc = _predict_on_dates(m, wk.index, wfut).set_index("ds")["yhat"]
    out = {}
    for h in horizons:
        target = train.index[-1] + pd.Timedelta(days=int(h * 7 / 5))  # ~h trading days in cal days
        nearest = min(fc.index, key=lambda d: abs((d - target).days))
        out[h] = (float(np.exp(fc.loc[nearest])), np.nan, np.nan)
    return out


METHOD_ORDER = ["RW", "Drift", "M0_current", "M1_log", "M2_trendonly",
                "M3_regressors", "M4_changepoint", "M5_trend", "M6_weekly", "M7_garch_cone"]


def evaluate_origin(train, fut_dates, horizons, last, bench):
    res = {}
    res["RW"] = m_baseline_rw(train, fut_dates, horizons, last)
    res["Drift"] = m_baseline_drift(train, fut_dates, horizons, last)
    for name, fn in (("M0_current", m0_current), ("M1_log", m1_log)):
        try:
            res[name] = fn(train, fut_dates, horizons, last)
        except Exception:
            res[name] = {}
    try:
        model = fit_trendonly(train)
        m2, fc_log = m2_from(model, train, fut_dates, horizons, last)
        res["M2_trendonly"] = m2
        res["M4_changepoint"] = m4_changepoint(model, train, fut_dates, horizons, last)
        res["M5_trend"] = m5_trend(fc_log, fut_dates, horizons, last)
        res["M7_garch_cone"] = m7_garch(m2, train, horizons, last)
    except Exception:
        for k in ("M2_trendonly", "M4_changepoint", "M5_trend", "M7_garch_cone"):
            res.setdefault(k, {})
    for name, fn in (("M3_regressors", m3_regressors), ("M6_weekly", m6_weekly)):
        try:
            res[name] = fn(train, fut_dates, horizons, last, bench=bench)
        except Exception:
            res[name] = {}
    return res


# ── scoring ─────────────────────────────────────────────────────────────────

def walk_forward(ticker: str, bench: pd.DataFrame | None, quick: bool) -> dict:
    df = load_ohlc(ticker)
    if df is None or len(df) < MIN_TRAIN + MAX_H + STEP:
        return {}
    close = df["Close"]
    n = len(df)
    origins = list(range(MIN_TRAIN, n - MAX_H, STEP))
    origins = origins[-(3 if quick else MAX_ORIGINS):]
    # raw records: method -> horizon -> list of (pred, lo, hi, last, actual)
    rec: dict = {m: {h: [] for h in HORIZONS} for m in METHOD_ORDER}
    for i in origins:
        train = df.iloc[:i]
        last = float(close.iloc[i - 1])
        fut_dates = list(df.index[i:i + MAX_H])
        actuals = {h: float(close.iloc[i + h - 1]) for h in HORIZONS if i + h - 1 < n}
        res = evaluate_origin(train, fut_dates, HORIZONS, last, bench)
        for m in METHOD_ORDER:
            for h in HORIZONS:
                if h in res.get(m, {}) and h in actuals:
                    pred, lo, hi = res[m][h]
                    rec[m][h].append((pred, lo, hi, last, actuals[h]))
    return _aggregate(rec)


def _aggregate(rec) -> dict:
    out = {}
    for m in METHOD_ORDER:
        out[m] = {}
        for h in HORIZONS:
            rows = rec[m][h]
            if not rows:
                continue
            pred, lo, hi, last, act = (np.array(x) for x in zip(*rows))
            up_pred, up_act = np.sign(pred - last), np.sign(act - last)
            mask = up_act != 0
            dir_acc = float(np.mean(up_pred[mask] == up_act[mask])) if mask.any() else np.nan
            mape = float(np.mean(np.abs(pred - act) / act))
            rmse = float(np.sqrt(np.mean((pred - act) ** 2)))
            realized = act / last - 1
            strat = np.where(pred > last, realized, 0.0)   # long-only timing rule
            cov = (float(np.mean((act >= lo) & (act <= hi)))
                   if not np.isnan(lo).all() else np.nan)
            out[m][h] = {
                "n": int(len(rows)), "dir_acc": dir_acc, "mape": mape, "rmse": rmse,
                "trade_ret": float(np.mean(strat)), "buyhold_ret": float(np.mean(realized)),
                "coverage": cov,
            }
    return out


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    quick = "--quick" in sys.argv
    bench = load_ohlc(BENCH)
    tickers = [t.upper() for t in args] if args else active_tickers()
    if quick and not args:
        tickers = tickers[:3]
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"\nProphet lab — {len(tickers)} stocks, horizons {HORIZONS}, "
          f"{'QUICK' if quick else 'FULL'} mode\n")
    per_stock = {}
    for ti, t in enumerate(tickers, 1):
        print(f"[{ti}/{len(tickers)}] {t} …", flush=True)
        sc = walk_forward(t, bench, quick)
        if sc:
            per_stock[t] = sc

    # aggregate across stocks (equal weight), per method × horizon
    agg = {m: {h: {} for h in HORIZONS} for m in METHOD_ORDER}
    for m in METHOD_ORDER:
        for h in HORIZONS:
            vals = [per_stock[t][m][h] for t in per_stock
                    if m in per_stock[t] and h in per_stock[t][m]]
            if not vals:
                continue
            rw = [per_stock[t]["RW"][h]["mape"] for t in per_stock
                  if "RW" in per_stock[t] and h in per_stock[t]["RW"]
                  and m in per_stock[t] and h in per_stock[t][m]]
            mape = float(np.mean([v["mape"] for v in vals]))
            agg[m][h] = {
                "stocks": len(vals),
                "dir_acc": float(np.nanmean([v["dir_acc"] for v in vals])),
                "mape": mape,
                "skill_vs_rw": float(1 - mape / np.mean(rw)) if rw else np.nan,
                "trade_ret": float(np.mean([v["trade_ret"] for v in vals])),
                "buyhold_ret": float(np.mean([v["buyhold_ret"] for v in vals])),
                "coverage": float(np.nanmean([v["coverage"] for v in vals])),
            }

    out = {"horizons": HORIZONS, "n_stocks": len(per_stock),
           "stocks": sorted(per_stock), "aggregate": agg, "per_stock": per_stock}
    path = OUT_DIR / ("scorecard_quick.json" if quick else "scorecard.json")
    path.write_text(json.dumps(out, indent=2))
    _print_table(agg)
    print(f"\nWrote {path}  ({len(per_stock)} stocks scored)")


def _print_table(agg):
    for h in HORIZONS:
        print(f"\n── horizon {h}d ── (dir>0.5 & skill>0 beat random walk) ──")
        print(f"{'method':<16}{'dirAcc':>8}{'MAPE%':>8}{'skillRW':>9}"
              f"{'tradeR%':>9}{'b&hR%':>8}{'cover':>7}")
        for m in METHOD_ORDER:
            a = agg[m].get(h)
            if not a:
                continue
            print(f"{m:<16}{a['dir_acc']*100:>7.1f}{a['mape']*100:>8.1f}"
                  f"{a['skill_vs_rw']*100:>8.1f}{a['trade_ret']*100:>8.2f}"
                  f"{a['buyhold_ret']*100:>7.2f}{a['coverage']:>7.2f}")


if __name__ == "__main__":
    main()
