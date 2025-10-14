import os
import json
import asyncio
import aiohttp
import urllib.parse
from typing import Optional, Dict, Any
from datetime import datetime
import discord
from discord.ext import commands, tasks

# ---------- Config ----------
API_BASE = os.getenv("WARERA_API_BASE", "https://api2.warera.io/trpc")
REQUEST_TIMEOUT = float(os.getenv("WARERA_REQUEST_TIMEOUT", "10"))
RETRY_ATTEMPTS = int(os.getenv("WARERA_RETRY_ATTEMPTS", "3"))
RETRY_BACKOFF = float(os.getenv("WARERA_RETRY_BACKOFF", "0.6"))
DEFAULT_DASHBOARD_INTERVAL = int(os.getenv("WARERA_DASH_INTERVAL", "60"))  # seconds

# ---------- Bot setup ----------
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# Shared aiohttp session
_session: Optional[aiohttp.ClientSession] = None

async def get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
        _session = aiohttp.ClientSession(timeout=timeout)
    return _session

# ---------- Utility: tRPC GET with ?input={} ----------
def build_trpc_url(endpoint: str, params: Optional[Dict] = None) -> str:
    base = API_BASE.rstrip("/")
    endpoint = endpoint.strip().lstrip("/")  # normalise
    url = f"{base}/{endpoint}"
    if params is None:
        input_json = "{}"
    else:
        # compact JSON
        input_json = json.dumps(params, separators=(",", ":"))
    encoded = urllib.parse.quote(input_json, safe='')
    return f"{url}?input={encoded}"

async def api_call(endpoint: str, params: Optional[Dict] = None) -> Optional[Any]:
    """
    GET ?input={} wrapper. Returns parsed JSON or None on error.
    Retries RETRY_ATTEMPTS times with simple backoff.
    """
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
                # parse JSON
                data = json.loads(text)
                # unwrap common tRPC envelope if present
                if isinstance(data, dict):
                    if 'result' in data and isinstance(data['result'], dict) and 'data' in data['result']:
                        return data['result']['data']
                    if 'result' in data and isinstance(data['result'], dict):
                        return data['result']
                return data
        except Exception as e:
            last_exc = e
            await asyncio.sleep(RETRY_BACKOFF * (2 ** attempt))
    # final failure
    print(f"[api_call] failed {endpoint} params={params} : {last_exc}")
    return None

# ---------- Helper: Embed formatting ----------
MAX_FIELD_CHARS = 1000
def safe_truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 3] + "..."

def make_embed(title: str, description: str = "", color: discord.Color = discord.Color.blurple()) -> discord.Embed:
    e = discord.Embed(title=title, description=safe_truncate(description, 2048), timestamp=datetime.utcnow(), color=color)
    return e

def add_dict_fields(embed: discord.Embed, d: Dict[str, Any], limit: int = 10):
    added = 0
    for k, v in d.items():
        if added >= limit:
            break
        text = v if isinstance(v, (str, int, float)) else json.dumps(v, default=str)
        embed.add_field(name=str(k), value=safe_truncate(str(text), MAX_FIELD_CHARS), inline=True)
        added += 1
    if len(d) > limit:
        embed.add_field(name="…", value=f"+{len(d)-limit} more fields", inline=False)

# ---------- Dashboard management (in-memory while running) ----------
# Dashboard structure: name -> dict(channel_id, message_id, endpoint, params, interval, task)
dashboards: Dict[str, Dict[str, Any]] = {}

