# warera_bot_v3.py
"""
WarEra Everything Bot v3
- Prefix commands
- Embeds + button-based pagination
- Auto-refresh dashboards by editing
- More endpoints from docs (economy, trading, battles, rankings, users, articles, MUs, etc.)
- All API calls via GET ?input={}
"""

import os
import json
import asyncio
import aiohttp
import urllib.parse
from typing import Optional, Dict, Any, List
from datetime import datetime
import discord
from discord.ext import commands, tasks
from discord.ui import View, Button

# ---------- Config ----------
API_BASE = os.getenv("WARERA_API_BASE", "https://api2.warera.io/trpc")
REQUEST_TIMEOUT = float(os.getenv("WARERA_REQUEST_TIMEOUT", "10"))
RETRY_ATTEMPTS = int(os.getenv("WARERA_RETRY_ATTEMPTS", "3"))
RETRY_BACKOFF = float(os.getenv("WARERA_RETRY_BACKOFF", "0.6"))
DEFAULT_DASH_INTERVAL = int(os.getenv("WARERA_DASH_INTERVAL", "60"))  # seconds

# Max number of items per page in pagination
PAGE_SIZE = 8

# ---------- Bot setup ----------
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

_session: Optional[aiohttp.ClientSession] = None

async def get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
        _session = aiohttp.ClientSession(timeout=timeout)
    return _session

def build_trpc_url(endpoint: str, params: Optional[Dict] = None) -> str:
    base = API_BASE.rstrip("/")
    endpoint = endpoint.strip().lstrip("/")
    url = f"{base}/{endpoint}"
    if params is None:
        input_json = "{}"
    else:
        input_json = json.dumps(params, separators=(",", ":"))
    encoded = urllib.parse.quote(input_json, safe='')
    return f"{url}?input={encoded}"

async def api_call(endpoint: str, params: Optional[Dict] = None) -> Optional[Any]:
    url = build_trpc_url(endpoint, params)
    sess = await get_session()
    last_exc = None
    for attempt in range(RETRY_ATTEMPTS):
        try:
            async with sess.get(url) as resp:
                text = await resp.text()
                if resp.status != 200:
                    last_exc = Exception(f"HTTP {resp.status}: {text[:400]}")
                    await asyncio.sleep(RETRY_BACKOFF * (2 ** attempt))
                    continue
                data = json.loads(text)
                if isinstance(data, dict):
                    if 'result' in data and isinstance(data['result'], dict) and 'data' in data['result']:
                        return data['result']['data']
                    if 'result' in data and isinstance(data['result'], dict):
                        return data['result']
                return data
        except Exception as e:
            last_exc = e
            await asyncio.sleep(RETRY_BACKOFF * (2 ** attempt))
    print(f"[api_call] failed {endpoint} params={params}: {last_exc}")
    return None

# ---------- Embeds + pagination ----------
MAX_DESC_CHARS = 2048
MAX_FIELD_CHARS = 1024

def safe_truncate(s: str, limit: int) -> str:
    if len(s) <= limit:
        return s
    return s[: limit - 3] + "..."

def make_embed(title: str, description: Optional[str] = None, color: discord.Color = discord.Color.blurple()) -> discord.Embed:
    desc = safe_truncate(description, MAX_DESC_CHARS) if description else None
    e = discord.Embed(title=title, description=desc, timestamp=datetime.utcnow(), color=color)
    return e

def add_small_fields(embed: discord.Embed, d: Dict[str, Any], limit: int = 10):
    added = 0
    for k, v in d.items():
        if added >= limit:
            break
        vs = v if isinstance(v, (str, int, float)) else json.dumps(v, default=str)
        embed.add_field(name=str(k), value=safe_truncate(str(vs), MAX_FIELD_CHARS), inline=True)
        added += 1
    if len(d) > limit:
        embed.add_field(name="…", value=f"+{len(d)-limit} more", inline=False)

