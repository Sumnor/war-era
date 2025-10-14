import os, json, asyncio, aiohttp, urllib.parse
from typing import Optional, Dict, Any, List
from datetime import datetime, timezone
import discord
from discord.ext import tasks
from discord import app_commands
from discord.ui import View, Button, Modal, TextInput

# ---------- Config ----------
API_BASE = os.getenv("WARERA_API_BASE", "https://api2.warera.io/trpc")
REQUEST_TIMEOUT = float(os.getenv("WARERA_REQUEST_TIMEOUT", "10"))
RETRY_ATTEMPTS = int(os.getenv("WARERA_RETRY_ATTEMPTS", "3"))
RETRY_BACKOFF = float(os.getenv("WARERA_RETRY_BACKOFF", "0.6"))
PAGE_SIZE = 8
DEFAULT_DASH_INTERVAL = int(os.getenv("WARERA_DASH_INTERVAL", "60"))  # seconds

# ---------- Bot Setup ----------
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
    input_json = "{}" if params is None else json.dumps(params, separators=(",", ":"))
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

# ---------- Embeds ----------
MAX_DESC_CHARS = 2048
MAX_FIELD_CHARS = 1024

def safe_truncate(s: str, limit: int) -> str:
    return s if len(s) <= limit else s[:limit-3]+"..."

def make_embed(title: str, description: Optional[str] = None, color: discord.Color = discord.Color.blurple()) -> discord.Embed:
    desc = safe_truncate(description, MAX_DESC_CHARS) if description else None
    e = discord.Embed(title=title, description=desc, timestamp=datetime.now(timezone.utc), color=color)
    return e

def add_small_fields(embed: discord.Embed, d: Dict[str, Any], limit: int = 10):
    added = 0
    for k, v in d.items():
        if added >= limit: break
        vs = v if isinstance(v, (str,int,float)) else json.dumps(v, default=str)
        embed.add_field(name=str(k), value=safe_truncate(str(vs), MAX_FIELD_CHARS), inline=True)
        added += 1
    if len(d) > limit:
        embed.add_field(name="…", value=f"+{len(d)-limit} more", inline=False)

# ---------- Pagination ----------
class PageView(View):
    def __init__(self, pages: List[discord.Embed], *, timeout: int = 180):
        super().__init__(timeout=timeout)
        self.pages = pages
        self.current = 0
        self.max = len(pages)
        self.message: Optional[discord.Message] = None
        self.prev = Button(label="⬅️", style=discord.ButtonStyle.secondary)
        self.next = Button(label="➡️", style=discord.ButtonStyle.secondary)
        self.add_item(self.prev)
        self.add_item(self.next)
        self.prev.callback = self.on_prev
        self.next.callback = self.on_next
        self._update_button_states()

    def _update_button_states(self):
        self.prev.disabled = self.current <= 0
        self.next.disabled = self.current >= self.max-1

    async def on_prev(self, interaction: discord.Interaction):
        self.current = max(0, self.current - 1)
        await interaction.response.edit_message(embed=self.pages[self.current], view=self)
        self._update_button_states()

    async def on_next(self, interaction: discord.Interaction):
        self.current = min(self.max-1, self.current + 1)
        await interaction.response.edit_message(embed=self.pages[self.current], view=self)
        self._update_button_states()

async def send_paginated(interaction: discord.Interaction, pages: List[discord.Embed]):
    view = PageView(pages)
    await interaction.response.send_message(embed=pages[0], view=view)

# ---------- Pretty Ranking / List Pages ----------
def pretty_ranking_pages(title:str, items:List[Dict], start_index:int=0) -> List[discord.Embed]:
    pages: List[discord.Embed] = []
    total = len(items)
    for i in range(0, total, PAGE_SIZE):
        chunk = items[i:i+PAGE_SIZE]
        e = make_embed(title, f"Showing {i+1}-{i+len(chunk)} of {total}", discord.Color.dark_gold())
        for idx, it in enumerate(chunk, start=1+i):
            if isinstance(it, dict):
                tier = it.get("tier","").lower()
                if tier.startswith("maste"): emoji="🥇"
                elif tier.startswith("gold"): emoji="🥈"
                elif tier.startswith("silv"): emoji="🥉"
                else: emoji="🏵️"
                uid = it.get("user") or it.get("id") or it.get("name") or str(idx)
                uname = it.get("name") or uid
                url = f"https://warera.io/profile/{uid}" if uid else "#"
                val = it.get("value") or it.get("score") or it.get("damage") or 0
                val_s = f"{val:,}" if isinstance(val,int) else str(val)
                e.add_field(name=f"{emoji} #{idx}", value=f"[{uname}]({url}) — ⚔️ {val_s}", inline=False)
            else:
                e.add_field(name=f"#{idx}", value=str(it), inline=False)
        pages.append(e)
    if not pages:
        pages.append(make_embed(title,"No data"))
    return pages

def list_pages(title: str, items: List[Any]) -> List[discord.Embed]:
    pages = []
    total = len(items)
    for i in range(0, total, PAGE_SIZE):
        chunk = items[i:i+PAGE_SIZE]
        e = make_embed(title, f"Showing {i+1}-{i+len(chunk)} of {total}")
        for idx, it in enumerate(chunk, start=1+i):
            if isinstance(it, dict):
                name = it.get("name") or it.get("user") or it.get("id") or str(idx)
                summary = json.dumps(it, default=str)[:80]
                e.add_field(name=f"#{idx} {name}", value=summary, inline=False)
            else:
                e.add_field(name=f"#{idx}", value=str(it), inline=False)
        pages.append(e)
    if not pages:
        pages.append(make_embed(title,"No data"))
    return pages