async def _render_endpoint_to_embed(endpoint: str, params: Optional[Dict]) -> discord.Embed:
    """Call endpoint and produce a human-friendly embed"""
    data = await api_call(endpoint, params)
    title = f"📡 {endpoint}"
    if params:
        title += f" {json.dumps(params, separators=(',',':'))}"
    if data is None:
        return make_embed(title, "❌ Failed to fetch data", discord.Color.red())
    # Build embed based on type
    emb = make_embed(title, "", discord.Color.blue())
    # If it's a dict with common keys, try to present them
    if isinstance(data, dict):
        # If top-level contains items/rankings/results, show summary and sample
        for key in ('items','rankings','results','data','battles','companies','transactions','items'):
            if key in data and isinstance(data[key], list):
                emb.add_field(name=f"{key} (count)", value=str(len(data[key])), inline=False)
                # show first few
                for i, item in enumerate(data[key][:6], 1):
                    if isinstance(item, dict):
                        # pick name-like fields
                        name = item.get('name') or item.get('title') or item.get('username') or item.get('id') or str(i)
                        summary = []
                        for s in ('id','country','damage','score','price','status','region'):
                            if s in item:
                                summary.append(f"{s}:{item[s]}")
                        emb.add_field(name=f"{i}. {name}", value=safe_truncate(", ".join(summary) or json.dumps(item, default=str)[:200], 1000), inline=False)
                return emb
        # Otherwise, list top-level small fields
        small = {}
        for k, v in data.items():
            if isinstance(v, (str, int, float)):
                small[k] = v
            elif isinstance(v, (list, dict)):
                small[k] = f"{type(v).__name__} ({len(v) if hasattr(v, '__len__') else 'x'})"
            else:
                small[k] = str(v)
        add_dict_fields(emb, small, limit=10)
        return emb
    # If list, show some items
    if isinstance(data, list):
        emb.add_field(name="Count", value=str(len(data)), inline=False)
        for i, it in enumerate(data[:6], 1):
            if isinstance(it, dict):
                name = it.get('id') or it.get('name') or str(i)
                emb.add_field(name=f"{i}. {name}", value=safe_truncate(json.dumps(it, default=str), 800), inline=False)
            else:
                emb.add_field(name=f"{i}", value=safe_truncate(str(it), 800), inline=False)
        return emb
    # fallback: present as text
    emb.add_field(name="Result", value=safe_truncate(json.dumps(data, default=str), 1024), inline=False)
    return emb

async def _dashboard_runner(name: str):
    """Background task to refresh a dashboard by editing the message."""
    cfg = dashboards.get(name)
    if not cfg:
        return
    interval = cfg.get("interval", DEFAULT_DASHBOARD_INTERVAL)
    channel_id = cfg["channel_id"]
    message_id = cfg["message_id"]
    endpoint = cfg["endpoint"]
    params = cfg.get("params")
    while dashboards.get(name) and dashboards[name].get("running", False):
        try:
            channel = bot.get_channel(channel_id)
            if channel is None:
                print(f"[dashboard:{name}] channel not found ({channel_id}), stopping")
                dashboards[name]["running"] = False
                break
            embed = await _render_endpoint_to_embed(endpoint, params)
            try:
                msg = await channel.fetch_message(message_id)
                await msg.edit(embed=embed)
            except discord.NotFound:
                # message removed - stop dashboard
                print(f"[dashboard:{name}] message {message_id} not found, stopping")
                dashboards[name]["running"] = False
                break
            except discord.Forbidden:
                print(f"[dashboard:{name}] missing permissions to edit message")
                dashboards[name]["running"] = False
                break
            except Exception as e:
                print(f"[dashboard:{name}] edit error: {e}")
        except Exception as e:
            print(f"[dashboard:{name}] runner error: {e}")
        await asyncio.sleep(interval)

# ---------- Commands ----------
@bot.event
async def on_ready():
    print(f"{bot.user} connected. WarEra API base: {API_BASE}")