# Pagination view
class PageView(View):
    def __init__(self, pages: List[discord.Embed], *, timeout: int = 120):
        super().__init__(timeout=timeout)
        self.pages = pages
        self.current = 0
        self.max = len(pages)
        self.message: Optional[discord.Message] = None
        # Buttons
        self.prev = Button(label="<<", style=discord.ButtonStyle.secondary)
        self.next = Button(label=">>", style=discord.ButtonStyle.secondary)
        self.add_item(self.prev)
        self.add_item(self.next)
        self.prev.callback = self.on_prev
        self.next.callback = self.on_next
        self._update_button_states()

    def _update_button_states(self):
        self.prev.disabled = (self.current <= 0)
        self.next.disabled = (self.current >= self.max - 1)

    async def on_prev(self, interaction: discord.Interaction):
        if self.current > 0:
            self.current -= 1
            await interaction.response.edit_message(embed=self.pages[self.current], view=self)
            self._update_button_states()

    async def on_next(self, interaction: discord.Interaction):
        if self.current < self.max - 1:
            self.current += 1
            await interaction.response.edit_message(embed=self.pages[self.current], view=self)
            self._update_button_states()

# Render endpoint to embed pages
async def render_to_pages(endpoint: str, params: Optional[Dict] = None) -> List[discord.Embed]:
    data = await api_call(endpoint, params)
    title = f"📡 {endpoint}"
    if params:
        title += " " + json.dumps(params, separators=(",", ":"))
    if data is None:
        return [make_embed(title, "❌ Failed to fetch data", discord.Color.red())]

    # If dict with list-like key
    if isinstance(data, dict):
        for key in ('items','rankings','results','battles','companies','transactions','offers','data','mUs'):
            if key in data and isinstance(data[key], list):
                lst = data[key]
                return pages_from_list(lst, title, key)
        # else, plain dict: just one page
        e = make_embed(title, "")
        small = {}
        for k, v in data.items():
            if isinstance(v, (str, int, float)):
                small[k] = v
            elif isinstance(v, (list, dict)):
                small[k] = f"{type(v).__name__}({len(v)})"
            else:
                small[k] = str(v)
        add_small_fields(e, small, limit=10)
        return [e]

    if isinstance(data, list):
        return pages_from_list(data, title, None)

    # fallback
    return [make_embed(title, safe_truncate(json.dumps(data, default=str), 1024))]

def pages_from_list(lst: List[Any], title: str, list_key: Optional[str]) -> List[discord.Embed]:
    pages: List[discord.Embed] = []
    # split into chunks
    for i in range(0, len(lst), PAGE_SIZE):
        sub = lst[i : i + PAGE_SIZE]
        e = make_embed(title, f"Showing {i+1}-{i+len(sub)} of {len(lst)}")
        for j, item in enumerate(sub, 1):
            if isinstance(item, dict):
                name = item.get('name') or item.get('id') or f"{j}"
                # build summary
                summary_parts = []
                for k in ('id','country','damage','score','price','status','region','title','username'):
                    if k in item:
                        summary_parts.append(f"{k}:{item[k]}")
                summ = ", ".join(summary_parts) if summary_parts else json.dumps(item, default=str)[:80]
                e.add_field(name=f"{i+j}. {name}", value=safe_truncate(summ, 200), inline=False)
            else:
                e.add_field(name=f"{i+j}", value=safe_truncate(str(item), 200), inline=False)
        pages.append(e)
    if not pages:
        pages.append(make_embed(title, "No data"))
    return pages

# ---------- Dashboard (auto-refresh) ----------
dashboards: Dict[str, Dict[str, Any]] = {}
# structure: name -> {
#   channel_id, message_id, endpoint, params, interval, running, task
# }

async def dashboard_task(name: str):
    cfg = dashboards.get(name)
    if not cfg:
        return
    interval = cfg.get("interval", DEFAULT_DASH_INTERVAL)
    channel_id = cfg["channel_id"]
    message_id = cfg["message_id"]
    endpoint = cfg["endpoint"]
    params = cfg.get("params")
    while dashboards.get(name) and cfg.get("running", False):
        try:
            ch = bot.get_channel(channel_id)
            if ch is None:
                print(f"[dashboard:{name}] channel missing, stopping.")
                break
            pages = await render_to_pages(endpoint, params)
            # pick first page initially, then cycle?
            embed = pages[0]
            try:
                msg = await ch.fetch_message(message_id)
                await msg.edit(embed=embed, view=View())  # view cleared, but we care only embed
            except discord.NotFound:
                print(f"[dashboard:{name}] message not found, stopping.")
                break
            except Exception as e:
                print(f"[dashboard:{name}] edit error: {e}")
        except Exception as e:
            print(f"[dashboard:{name}] error: {e}")
        await asyncio.sleep(interval)

# ---------- Commands ----------
@bot.event
async def on_ready():
    print(f"{bot.user} connected (v3). API base: {API_BASE}")

