"""
NSE Stock Monitor — terminal UI for the alert bot.

Run alongside run.py:
  Terminal 1:  python3 run.py
  Terminal 2:  python3 tui.py

Keys:
  ↑ / ↓     — navigate stocks
  1          — 1 Day chart (intraday, 5-min candles)
  2          — 1 Week chart
  3          — 1 Month chart  (default)
  4          — 3 Months chart
  r          — refresh chart (clears cache)
  q          — quit

Layout:
  Header
  └─ MMIBar                                      (second line, current MMI zone)
  ├─ Horizontal:
  │    ├─ ListView                               (stocks)
  │    └─ Vertical:
  │         ├─ ChartWidget                       (candlesticks + level overlays)
  │         └─ AlertLogWidget                    (fired-alert history)
  Footer

Chart rendering:
  Uses plotext (terminal-native plotter, no GUI). Alert levels are drawn as
  horizontal bands with 256-color confidence shading — darker = higher
  confidence at that level. Fired alerts appear as in-chart markers at the
  level price + bar timestamp.

  Intraday (1D / key "1"): 5-min candles, date_form "Y-m-d H:M". Intraday data
  is **never cached** — every render fetches fresh from yfinance — to avoid
  stale intraday bars. All other timeframes use the shared OHLC CSV cache via
  alert_bot.ohlc_cache.

MMIBar threading:
  Uses Textual's @work(thread=True) + call_from_thread for refresh.
  IMPORTANT: do NOT switch this to async @work — that pattern silently fails
  with the TickerTape scraper (the scrape is sync requests, mixing it with
  async-worker scheduling produces a hung UI). The threaded form is load-bearing.
"""
import json
import math
import threading
from datetime import datetime
from pathlib import Path

import plotext as plt
import pytz
import yfinance as yf
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.widgets import Footer, Header, Label, ListItem, ListView, RichLog, Static

from alert_bot.config import STOCKS_DIR, EXCLUDED_MD_FILES, MARKET_TIMEZONE, ALERTS_LOG
from alert_bot.parser import load_all_stocks, StockConfig
from alert_bot.mmi import fetch_mmi, MMISnapshot

_BELIEF_CACHE: dict[str, str] = {}


def _parse_belief_level(ticker: str) -> str:
    """Read stocks/TICKER.md and extract the Belief Level (e.g. 'CONVICTION').
    Returns an empty string if the section is absent or the file doesn't exist."""
    stock_file = STOCKS_DIR / f"{ticker}.md"
    if not stock_file.exists():
        return ""
    try:
        lines = stock_file.read_text(encoding="utf-8").splitlines()
        in_section = False
        for line in lines:
            if line.strip().startswith("## Belief Level"):
                in_section = True
                continue
            if in_section:
                stripped = line.strip()
                if not stripped:
                    continue
                # Extract **LEVEL** pattern
                import re
                m = re.search(r"\*\*(.+?)\*\*", stripped)
                if m:
                    return m.group(1)
                return stripped.lstrip("#").strip()
    except Exception:
        pass
    return ""

IST = pytz.timezone(MARKET_TIMEZONE)

# plotext uses global state — protect it from concurrent access
_plt_lock = threading.Lock()

RANGES = {
    "1": ("1d",  "1 Day"),
    "2": ("1wk", "1 Week"),
    "3": ("1mo", "1 Month"),
    "4": ("3mo", "3 Months"),
}

# Maps (alert_type, confidence) → plotext 256-color number
# BUY: darkest green (low conviction) → vivid green (max conviction)
# SELL: darkest red (first trim) → vivid red (biggest trim)
LEVEL_COLORS: dict[tuple[str, int], int] = {
    ("BUY",  1): 22,   # 🟡 dark green
    ("BUY",  2): 34,   # 🟢 medium-dark green
    ("BUY",  3): 40,   # 🔵 medium-bright green
    ("BUY",  4): 46,   # 🟠 bright green
    ("BUY",  5): 82,   # 🔴 vivid green
    ("SELL", 1): 88,   # ⬆️  dark red
    ("SELL", 2): 124,  # ⬆️⬆️ medium red
    ("SELL", 3): 160,  # 🚀 bright red
    ("SELL", 4): 196,  # 🚀🚀 vivid red
    ("WATCH",1): 226,  # 👁️ yellow
}
_TYPE_FALLBACK = {"BUY": 40, "SELL": 160, "WATCH": 226}

_ZONE_STYLE = {
    "Extreme Fear": "bold green",
    "Fear":         "bold yellow",
    "Greed":        "bold dark_orange",
    "Extreme Greed":"bold red",
}


