import os, json, asyncio, aiohttp, urllib.parse
from typing import Optional, Dict, Any, List
from datetime import datetime
import discord
from discord import app_commands
from discord.ext import tasks
from discord.ui import View, Button

# ---------- Config ----------
API_BASE = os.getenv("WARERA_API_BASE", "https://api2.warera.io/trpc")
REQUEST_TIMEOUT = float(os.getenv("WARERA_REQUEST_TIMEOUT", "10"))
RETRY_ATTEMPTS = int(os.getenv("WARERA_RETRY_ATTEMPTS", "3"))
RETRY_BACKOFF = float(os.getenv("WARERA_RETRY_BACKOFF", "0.6"))
DEFAULT_DASH_INTERVAL = int(os.getenv("WARERA_DASH_INTERVAL", "60"))  # seconds
PAGE_SIZE = 8

# ---------- Bot Setup ----------
intents = discord.Intents.default()
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
    input_json = json.dumps(params or {}, separators=(",", ":"))
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

# ---------- Embeds + Pagination ----------
MAX_DESC_CHARS = 2048
MAX_FIELD_CHARS = 1024

def safe_truncate(s: str, limit: int) -> str:
    return s if len(s) <= limit else s[:limit-3]+"..."

def make_embed(title: str, description: Optional[str] = None, color: discord.Color = discord.Color.blurple()) -> discord.Embed:
    desc = safe_truncate(description, MAX_DESC_CHARS) if description else None
    return discord.Embed(title=title, description=desc, timestamp=datetime.utcnow(), color=color)

def add_small_fields(embed: discord.Embed, d: Dict[str, Any], limit: int = 10):
    added = 0
    for k, v in d.items():
        if added >= limit: break
        vs = v if isinstance(v, (str,int,float)) else json.dumps(v, default=str)
        embed.add_field(name=str(k), value=safe_truncate(str(vs), MAX_FIELD_CHARS), inline=True)
        added += 1
    if len(d) > limit:
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
        self.prev.disabled = (self.current <= 0)
        self.next.disabled = (self.current >= self.max-1)
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

# ---------- Pretty Ranking Embed ----------
def pretty_ranking_embed(title:str, items:List[Dict], start_index:int=0) -> List[discord.Embed]:
    pages:List[discord.Embed] = []
    for i in range(0, len(items), PAGE_SIZE):
        chunk = items[i:i+PAGE_SIZE]
        e = make_embed(title, f"Showing {i+1}-{i+len(chunk)} of {len(items)}")
        for idx, entry in enumerate(chunk, start=1+i):
            tier = entry.get("tier","").lower()
            if tier.startswith("maste"): emoji="🥇"
            elif tier.startswith("gold"): emoji="🥈"
            elif tier.startswith("silv"): emoji="🥉"
            else: emoji="🏵️"
            uid = entry.get("user") or entry.get("id") or entry.get("name")
            uname = entry.get("name") or uid
            url = f"https://warera.io/profile/{uid}" if uid else "#"
            val = entry.get("value") or entry.get("score") or entry.get("damage") or 0
            val_s = f"{val:,}" if isinstance(val,int) else str(val)
            e.add_field(name=f"{emoji} #{idx}", value=f"[{uname}]({url}) — ⚔️ {val_s}", inline=False)
        pages.append(e)
    if not pages:
        pages.append(make_embed(title,"No data"))
    return pages

# ---------- Render Pages ----------
async def render_to_pages(endpoint:str, params:Optional[Dict]=None) -> List[discord.Embed]:
    data = await api_call(endpoint, params)
    title = f"📡 {endpoint}"
    if params: title += " " + json.dumps(params,separators=(",",":"))
    if data is None: return [make_embed(title,"❌ Failed to fetch data",discord.Color.red())]
    if isinstance(data,dict) and 'items' in data and isinstance(data['items'],list):
        return pretty_ranking_embed(title,data['items'])
    if isinstance(data,list):
        return pages_from_list(data,title)
    if isinstance(data,dict):
        e = make_embed(title)
        small = {}
        for k,v in data.items():
            if isinstance(v,(str,int,float)): small[k]=v
            elif isinstance(v,(list,dict)): small[k]=f"{type(v).__name__}({len(v)})"
            else: small[k]=str(v)
        add_small_fields(e,small,10)
        return [e]
    return [make_embed(title,safe_truncate(json.dumps(data,default=str),1024))]

