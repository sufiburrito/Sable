#!/usr/bin/env python3
"""
journal/obsidian.py — generate the local Obsidian trade-journal vault (Phase C1).

Writes a markdown vault under journal/vault/ that you open directly in Obsidian:
  Trades/          one note per closed lot   (frontmatter from portfolio.db FIFO)
  Missed/          one note per missed Sable call (frontmatter from the forward rig)
  Reviews/         your daily reviews (manual)
  Trades DB.md / Missed Trades DB.md   live Dataview tables over those notes
  Analytics.md     dashboard — the DataviewJS calendar + KPI/radar land here in C2/C3
  Milestones.md    goals

Pure Python, no LLM, no network — the same deterministic family as the forward rig.
Idempotent and SAFE: trade/missed notes are **create-if-absent**, so your hand-written
reflections in a note body are never overwritten on a re-run.

Plugins to enable in Obsidian: Dataview (turn ON "Enable JavaScript Queries" for C2/C3),
and Charts for the radar.

Usage:  python3 -m journal.obsidian
"""
import hashlib
import re
from pathlib import Path

import forward_lib as fl
from journal import realized_pnl, missed_trades, pnl_statement, tax, execution_review

VAULT = Path(__file__).resolve().parent / "vault"


def _q(v) -> str:
    """YAML scalar: numbers bare, everything else double-quoted + escaped."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return str(v)
    s = str(v).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def _frontmatter(d: dict) -> str:
    return "---\n" + "".join(f"{k}: {_q(v)}\n" for k, v in d.items()) + "---\n"


def _write_if_absent(path: Path, content: str) -> bool:
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return True


_FM_RE = re.compile(r"^---\n.*?\n---\n", re.DOTALL)


def _refresh_note(path: Path, content: str) -> str:
    """Create the note if absent; else refresh ONLY its frontmatter (managed data) and keep
    the user's body (their 'Why I skipped' / 'Lesson' reflections) untouched. Returns
    'created' / 'updated' / 'kept'."""
    new_fm = _FM_RE.match(content)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return "created"
    if new_fm is None:
        return "kept"
    old = path.read_text(encoding="utf-8")
    old_fm = _FM_RE.match(old)
    if old_fm is None:
        return "kept"                       # malformed — don't risk the user's note
    refreshed = new_fm.group(0) + old[old_fm.end():]
    if refreshed != old:
        path.write_text(refreshed, encoding="utf-8")
        return "updated"
    return "kept"


def _tid(*parts) -> str:
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:6]


# ── note builders ───────────────────────────────────────────────────────────

def trade_note(lot: dict) -> tuple[Path, str]:
    tid = _tid(lot["symbol"], lot["buy_date"], lot["sell_date"], lot["quantity"], lot["sell_price"])
    fm = _frontmatter({
        "type": "trade", "symbol": lot["symbol"], "quantity": lot["quantity"],
        "buy_date": lot["buy_date"], "buy_price": lot["buy_price"],
        "sell_date": lot["sell_date"], "sell_price": lot["sell_price"],
        "realized_pnl": lot["realized_pnl"], "realized_pct": lot["realized_pct"],
        "holding_days": lot["holding_days"], "gain_type": lot["gain_type"],
        "basis": "gross", "id": tid,
    })
    sign = "+" if lot["realized_pnl"] >= 0 else ""
    body = (f"\n# {lot['symbol']} · sold {lot['sell_date']}  "
            f"({sign}₹{lot['realized_pnl']:,.0f}, {lot['realized_pct']:+.0f}%)\n\n"
            "> Auto-generated from FIFO realized P&L (gross). The reflection below is yours — "
            "re-runs never overwrite it.\n\n"
            "## Setup / why I held\n\n## What happened\n\n## Lesson\n")
    return VAULT / "Trades" / f"{lot['sell_date']}_{lot['symbol']}_{tid}.md", fm + body


def missed_note(m: dict) -> tuple[Path, str]:
    tid = _tid(m["ticker"], m["fired_on"], m["entry"])
    fm = _frontmatter({
        "type": "missed", "ticker": m["ticker"], "fired_on": m["fired_on"],
        "fired_at": m.get("fired_at"),
        "entry": m["entry"], "exit_price": m.get("exit_price"),
        "target": m.get("target"), "stop": m.get("stop"), "rr": m.get("rr"),
        "conviction": m.get("conviction"), "regime": m.get("regime"),
        "outcome": m["outcome"], "counterfactual_pct": m.get("counterfactual_pct"),
        "corroboration": m.get("corroboration"), "actual_peak_pct": m.get("actual_peak_pct"),
        "actual_trough_pct": m.get("actual_trough_pct"),
        "realized_R": m.get("realized_R"), "exit_reason": m.get("exit_reason"),
        "days_to_exit": m.get("days_to_exit"), "id": tid,
    })
    pct = m.get("counterfactual_pct")
    headline = (f"would have made **+{pct:.1f}%**" if m["outcome"] == "missed_winner"
                else f"avoided a {pct:.1f}% loss" if m["outcome"] == "dodged_loser"
                else "not yet resolved")
    xp, xr = m.get("exit_price"), m.get("exit_reason")
    exit_line = (f" Counterfactual: entry ₹{m['entry']:,} → exit ₹{xp:,} ({xr})." if xp is not None else "")
    body = (f"\n# {m['ticker']} — Sable advised ₹{m['entry']:,} on {m['fired_on']} (not taken)\n\n"
            f"> {headline}. Outcome: **{m['outcome']}**.{exit_line}\n\n## Why I skipped it\n\n## Lesson\n")
    return VAULT / "Missed" / f"{m['fired_on']}_{m['ticker']}_{tid}.md", fm + body


# ── dashboards ───────────────────────────────────────────────────────────────
# Managed notes (regenerated each run — they hold Sable-authored Dataview/JS, not
# your reflections). The per-trade/missed/review notes + Milestones are yours and
# stay create-if-absent.

_TRADES_DB = (
    "# Trades DB\n\nEvery closed lot (FIFO realized P&L, gross). Live table:\n\n"
    "```dataview\nTABLE WITHOUT ID symbol AS Ticker, quantity AS Qty, "
    'buy_price AS "Buy ₹", sell_price AS "Sell ₹", realized_pnl AS "P&L ₹", '
    'realized_pct AS "%", gain_type AS Type, holding_days AS Days, sell_date AS Sold\n'
    'FROM "Trades"\nWHERE type = "trade"\nSORT sell_date DESC\n```\n'
)
# Missed Trades dashboard — KPI cards + a top-missed-winners bar (live DataviewJS).
_JS_MISSED_KPI = r"""const m = dv.pages('"Missed"').where(p => p.type == "missed").array();
const num = v => Number(v)||0;
const W = m.filter(x=>x.outcome=="missed_winner"), D = m.filter(x=>x.outcome=="dodged_loser");
const gain = W.reduce((s,x)=>s+num(x.counterfactual_pct),0);
const saved = D.reduce((s,x)=>s+num(x.counterfactual_pct),0);
const net = gain+saved;
const card=(k,v,good)=>`<div style="flex:1;min-width:120px;border:1px solid #8884;border-radius:8px;padding:10px"><div style="color:#888;font-size:11px;text-transform:uppercase;letter-spacing:.4px">${k}</div><div style="font-size:20px;font-weight:800;color:${good?'#16a34a':'#dc2626'}">${v}</div></div>`;
let h='<div style="display:flex;gap:10px;flex-wrap:wrap">';
h+=card("Missed calls", m.length, true);
h+=card("Missed winners", `${W.length} · +${gain.toFixed(0)}%`, true);
h+=card("Dodged losers", `${D.length} · ${saved.toFixed(0)}%`, false);
h+=card("If followed all", `${net>=0?'+':''}${net.toFixed(0)}%`, net>=0);
h+='</div>';
const top = W.slice().sort((a,b)=>num(b.counterfactual_pct)-num(a.counterfactual_pct)).slice(0,6);
if(top.length){ const mx=Math.max(...top.map(x=>num(x.counterfactual_pct)),1);
  h+='<div style="margin-top:12px;font-size:11px;color:#888;text-transform:uppercase;letter-spacing:.4px">Top missed winners</div><div style="margin-top:6px">';
  for(const t of top){ const w=num(t.counterfactual_pct)/mx*100;
    h+=`<div style="display:flex;align-items:center;gap:8px;margin:3px 0"><div style="width:90px;font-size:12px">${t.ticker}</div><div style="flex:1;background:#8881;border-radius:4px"><div style="width:${Math.max(w,12)}%;background:#16a34a55;border-radius:4px;padding:2px 6px;color:#16a34a;font-weight:700;font-size:12px;white-space:nowrap">+${num(t.counterfactual_pct).toFixed(1)}%</div></div></div>`; }
  h+='</div>'; }
