import os, json, asyncio, aiohttp, urllib.parse
from typing import Optional, Dict, Any, List
from datetime import datetime
import discord
from discord import app_commands
from discord.ext import commands, tasks
from discord.ui import View, Button, Modal, TextInput

# ---------- Config ----------
API_BASE = os.getenv("WARERA_API_BASE", "https://api2.warera.io/trpc")
REQUEST_TIMEOUT = float(os.getenv("WARERA_REQUEST_TIMEOUT", "10"))
RETRY_ATTEMPTS = int(os.getenv("WARERA_RETRY_ATTEMPTS", "3"))
RETRY_BACKOFF = float(os.getenv("WARERA_RETRY_BACKOFF", "0.6"))
DEFAULT_DASH_INTERVAL = int(os.getenv("WARERA_DASH_INTERVAL", "60"))
PAGE_SIZE = 8

# ---------- Bot Setup ----------
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)
_session: Optional[aiohttp.ClientSession] = None

# ---------- API ----------
class WarEraAPI:
    def __init__(self, base_url=API_BASE):
        self.base_url = base_url

    async def get_session(self) -> aiohttp.ClientSession:
        global _session
        if _session is None or _session.closed:
            timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
            _session = aiohttp.ClientSession(timeout=timeout)
        return _session

    def build_url(self, endpoint: str, params: Optional[Dict] = None) -> str:
        base = self.base_url.rstrip("/")
        url = f"{base}/{endpoint.lstrip('/')}"
        input_json = json.dumps(params or {}, separators=(",", ":"))
        return f"{url}?input={urllib.parse.quote(input_json, safe='')}"

    async def call(self, endpoint: str, params: Optional[Dict] = None) -> Optional[Any]:
        url = self.build_url(endpoint, params)
        sess = await self.get_session()
        last_exc = None
        for attempt in range(RETRY_ATTEMPTS):
            try:
                async with sess.get(url) as resp:
                    text = await resp.text()
                    if resp.status != 200:
                        last_exc = Exception(f"HTTP {resp.status}")
                        await asyncio.sleep(RETRY_BACKOFF * (2**attempt))
                        continue
                    data = json.loads(text)
                    if isinstance(data, dict) and 'result' in data:
                        if isinstance(data['result'], dict) and 'data' in data['result']:
                            return data['result']['data']
                        return data['result']
                    return data
            except Exception as e:
                last_exc = e
                await asyncio.sleep(RETRY_BACKOFF * (2**attempt))
        print(f"[API] failed {endpoint}: {last_exc}")
        return None

# ---------- Embeds & Pagination ----------
MAX_DESC_CHARS = 2048
MAX_FIELD_CHARS = 1024

def safe_truncate(s: str, limit: int) -> str:
    return s if len(s) <= limit else s[:limit-3]+"..."

def make_embed(title: str, description: Optional[str] = None, color: discord.Color = discord.Color.blurple()) -> discord.Embed:
    return discord.Embed(title=title, description=safe_truncate(description, MAX_DESC_CHARS) if description else None, timestamp=datetime.utcnow(), color=color)

def add_small_fields(embed: discord.Embed, d: Dict[str, Any], limit: int = 10):
    added = 0
    for k,v in d.items():
        if added>=limit: break
        vs = v if isinstance(v,(str,int,float)) else json.dumps(v, default=str)
        embed.add_field(name=str(k), value=safe_truncate(str(vs), MAX_FIELD_CHARS), inline=True)
        added +=1
    if len(d)>limit:
        embed.add_field(name="…", value=f"+{len(d)-limit} more", inline=False)

class PageView(View):
    def __init__(self, pages: List[discord.Embed], *, timeout: int=120):
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
        self.prev.disabled = self.current <=0
        self.next.disabled = self.current >= self.max-1

    async def on_prev(self, interaction: discord.Interaction):
        if self.current>0:
            self.current-=1
            await interaction.response.edit_message(embed=self.pages[self.current], view=self)
            self._update_button_states()

    async def on_next(self, interaction: discord.Interaction):
        if self.current<self.max-1:
            self.current+=1
            await interaction.response.edit_message(embed=self.pages[self.current], view=self)
            self._update_button_states()