@bot.command()
async def help(ctx):
    embed = discord.Embed(title="WarEra Bot — Commands", color=discord.Color.blue())
    rows = [
        ("!prices", "Show item prices (itemTrading.getPrices)"),
        ("!battles", "List active battles (battle.getBattles)"),
        ("!wars", "War snapshot (battle.getBattles + helpers)"),
        ("!econ", "Economy snapshot (prices + top orders)"),
        ("!rankings [type]", "Rankings (ranking.getRanking)"),
        ("!countries", "List countries (country.getAllCountries)"),
        ("!user <id>", "Get user (user.getUserLite)"),
        ("!company <id>", "Get company (company.getById)"),
        ("!call <endpoint> [json_params]", "Call any endpoint (tRPC) — params as JSON"),
        ("!dashboard create <name> <endpoint> [json_params] [interval]", "Create dashboard that edits one message"),
        ("!dashboard list", "List dashboards"),
        ("!dashboard stop <name>", "Stop dashboard"),
        ("!dashboard refresh <name>", "Force refresh dashboard now"),
    ]
    for c, d in rows:
        embed.add_field(name=c, value=d, inline=False)
    await ctx.send(embed=embed)

# Basic helpers
@bot.command()
async def prices(ctx):
    await ctx.send("💰 Fetching prices...")
    data = await api_call("itemTrading.getPrices", None)
    emb = await _render_endpoint_to_embed("itemTrading.getPrices", None)
    await ctx.send(embed=emb)

@bot.command()
async def battles(ctx):
    await ctx.send("⚔️ Fetching battles...")
    emb = await _render_endpoint_to_embed("battle.getBattles", None)
    await ctx.send(embed=emb)

@bot.command()
async def wars(ctx):
    await ctx.send("⚔️ Fetching war snapshot...")
    emb = await _render_endpoint_to_embed("battle.getBattles", None)
    await ctx.send(embed=emb)

@bot.command()
async def econ(ctx):
    await ctx.send("💹 Fetching economy snapshot...")
    # compose an embed combining prices and top orders
    prices = await api_call("itemTrading.getPrices", None)
    orders = await api_call("tradingOrder.getTopOrders", {"itemType": "FOOD"})
    emb = make_embed("💹 Economy Snapshot")
    if isinstance(prices, dict):
        sample = {k: prices[k] for k in list(prices.keys())[:10] if isinstance(prices[k], (int, float))}
        if sample:
            add_dict_fields(emb, sample, limit=8)
    if isinstance(orders, list):
        emb.add_field(name="Top Orders (sample)", value=safe_truncate(json.dumps(orders[:6], default=str), 1000), inline=False)
    await ctx.send(embed=emb)

@bot.command()
async def rankings(ctx, ranking_type: str = "weeklyCountryDamages"):
    await ctx.send(f"🏆 Fetching {ranking_type}...")
    emb = await _render_endpoint_to_embed("ranking.getRanking", {"rankingType": ranking_type})
    await ctx.send(embed=emb)

@bot.command()
async def countries(ctx):
    await ctx.send("🌍 Fetching countries...")
    emb = await _render_endpoint_to_embed("country.getAllCountries", None)
    await ctx.send(embed=emb)

@bot.command()
async def user(ctx, user_id: int):
    if not user_id:
        await ctx.send("Usage: `!user <id>`")
        return
    await ctx.send(f"🔎 Fetching user {user_id}...")
    emb = await _render_endpoint_to_embed("user.getUserLite", {"userId": user_id})
    await ctx.send(embed=emb)

@bot.command()
async def company(ctx, company_id: int):
    if not company_id:
        await ctx.send("Usage: `!company <id>`")
        return
    await ctx.send(f"🏢 Fetching company {company_id}...")
    emb = await _render_endpoint_to_embed("company.getById", {"companyId": company_id})
    await ctx.send(embed=emb)

@bot.command()
async def call(ctx, endpoint: str, *, params_json: str = None):
    """
    Generic call: !call endpoint {"key":1}
    Example: !call ranking.getRanking {"rankingType":"weeklyCountryDamages"}
    """
    params = None
    if params_json:
        try:
            params = json.loads(params_json)
        except Exception:
            await ctx.send("❌ Invalid JSON for params")
            return
    await ctx.send(f"📡 Calling `{endpoint}` …")
    emb = await _render_endpoint_to_embed(endpoint, params)
    await ctx.send(embed=emb)

# Dashboard commands
@bot.group()
async def dashboard(ctx):
    if ctx.invoked_subcommand is None:
        await ctx.send("Use subcommands: create, list, stop, refresh")