@bot.command()
async def help(ctx):
    embed = discord.Embed(title="WarEra Bot v3 — Commands", color=discord.Color.blue())
    rows = [
        ("!prices", "Show item prices"),
        ("!battles", "List active battles"),
        ("!wars", "World war snapshot"),
        ("!econ", "Economy snapshot (prices & orders)"),
        ("!rankings [type]", "Ranking list"),
        ("!countries", "All countries list"),
        ("!user <id>", "Get user data"),
        ("!company <id>", "Get company data"),
        ("!articles [page]", "List recent articles/news"),
        ("!mus", "Military Units list"),
        ("!offers", "Item offers / trading orders list"),
        ("!call <endpoint> [json_params]", "Generic API call"),
        ("!dashboard create <name> <endpoint> [json_params] [interval]", "Auto-refresh dashboard"),
        ("!dashboard list", "List dashboards"),
        ("!dashboard stop <name>", "Stop dashboard"),
        ("!dashboard refresh <name>", "Force refresh dashboard"),
    ]
    for c, d in rows:
        embed.add_field(name=c, value=d, inline=False)
    await ctx.send(embed=embed)

@bot.command()
async def prices(ctx):
    await ctx.send("💰 Fetching prices …")
    pages = await render_to_pages("itemTrading.getPrices", None)
    view = PageView(pages)
    msg = await ctx.send(embed=pages[0], view=view)
    view.message = msg

@bot.command()
async def battles(ctx):
    await ctx.send("⚔️ Fetching battles …")
    pages = await render_to_pages("battle.getBattles", None)
    view = PageView(pages)
    msg = await ctx.send(embed=pages[0], view=view)
    view.message = msg

@bot.command()
async def wars(ctx):
    # can combine battle + liveBattle for a richer snapshot; for now battle list
    await ctx.send("🌐 Fetching wars snapshot …")
    pages = await render_to_pages("battle.getBattles", None)
    view = PageView(pages)
    msg = await ctx.send(embed=pages[0], view=view)
    view.message = msg

@bot.command()
async def econ(ctx):
    await ctx.send("💹 Fetching economy snapshot …")
    # embed combining prices and top orders
    prices = await api_call("itemTrading.getPrices", None)
    top = await api_call("tradingOrder.getTopOrders", {"itemType": "FOOD"})
    title = "💹 Economy Snapshot"
    emb = make_embed(title)
    if isinstance(prices, dict):
        sample = {k: prices[k] for k in list(prices.keys())[:6] if isinstance(prices[k], (int, float))}
        if sample:
            add_small_fields(emb, sample, limit=6)
    if isinstance(top, list):
        # show first few orders
        text = ""
        for o in top[:6]:
            text += json.dumps(o, default=str) + "\n"
        emb.add_field(name="Top Orders (sample)", value=safe_truncate(text, 1000), inline=False)
    await ctx.send(embed=emb)

@bot.command()
async def rankings(ctx, ranking_type: str = "weeklyCountryDamages"):
    await ctx.send(f"🏆 Fetching rankings ({ranking_type}) …")
    pages = await render_to_pages("ranking.getRanking", {"rankingType": ranking_type})
    view = PageView(pages)
    msg = await ctx.send(embed=pages[0], view=view)
    view.message = msg

@bot.command()
async def countries(ctx):
    await ctx.send("🌍 Fetching countries …")
    pages = await render_to_pages("country.getAllCountries", None)
    view = PageView(pages)
    msg = await ctx.send(embed=pages[0], view=view)
    view.message = msg

@bot.command()
async def user(ctx, user_id: int):
    await ctx.send(f"🔍 Fetching user {user_id} …")
    pages = await render_to_pages("user.getUserLite", {"userId": user_id})
    view = PageView(pages)
    msg = await ctx.send(embed=pages[0], view=view)
    view.message = msg

@bot.command()
async def company(ctx, comp_id: int):
    await ctx.send(f"🏢 Fetching company {comp_id} …")
    pages = await render_to_pages("company.getById", {"companyId": comp_id})
    view = PageView(pages)
    msg = await ctx.send(embed=pages[0], view=view)
    view.message = msg

@bot.command()
async def articles(ctx, page: int = 1):
    await ctx.send("📰 Fetching recent articles …")
    pages = await render_to_pages("article.getArticlesPaginated", {"page": page, "limit": PAGE_SIZE})
    view = PageView(pages)
    msg = await ctx.send(embed=pages[0], view=view)
    view.message = msg