const c=dv.el("div","");c.innerHTML=h;"""

# Interactive sortable table — click a header to sort, a ticker to open its note.
_JS_MISSED_TABLE = r"""const m = dv.pages('"Missed"').where(p => p.type == "missed").array();
const num = v => Number(v)||0;
const MON=["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
const fmtDT = s => { const [d,t]=String(s).replace("T"," ").split(" "); const p=String(d).split("-"); if(p.length!=3 || isNaN(+p[2])) return String(s); let out=`${+p[2]} ${MON[+p[1]-1]} ${p[0]}`; if(t&&t.length>=5){ const hh=+t.slice(0,2); const mm=t.slice(3,5); if(!isNaN(hh)){ const ap=hh<12?"AM":"PM"; const h12=hh%12||12; out+=`, ${h12}:${mm} ${ap}`; } } return out; };
const rows = m.map(x=>({ticker:String(x.ticker??""),
  advised:(x.fired_at&&x.fired_at.toFormat)?x.fired_at.toFormat("yyyy-MM-dd HH:mm"):String(x.fired_at??x.fired_on??"").replace("T"," ").slice(0,16),
  entry:num(x.entry), target:(x.target!=null?num(x.target):null), exit:(x.exit_price!=null?num(x.exit_price):null),
  outcome:String(x.outcome??""), cf:num(x.counterfactual_pct),
  corr:String(x.corroboration??""), peak:(x.actual_peak_pct!=null?num(x.actual_peak_pct):null),
  conv:num(x.conviction), regime:String(x.regime??""), path:x.file.path}));
const cols=[["ticker","Ticker"],["advised","Advised"],["entry","Entry ₹"],["target","Target ₹"],["exit","Exit ₹"],["outcome","Outcome"],["corr","Corroboration"],["cf","Counterfactual %"],["peak","Actual peak %"],["conv","Conv"],["regime","Regime"]];
let sortKey="cf", asc=false;
const root=dv.el("div","");
function render(){
  rows.sort((a,b)=>{const av=a[sortKey],bv=b[sortKey];const r=(av>bv?1:av<bv?-1:0);return asc?r:-r;});
  let h='<table style="width:100%;border-collapse:collapse;font-size:12px"><thead><tr>';
  for(const [k,label] of cols){const ar=sortKey==k?(asc?" ▲":" ▼"):"";h+=`<th class="sh" data-k="${k}" style="cursor:pointer;text-align:left;padding:5px;border-bottom:2px solid #8884;color:#888;white-space:nowrap">${label}${ar}</th>`;}
  h+='</tr></thead><tbody>';
  for(const r of rows){const col=r.cf>=0?"#16a34a":"#dc2626";
    const cc={target_hit:"#16a34a",stopped:"#16a34a",soft:"#d97706",pending:"#888"}[r.corr]||"#888";
    h+=`<tr><td style="padding:4px;border-bottom:1px solid #8882"><a class="ml" data-p="${r.path}" style="cursor:pointer;color:var(--link-color)">${r.ticker}</a></td><td style="white-space:nowrap">${fmtDT(r.advised)}</td><td style="text-align:right">${r.entry}</td><td style="text-align:right">${r.target!=null?r.target:'—'}</td><td style="text-align:right">${r.exit!=null?'₹'+r.exit:'—'}</td><td>${r.outcome}</td><td style="color:${cc};font-weight:600">${r.corr}</td><td style="text-align:right;color:${col};font-weight:700">${r.cf.toFixed(1)}</td><td style="text-align:right">${r.peak!=null?(r.peak>=0?'+':'')+r.peak.toFixed(0)+'%':'—'}</td><td style="text-align:center">${r.conv||""}</td><td>${r.regime}</td></tr>`;}
  h+='</tbody></table>';
  root.innerHTML=h;
  root.querySelectorAll("th.sh").forEach(th=>th.onclick=()=>{const k=th.getAttribute("data-k");if(sortKey==k)asc=!asc;else{sortKey=k;asc=false;}render();});
  root.querySelectorAll("a.ml").forEach(a=>a.onclick=()=>app.workspace.openLinkText(a.getAttribute("data-p"),"",false));
}
render();"""

_MISSED_DB = (
    "# Missed Trades DB\n\nSable calls you didn't take, scored forward (swing). "
    "`missed_winner` = left on the table · `dodged_loser` = skipping was right.\n\n"
    "> **Corroboration** grounds each verdict in real price: `target_hit` / `stopped` = it genuinely "
    "happened; `soft` = time-cap close, the advised target **never printed** (theoretical); `pending` = "
    "not enough sessions yet. **Actual peak %** is the real high the stock reached from the alert level.\n\n"
    "## Summary\n\n```dataviewjs\n" + _JS_MISSED_KPI + "\n```\n\n"
    "## All missed calls\n\n_Click a column header to sort; click a ticker to open its note._\n\n"
    "```dataviewjs\n" + _JS_MISSED_TABLE + "\n```\n\n"
    "Calls you **did** take (advice vs your real fill) → [[Execution Review]].\n"
)
_MILESTONES = "# Milestones\n\n- [ ] First reviewed month\n- [ ] 20 reflected trades\n"

# Seed template for a daily review (the user duplicates it per trading day).
_REVIEW_TEMPLATE = (
    "---\ntype: review\ndate: 2000-01-01\n---\n\n"
    "# Daily Review — YYYY-MM-DD\n\n"
    "> **Duplicate this note**, rename it to the date (e.g. `2026-06-18`), set `date:` in "
    "the frontmatter, and fill it in. It then appears under *Recent reviews* on [[Analytics]].\n\n"
    "## Market read (regime · Nifty · FII/DII)\n\n"
    "## My trades today\n\n"
    "## What Sable flagged that I skipped — see [[Missed Trades DB]]\n\n"
    "## Mistakes / lessons\n\n"
    "## Plan for tomorrow\n"
)

# Live DataviewJS — timeframe-scoped KPI cards + Sable's read. A toggle (Week/Month/FY/All,
# default Month) re-filters the closed lots by sell_date and recomputes everything client-side,
# so the journal need not rebuild. Lifetime totals are useless in a journal — scope to the window.
_JS_KPI = r"""const t = dv.pages('"Trades"').where(p => p.type == "trade").array();
const day = p => { const sd=p.sell_date; return (sd&&sd.toFormat)?sd.toFormat("yyyy-MM-dd"):String(sd).slice(0,10); };
const num = v => Number(v)||0;
const inr = v => "₹"+Math.round(v).toLocaleString("en-IN");
const MON=["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
const now = new Date();
const iso = d => `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,"0")}-${String(d.getDate()).padStart(2,"0")}`;
const today = iso(now);
const monD = new Date(now); monD.setDate(now.getDate()-((now.getDay()+6)%7));   // Monday of this week
const weekStart = iso(monD);
const monthStart = `${now.getFullYear()}-${String(now.getMonth()+1).padStart(2,"0")}-01`;
const fyY = now.getMonth()>=3 ? now.getFullYear() : now.getFullYear()-1;          // Indian FY: Apr–Mar
const fyStart = `${fyY}-04-01`, fyEnd = `${fyY+1}-03-31`;
const TF = {
  Week:  {pred:g=>g>=weekStart&&g<=today, label:`Week of ${+weekStart.slice(8)} ${MON[+weekStart.slice(5,7)-1]}`},
  Month: {pred:g=>g>=monthStart&&g<=today, label:`${MON[now.getMonth()]} ${now.getFullYear()}`},
  FY:    {pred:g=>g>=fyStart&&g<=fyEnd, label:`FY${fyY}-${String(fyY+1).slice(2)}`},
  All:   {pred:()=>true, label:`All time`},
};
const order = ["Week","Month","FY","All"];
let active = "Month";
const root = dv.el("div","");
const chartBox = dv.el("div","");           // persists across renders (cleared + redrawn per toggle)
function compute(tf){
  const lots = t.filter(p=>TF[tf].pred(day(p)));
  const n=lots.length, wins=lots.filter(x=>num(x.realized_pnl)>0).length;
  const total=lots.reduce((s,x)=>s+num(x.realized_pnl),0);
  const gp=lots.filter(x=>num(x.realized_pnl)>0).reduce((s,x)=>s+num(x.realized_pnl),0);
  const gl=-lots.filter(x=>num(x.realized_pnl)<0).reduce((s,x)=>s+num(x.realized_pnl),0);
  const cost=lots.reduce((s,x)=>s+num(x.buy_price)*num(x.quantity),0);
  const pf = gl>0 ? gp/gl : (gp>0?Infinity:0);
  return {n,wins,total,gp,gl,cost,pf};
}
function read(m){
  if(m.n===0) return `<i>Sable:</i> no lots closed in this window — widen to Month or FY.`;
  const wr=Math.round(m.wins/m.n*100);
  const pfTxt = m.pf===Infinity ? "no losers at all — a clean window" :
    m.pf>=2 ? `winners are crushing losers (PF ${m.pf.toFixed(1)})` :
    m.pf>=1.2 ? `winners outpace losers with room to tighten (PF ${m.pf.toFixed(2)})` :
    m.pf>=1 ? `a thin edge — winners barely cover losers (PF ${m.pf.toFixed(2)})` :
    `losers are outweighing winners here (PF ${m.pf.toFixed(2)})`;
  const pnlTxt = m.total>=0 ? `booked ${inr(m.total)}` : `down ${inr(-m.total)}`;
  const small = (m.n<5) ? " Small sample — read it lightly." : "";
  return `<i>Sable:</i> ${m.n} lots, ${wr}% green — ${pfTxt}; ${pnlTxt} this window.${small}`;
}
function drawChart(tf){
  const byMonth = (tf==="FY"||tf==="All");          // per-month for long windows, per-day for short
  const map = new Map();
  for(const p of t.filter(p=>TF[tf].pred(day(p)))){ const g=day(p); const key=byMonth?g.slice(0,7):g;
    map.set(key,(map.get(key)||0)+num(p.realized_pnl)); }
  const keys=[...map.keys()].sort();
  const labels=keys.map(k=> byMonth?`${MON[+k.slice(5,7)-1]} ${k.slice(0,4)}`:`${+k.slice(8)} ${MON[+k.slice(5,7)-1]}`);
  const data=keys.map(k=>Math.round(map.get(k)));
  chartBox.innerHTML="";
  if(!data.length) return;
  if(!window.renderChart){ chartBox.innerHTML='<i style="color:#888">Enable the Charts plugin to see the P&L bars.</i>'; return; }
  const col=v=>v>=0?"#16a34a":"#dc2626";
  window.renderChart({type:"bar",data:{labels,datasets:[{label:"P&L ₹",data,
    backgroundColor:data.map(v=>col(v)+"99"),borderColor:data.map(col),borderWidth:1}]},
    options:{plugins:{legend:{display:false},title:{display:true,text:`P&L by ${byMonth?"month":"day"} — ${TF[tf].label}`}},
    scales:{y:{ticks:{callback:v=>"₹"+v}}}}}, chartBox);
}
function render(){
  const m=compute(active);
  const pf = m.pf===Infinity ? "∞" : m.pf.toFixed(2);
  const cards=[["Win Rate", m.n?`${Math.round(m.wins/m.n*100)}%`:"—", m.n?m.wins/m.n>=0.5:true],
    ["Total P&L", m.n?inr(m.total):"—", m.total>=0],
    ["Return on cost", m.cost?`${(m.total/m.cost*100).toFixed(1)}%`:"—", m.total>=0],
    ["Profit Factor", m.n?pf:"—", m.pf>=1]];
  let h='<div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px">';
  for(const tf of order){const on=tf===active;
    h+=`<button class="tf" data-tf="${tf}" style="cursor:pointer;border:1px solid #8884;border-radius:6px;padding:4px 12px;font-size:12px;font-weight:${on?700:400};background:${on?'#16a34a22':'transparent'};color:${on?'#16a34a':'#888'}">${tf}</button>`;}
  h+=`</div><div style="color:#888;font-size:12px;margin-bottom:6px">${TF[active].label} · ${m.n} lot${m.n===1?"":"s"} closed</div>`;
  h+='<div style="display:flex;gap:10px;flex-wrap:wrap">';
  for(const [k,v,good] of cards) h+=`<div style="flex:1;min-width:130px;border:1px solid #8884;border-radius:8px;padding:10px"><div style="color:#888;font-size:11px;text-transform:uppercase;letter-spacing:.4px">${k}</div><div style="font-size:22px;font-weight:800;color:${good?'#16a34a':'#dc2626'}">${v}</div></div>`;
  h+='</div>';
  h+=`<p style="margin:10px 2px 0;font-size:13px;color:var(--text-normal)">${read(m)}</p>`;
  root.innerHTML=h;
  root.querySelectorAll("button.tf").forEach(b=>b.onclick=()=>{active=b.getAttribute("data-tf");render();});
  drawChart(active);
}
render();"""

# Live DataviewJS — monthly P&L calendar (C2): Mon–Fri + weekly Summary, days
# coloured by realized P&L, with ‹ › month navigation.
_JS_CAL = r"""const trades = dv.pages('"Trades"').where(p => p.type == "trade");
const byDay = {}, byDayTrades = {}; let maxYM = null;
for (const t of trades) {
  const sd = t.sell_date;
  const d = (sd && sd.toFormat) ? sd.toFormat("yyyy-MM-dd") : String(sd).slice(0,10);
  (byDay[d] ??= {pnl:0,n:0}); byDay[d].pnl += Number(t.realized_pnl)||0; byDay[d].n += 1;
  (byDayTrades[d] ??= []).push(t);
  const ym = d.slice(0,7); if (!maxYM || ym > maxYM) maxYM = ym; }
let [Y,M] = (maxYM || new Date().toISOString().slice(0,7)).split("-").map(Number);
const root = dv.el("div","");
const detail = dv.el("div","");
const fp = v => (v>=0?"+":"")+Math.round(v).toLocaleString("en-IN");
function showDay(day){
  const ts = (byDayTrades[day]||[]).slice().sort((a,b)=>(Number(b.realized_pnl)||0)-(Number(a.realized_pnl)||0));
  let h = `<div style="margin:12px 0 4px"><b>${day}</b> — ${ts.length} trade(s). Click a ticker to open its note.</div><table style="width:100%;border-collapse:collapse;font-size:12px">`;
  for (const t of ts){ const col=(Number(t.realized_pnl)||0)>=0?"#16a34a":"#dc2626";
    h += `<tr><td style="padding:3px;border-bottom:1px solid #8883"><a class="dl" data-p="${t.file.path}" style="cursor:pointer;color:var(--link-color)">${t.symbol}</a></td><td style="text-align:center">${t.quantity}</td><td style="text-align:right;color:${col};font-weight:700">${fp(Number(t.realized_pnl)||0)}</td><td style="text-align:center;color:#888">${t.gain_type}</td></tr>`; }
  h += `</table>`; detail.innerHTML = h;
  detail.querySelectorAll("a.dl").forEach(a=>a.onclick=()=>app.workspace.openLinkText(a.getAttribute("data-p"),"",false));
}
function render(){
  const monthName = new Date(Y,M-1,1).toLocaleString("en-US",{month:"long"});
  let html = `<div style="display:flex;align-items:center;gap:12px;margin:4px 0 8px"><button class="cp">‹</button><b>${monthName}-${Y}</b><button class="cn">›</button></div>`;
  html += `<table style="width:100%;border-collapse:collapse;text-align:center;font-size:12px"><thead><tr>`;
  for (const hd of ["Mon","Tue","Wed","Thu","Fri","Summary"]) html += `<th style="padding:4px;color:#888">${hd}</th>`;
  html += `</tr></thead><tbody>`;
  let cur = new Date(Y,M-1,1); cur.setDate(cur.getDate()-((cur.getDay()+6)%7));
  let mPnl=0,mN=0;
  for (let w=0; w<6; w++){
    let wPnl=0,wN=0,cells="";
    for (let d=0; d<5; d++){
      const inM = cur.getMonth()===M-1;
      const key = `${cur.getFullYear()}-${String(cur.getMonth()+1).padStart(2,"0")}-${String(cur.getDate()).padStart(2,"0")}`;
      const rec = byDay[key]; let bg = inM ? "transparent" : "#8881";
      let cell = `<div style="color:#999;font-size:11px;text-align:left">${cur.getDate()}</div>`;
      let dd = "";
      if (rec && inM){ const col = rec.pnl>=0?"#16a34a":"#dc2626"; bg = rec.pnl>=0?"#16a34a22":"#dc262622";
        cell += `<div style="font-weight:700">${rec.n}</div><div style="color:${col};font-weight:700">${fp(rec.pnl)}</div>`;
        dd = key; wPnl+=rec.pnl; wN+=rec.n; mPnl+=rec.pnl; mN+=rec.n; }
      cells += `<td data-day="${dd}" title="${dd?'Click for trades':''}" style="border:1px solid #8884;height:60px;vertical-align:top;padding:3px;background:${bg};cursor:${dd?'pointer':'default'}">${cell}</td>`;
      cur.setDate(cur.getDate()+1);
    }
    cur.setDate(cur.getDate()+2);
    const sc = wPnl>=0?"#16a34a":"#dc2626";
    const summ = wN ? `<div style="font-size:11px;color:#888">${wN} trades</div><div style="color:${sc};font-weight:700">${fp(wPnl)}</div>` : "";
    cells += `<td style="border:1px solid #8884;background:#8881;vertical-align:top;padding:3px">${summ}</td>`;
    html += `<tr>${cells}</tr>`;
    if (cur.getMonth()!==M-1 && w>=3) break;
  }
  const tc = mPnl>=0?"#16a34a":"#dc2626";
  html += `</tbody></table><div style="margin-top:6px;text-align:right">Month P/L: <b style="color:${tc}">${fp(mPnl)}</b> · Trades: <b>${mN}</b></div>`;
  root.innerHTML = html;
  root.querySelector(".cp").onclick = ()=>{ M--; if(M<1){M=12;Y--;} detail.innerHTML=""; render(); };
  root.querySelector(".cn").onclick = ()=>{ M++; if(M>12){M=1;Y++;} detail.innerHTML=""; render(); };
  root.querySelectorAll('td[data-day]').forEach(td=>{ const day=td.getAttribute("data-day"); if(day) td.onclick=()=>showDay(day); });
}
render();"""


# Performance-Profile radar drawn as inline SVG (Dataview only — no Charts plugin).
# Python injects the five 0–100 values where __DATA__ sits.
_JS_RADAR = r"""const labels = ["Win Rate","Profit Factor","Recovery","Consistency","Plan Adherence"];
const data = __DATA__;
const cx=170, cy=160, R=110, N=labels.length;
const pt=(i,r)=>{const a=-Math.PI/2+i*2*Math.PI/N;return [cx+r*Math.cos(a), cy+r*Math.sin(a)];};
let svg=`<svg width="340" height="320" style="font:11px var(--font-interface)">`;
for(let ring=1;ring<=4;ring++){let p="";for(let i=0;i<N;i++){const[x,y]=pt(i,R*ring/4);p+=`${x},${y} `;}svg+=`<polygon points="${p}" fill="none" stroke="#8883"/>`;}
for(let i=0;i<N;i++){const[x,y]=pt(i,R);svg+=`<line x1="${cx}" y1="${cy}" x2="${x}" y2="${y}" stroke="#8883"/>`;const[lx,ly]=pt(i,R+20);svg+=`<text x="${lx}" y="${ly}" text-anchor="middle" fill="#888">${labels[i]}</text>`;}
let dp="";for(let i=0;i<N;i++){const v=Math.max(0,Math.min(100,data[i]));const[x,y]=pt(i,R*v/100);dp+=`${x},${y} `;svg+=`<text x="${pt(i,R*v/100)[0]}" y="${pt(i,R*v/100)[1]-4}" text-anchor="middle" fill="#16a34a" font-weight="700">${Math.round(data[i])}</text>`;}
svg+=`<polygon points="${dp}" fill="#16a34a33" stroke="#16a34a" stroke-width="2"/></svg>`;
const c=dv.el("div","");c.innerHTML=svg;"""


def _radar_metrics(closed: list[dict], ledger: list[dict]) -> tuple[float, ...]:
    """The 5 Performance-Profile axes (each returned 0–100 for the radar)."""
    from collections import defaultdict
    n = len(closed) or 1
    wins = sum(1 for c in closed if c["realized_pnl"] > 0)
    gp = sum(c["realized_pnl"] for c in closed if c["realized_pnl"] > 0)
    gl = -sum(c["realized_pnl"] for c in closed if c["realized_pnl"] < 0)
    pf = gp / gl if gl > 0 else (3.0 if gp > 0 else 0.0)
    s = sorted(closed, key=lambda c: c["sell_date"])
    eq = peak = maxdd = 0.0
    months: dict = defaultdict(float)
    for c in s:
        eq += c["realized_pnl"]; peak = max(peak, eq); maxdd = max(maxdd, peak - eq)
        months[c["sell_date"][:7]] += c["realized_pnl"]
    recovery = eq / maxdd if maxdd > 0 else (3.0 if eq > 0 else 0.0)
    consistency = (sum(1 for v in months.values() if v > 0) / len(months) * 100) if months else 0
    buys = missed_trades.load_user_buys()
    calls = [r for r in ledger if r.get("alert_type") == "BUY" and r.get("entry") and r.get("status") != "excluded"]
    taken = sum(1 for r in calls if missed_trades.was_taken(r, buys))
    adherence = taken / len(calls) * 100 if calls else 0
    return (wins / n * 100, min(pf / 3 * 100, 100), min(recovery / 3 * 100, 100),
            consistency, adherence)


_MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _equity_curve_block(closed: list[dict]) -> str:
    """A static Charts ```chart block: cumulative P&L by month (all-time). Python-rendered
    nightly — shows the drawdown→recovery arc the aggregate cards can't. Needs the Charts plugin."""
    import json
    from collections import defaultdict
    months: dict = defaultdict(float)
    for c in closed:
        months[str(c["sell_date"])[:7]] += c["realized_pnl"]
    keys = sorted(k for k in months if len(k) == 7)
    if not keys:
        return "_No closed lots yet — equity curve appears once you've booked a trade._\n"
    labels, cum, data = [], 0.0, []
    for k in keys:
        cum += months[k]
        labels.append(f"{_MON[int(k[5:7]) - 1]} {k[:4]}")
        data.append(round(cum))
    return ("```chart\ntype: line\n"
            f"labels: {json.dumps(labels)}\n"
            "series:\n"
            f"  - title: Cumulative P&L (₹)\n    data: {json.dumps(data)}\n"
            "tension: 0.2\nfill: false\nlabelColors: false\n```\n")


def build_analytics(closed: list[dict], ledger: list[dict]) -> str:
    wr, pf_n, rec_n, cons, adh = _radar_metrics(closed, ledger)
    radar = ("```dataviewjs\n"
             + _JS_RADAR.replace("__DATA__", f"[{wr:.0f},{pf_n:.0f},{rec_n:.0f},{cons:.0f},{adh:.0f}]")
             + "\n```\n")
    return (
        "# Trade Journal — Analytics\n\n"
        "> Setup: enable **Dataview** with *Enable JavaScript Queries* (Settings → Dataview). "
        "No other plugins needed. Managed by Sable — edits here are overwritten; reflect in "
        "the trade notes instead.\n\n"
        "## Scorecard\n\n"
        "> Pick a window — these recompute from the lots **closed in it** (by sell date). GROSS "
        "(pre-charge/tax); take-home is in [[Effective P&L]].\n\n"
        "```dataviewjs\n" + _JS_KPI + "\n```\n\n"
        "## P&L calendar\n\n```dataviewjs\n" + _JS_CAL + "\n```\n\n"
        "## Performance profile\n\n"
        "> An **all-time** character read (not windowed) — these axes need many months to mean "
        "anything.\n\n" + radar + "\n"
        "## Equity curve\n\n"
        "> Cumulative P&L by month, all-time (GROSS) — the drawdown and the recovery, in one line.\n\n"
        + _equity_curve_block(closed) + "\n"
        "## Recent reviews\n\n"
        "```dataview\nTABLE WITHOUT ID file.link AS Review, date AS Date\n"
        'FROM "Reviews"\nWHERE type = "review" AND file.name != "_Daily Review Template"\n'
        "SORT file.name DESC\nLIMIT 10\n```\n\n"
        "Browse the raw rows in [[Trades DB]] · [[Missed Trades DB]] · [[Execution Review]] · [[Effective P&L]] · [[Tax Planning]].\n"
    )


def build_tax_view(d: dict) -> str:
    """The Tax Planning note — Python-rendered tables (current prices aren't in any
    note), regenerated nightly. Planning aid, not tax advice."""
    k, r, t, m = d["key_dates"], d["realized"], d["tax"], d["model"]
    inr = lambda v: f"₹{round(v):,}"
    ex = m.get("ltcg_exemption", 125000)
    out = [
        f"# Tax Planning — {d['fy']}\n",
        "> **Planning aid, not tax advice.** Indian listed-equity CG tax: "
        f"STCG **{m['stcg_rate']*100:.0f}%** · LTCG **{m['ltcg_rate']*100:.1f}%** "
        f"(₹{ex//1000}k/yr exempt, aggregate). STCL offsets STCG+LTCG; LTCL offsets LTCG only. "
        "FY = 1 Apr–31 Mar. Rates in `charge_model.json`.\n",
        "## Key dates",
        f"- **{k['days_to_harvest']}d** to the {d['fy']} harvest deadline ({k['harvest_by']}, "
        "~T+1 before the 31 Mar FY-end)",
        f"- **{k['days_to_advance_tax']}d** to the next advance-tax installment ({k['next_advance_tax']})",
        f"- **{k['days_to_itr']}d** to ITR filing for {k['itr_fy']} ({k['itr_due']})\n",
        "## This FY so far\n",
        "| | STCG | LTCG |\n|---|--:|--:|",
        f"| Gains | {inr(r['stcg'])} | {inr(r['ltcg'])} |",
        f"| Losses | {inr(r['stcl'])} | {inr(r['ltcl'])} |",
        f"| Net (after set-off) | {inr(t['net_stcg'])} | {inr(t['net_ltcg'])} |",
        f"| Est. tax | {inr(t['tax_stcg'])} | {inr(t['tax_ltcg'])} |\n",
        f"**Estimated total tax: {inr(t['total_tax'])}** · LTCG exemption: {inr(t['exemption_used'])} "
        f"used, **{inr(t['exemption_left'])} tax-free headroom left** (book gains up to this, tax-free).\n",
    ]
    if d["harvest"]:
        out += ["## Loss-harvest candidates (book before 28 Mar to offset gains)\n",
                "| Ticker | Class | Qty | Buy ₹ | Now ₹ | Loss ₹ | Max tax offset ₹ |\n"
                "|---|---|--:|--:|--:|--:|--:|"]
        for h in d["harvest"][:15]:
            out.append(f"| {h['symbol']} | {h['loss_class']} | {h['quantity']} | {h['buy_price']} | "
                       f"{h['price']} | {round(h['harvestable_loss']):,} | {round(h['max_tax_offset']):,} |")
        out.append("")
    if d["ltcg_watch"]:
        out += ["## LTCG-threshold watch (wait to convert STCG → LTCG)\n",
                "| Ticker | Held (d) | Days to LTCG | LTCG date | Unrealized ₹ | Tax saved by waiting ₹ |\n"
                "|---|--:|--:|---|--:|--:|"]
        for w in d["ltcg_watch"]:
            out.append(f"| {w['symbol']} | {w['holding_days']} | {w['days_to_ltcg']} | {w['ltcg_date']} | "
                       f"{round(w['unrealized']):,} | {round(w['tax_saving']):,} |")
        out.append("")
    out.append(f"_Holdings priced from the OHLC-cache last close · {d['n_holdings']} lots · "
               "rebuilt nightly. See also [[Effective P&L]]._")
    return "\n".join(out)


# Execution Review — a sortable DataviewJS table. Records are computed in Python (the join of
# Sable's calls to the user's real fills) and injected as a JS array at __DATA__; the JS renders
# clickable-sortable headers like the other dashboards. The advised-entry cell carries a ✓ when
# the OHLC actually reached the level, else the closest price the stock traded.
_JS_EXEC_TABLE = r"""const rows = __DATA__;
const inr = v => v==null?"—":(v<0?"−₹":"₹")+Math.abs(Math.round(v)).toLocaleString("en-IN");
const pct = v => v==null?"—":(v>=0?"+":"")+Number(v).toFixed(1)+"%";
const lagf = d => d==null?"":(d>0?`+${d}d`:(d<0?`${-d}d early`:"same day"));
function entryCell(r){ let s=inr(r.entry);
  if(r.entry_hit===true) s+=' <span style="color:#16a34a">✓</span>';
  else if(r.entry_hit===false) s+=` (<b>${inr(r.entry_closest)}</b>)`;
  return s; }
function leftCell(r){ if(r.left==null) return "—";
  const c=r.quality=="early"?"#dc2626":(r.quality=="good"?"#16a34a":"#888");
  return `<span style="color:${c};font-weight:600">${pct(r.left)} (${r.quality})</span>`; }
const pnlCell = r => { if(r.pnl==null) return "—"; const c=r.pnl>=0?"#16a34a":"#dc2626";
  return `<span style="color:${c};font-weight:700">${inr(r.pnl)}</span>`; };
const cols=[["ticker","Ticker"],["tier","Match"],["entry","Advised entry"],["buy","You bought"],
  ["slip","Slippage"],["target","Target"],["sold","You sold"],["vstgt","vs target"],
  ["left","Left on table"],["pnl","P&L"],["status","Status"]];
const fmt={ticker:r=>r.ticker, tier:r=>r.tier, entry:entryCell,
  buy:r=>inr(r.buy)+(r.lag!=null?` <span style="color:#888;font-size:11px">${lagf(r.lag)}</span>`:""),
  slip:r=>pct(r.slip), target:r=>inr(r.target), sold:r=>inr(r.sold), vstgt:r=>pct(r.vstgt),
  left:leftCell, pnl:pnlCell, status:r=>r.status};
const align={ticker:"left",tier:"left",status:"left"};   // others right
let sortKey="fired", asc=false;
const root=dv.el("div","");
function render(){
  rows.sort((a,b)=>{let av=a[sortKey],bv=b[sortKey]; av=(av==null)?-Infinity:av; bv=(bv==null)?-Infinity:bv;
    const r=(av>bv?1:av<bv?-1:0); return asc?r:-r;});
  let h='<table style="width:100%;border-collapse:collapse;font-size:12px"><thead><tr>';
  for(const [k,label] of cols){const ar=sortKey==k?(asc?" ▲":" ▼"):"";const al=align[k]||"right";
    h+=`<th class="sh" data-k="${k}" style="cursor:pointer;text-align:${al};padding:5px;border-bottom:2px solid #8884;color:#888;white-space:nowrap">${label}${ar}</th>`;}
  h+='</tr></thead><tbody>';
  for(const r of rows){h+='<tr>';
    for(const [k] of cols){const al=align[k]||"right";
      h+=`<td style="padding:4px;border-bottom:1px solid #8882;text-align:${al};white-space:nowrap">${fmt[k](r)}</td>`;}
    h+='</tr>';}
  h+='</tbody></table>'; root.innerHTML=h;
  root.querySelectorAll("th.sh").forEach(th=>th.onclick=()=>{const k=th.getAttribute("data-k");
    if(sortKey==k)asc=!asc; else{sortKey=k;asc=false;} render();});
}
render();"""


def _exec_row(r: dict) -> dict:
    """A record → the flat row object the sortable JS table consumes (numeric fields sort,
    display formatting happens in JS)."""
    raw = r.get("status") or ""
    st = {"taken_closed": "closed", "taken_open": "holding"}.get(raw, raw)
    return {
        "ticker": r["ticker"], "tier": r["tier"], "fired": r.get("fired_on"),
        "entry": r["advised_entry"], "entry_hit": r.get("entry_hit"), "entry_closest": r.get("entry_closest"),
        "buy": r["user_buy_price"], "lag": r["lag_days"], "slip": r["entry_slippage_pct"],
        "target": r.get("advised_target"), "sold": r.get("user_sell_price"),
        "vstgt": r.get("exit_vs_target_pct"), "left": r.get("left_on_table_pct"),
        "quality": r.get("exit_quality"), "pnl": r.get("realized_pnl"), "status": st,
    }


def build_execution_view(recs: list[dict]) -> str:
    """Advice vs your actual fills, as a sortable DataviewJS table: each BUY Sable advised that
    you took, joined to your real entry/exit. The advised-entry cell shows ✓ if the OHLC actually
    reached the level, else the closest price the stock traded. Records computed in Python."""
    import json
    out = [
        "# Execution Review — Sable's advice vs your fills\n",
        "> For every BUY Sable advised that you **took**, how your real trade compared. The advised "
        "entry shows **✓** when the stock's price actually reached it (within 45d), or `(₹closest)` when "
        "it never did. *vs target* is Sable's *forecast* (proves nothing alone); **Left on table** is the "
        "**verified** exit — how much higher it actually traded after you sold (`pending` until ~63 sessions "
        "print). **Loose** rows are your nearest buy within 45d at any price — check the slippage before "
        "trusting the link. Click any header to sort. GROSS; calls you didn't take are in "
        "[[Missed Trades DB]].\n",
    ]
    if not recs:
        out.append("_No taken calls matched yet — appears once you've bought into a Sable BUY call._")
        return "\n".join(out)

    on_lvl = [r for r in recs if r["tier"] == "on_level"]
    closed_r = [r for r in recs if r.get("status") == "taken_closed"]
    cheaper = [r for r in recs if r["entry_slippage_pct"] < 0]
    reached = [r for r in recs if r.get("entry_hit") is True]
    unreach = [r for r in recs if r.get("entry_hit") is False]
    early = [r for r in closed_r if r.get("exit_quality") == "early"]
    good = [r for r in closed_r if r.get("exit_quality") == "good"]
    out.append(f"**{len(recs)} taken** ({len(on_lvl)} on-level · {len(recs)-len(on_lvl)} loose) · "
               f"{len(closed_r)} closed · entered cheaper than advised on {len(cheaper)}/{len(recs)} · "
               f"advised entry was reachable on {len(reached)}/{len(recs)}"
               + (f" (**{len(unreach)}** never printed)" if unreach else "") + ". "
               f"**Verified exit:** {len(early)} early · {len(good)} good.\n")
    rows = [_exec_row(r) for r in recs]
    out.append("```dataviewjs\n" + _JS_EXEC_TABLE.replace("__DATA__", json.dumps(rows, ensure_ascii=False))
               + "\n```\n")
    out.append("_The real lesson is in **Left on table**: a sale the stock then ran far above is timing left "
               "behind; one near the actual high was a good exit. A **✓** entry means the level was real; "
               "`(₹closest)` means Sable's entry was never actually offered — so your slippage there isn't "
               "your fault. A loose link with big slippage may be a different trade — tell Sable to drop it._")
    return "\n".join(out)


def build_effective(closed: list[dict], model: dict) -> str:
    """Effective (post-charges, post-tax) P&L split by financial year. Python-rendered
    nightly: FY-accurate CG tax (set-off + ₹1.25L exemption) plus a second line showing
    tax after net-loss carry-forward. Gross is never modified. Planning aid, not tax advice."""
    inr = lambda v: ("−₹" if v < 0 else "₹") + f"{abs(round(v)):,}"
    rate_pct = model["charge_rate"] * 100
    series = tax.fy_effective_series(closed, model)

    out = [
        "# Effective P&L — by financial year\n",
        "> Take-home after charges + CG tax, **per FY (1 Apr–31 Mar)** — the only unit Indian "
        "tax is assessed in. Layered from your **gross** lots; gross is never modified. "
        f"Charges ≈ **{rate_pct:.3f}%** of turnover (from `{model['source']}`). CG tax is "
        f"FY-accurate: STCG **{model['stcg_rate']*100:.0f}%** / LTCG **{model['ltcg_rate']*100:.1f}%** "
        "after **set-off** (STCL→STCG+LTCG, LTCL→LTCG) and the **₹1.25L LTCG exemption**. The "
        "*after carry-forward* line nets prior-year losses into later gains (8-yr carry-forward) — "
        "**assumes the loss was declared in that FY's ITR**. Rates in `journal/charge_model.json`. "
        "*Planning aid, not tax advice.*\n",
    ]

    # Year-over-year summary
    out += ["## Year-over-year\n",
            "| FY | Lots | Gross ₹ | Charges ₹ | CG tax ₹ | Net take-home ₹ | Eff. tax-rate | "
            "After carry-forward ₹ |\n|---|--:|--:|--:|--:|--:|--:|--:|"]
    for s in series:
        eff = "—" if s["eff_rate"] is None else f"{s['eff_rate']:.1f}%"
        cf_tax = s["tax_after_cf"]["total_tax"]
        cf_cell = f"{inr(s['net_takehome_cf'])}" + (f" (tax {inr(cf_tax)})" if cf_tax != s["tax_standalone"]["total_tax"] else "")
        out.append(f"| **{s['fy']}** | {s['n_lots']} | {inr(s['gross'])} | {inr(s['charges'])} | "
                   f"{inr(s['tax_standalone']['total_tax'])} | {inr(s['net_takehome'])} | {eff} | {cf_cell} |")
    out.append("")

    # Per-FY sections, newest first
    for i, s in enumerate(series):
        st, cf = s["tax_standalone"], s["tax_after_cf"]
        tag = "  ·  *current FY*" if i == 0 else ""
        out.append(f"## {s['fy']}{tag}\n")
        out.append(f"- **Gross realized:** {inr(s['gross'])}  ({s['n_lots']} lots)")
        out.append(f"- **− Charges:** {inr(s['charges'])}")
        if s["net_loss"]:
            out.append(f"- **CG tax:** ₹0 — net-loss year, nothing to tax")
            out.append(f"  - ⇨ **carries forward:** STCL {inr(s['cf_out']['stcl'])} + LTCL "
                       f"{inr(s['cf_out']['ltcl'])} (offsets future gains, ≤8 yrs — if declared in this FY's ITR)")
        else:
            out.append(f"- **− CG tax (standalone):** {inr(st['total_tax'])}  "
                       f"(net STCG {inr(st['net_stcg'])} @ {model['stcg_rate']*100:.0f}% · "
                       f"net LTCG {inr(st['net_ltcg'])}, exemption {inr(st['exemption_used'])} used / "
                       f"{inr(st['exemption_left'])} left)")
            if cf["cf_used"] > 0:
                out.append(f"  - **with carry-forward applied:** CG tax {inr(cf['total_tax'])} "
                           f"(absorbs {inr(cf['cf_used'])} of prior-year losses) → saves "
                           f"{inr(st['total_tax'] - cf['total_tax'])}")
        out.append(f"- **= Net take-home:** **{inr(s['net_takehome'])}**"
                   + (f"  ·  after carry-forward **{inr(s['net_takehome_cf'])}**" if cf["cf_used"] > 0 else "")
                   + (f"  ·  effective tax-rate {s['eff_rate']:.1f}%" if s["eff_rate"] is not None else ""))
        if s["cf_in"]["stcl"] or s["cf_in"]["ltcl"]:
            out.append(f"- _entering with carried-forward losses: STCL {inr(s['cf_in']['stcl'])} + "
                       f"LTCL {inr(s['cf_in']['ltcl'])}_")
        # live, sortable lot table for this FY
        out.append("\n```dataview\nTABLE WITHOUT ID symbol AS Ticker, sell_date AS Sold, "
                   "realized_pnl AS \"Gross ₹\", gain_type AS Type\nFROM \"Trades\"\n"
                   f"WHERE type = \"trade\" AND sell_date >= date({s['fy_start']}) "
                   f"AND sell_date <= date({s['fy_end']})\nSORT sell_date DESC\n```\n")

    out.append("Gross figures live in [[Trades DB]] and the [[Analytics]] calendar · "
               "tax planning in [[Tax Planning]].")
    return "\n".join(out)


def main():
    VAULT.mkdir(parents=True, exist_ok=True)
    (VAULT / "Reviews").mkdir(exist_ok=True)

    closed = realized_pnl.compute_closed_lots(realized_pnl.load_transactions())[0]
    missed = missed_trades.build_missed(fl.load_ledger(), missed_trades.load_user_buys())

    # Managed dashboards — always rewritten with the latest queries/metrics.
    (VAULT / "Trades DB.md").write_text(_TRADES_DB, encoding="utf-8")
    (VAULT / "Missed Trades DB.md").write_text(_MISSED_DB, encoding="utf-8")
    (VAULT / "Analytics.md").write_text(build_analytics(closed, fl.load_ledger()), encoding="utf-8")
    (VAULT / "Effective P&L.md").write_text(build_effective(closed, pnl_statement.load_model()), encoding="utf-8")
    (VAULT / "Tax Planning.md").write_text(build_tax_view(tax.build_tax_data()), encoding="utf-8")
    _exec_recs = execution_review.build_execution_review(
        fl.load_ledger(), missed_trades.load_user_buys(), closed)
    (VAULT / "Execution Review.md").write_text(build_execution_view(_exec_recs), encoding="utf-8")
    seeded = _write_if_absent(VAULT / "Milestones.md", _MILESTONES)
    _write_if_absent(VAULT / "Reviews" / "_Daily Review Template.md", _REVIEW_TEMPLATE)

    nt = sum(_write_if_absent(*trade_note(l)) for l in closed)
    # Missed notes: frontmatter is managed (corroboration / actual-peak feed the Dataview), the
    # body is the user's — refresh the former, keep the latter.
    expected, ms = set(), []
    for m in missed:
        path, content = missed_note(m)
        expected.add(path.name)
        ms.append(_refresh_note(path, content))
    nm, um = ms.count("created"), ms.count("updated")
    # Reconcile: a call that's no longer "missed" (now taken / reclassified) keeps its note +
    # reflection, but its type is flipped so it drops out of the Missed Trades DB Dataview.
    archived = 0
    for p in (VAULT / "Missed").glob("*.md"):
        if p.name not in expected:
            txt = p.read_text(encoding="utf-8")
            if 'type: "missed"' in txt:
                p.write_text(txt.replace('type: "missed"', 'type: "missed_taken"', 1), encoding="utf-8")
                archived += 1

    print(f"Obsidian vault → {VAULT}")
    print(f"  dashboards: regenerated (Analytics + Trades/Missed DB){' + seeded Milestones' if seeded else ''}")
    print(f"  trade notes:  +{nt} new  (of {len(closed)} closed lots)")
    print(f"  missed notes: +{nm} new, {um} refreshed, {archived} reclassified-as-taken  "
          f"(of {len(missed)} missed calls)")
    print("Open journal/vault/ in Obsidian; enable Dataview (JS queries). No other plugins needed.")


if __name__ == "__main__":
    main()
