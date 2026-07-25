"""Local dashboard.

A single self-contained page that polls one JSON endpoint. Deliberately plain:
during a 14-day unattended run the dashboard must not be a source of failure, so
it has no build step, no external assets, and no write access to anything.

It binds to ``127.0.0.1`` by default — local only.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from typing import Any

import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from ..config.schema import DashboardConfig
from ..utils.logging import get_logger

log = get_logger(__name__)


def create_app(state_provider: Callable[[], dict[str, Any]], refresh_seconds: int = 5) -> FastAPI:
    """Build the FastAPI app around a state callable."""
    app = FastAPI(title="BTC Adaptive Bot", docs_url=None, redoc_url=None)

    @app.get("/api/state")
    async def api_state() -> JSONResponse:
        try:
            return JSONResponse(state_provider())
        except Exception as exc:  # noqa: BLE001 - the dashboard must never crash the bot
            log.debug("DASHBOARD", f"State provider failed: {exc}")
            return JSONResponse({"error": str(exc)}, status_code=503)

    @app.get("/api/health")
    async def api_health() -> JSONResponse:
        return JSONResponse({"ok": True})

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        return HTMLResponse(_PAGE.replace("__REFRESH__", str(refresh_seconds * 1000)))

    return app


class DashboardServer:
    """Runs the dashboard alongside the engine."""

    def __init__(
        self, config: DashboardConfig, state_provider: Callable[[], dict[str, Any]]
    ) -> None:
        self.config = config
        self.app = create_app(state_provider, config.refresh_seconds)
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if not self.config.enabled:
            return
        settings = uvicorn.Config(
            self.app,
            host=self.config.host,
            port=self.config.port,
            log_level="warning",
            access_log=False,
        )
        self._server = uvicorn.Server(settings)
        self._task = asyncio.create_task(self._server.serve(), name="dashboard")
        log.info(
            "DASHBOARD",
            f"Dashboard running at http://{self.config.host}:{self.config.port}",
        )

    async def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(self._task, timeout=5)
            self._task = None


_PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>BTC Adaptive Bot</title>
<style>
:root{color-scheme:light dark}
*{box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;margin:0;
padding:1.25rem;background:#f6f7f9;color:#181b1f;line-height:1.5}
@media(prefers-color-scheme:dark){body{background:#131519;color:#e6e8eb}
.card{background:#1b1e23!important;border-color:#2a2e35!important}
th{background:#23272e!important}code{background:#23272e!important}}
h1{font-size:1.35rem;margin:0 0 .2rem;letter-spacing:-.02em}
.sub{opacity:.6;font-size:.82rem;margin-bottom:1.1rem}
.grid{display:grid;gap:1rem;grid-template-columns:repeat(auto-fit,minmax(310px,1fr))}
.card{background:#fff;border:1px solid #e2e5e9;border-radius:10px;padding:1rem 1.1rem}
.card h2{font-size:.74rem;text-transform:uppercase;letter-spacing:.07em;opacity:.6;
margin:0 0 .7rem;font-weight:700}
.row{display:flex;justify-content:space-between;gap:1rem;padding:.22rem 0;font-size:.88rem}
.row .k{opacity:.65}.row .v{font-weight:600;text-align:right}
.ok{color:#1a7f37}.bad{color:#cf222e}.warn{color:#bf8700}
.bar{height:7px;background:#e2e5e9;border-radius:99px;overflow:hidden;margin:.55rem 0}
.bar>i{display:block;height:100%;background:#2f81f7}
table{width:100%;border-collapse:collapse;font-size:.8rem}
th,td{padding:.32rem .45rem;text-align:right;border-bottom:1px solid #e8eaed;white-space:nowrap}
th:first-child,td:first-child{text-align:left}
th{background:#eef0f3;font-size:.68rem;text-transform:uppercase;letter-spacing:.04em}
.scroll{overflow-x:auto;max-height:340px;overflow-y:auto}
.tag{display:inline-block;padding:.05rem .45rem;border-radius:99px;font-size:.68rem;
font-weight:700;background:#6e7781;color:#fff}
.tag.on{background:#1a7f37}.tag.off{background:#cf222e}.tag.warn{background:#bf8700}
.news{font-size:.8rem;padding:.3rem 0;border-bottom:1px solid #e8eaed}
.news:last-child{border:0}
.err{background:#cf222e;color:#fff;padding:.6rem .9rem;border-radius:8px;margin-bottom:1rem}
</style></head><body>
<h1>BTC Adaptive Bot</h1>
<div class="sub" id="updated">connecting…</div>
<div id="err"></div>
<div class="grid" id="grid"></div>
<script>
const $=(id)=>document.getElementById(id);
const money=(v)=>'$'+Number(v||0).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});
const pct=(v)=>Number(v||0).toFixed(2)+'%';
const esc=(s)=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const tag=(ok,on,off)=>`<span class="tag ${ok?'on':'off'}">${ok?on:off}</span>`;
function row(k,v,cls){return `<div class="row"><span class="k">${esc(k)}</span><span class="v ${cls||''}">${v}</span></div>`}
function card(title,inner){return `<div class="card"><h2>${esc(title)}</h2>${inner}</div>`}

function render(d){
  const cards=[];
  const s=d.system||{};
  cards.push(card('System',
    row('Status', tag(s.running,'RUNNING','STOPPED'))+
    row('Demo verified', tag(s.demo_verified,'VERIFIED','NOT VERIFIED'))+
    row('Mode', esc(s.mode||'-')+(s.dry_run?' (dry run)':''))+
    row('Data health', tag(s.data_healthy,'HEALTHY','DEGRADED'))+
    (s.data_healthy?'':row('Detail', esc(s.data_detail||''),'warn'))+
    row('News', tag(!(s.news_health||{}).degraded,'OK','DEGRADED'))+
    row('Safe mode', tag(!((s.safety||{}).safe_mode||{}).active,'CLEAR','ACTIVE'))
  ));

  const e=d.experiment;
  if(e){
    cards.push(card('Experiment',
      row('Day', `${e.day} / ${e.duration_days}`)+
      `<div class="bar"><i style="width:${Math.min(100,e.progress_pct)}%"></i></div>`+
      row('Progress', pct(e.progress_pct))+
      row('Elapsed', esc(e.elapsed))+
      row('Remaining', esc(e.remaining))+
      row('Start', esc(e.start_ts_utc))+
      row('End', esc(e.scheduled_end_ts_utc))+
      row('Experiment', '<code>'+esc(e.experiment_id)+'</code>')
    ));
  }

  const a=d.demo_account||{};
  cards.push(card('Bybit Demo',
    row('Equity', money(a.equity))+
    row('Available', money(a.available))+
    row('Starting', money(a.starting_equity))+
    row('Realised PnL', money(a.realized_pnl), a.realized_pnl>=0?'ok':'bad')+
    row('Drawdown', pct(a.drawdown_pct), a.drawdown_pct>5?'warn':'')+
    row('Open positions', (a.positions||[]).length)+
    (a.positions||[]).map(p=>row(esc(p.strategy_id),
      `${esc(p.direction)} ${Number(p.quantity).toFixed(6)} @ ${money(p.entry_price)}`)).join('')
  ));

  const m=d.market||{}, r=d.regime||{};
  cards.push(card('Market',
    row('Price', money(m.last_price))+
    row('Spread', `${Number(m.spread_bps||0).toFixed(2)} bps`)+
    row('Book imbalance', Number(m.orderbook_imbalance||0).toFixed(3))+
    row('Flow imbalance', Number(m.trade_flow_imbalance||0).toFixed(3))+
    row('Regime', esc(r.current))+
    row('Regime confidence', Number(r.confidence||0).toFixed(2))+
    row('Regime stability', Number(r.stability||0).toFixed(2))
  ));

  const res=d.research||{}, sh=res.shadow||{};
  cards.push(card('Research',
    row('Strategies', res.strategies)+
    row('Shadow trades', sh.total_trades||0)+
    row('Open shadow positions', sh.open_positions||0)+
    row('Winners / losers', `${sh.winners||0} / ${sh.losers||0}`)+
    row('Demo orders sent', (res.demo_orders||{}).submitted||0)+
    row('Demo orders blocked', (res.demo_orders||{}).rejected||0)+
    row('Allocation fairness', Number(res.allocation_fairness||0).toFixed(2))
  ));

  const lb=res.leaderboard||[];
  cards.push(card('Top strategies',
    '<div class="scroll"><table><thead><tr><th>Strategy</th><th>Equity</th><th>Return</th>'+
    '<th>Trades</th><th>Win%</th><th>DD%</th></tr></thead><tbody>'+
    (lb.length?lb.map(x=>`<tr><td>${esc(x.strategy_id)}</td><td>${money(x.equity)}</td>`+
      `<td class="${x.return_pct>=0?'ok':'bad'}">${pct(x.return_pct)}</td><td>${x.trades}</td>`+
      `<td>${Number(x.win_rate).toFixed(0)}</td><td>${pct(x.drawdown_pct)}</td></tr>`).join('')
      :'<tr><td colspan="6">no shadow trades yet</td></tr>')+
    '</tbody></table></div>'
  ));

  const news=d.news||[];
  cards.push(card('News',
    news.length?news.map(n=>`<div class="news"><span class="tag ${n.impact==='high'?'off':(n.impact==='medium'?'warn':'')}">`+
      `${esc(n.impact)}</span> ${esc(String(n.headline).slice(0,110))}</div>`).join('')
    :'<div class="news">no news events recorded</div>'
  ));

  const t=d.trades||{}, orders=t.demo||[];
  cards.push(card('Recent demo orders',
    '<div class="scroll"><table><thead><tr><th>Strategy</th><th>Side</th><th>Qty</th>'+
    '<th>Status</th></tr></thead><tbody>'+
    (orders.length?orders.map(o=>`<tr><td>${esc(o.strategy_id)}</td><td>${esc(o.side)}</td>`+
      `<td>${esc(o.quantity_str)}</td><td>${esc(o.status)}</td></tr>`).join('')
      :'<tr><td colspan="4">no demo orders yet</td></tr>')+
    '</tbody></table></div>'
  ));

  const l=d.learning||{}, cands=l.candidates||[];
  cards.push(card('Learning',
    row('News influence', Number(l.news_influence||0).toFixed(2))+
    '<div class="scroll"><table><thead><tr><th>Strategy</th><th>Version</th><th>Status</th>'+
    '</tr></thead><tbody>'+
    (cands.length?cands.map(c=>`<tr><td>${esc(c.strategy_id)}</td>`+
      `<td>${esc(c.base_version)}→${esc(c.candidate_version)}</td><td>${esc(c.status)}</td></tr>`).join('')
      :'<tr><td colspan="3">no candidates yet</td></tr>')+
    '</tbody></table></div>'
  ));

  const ch=d.champion;
  cards.push(card('Champion / challengers',
    ch?row('Champion', esc(ch.new_champion))+row('Type', esc(ch.champion_type))+
       row('Confidence', esc(ch.confidence))+row('Selected', esc(ch.ts_utc))+
       `<div class="news">${esc(String(ch.reason).slice(0,320))}</div>`
      :'<div class="news">No champion yet — selected at the end of day 14.</div>'
  ));

  $('grid').innerHTML=cards.join('');
  $('updated').textContent='Updated '+(d.updated_at||'')+' · auto-refresh';
  $('err').innerHTML='';
}

async function tick(){
  try{
    const res=await fetch('/api/state',{cache:'no-store'});
    if(!res.ok) throw new Error('HTTP '+res.status);
    render(await res.json());
  }catch(err){
    $('err').innerHTML='<div class="err">Dashboard cannot reach the bot: '+esc(err.message)+
      '. The bot may still be running — this page only reads state.</div>';
  }
}
tick(); setInterval(tick, __REFRESH__);
</script></body></html>
"""