@dashboard.command(name="create")
async def dashboard_create(ctx, name: str, endpoint: str, params_json: str = None, interval: int = DEFAULT_DASHBOARD_INTERVAL):
    """
    Create a dashboard message that the bot will edit periodically.
    Example: !dashboard create econ itemTrading.getPrices 60
    Or: !dashboard create ranks ranking.getRanking '{"rankingType":"weeklyCountryDamages"}' 30
    """
    if name in dashboards:
        await ctx.send(f"❌ Dashboard `{name}` already exists.")
        return
    params = None
    if params_json:
        try:
            params = json.loads(params_json)
        except Exception:
            await ctx.send("❌ params_json invalid JSON")
            return
    # create initial embed
    emb = await _render_endpoint_to_embed(endpoint, params)
    try:
        msg = await ctx.send(embed=emb)
    except discord.Forbidden:
        await ctx.send("❌ Missing permission to send messages in this channel.")
        return
    # store dashboard
    dashboards[name] = {
        "channel_id": ctx.channel.id,
        "message_id": msg.id,
        "endpoint": endpoint,
        "params": params,
        "interval": interval,
        "running": True,
        "task": None
    }
    # spawn runner
    dashboards[name]["task"] = asyncio.create_task(_dashboard_runner(name))
    await ctx.send(f"✅ Dashboard `{name}` created and will refresh every {interval}s (editing one message).")

@dashboard.command(name="list")
async def dashboard_list(ctx):
    if not dashboards:
        await ctx.send("No dashboards running.")
        return
    embed = discord.Embed(title="Dashboards (in-memory)", color=discord.Color.green())
    for name, cfg in dashboards.items():
        ch = bot.get_channel(cfg["channel_id"])
        msg_link = f"https://discord.com/channels/{ctx.guild.id}/{cfg['channel_id']}/{cfg['message_id']}" if ctx.guild else "link-unavailable"
        embed.add_field(name=name, value=f"endpoint: `{cfg['endpoint']}`\nchannel: {ch.mention if ch else cfg['channel_id']}\ninterval: {cfg['interval']}s\nrunning: {cfg['running']}\n[message]({msg_link})", inline=False)
    await ctx.send(embed=embed)

@dashboard.command(name="stop")
async def dashboard_stop(ctx, name: str):
    cfg = dashboards.get(name)
    if not cfg:
        await ctx.send(f"❌ No dashboard named `{name}`")
        return
    cfg["running"] = False
    # cancel task if exists
    t = cfg.get("task")
    if t:
        t.cancel()
    dashboards.pop(name, None)
    await ctx.send(f"🛑 Dashboard `{name}` stopped and removed from memory (while running).")

@dashboard.command(name="refresh")
async def dashboard_refresh(ctx, name: str):
    cfg = dashboards.get(name)
    if not cfg:
        await ctx.send(f"❌ No dashboard named `{name}`")
        return
    # force immediate render+edit
    channel = bot.get_channel(cfg["channel_id"])
    if channel is None:
        await ctx.send("❌ Dashboard channel not found")
        return
    try:
        msg = await channel.fetch_message(cfg["message_id"])
    except Exception:
        await ctx.send("❌ Dashboard message not found")
        return
    emb = await _render_endpoint_to_embed(cfg["endpoint"], cfg.get("params"))
    await msg.edit(embed=emb)
    await ctx.send(f"🔄 Dashboard `{name}` refreshed.")

# ---------- Graceful shutdown ----------
@bot.command()
@commands.is_owner()
async def shutdown(ctx):
    await ctx.send("Shutting down...")
    await get_session()  # ensure session created
    if _session and not _session.closed:
        await _session.close()
    await bot.close()

# ---------- Run ----------
if __name__ == "__main__":
    TOKEN = os.getenv("DISCORD_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
    if TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("⚠️ Set DISCORD_BOT_TOKEN environment variable before running.")
    else:
        bot.run(TOKEN)