# ---------- Render Endpoint ----------
async def render_endpoint(endpoint:str, params:Optional[Dict]=None):
    data = await api_call(endpoint, params)
    if data is None: return [make_embed(endpoint,"❌ Failed to fetch",discord.Color.red())], [make_embed(endpoint,"❌ Failed to fetch",discord.Color.red())]
    game_pages, dev_pages = [], []

    # Items list
    if isinstance(data, list):
        game_pages = list_pages(f"📋 {endpoint}", data)
        dev_pages = list_pages(f"🛠️ Dev: {endpoint}", data)
    elif isinstance(data, dict):
        # Ranking items
        items = data.get("items") or data.get("results") or data.get("data") or []
        if isinstance(items, list) and items:
            game_pages = pretty_ranking_pages(f"🏆 {endpoint}", items)
            dev_pages = pretty_ranking_pages(f"🛠️ Dev: {endpoint}", items)
        else:
            e_game = make_embed(f"📋 {endpoint}")
            e_dev = make_embed(f"🛠️ Dev: {endpoint}")
            small = {k:v for k,v in data.items() if isinstance(v,(str,int,float))}
            add_small_fields(e_game, small)
            add_small_fields(e_dev, data, 15)
            game_pages=[e_game]; dev_pages=[e_dev]
    else:
        game_pages=[make_embed(endpoint,str(data))]; dev_pages=[make_embed(endpoint,str(data))]
    return game_pages, dev_pages

# ---------- Send game/dev paginated ----------
async def send_game_dev_paginated(interaction: discord.Interaction, game_pages, dev_pages=None):
    if not interaction.response.is_done():
        await interaction.response.defer()
    view = PageView(game_pages)
    await interaction.followup.send(embed=game_pages[0], view=view)

# ---------- Dashboard ----------
DASH_MESSAGE: Optional[discord.Message] = None
DASH_INTERVAL = DEFAULT_DASH_INTERVAL

async def refresh_dashboard(channel:discord.TextChannel):
    global DASH_MESSAGE
    pages, _ = await render_endpoint("ranking.getRanking", {"rankingType":"userDamages"})
    if DASH_MESSAGE is None:
        DASH_MESSAGE = await channel.send(embed=pages[0], view=PageView(pages))
    else:
        await DASH_MESSAGE.edit(embed=pages[0], view=PageView(pages))

# ---------- Commands (Slash Only) ----------
@bot.tree.command(name="rankings")
@app_commands.describe(ranking_type="Type of ranking")
@app_commands.choices(ranking_type=[
    app_commands.Choice(name="User Damage", value="userDamages"),
    app_commands.Choice(name="Weekly User Damage", value="weeklyUserDamages"),
    app_commands.Choice(name="Wealth", value="userWealth"),
    app_commands.Choice(name="Level", value="userLevel"),
    app_commands.Choice(name="Referals", value="userReferrals"),
    app_commands.Choice(name="Subscribers", value="userSubscribers"),
    app_commands.Choice(name="Ground", value="userTerrain"),
    app_commands.Choice(name="Premium", value="userPremiumMonths"),
    app_commands.Choice(name="Premium Gifts", value="userPremiumGifts"),
])
async def rankings_cmd(interaction: discord.Interaction, ranking_type: app_commands.Choice[str]):
    pages, dev_pages = await render_endpoint("ranking.getRanking", {"rankingType": ranking_type.value})
    await send_game_dev_paginated(interaction, pages, dev_pages)

@bot.tree.command(name="countries")
async def countries_cmd(interaction: discord.Interaction):
    pages, _ = await render_endpoint("country.getAllCountries")
    await send_game_dev_paginated(interaction, pages)

@bot.tree.command(name="companies")
async def companies_cmd(interaction: discord.Interaction):
    pages, dev_pages = await render_endpoint("company.getCompanies", {"page":1,"limit":50})
    await send_game_dev_paginated(interaction, pages, dev_pages)

@bot.tree.command(name="military_units")
async def mus_cmd(interaction: discord.Interaction):
    pages, dev_pages = await render_endpoint("mu.getManyPaginated", {"page":1,"limit":50})
    await send_game_dev_paginated(interaction, pages, dev_pages)

# JSON debugger modal
class JSONModal(Modal):
    def __init__(self):
        super().__init__(title="JSON to Embed")
        self.input = TextInput(label="Paste JSON here", style=discord.TextStyle.paragraph, placeholder='{"example":1}', required=True, max_length=3000)
        self.add_item(self.input)
    async def on_submit(self, interaction: discord.Interaction):
        try:
            data = json.loads(self.input.value)
            e = make_embed("JSON Debugger")
            add_small_fields(e, data, 20)
            await interaction.response.send_message(embed=e)
        except Exception as e:
            await interaction.response.send_message(f"❌ Failed: {e}")

@bot.tree.command(name="json_embed")
async def json_embed_cmd(interaction: discord.Interaction):
    await interaction.response.send_modal(JSONModal())

# ---------- Bot Events ----------
@bot.event
async def on_ready():
    print(f"Bot ready! Logged in as {bot.user}")
    await bot.tree.sync()

# ---------- Run ----------
if __name__=="__main__":
    TOKEN = os.getenv("DISCORD_BOT_TOKEN","YOUR_TOKEN_HERE")
    if TOKEN=="YOUR_TOKEN_HERE":
        print("Set DISCORD_BOT_TOKEN environment variable")
    else:
        bot.run(TOKEN)