def _level_color(alert_type: str, confidence: int) -> int:
    return LEVEL_COLORS.get((alert_type, confidence), _TYPE_FALLBACK.get(alert_type, 255))


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def fetch_history(yf_symbol: str, period: str) -> tuple[list[str], dict[str, list[float]], bool]:
    """
    Returns (x_labels, ohlc_dict, is_intraday).
    ohlc_dict has keys "Open", "High", "Low", "Close" — each a list of floats.
    For period="1d": 5-minute candles with HH:MM labels (IST).
    For all others:  daily candles with YYYY-MM-DD labels.
    """
    try:
        if period == "1d":
            hist = yf.Ticker(yf_symbol).history(period="1d", interval="5m")
            if hist.empty:
                return [], {}, True
            ist_index = hist.index.tz_convert(IST)
            dates = [dt.strftime("%H:%M") for dt in ist_index]
            ohlc  = {
                "Open":  [float(p) for p in hist["Open"]],
                "High":  [float(p) for p in hist["High"]],
                "Low":   [float(p) for p in hist["Low"]],
                "Close": [float(p) for p in hist["Close"]],
            }
            return dates, ohlc, True
        else:
            hist = yf.Ticker(yf_symbol).history(period=period, interval="1d")
            if hist.empty:
                return [], {}, False
            dates = [d.strftime("%Y-%m-%d") for d in hist.index]
            ohlc  = {
                "Open":  [float(p) for p in hist["Open"]],
                "High":  [float(p) for p in hist["High"]],
                "Low":   [float(p) for p in hist["Low"]],
                "Close": [float(p) for p in hist["Close"]],
            }
            return dates, ohlc, False
    except Exception:
        return [], {}, period == "1d"


def _subsample(
    dates: list[str], ohlc: dict[str, list[float]], max_candles: int
) -> tuple[list[str], dict[str, list[float]]]:
    """Thin out dates/ohlc to at most max_candles entries by uniform stride."""
    n = len(dates)
    if n <= max_candles:
        return dates, ohlc
    step = math.ceil(n / max_candles)
    idx  = range(0, n, step)
    return (
        [dates[i] for i in idx],
        {k: [v[i] for i in idx] for k, v in ohlc.items()},
    )


def load_alert_history(ticker: str) -> list[dict]:
    """Load the last 100 fired alerts for this ticker from alerts.jsonl."""
    if not ALERTS_LOG.exists():
        return []
    entries = []
    try:
        for line in ALERTS_LOG.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            entry = json.loads(line)
            if entry.get("ticker") == ticker:
                entries.append(entry)
    except Exception:
        pass
    return entries[-100:]


# ---------------------------------------------------------------------------
# Widgets
# ---------------------------------------------------------------------------