def pages_from_list(lst:List[Any], title:str) -> List[discord.Embed]:
    pages=[]
    for i in range(0,len(lst),PAGE_SIZE):
        sub=lst[i:i+PAGE_SIZE]
        e=make_embed(title,f"Showing {i+1}-{i+len(sub)} of {len(lst)}")
        for j,item in enumerate(sub,1):
            if isinstance(item,dict):
                name=item.get("name") or item.get("id") or str(i+j)
                summary_parts=[]
                for k in ('id','country','damage','score','price','status','region','title','username'):
                    if k in item: summary_parts.append(f"{k}:{item[k]}")
                summ=", ".join(summary_parts) if summary_parts else json.dumps(item,default=str)[:80]
                e.add_field(name=f"#{i+j}",value=safe_truncate(summ,MAX_FIELD_CHARS),inline=False)
            else:
                e.add_field(name=f"#{i+j}",value=str(item)[:MAX_FIELD_CHARS],inline=False)
        pages.append(e)
    if not pages:
        pages.append(make_embed(title,"No data"))
    return pages

async def send_paginated(interaction: discord.Interaction, pages:List[discord.Embed]):
    view=PageView(pages)
    await interaction.response.send_message(embed=pages[0], view=view)

# Example ranking slash
@bot.tree.command(name="rankings", description="View WarEra ")
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
@app_commands.describe(ranking_type="Type of ranking")
async def (interaction: discord.Interaction, ranking_type: str):
    pages=await render_to_pages("ranking.getRanking", {"rankingType": ranking_type})
    await send_paginated(interaction, pages)

# ---------- Custom Lists ----------
CUSTOM_LISTS={
    "fav_countries":["CountryA","CountryB","CountryC"],
    "top_companies":["Comp1","Comp2","Comp3"]
}

@bot.tree.command(name="lists", description="View your custom lists")
async def lists(interaction: discord.Interaction):
    e=make_embed("📋 Custom Lists")
    for k,v in CUSTOM_LISTS.items():
        e.add_field(name=k.title(),value="\n".join(v),inline=False)
    await interaction.response.send_message(embed=e)

# ---------- Auto-refresh dashboard ----------
DASH_MESSAGE: Optional[discord.Message]=None
async def refresh_dashboard(channel:discord.TextChannel):
    global DASH_MESSAGE
    pages=await render_to_pages("ranking.getRanking",{"rankingType":"userDamages"})
    if DASH_MESSAGE is None:
        DASH_MESSAGE=await channel.send(embed=pages[0],view=PageView(pages))
    else:
        await DASH_MESSAGE.edit(embed=pages[0],view=PageView(pages))

@tasks.loop(seconds=DEFAULT_DASH_INTERVAL)
async def dash_loop():
    if DASH_CHANNEL_ID:=os.getenv("WARERA_DASH_CHANNEL"):
        ch=bot.get_channel(int(DASH_CHANNEL_ID))
        if ch: await refresh_dashboard(ch)

# ---------- Bot Events ----------
@bot.event
async def on_ready():
    print(f"Bot ready! Logged in as {bot.user}")
    try: await bot.tree.sync()
    except: pass
    dash_loop.start()

# ---------- Run Bot ----------
if __name__=="__main__":
    TOKEN=os.getenv("DISCORD_BOT_TOKEN","YOUR_TOKEN_HERE")
    if TOKEN=="YOUR_TOKEN_HERE":
        print("Set DISCORD_BOT_TOKEN environment variable")
    else:
        bot.run(TOKEN)