# ---------- JSON Modal & Button ----------
class JsonModal(Modal):
    def __init__(self, endpoint: str, api: WarEraAPI):
        super().__init__(title=f"JSON Viewer for {endpoint}")
        self.endpoint = endpoint
        self.api = api
        self.input_field = TextInput(label="Enter key/id (optional)", required=False)
        self.add_item(self.input_field)

    async def on_submit(self, interaction: discord.Interaction):
        key = self.input_field.value.strip()
        data = await self.api.call(self.endpoint)
        if data is None:
            await interaction.response.send_message("❌ Failed to fetch data", ephemeral=True)
            return
        if key and isinstance(data, dict):
            data = data.get(key, f"❌ Key '{key}' not found")
        content = json.dumps(data, indent=2, default=str)
        if len(content)>1024: content = content[:1021]+"..."
        embed = make_embed(f"{self.endpoint} JSON", f"```json\n{content}\n```")
        await interaction.response.send_message(embed=embed, ephemeral=True)

class JsonButton(Button):
    def __init__(self, endpoint: str, api: WarEraAPI):
        super().__init__(label="View JSON", style=discord.ButtonStyle.primary)
        self.endpoint = endpoint
        self.api = api

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(JsonModal(self.endpoint, self.api))

# ---------- WarEra Bot ----------
class WarEraBot:
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.api = WarEraAPI()
        self.DASH_MESSAGE: Optional[discord.Message]=None
        self.DASH_INTERVAL = DEFAULT_DASH_INTERVAL

    async def render_dual_embed(self, endpoint:str, title:str, data:Any):
        pages = []
        if isinstance(data, list):
            for i in range(0, len(data), PAGE_SIZE):
                chunk = data[i:i+PAGE_SIZE]
                e = make_embed(title, f"Showing {i+1}-{i+len(chunk)} of {len(data)}")
                for idx,item in enumerate(chunk, start=i+1):
                    name = item.get("name") or item.get("id") or str(idx)
                    stats = {k:v for k,v in item.items() if k!="name"}
                    add_small_fields(e, stats, limit=5)
                pages.append(e)
        elif isinstance(data, dict):
            e = make_embed(title)
            add_small_fields(e, data, limit=10)
            pages.append(e)
        else:
            pages.append(make_embed(title,str(data)))
        view = PageView(pages)
        view.add_item(JsonButton(endpoint,self.api))
        return pages, view

    async def send_paginated(self, ctx_or_interaction, pages:List[discord.Embed], view:Optional[View]=None):
        if view is None: view = PageView(pages)
        if isinstance(ctx_or_interaction, discord.Interaction):
            await ctx_or_interaction.response.send_message(embed=pages[0], view=view)
        else:
            msg = await ctx_or_interaction.send(embed=pages[0], view=view)
            view.message = msg

    # ---------- Dashboard ----------
    async def refresh_dashboard(self, channel:discord.TextChannel):
        data = await self.api.call("ranking.getRanking", {"rankingType":"userDamages"})
        pages, view = await self.render_dual_embed("ranking.getRanking", "📊 User Damages", data or [])
        if self.DASH_MESSAGE is None:
            self.DASH_MESSAGE = await channel.send(embed=pages[0], view=view)
        else:
            await self.DASH_MESSAGE.edit(embed=pages[0], view=view)

# ---------- Bot Commands ----------
bot_instance = WarEraBot(bot)

@bot.tree.command(name="countries", description="View countries")
async def countries(interaction: discord.Interaction):
    data = await bot_instance.api.call("country.getAllCountries")
    pages, view = await bot_instance.render_dual_embed("country.getAllCountries","🌍 Countries", data or [])
    await bot_instance.send_paginated(interaction, pages, view)

@bot.tree.command(name="companies", description="View companies")
async def companies(interaction: discord.Interaction):
    data = await bot_instance.api.call("company.getCompanies", {"page":1,"limit":50})
    pages, view = await bot_instance.render_dual_embed("company.getCompanies","🏢 Companies", data or [])
    await bot_instance.send_paginated(interaction, pages, view)