@bot.command()
async def mus(ctx):
    await ctx.send("🎖️ Fetching military units …")
    pages = await render_to_pages("mu.getManyPaginated", {"page": 1, "limit": PAGE_SIZE})
    view = PageView(pages)
    msg = await ctx.send(embed=pages[0], view=view)
    view.message = msg

@bot.command()
async def offers(ctx):
    await ctx.send("📦 Fetching trading offers/orders …")
    pages = await render_to_pages("itemOffer.getPaginatedItemOffers", {"page": 1, "limit": PAGE_SIZE})
    view = PageView(pages)
    msg = await ctx.send(embed=pages[0], view=view)
    view.message = msg

@bot.command()
async def call(ctx, endpoint: str, *, params_json: str = None):
    params = None
    if params_json:
        try:
            params = json.loads(params_json)
        except Exception:
            await ctx.send("❌ Invalid JSON")
            return
    await ctx.send(f"📡 Calling `{endpoint}` …")
    pages = await render_to_pages(endpoint, params)
    view = PageView(pages)
    msg = await ctx.send(embed=pages[0], view=view)
    view.message = msg

@bot.group()
async def dashboard(ctx):
    if ctx.invoked_subcommand is None:
        await ctx.send("Use: create / list / stop / refresh")

@dashboard.command(name="create")
async def dashboard_create(ctx, name: str, endpoint: str, params_json: str = None, interval: int = DEFAULT_DASH_INTERVAL):
    if name in dashboards:
        await ctx.send(f"❌ Dashboard `{name}` already exists.")
        return
    params = None
    if params_json:
        try:
            params = json.loads(params_json)
        except Exception:
            await ctx.send("❌ Invalid JSON for params")
            return
    pages = await render_to_pages(endpoint, params)
    view = PageView(pages)
    msg = await ctx.send(embed=pages[0], view=view)
    dashboards[name] = {
        "channel_id": ctx.channel.id,
        "message_id": msg.id,
        "endpoint": endpoint,
        "params": params,
        "interval": interval,
        "running": True,
        "task": asyncio.create_task(dashboard_task(name))
    }
    await ctx.send(f"✅ Dashboard `{name}` created (refresh every {interval}s)")

@dashboard.command(name="list")
async def dashboard_list(ctx):
    if not dashboards:
        await ctx.send("No dashboards.")
        return
    embed = discord.Embed(title="Dashboards", color=discord.Color.green())
    for name, cfg in dashboards.items():
        ch = bot.get_channel(cfg["channel_id"])
        embed.add_field(name=name,
                        value=f"endpoint: `{cfg['endpoint']}`\nchannel: {ch.mention if ch else cfg['channel_id']}\ninterval: {cfg['interval']}s\nrunning: {cfg['running']}\nmessage_id: {cfg['message_id']}",
                        inline=False)
    await ctx.send(embed=embed)

@dashboard.command(name="stop")
async def dashboard_stop(ctx, name: str):
    cfg = dashboards.get(name)
    if not cfg:
        await ctx.send(f"❌ Dashboard `{name}` not found")
        return
    cfg["running"] = False
    t = cfg.get("task")
    if t:
        t.cancel()
    dashboards.pop(name, None)
    await ctx.send(f"🛑 Dashboard `{name}` stopped")

@dashboard.command(name="refresh")
async def dashboard_refresh(ctx, name: str):
    cfg = dashboards.get(name)
    if not cfg:
        await ctx.send(f"❌ Dashboard `{name}` not found")
        return
    ch = bot.get_channel(cfg["channel_id"])
    if ch is None:
        await ctx.send("❌ Channel not found")
        return
    try:
        msg = await ch.fetch_message(cfg["message_id"])
    except Exception:
        await ctx.send("❌ Message not found")
        return
    pages = await render_to_pages(cfg["endpoint"], cfg.get("params"))
    view = PageView(pages)
    await msg.edit(embed=pages[0], view=view)
    await ctx.send(f"🔄 Dashboard `{name}` refreshed")

@bot.command()
@commands.is_owner()
async def shutdown(ctx):
    await ctx.send("Shutting down …")
    sess = await get_session()
    if sess and not sess.closed:
        await sess.close()
    await bot.close()

# ---------- Run ----------
if __name__ == "__main__":
    TOKEN = os.getenv("DISCORD_BOT_TOKEN", "YOUR_TOKEN_HERE")
    if TOKEN == "YOUR_TOKEN_HERE":
        print("⚠️ Set DISCORD_BOT_TOKEN environment variable")
    else:
        bot.run(TOKEN)