class ChartWidget(Static):
    """Renders a plotext price chart with alert-level overlays."""

    DEFAULT_CSS = """
    ChartWidget {
        height: 2fr;
        border: solid $primary;
    }
    """

    def show_loading(self, ticker: str) -> None:
        self.update(f"  Loading {ticker}…")

    def render_chart(
        self,
        stock: StockConfig,
        dates: list[str],
        ohlc: dict[str, list[float]],
        period_label: str,
        is_intraday: bool,
    ) -> None:
        if not dates or not ohlc.get("Close"):
            self.update(f"  No data available for {stock.ticker}.")
            return

        w = self.size.width - 2
        h = self.size.height - 2
        if w < 20 or h < 5:
            return

        # Subsample so candles don't overlap: allow ~3 terminal cols per candle
        max_candles = max(15, (w - 10) // 3)
        dates, ohlc = _subsample(dates, ohlc, max_candles)

        closes = ohlc["Close"]
        min_p   = min(ohlc["Low"])
        max_p   = max(ohlc["High"])
        margin  = (max_p - min_p) * 0.25
        visible_min = min_p - margin
        visible_max = max_p + margin
        today = datetime.now(IST).strftime("%Y-%m-%d")

        # For intraday, expand HH:MM → "YYYY-MM-DD HH:MM" so plotext can parse them
        if is_intraday:
            plot_dates = [f"{today} {t}" for t in dates]
            time_to_plot = {t: f"{today} {t}" for t in dates}
            date_form = "Y-m-d H:M"
        else:
            plot_dates = dates
            time_to_plot = {d: d for d in dates}
            date_form = "Y-m-d"

        plot_dates_set = set(plot_dates)

        with _plt_lock:
            plt.clear_figure()
            plt.theme("dark")
            plt.plotsize(w, h)
            plt.date_form(date_form)

            # Candlestick chart — red for bearish, green for bullish
            plt.candlestick(plot_dates, ohlc, colors=[196, 46])

            # Alert levels — confidence-shaded dotted line + right-edge price label
            for level in stock.levels:
                mid = (level.lower + level.upper) / 2
                if not (visible_min <= mid <= visible_max):
                    continue
                color = _level_color(level.alert_type, level.confidence)
                plt.scatter(plot_dates, [mid] * len(plot_dates), color=color, marker="dot")
                plt.text(f"₹{mid:,.0f}", x=plot_dates[-1], y=mid, color=color, alignment="right")

            # Forecast band — light blue shaded region for 30-day CI
            if not is_intraday:
                try:
                    from alert_bot.forecaster import trend_forecast
                    from alert_bot.floor_context import _load_ohlc
                    fc_df = _load_ohlc(stock.ticker)
                    if fc_df is not None and len(fc_df) >= 30:
                        fc = trend_forecast(fc_df["Close"], horizon=10)
                        if fc and fc.confidence > 0.3:
                            # Draw upper and lower CI bounds as dotted lines
                            fc_lower = fc.lower[-1]
                            fc_upper = fc.upper[-1]
                            if visible_min <= fc_lower <= visible_max:
                                plt.scatter(plot_dates, [fc_lower] * len(plot_dates),
                                          color=39, marker="dot")  # 39 = light blue
                            if visible_min <= fc_upper <= visible_max:
                                plt.scatter(plot_dates, [fc_upper] * len(plot_dates),
                                          color=39, marker="dot")
                            # Label at right edge
                            mid_fc = (fc_lower + fc_upper) / 2
                            if visible_min <= mid_fc <= visible_max:
                                plt.text(f"FC ₹{fc_lower:,.0f}-{fc_upper:,.0f}",
                                        x=plot_dates[-1], y=mid_fc,
                                        color=39, alignment="right")
                except Exception:
                    pass  # forecast overlay is best-effort, never crashes the TUI

            # Fired alert markers — white * at the actual trigger price
            for entry in load_alert_history(stock.ticker):
                ap = entry.get("price")
                if ap is None:
                    continue
                if is_intraday:
                    if entry.get("date") != today:
                        continue
                    x_val = time_to_plot.get(entry.get("time", "")[:5])
                else:
                    x_val = time_to_plot.get(entry.get("date", entry.get("ts", "")[:10]))
                if not x_val or x_val not in plot_dates_set:
                    continue
                if not (visible_min <= ap <= visible_max):
                    continue
                plt.scatter([x_val], [ap], color="white", marker="*")
                plt.text(f"₹{ap:,.0f}", x=x_val, y=ap, color="white", alignment="right")

            current = closes[-1]
            belief = _parse_belief_level(stock.ticker)
            belief_str = f" [{belief}]" if belief else ""
            plt.title(f"{stock.ticker}{belief_str}  ₹{current:,.2f}  |  {period_label}")
            plt.ylabel("Price (₹)")
            plt.ylim(visible_min, visible_max)

            ansi = plt.build()

        self.update(Text.from_ansi(ansi))


class AlertLogWidget(RichLog):
    """Displays the fired-alert history for the selected stock."""

    DEFAULT_CSS = """
    AlertLogWidget {
        height: 1fr;
        border: solid $warning;
        padding: 0 1;
    }
    """

    def populate(self, ticker: str) -> None:
        self.clear()
        self.write(f"[bold]Alert history — {ticker}[/bold]\n")

        entries = load_alert_history(ticker)
        if not entries:
            self.write("[dim]No alerts have fired for this stock yet.[/dim]")
            return

        color_map = {"BUY": "green", "SELL": "red", "WATCH": "yellow"}
        for entry in reversed(entries):
            date       = entry.get("date", entry.get("ts", "")[:10])
            time       = entry.get("time", entry.get("ts", "")[11:19])
            atype      = entry.get("alert_type", "")
            price      = entry.get("price")
            signal     = entry.get("signal", "")
            confidence = entry.get("confidence", "")
            message    = entry.get("message", "")
            color      = color_map.get(atype, "white")
            price_str  = f"₹{price:,.2f}" if price else ""
            conf_str   = f"conf:{confidence}" if confidence else ""
            self.write(
                f"[dim]{date} {time}[/dim]  {signal}  [{color}]{atype:4}[/{color}]"
                f"  [bold]{price_str}[/bold]  [dim]{conf_str}[/dim]  {message}"
            )


class MMIBar(Static):
    """Thin bar showing the current Market Mood Index. Updated by the App."""

    DEFAULT_CSS = """
    MMIBar {
        height: 1;
        padding: 0 2;
        background: $surface;
        text-align: center;
    }
    """

    def on_mount(self) -> None:
        self.update("[dim]MMI: loading…[/dim]")

    def set_snapshot(self, snap: MMISnapshot | None) -> None:
        if snap is None:
            self.update("[dim]MMI: unavailable[/dim]")
            return
        style     = _ZONE_STYLE.get(snap.zone, "bold white")
        day_diff  = snap.value - snap.last_day
        week_diff = snap.value - snap.last_week
        day_arrow  = ("↑" if day_diff  > 0 else "↓") + f" {abs(day_diff):.1f} vs yesterday"
        week_arrow = ("↑" if week_diff > 0 else "↓") + f" {abs(week_diff):.1f} vs last week"
        self.update(
            f"[{style}]MMI: {snap.value:.1f}  {snap.zone}[/{style}]"
            f"   [dim]{day_arrow}   {week_arrow}[/dim]"
        )


class StockItem(ListItem):
    """A ListView item that carries a reference to its StockConfig."""

    def __init__(self, stock: StockConfig) -> None:
        super().__init__(Label(stock.ticker))
        self.stock = stock


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

class StockMonitorApp(App):
    TITLE = "NSE Stock Monitor"
    CSS = """
    ListView {
        width: 14;
        border: solid $accent;
        padding: 0;
    }
    ListItem {
        padding: 0 1;
    }
    """
    BINDINGS = [
        Binding("q",    "quit",           "Quit"),
        Binding("1",    "set_range('1')", "1D"),
        Binding("2",    "set_range('2')", "1W"),
        Binding("3",    "set_range('3')", "1M"),
        Binding("4",    "set_range('4')", "3M"),
        Binding("r",    "refresh",        "Refresh"),
        Binding("up",   "cursor_up",      "Up",   show=False),
        Binding("down", "cursor_down",    "Down", show=False),
    ]

    time_range_key: reactive[str] = reactive("3")  # default: 1 month

    def __init__(self) -> None:
        super().__init__()
        self.stocks = load_all_stocks(STOCKS_DIR, EXCLUDED_MD_FILES)
        self._cache: dict[str, tuple[list, dict, bool]] = {}
        self._current_stock: StockConfig | None = self.stocks[0] if self.stocks else None

    def compose(self) -> ComposeResult:
        yield Header()
        yield MMIBar()
        with Horizontal():
            with ListView(id="stock-list"):
                for stock in self.stocks:
                    yield StockItem(stock)
            with Vertical():
                yield ChartWidget(id="chart")
                yield AlertLogWidget(id="alert-log", highlight=True, markup=True)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#stock-list", ListView).focus()
        if self._current_stock:
            self.call_after_refresh(self._load_stock, self._current_stock)
        self.set_interval(300, self._refresh_mmi)
        self._refresh_mmi()

    @work(thread=True)
    def _refresh_mmi(self) -> None:
        snap = fetch_mmi()
        self.call_from_thread(self.query_one(MMIBar).set_snapshot, snap)

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if isinstance(event.item, StockItem):
            self._current_stock = event.item.stock
            self._load_stock(event.item.stock)

    def on_chart_widget_resize(self) -> None:
        if self._current_stock:
            self._load_stock(self._current_stock)

    def action_set_range(self, key: str) -> None:
        self.time_range_key = key
        if self._current_stock:
            self._load_stock(self._current_stock)

    def action_refresh(self) -> None:
        if self._current_stock:
            period = RANGES[self.time_range_key][0]
            # Always clear intraday cache (data changes constantly)
            self._cache.pop(f"{self._current_stock.ticker}:{period}", None)
            self._load_stock(self._current_stock)

    def action_cursor_up(self) -> None:
        self.query_one("#stock-list", ListView).action_cursor_up()

    def action_cursor_down(self) -> None:
        self.query_one("#stock-list", ListView).action_cursor_down()

    def _load_stock(self, stock: StockConfig) -> None:
        self.query_one("#chart", ChartWidget).show_loading(stock.ticker)
        self._fetch_and_render(stock, self.time_range_key)

    @work(thread=True, exclusive=True)
    def _fetch_and_render(self, stock: StockConfig, range_key: str) -> None:
        period, period_label = RANGES[range_key]
        cache_key = f"{stock.ticker}:{period}"

        # Never cache intraday — data changes every 5 minutes
        if period == "1d" or cache_key not in self._cache:
            dates, ohlc, is_intraday = fetch_history(stock.yf_symbol, period)
            if period != "1d":
                self._cache[cache_key] = (dates, ohlc, is_intraday)
        else:
            dates, ohlc, is_intraday = self._cache[cache_key]

        def _render() -> None:
            self.query_one("#chart", ChartWidget).render_chart(
                stock, dates, ohlc, period_label, is_intraday
            )
            self.query_one("#alert-log", AlertLogWidget).populate(stock.ticker)

        self.call_from_thread(_render)


if __name__ == "__main__":
    StockMonitorApp().run()