@bot.tree.command(name="battles", description="View battles")
async def battles(interaction: discord.Interaction):
    data = await bot_instance.api.call("battle.getBattles")
    pages, view = await bot_instance.render_dual_embed("battle.getBattles","⚔️ Battles", data or [])
    await bot_instance.send_paginated(interaction, pages, view)

@bot.tree.command(name="workoffers", description="View work offers")
async def workoffers(interaction: discord.Interaction):
    data = await bot_instance.api.call("workOffer.getWorkOffersPaginated", {"page":1,"limit":50})
    pages, view = await bot_instance.render_dual_embed("workOffer.getWorkOffersPaginated","💼 Work Offers", data or [])
    await bot_instance.send_paginated(interaction, pages, view)

@bot.tree.command(name="mu", description="View military units")
async def mu(interaction: discord.Interaction):
    data = await bot_instance.api.call("mu.getManyPaginated", {"page":1,"limit":50})
    pages, view = await bot_instance.render_dual_embed("mu.getManyPaginated","🎖️ Military Units", data or [])
    await bot_instance.send_paginated(interaction, pages, view)

@bot.tree.command(name="articles", description="View articles/news")
async def articles(interaction: discord.Interaction):
    data = await bot_instance.api.call("article.getArticlesPaginated", {"page":1,"limit":50})
    pages, view = await bot_instance.render_dual_embed("article.getArticlesPaginated","📰 Articles", data or [])
    await bot_instance.send_paginated(interaction, pages, view)

@bot.tree.command(name="prices", description="View item prices")
async def prices(interaction: discord.Interaction):
    data = await bot_instance.api.call("itemTrading.getPrices")
    pages, view = await bot_instance.render_dual_embed("itemTrading.getPrices","💰 Item Prices", data or [])
    await bot_instance.send_paginated(interaction, pages, view)

@bot.tree.command(name="rankings", description="View rankings")
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
async def rankings(interaction: discord.Interaction, ranking_type: app_commands.Choice[str]):
    data = await bot_instance.api.call("ranking.getRanking", {"rankingType": ranking_type.value})
    pages, view = await bot_instance.render_dual_embed("ranking.getRanking", f"🏆 Rankings ({ranking_type})", data.get("items",[]) if data else [])
    await bot_instance.send_paginated(interaction, pages, view)

@bot.tree.command(name="dashboard", description="Post auto-refresh dashboard")
async def dashboard(interaction: discord.Interaction):
    ch = interaction.channel
    if ch:
        await bot_instance.refresh_dashboard(ch)
        await interaction.response.send_message("✅ Dashboard posted/updated", ephemeral=True)

@bot.tree.command(name="help", description="View bot commands")
async def help(interaction: discord.Interaction):
    cmds = [
        ("countries","🌍 View countries"),
        ("companies","🏢 View companies"),
        ("battles","⚔️ Active battles"),
        ("workoffers","💼 Work offers"),
        ("mu","🎖️ Military units"),
        ("articles","📰 Articles/news"),
        ("prices","💰 Item prices"),
        ("rankings <type>","🏆 Rankings"),
        ("dashboard","📊 Live dashboard"),
    ]
    e = make_embed("🎯 WarEra Bot Commands")
    for cmd, desc in cmds:
        e.add_field(name=f"/{cmd}", value=desc, inline=False)
    await interaction.response.send_message(embed=e)

# ---------- Loops ----------
@tasks.loop(seconds=DEFAULT_DASH_INTERVAL)
async def dash_loop():
    if DASH_CHANNEL_ID := os.getenv("WARERA_DASH_CHANNEL"):
        ch = bot.get_channel(int(DASH_CHANNEL_ID))
        if ch:
            await bot_instance.refresh_dashboard(ch)

# ---------- Bot Start ----------
@bot.event
async def on_ready():
    print(f"Bot ready! Logged in as {bot.user}")
    try: await bot.tree.sync()
    except: pass
    dash_loop.start()

if __name__=="__main__":
    TOKEN = os.getenv("DISCORD_BOT_TOKEN","YOUR_TOKEN_HERE")
    if TOKEN=="YOUR_TOKEN_HERE":
        print("Set DISCORD_BOT_TOKEN environment variable")
    else:
        bot.run(TOKEN)
