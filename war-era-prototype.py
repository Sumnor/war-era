"""
WarEra Enhanced Bot — Comprehensive API Coverage
All endpoints from https://api2.warera.io/docs/# with beautiful formatting
"""

import os
import json
import asyncio
import aiohttp
import urllib.parse
from typing import Optional, Dict, Any, List, Tuple
from datetime import datetime, timezone
import discord
from discord import app_commands
from discord.ext import tasks, commands
from discord.ui import View, Button, Modal, TextInput
from dataclasses import dataclass, field

# ---------------- CONFIG ----------------
API_BASE = os.getenv("WARERA_API_BASE", "https://api2.warera.io/trpc")
DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "YOUR_TOKEN_HERE")
DASH_CHANNEL_ID = os.getenv("WARERA_DASH_CHANNEL")
ALERT_CHANNEL_ID = os.getenv("WARERA_ALERT_CHANNEL")
STATE_PATH = os.getenv("WARERA_STATE_PATH", "state_warera.json")
REQUEST_TIMEOUT = float(os.getenv("WARERA_REQUEST_TIMEOUT", "10"))
RETRY_ATTEMPTS = int(os.getenv("WARERA_RETRY_ATTEMPTS", "3"))
RETRY_BACKOFF = float(os.getenv("WARERA_RETRY_BACKOFF", "0.6"))
DEFAULT_DASH_INTERVAL = int(os.getenv("WARERA_DASH_INTERVAL", "60"))
PAGE_SIZE = int(os.getenv("WARERA_PAGE_SIZE", "8"))

# ---------------- URL MAP ----------------
URLS = {
    "user": "https://app.warera.io/user/",
    "company": "https://app.warera.io/company/",
    "country": "https://app.warera.io/country/",
    "region": "https://app.warera.io/region/",
    "mu": "https://app.warera.io/mu/",
    "battle": "https://app.warera.io/battle/",
    "work": "https://app.warera.io/market/work",
    "article": "https://app.warera.io/article/",
    "party": "https://app.warera.io/party/",
}

# Emojis for different categories
ICON_DAMAGE = "⚔️"
ICON_WEALTH = "💰"
ICON_MU = "🎖️"
ICON_COMPANY = "🏢"
ICON_COUNTRY = "🌍"
ICON_REGION = "🏔️"
ICON_USER = "👤"
ICON_BATTLE = "⚡"
ICON_PARTY = "🏛️"
ICON_ARTICLE = "📰"
ICON_LEVEL = "⭐"
ICON_PREMIUM = "💎"
ICON_REFERRAL = "🔗"
ICON_GROUND = "🌾"

# ---------------- BOT SETUP ----------------
intents = discord.Intents.default()
intents.message_content = False
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)
tree = bot.tree

_session: Optional[aiohttp.ClientSession] = None
async def get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT))
    return _session

# ---------------- UTIL ----------------
def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def safe_truncate(s: Optional[str], n: int) -> str:
    if s is None: return ""
    s = str(s)
    return s if len(s) <= n else s[:n-3] + "..."

def fmt_num(v: Any, decimals: int = 2) -> str:
    try:
        if isinstance(v, int):
            return f"{v:,}"
        if isinstance(v, float):
            return f"{v:,.{decimals}f}"
        fv = float(v)
        if abs(fv - int(fv)) < 1e-9:
            return f"{int(fv):,}"
        return f"{fv:,.{decimals}f}"
    except Exception:
        return str(v)

def format_date_iso(iso_s: str) -> str:
    try:
        dt = datetime.fromisoformat(iso_s.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc).strftime("📅 %Y-%m-%d %H:%M UTC")
    except Exception:
        return iso_s

def codeblock_json(obj: Any) -> str:
    try:
        j = json.dumps(obj, indent=2, default=str)
    except Exception:
        j = str(obj)
    if len(j) > 1900:
        j = j[:1897] + "..."
    return f"```json\n{j}\n```"

# ---------------- STATE ----------------
_state_lock = asyncio.Lock()
DEFAULT_STATE = {"alerts_subscribers": [], "monitor_prev": {}, "monitor_alerts": [], "dash_message": None}
state: Dict[str, Any] = {}

def load_state():
    global state
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                state = json.load(f)
        except Exception:
            state = DEFAULT_STATE.copy()
    else:
        state = DEFAULT_STATE.copy()

async def save_state():
    async with _state_lock:
        try:
            with open(STATE_PATH, "w", encoding="utf-8") as f:
                json.dump(state, f, default=str, indent=2)
        except Exception as e:
            print("[save_state]", e)

load_state()

# ---------------- WarEra API client ----------------
class WarEraAPI:
    def __init__(self, base: str = API_BASE):
        self.base = base.rstrip("/")

    def build_url(self, endpoint: str, params: Optional[Dict] = None) -> str:
        ep = endpoint.strip().lstrip("/")
        url = f"{self.base}/{ep}"
        input_json = json.dumps(params or {}, separators=(",", ":"))
        encoded = urllib.parse.quote(input_json, safe='')
        return f"{url}?input={encoded}"

    async def call(self, endpoint: str, params: Optional[Dict] = None) -> Optional[Any]:
        url = self.build_url(endpoint, params)
        sess = await get_session()
        exc = None
        for attempt in range(RETRY_ATTEMPTS):
            try:
                async with sess.get(url) as resp:
                    txt = await resp.text()
                    if resp.status != 200:
                        exc = Exception(f"HTTP {resp.status}: {txt[:200]}")
                        await asyncio.sleep(RETRY_BACKOFF * (2 ** attempt))
                        continue
                    d = json.loads(txt)
                    if isinstance(d, dict) and "result" in d:
                        res = d["result"]
                        if isinstance(res, dict) and "data" in res:
                            return res["data"]
                        return res
                    return d
            except Exception as e:
                exc = e
                await asyncio.sleep(RETRY_BACKOFF * (2 ** attempt))
        print(f"[WarEraAPI] failed {endpoint} {params}: {exc}")
        return None

war_api = WarEraAPI()

# ---------------- Entity helpers ----------------
def extract_avatar(obj: Dict[str,Any]) -> Optional[str]:
    for k in ("animatedAvatarUrl","avatarUrl","avatar","image","picture","flag"):
        v = obj.get(k)
        if isinstance(v,str) and v.startswith("http"):
            return v
    if isinstance(obj.get("user"), dict):
        return extract_avatar(obj["user"])
    if isinstance(obj.get("country"), dict):
        return extract_avatar(obj["country"])
    return None

def is_likely_id(s: Any) -> bool:
    return isinstance(s, str) and len(s) in (24,26,36)

def get_entity_icon(item: Dict[str,Any]) -> str:
    """Return appropriate emoji based on entity type"""
    if "companyId" in item or "company" in item:
        return ICON_COMPANY
    if "countryId" in item or "country" in item:
        return ICON_COUNTRY
    if "region" in item or "regionId" in item:
        return ICON_REGION
    if "members" in item or "muId" in item:
        return ICON_MU
    if "battleId" in item or "attacker" in item:
        return ICON_BATTLE
    if "partyId" in item or "party" in item:
        return ICON_PARTY
    if "articleId" in item or "article" in item:
        return ICON_ARTICLE
    return ICON_USER

def link_for_entity(item: Dict[str,Any]) -> Tuple[str, Optional[str], str]:
    """
    Returns (name_with_link, avatar_url, icon_emoji)
    """
    icon = get_entity_icon(item)
    
    # nested user object
    if isinstance(item.get("user"), dict):
        u = item["user"]
        uid = u.get("_id") or u.get("id")
        name = u.get("name") or u.get("username") or uid
        if uid:
            return (f"[{safe_truncate(name,40)}]({URLS['user']}{uid})", extract_avatar(u), ICON_USER)
    
    # direct user id
    if item.get("user") and is_likely_id(item.get("user")):
        uid = item.get("user")
        name = item.get("name") or item.get("username") or uid
        return (f"[{safe_truncate(name,40)}]({URLS['user']}{uid})", extract_avatar(item), ICON_USER)
    
    # company
    cid = item.get("companyId") or item.get("company")
    if cid and is_likely_id(cid):
        name = item.get("name") or item.get("title") or cid
        return (f"[{safe_truncate(name,40)}]({URLS['company']}{cid})", extract_avatar(item), ICON_COMPANY)
    
    # country
    cc = item.get("countryId") or item.get("country")
    if cc:
        name = item.get("name") or item.get("countryName") or cc
        return (f"[{safe_truncate(name,40)}]({URLS['country']}{cc})", extract_avatar(item), ICON_COUNTRY)
    
    # region
    rid = item.get("regionId") or item.get("region")
    if rid:
        name = item.get("name") or item.get("regionName") or rid
        return (f"[{safe_truncate(name,40)}]({URLS['region']}{rid})", extract_avatar(item), ICON_REGION)
    
    # mu
    if item.get("members") is not None or item.get("muId"):
        mid = item.get("muId") or item.get("_id") or item.get("id")
        name = item.get("name") or mid
        if mid:
            return (f"[{safe_truncate(name,40)}]({URLS['mu']}{mid})", extract_avatar(item), ICON_MU)
    
    # party
    pid = item.get("partyId") or item.get("party")
    if pid:
        name = item.get("name") or item.get("partyName") or pid
        return (f"[{safe_truncate(name,40)}]({URLS['party']}{pid})", extract_avatar(item), ICON_PARTY)
    
    # battle
    if item.get("battleId") or (item.get("_id") and "attacker" in item):
        bid = item.get("battleId") or item.get("_id") or item.get("id")
        name = item.get("title") or bid
        if bid:
            return (f"[{safe_truncate(name,40)}]({URLS['battle']}{bid})", extract_avatar(item), ICON_BATTLE)
    
    # article
    aid = item.get("articleId") or item.get("_id")
    if aid and is_likely_id(aid):
        name = item.get("title") or item.get("name") or aid
        return (f"[{safe_truncate(name,40)}]({URLS['article']}{aid})", extract_avatar(item), ICON_ARTICLE)
    
    # fallback: if _id looks like user id
    mid = item.get("_id") or item.get("id")
    if is_likely_id(mid):
        name = item.get("name") or item.get("title") or mid
        return (f"[{safe_truncate(name,40)}]({URLS['user']}{mid})", extract_avatar(item), icon)
    
    # final fallback
    name = item.get("name") or item.get("title") or str(mid)
    return (safe_truncate(name,40), extract_avatar(item), icon)

# ---------------- Make per-item embed ----------------
def make_item_embed(idx:int, total:int, name_markup:str, avatar:Optional[str], value_str:str, tier:Optional[str], endpoint_title:str, icon:str=ICON_DAMAGE, color:discord.Color=None) -> discord.Embed:
    title = f"#{idx} {icon} {name_markup}"
    desc = f"**Value:** {value_str}"
    if tier:
        desc += f"\n**Tier:** {safe_truncate(str(tier),100)}"
    
    if color is None:
        color = discord.Color.dark_gold() if idx <= 3 else (discord.Color.gold() if idx <= 10 else discord.Color.blue())
    
    emb = discord.Embed(title=title, description=desc, color=color, timestamp=now_utc())
    if avatar:
        try: 
            emb.set_thumbnail(url=avatar)
        except: 
            pass
    
    # footer: show page range
    start = ((idx-1)//PAGE_SIZE)*PAGE_SIZE + 1
    end = min(total, start + PAGE_SIZE - 1)
    emb.set_footer(text=f"Showing {start}-{end} of {total} • {endpoint_title}")
    return emb

# ---------------- Render endpoint to pages ----------------
async def render_endpoint_to_pages(endpoint:str, params:Optional[Dict]=None, title_override:str=None) -> Tuple[List[discord.Embed], List[str]]:
    data = await war_api.call(endpoint, params)
    display_title = title_override or endpoint
    
    if data is None:
        return [discord.Embed(title=display_title, description="❌ Failed to fetch data", color=discord.Color.red(), timestamp=now_utc())], [json.dumps({"error":"fetch failed"})]
    
    # Handle dict with list inside
    if isinstance(data, dict):
        for list_key in ("items","results","data","countries","regions","battles","companies","users"):
            if list_key in data and isinstance(data[list_key], list):
                items = data[list_key]
                return process_items_list(items, display_title)
        
        # Single object
        return process_single_object(data, display_title)
    
    # Handle list
    if isinstance(data, list):
        return process_items_list(data, display_title)
    
    # Primitive fallback
    return [discord.Embed(title=display_title, description=safe_truncate(str(data),1000), timestamp=now_utc())], [json.dumps(data, default=str)]

def process_items_list(items: List, title: str) -> Tuple[List[discord.Embed], List[str]]:
    """Process a list of items into embeds"""
    game_pages: List[discord.Embed] = []
    dev_json: List[str] = []
    total = len(items)
    
    for idx, it in enumerate(items, start=1):
        if isinstance(it, dict):
            name_link, avatar, icon = link_for_entity(it)
            
            # Try to extract value
            val = (it.get("value") or it.get("damage") or it.get("score") or 
                   it.get("wealth") or it.get("price") or it.get("population") or 
                   it.get("gdp") or it.get("treasury") or 0)
            val_s = fmt_num(val)
            
            # Try to extract tier/rank
            tier = it.get("tier") or it.get("rank") or it.get("title") or it.get("level")
            
            emb = make_item_embed(idx, total, name_link, avatar, val_s, tier, title, icon)
            game_pages.append(emb)
            dev_json.append(json.dumps(it, default=str))
        else:
            emb = discord.Embed(title=f"#{idx}", description=safe_truncate(str(it),1000), timestamp=now_utc())
            game_pages.append(emb)
            dev_json.append(json.dumps(it, default=str))
    
    if not game_pages:
        game_pages.append(discord.Embed(title=title, description="No data available", timestamp=now_utc()))
        dev_json.append("[]")
    
    return game_pages, dev_json

def process_single_object(data: Dict, title: str) -> Tuple[List[discord.Embed], List[str]]:
    """Process a single object into an embed"""
    e = discord.Embed(title=title, timestamp=now_utc(), color=discord.Color.blue())
    
    # Try to get name and link
    name_link, avatar, icon = link_for_entity(data)
    e.description = f"{icon} {name_link}"
    
    if avatar:
        try:
            e.set_thumbnail(url=avatar)
        except:
            pass
    
    # Add key fields
    small = {}
    for k, v in data.items():
        if isinstance(v, (str, int, float, bool)) and k not in ("_id", "id", "avatar", "avatarUrl", "animatedAvatarUrl"):
            small[k] = v
    
    for k, v in list(small.items())[:20]:
        e.add_field(name=str(k), value=safe_truncate(str(v), 256), inline=True)
    
    return [e], [json.dumps(data, default=str)]

# ---------------- Leaderboard View ----------------
class LeaderboardView(View):
    def __init__(self, pages: List[discord.Embed], dev_json: List[str], *, timeout:int=300):
        super().__init__(timeout=timeout)
        self.pages = pages
        self.dev_json = dev_json
        self.idx = 0
        self.mode = "game"
        
        self.prev = Button(emoji="◀️", style=discord.ButtonStyle.secondary)
        self.toggle = Button(label="🧠 Dev View", style=discord.ButtonStyle.primary)
        self.next = Button(emoji="▶️", style=discord.ButtonStyle.secondary)
        
        self.add_item(self.prev)
        self.add_item(self.toggle)
        self.add_item(self.next)
        
        self.prev.callback = self.on_prev
        self.next.callback = self.on_next
        self.toggle.callback = self.on_toggle
        self._update_buttons()

    def _update_buttons(self):
        length = len(self.pages) if self.mode == "game" else len(self.dev_json)
        self.prev.disabled = self.idx <= 0 or length <= 1
        self.next.disabled = self.idx >= (length - 1) or length <= 1
        self.toggle.label = "🧠 Dev View" if self.mode == "game" else "🎮 Game View"

    async def on_prev(self, interaction: discord.Interaction):
        await interaction.response.defer()
        self.idx = max(0, self.idx - 1)
        await self._refresh(interaction)

    async def on_next(self, interaction: discord.Interaction):
        await interaction.response.defer()
        length = len(self.pages) if self.mode == "game" else len(self.dev_json)
        self.idx = min(length - 1, self.idx + 1)
        await self._refresh(interaction)

    async def on_toggle(self, interaction: discord.Interaction):
        await interaction.response.defer()
        self.mode = "dev" if self.mode == "game" else "game"
        if self.mode == "dev" and (self.idx >= len(self.dev_json) or not self.dev_json):
            await interaction.followup.send("No dev JSON for this page.", ephemeral=True)
            self.mode = "game"
            return
        await self._refresh(interaction)

    async def _refresh(self, interaction: discord.Interaction):
        if self.mode == "game":
            emb = self.pages[self.idx]
        else:
            j = self.dev_json[self.idx] if self.idx < len(self.dev_json) else "{}"
            emb = discord.Embed(
                title=f"🧠 Dev JSON (page {self.idx+1})", 
                description=f"```json\n{j[:1800]}\n```", 
                timestamp=now_utc(), 
                color=discord.Color.dark_grey()
            )
        
        self._update_buttons()
        try:
            await interaction.followup.edit_message(interaction.message.id, embed=emb, view=self)
        except Exception:
            try:
                await interaction.response.edit_message(embed=emb, view=self)
            except Exception:
                pass

# ---------------- Safe defer ----------------
async def safe_defer(interaction: discord.Interaction, ephemeral: bool=False):
    if not interaction.response.is_done():
        try:
            await interaction.response.defer(ephemeral=ephemeral)
        except Exception:
            pass

# ---------------- Generic command helper ----------------
async def send_endpoint_pages(interaction: discord.Interaction, endpoint: str, params: Optional[Dict]=None, title: str=None):
    await safe_defer(interaction)
    pages, dev = await render_endpoint_to_pages(endpoint, params, title)
    view = LeaderboardView(pages, dev)
    await interaction.followup.send(embed=pages[0], view=view)

# ==================== SLASH COMMANDS ====================

@tree.command(name="help", description="📖 Show all available commands")
async def help_cmd(interaction: discord.Interaction):
    await safe_defer(interaction)
    e = discord.Embed(title="🎮 WarEra Bot — Command Guide", color=discord.Color.gold(), timestamp=now_utc())
    e.description = "Comprehensive WarEra game data at your fingertips!"
    
    # Rankings
    e.add_field(name="📊 Rankings", value=(
        "`/rankings` - View all ranking types\n"
        "`/topdamage` - Top damage dealers\n"
        "`/topwealth` - Wealthiest players\n"
        "`/topland` - Top land producers\n"
        "`/toplevel` - Highest level players\n"
        "`/topreferrals` - Top referrers"
    ), inline=False)
    
    # Countries
    e.add_field(name="🌍 Countries", value=(
        "`/countries` - List all countries\n"
        "`/country <id>` - Country details\n"
        "`/topcountries` - Top countries by GDP"
    ), inline=False)
    
    # Regions
    e.add_field(name="🏔️ Regions", value=(
        "`/regions` - List all regions\n"
        "`/region <id>` - Region details"
    ), inline=False)
    
    # Military
    e.add_field(name="⚔️ Military", value=(
        "`/battles` - Active battles\n"
        "`/battle <id>` - Battle details\n"
        "`/topmu` - Top military units"
    ), inline=False)
    
    # Economy
    e.add_field(name="💰 Economy", value=(
        "`/companies` - List companies\n"
        "`/company <id>` - Company details\n"
        "`/prices` - Item prices\n"
        "`/transactions` - Recent transactions\n"
        "`/workoffers` - Available jobs"
    ), inline=False)
    
    # Other
    e.add_field(name="🔧 Other", value=(
        "`/user <id>` - User profile\n"
        "`/articles` - Latest articles\n"
        "`/search <query>` - Search anything\n"
        "`/dashboard` - Live dashboard\n"
        "`/jsondebug` - Format JSON"
    ), inline=False)
    
    await interaction.followup.send(embed=e)

# ==================== RANKING COMMANDS ====================

RANKING_CHOICES = [
    app_commands.Choice(name=f"{ICON_DAMAGE} User Damage", value="userDamages"),
    app_commands.Choice(name=f"{ICON_DAMAGE} Weekly Damage", value="weeklyUserDamages"),
    app_commands.Choice(name=f"{ICON_WEALTH} Wealth", value="userWealth"),
    app_commands.Choice(name=f"{ICON_LEVEL} Level", value="userLevel"),
    app_commands.Choice(name=f"{ICON_REFERRAL} Referrals", value="userReferrals"),
    app_commands.Choice(name="👥 Subscribers", value="userSubscribers"),
    app_commands.Choice(name=f"{ICON_GROUND} Ground/Terrain", value="userTerrain"),
    app_commands.Choice(name=f"{ICON_PREMIUM} Premium Months", value="userPremiumMonths"),
    app_commands.Choice(name=f"{ICON_PREMIUM} Premium Gifts", value="userPremiumGifts"),
]

@tree.command(name="rankings", description="📊 View player rankings")
@app_commands.choices(ranking_type=RANKING_CHOICES)
async def rankings_cmd(interaction: discord.Interaction, ranking_type: app_commands.Choice[str]):
    await send_endpoint_pages(
        interaction, 
        "ranking.getRanking", 
        {"rankingType": ranking_type.value},
        f"📊 {ranking_type.name}"
    )

# Aggregated rankings
async def aggregate_users_from_ranking(ranking_type: str) -> List[Tuple[str, float]]:
    sums: Dict[str,float] = {}
    data = await war_api.call("ranking.getRanking", {"rankingType": ranking_type})
    items = []
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        items = data["items"]
    elif isinstance(data, list):
        items = data
    
    for it in items:
        if isinstance(it, dict):
            uid = it.get("user") or it.get("_id") or it.get("id")
            val = it.get("value") or it.get("damage") or it.get("score") or it.get("wealth") or 0
            if uid is None: 
                continue
            try: 
                sums[str(uid)] = sums.get(str(uid),0.0) + float(val)
            except: 
                pass
    return sorted(sums.items(), key=lambda kv: kv[1], reverse=True)

def ranking_list_to_pages(title:str, ranked:List[Tuple[str,float]], names_map:Optional[Dict[str,str]]=None) -> Tuple[List[discord.Embed], List[str]]:
    pages = []
    dev = []
    total = len(ranked)
    
    for idx,(uid,val) in enumerate(ranked, start=1):
        name = (names_map or {}).get(uid) or uid
        name_markup = f"[{safe_truncate(name,40)}]({URLS['user']}{uid})" if uid else safe_truncate(str(name),40)
        emb = make_item_embed(idx, total, name_markup, None, fmt_num(val), None, title, ICON_USER)
        pages.append(emb)
        dev.append(json.dumps({"user":uid,"value":val}, default=str))
        if idx >= 500: 
            break
    
    if not pages:
        pages.append(discord.Embed(title=title, description="No data", timestamp=now_utc()))
        dev.append("[]")
    return pages, dev

@tree.command(name="topdamage", description="⚔️ Top damage dealers (aggregated)")
async def topdamage_cmd(interaction: discord.Interaction):
    await safe_defer(interaction)
    ranked = await aggregate_users_from_ranking("userDamages")
    names_map={}
    for uid,_ in ranked[:50]:
        try:
            r = await war_api.call("user.getUserLite", {"userId":uid})
            if isinstance(r, dict): 
                names_map[uid] = r.get("name") or r.get("username") or uid
        except: 
            pass
    pages, dev = ranking_list_to_pages(f"{ICON_DAMAGE} Top Damage Dealers", ranked[:500], names_map)
    view = LeaderboardView(pages, dev)
    await interaction.followup.send(embed=pages[0], view=view)

@tree.command(name="topwealth", description="💰 Wealthiest players (aggregated)")
async def topwealth_cmd(interaction: discord.Interaction):
    await safe_defer(interaction)
    ranked = await aggregate_users_from_ranking("userWealth")
    names_map={}
    for uid,_ in ranked[:50]:
        try:
            r = await war_api.call("user.getUserLite", {"userId":uid})
            if isinstance(r, dict): 
                names_map[uid] = r.get("name") or r.get("username") or uid
        except: 
            pass
    pages, dev = ranking_list_to_pages(f"{ICON_WEALTH} Top Wealth", ranked[:500], names_map)
    view = LeaderboardView(pages, dev)
    await interaction.followup.send(embed=pages[0], view=view)

@tree.command(name="topland", description="🌾 Top land producers (aggregated)")
async def topland_cmd(interaction: discord.Interaction):
    await safe_defer(interaction)
    ranked = await aggregate_users_from_ranking("userTerrain")
    names_map={}
    for uid,_ in ranked[:50]:
        try:
            r = await war_api.call("user.getUserLite", {"userId":uid})
            if isinstance(r, dict): 
                names_map[uid] = r.get("name") or r.get("username") or uid
        except: 
            pass
    pages, dev = ranking_list_to_pages(f"{ICON_GROUND} Top Land Producers", ranked[:500], names_map)
    view = LeaderboardView(pages, dev)
    await interaction.followup.send(embed=pages[0], view=view)

@tree.command(name="toplevel", description="⭐ Highest level players")
async def toplevel_cmd(interaction: discord.Interaction):
    await safe_defer(interaction)
    ranked = await aggregate_users_from_ranking("userLevel")
    names_map={}
    for uid,_ in ranked[:50]:
        try:
            r = await war_api.call("user.getUserLite", {"userId":uid})
            if isinstance(r, dict): 
                names_map[uid] = r.get("name") or r.get("username") or uid
        except: 
            pass
    pages, dev = ranking_list_to_pages(f"{ICON_LEVEL} Highest Levels", ranked[:500], names_map)
    view = LeaderboardView(pages, dev)
    await interaction.followup.send(embed=pages[0], view=view)

@tree.command(name="topreferrals", description="🔗 Top referrers")
async def topreferrals_cmd(interaction: discord.Interaction):
    await safe_defer(interaction)
    ranked = await aggregate_users_from_ranking("userReferrals")
    names_map={}
    for uid,_ in ranked[:50]:
        try:
            r = await war_api.call("user.getUserLite", {"userId":uid})
            if isinstance(r, dict): 
                names_map[uid] = r.get("name") or r.get("username") or uid
        except: 
            pass
    pages, dev = ranking_list_to_pages(f"{ICON_REFERRAL} Top Referrers", ranked[:500], names_map)
    view = LeaderboardView(pages, dev)
    await interaction.followup.send(embed=pages[0], view=view)

# ==================== COUNTRY COMMANDS ====================

@tree.command(name="countries", description="🌍 List all countries")
async def countries_cmd(interaction: discord.Interaction):
    await send_endpoint_pages(interaction, "country.getAllCountries", None, "🌍 All Countries")

@tree.command(name="country", description="🌍 Get country details")
@app_commands.describe(country_id="Country ID or name")
async def country_cmd(interaction: discord.Interaction, country_id: str):
    await send_endpoint_pages(interaction, "country.getCountryById", {"countryId": country_id}, f"🌍 Country: {country_id}")

@tree.command(name="topcountries", description="🏆 Top countries by GDP/Treasury")
async def topcountries_cmd(interaction: discord.Interaction):
    await safe_defer(interaction)
    data = await war_api.call("country.getAllCountries")
    
    countries = []
    if isinstance(data, dict) and "countries" in data:
        countries = data["countries"]
    elif isinstance(data, list):
        countries = data
    
    # Score by GDP + treasury
    scored = []
    for c in countries:
        if isinstance(c, dict):
            gdp = c.get("gdp") or 0
            treasury = c.get("treasury") or 0
            try:
                score = float(gdp) + float(treasury)
                scored.append((c, score))
            except:
                pass
    
    scored.sort(key=lambda x: x[1], reverse=True)
    
    pages = []
    dev = []
    total = len(scored)
    
    for idx, (country, score) in enumerate(scored, start=1):
        name = country.get("name") or country.get("_id") or str(idx)
        cid = country.get("_id") or country.get("countryId")
        link = f"[{safe_truncate(name,40)}]({URLS['country']}{cid})" if cid else safe_truncate(name,40)
        avatar = extract_avatar(country)
        
        gdp = fmt_num(country.get("gdp") or 0)
        treasury = fmt_num(country.get("treasury") or 0)
        population = fmt_num(country.get("population") or 0)
        
        tier = f"GDP: {gdp} | Treasury: {treasury} | Pop: {population}"
        
        emb = make_item_embed(idx, total, link, avatar, fmt_num(score), tier, "Top Countries", ICON_COUNTRY)
        pages.append(emb)
        dev.append(json.dumps(country, default=str))
        
        if idx >= 100:
            break
    
    if not pages:
        pages.append(discord.Embed(title="Top Countries", description="No data", timestamp=now_utc()))
        dev.append("[]")
    
    view = LeaderboardView(pages, dev)
    await interaction.followup.send(embed=pages[0], view=view)

# ==================== REGION COMMANDS ====================

@tree.command(name="regions", description="🏔️ List all regions")
async def regions_cmd(interaction: discord.Interaction):
    await send_endpoint_pages(interaction, "region.getRegionsObject", None, "🏔️ All Regions")

@tree.command(name="region", description="🏔️ Get region details")
@app_commands.describe(region_id="Region ID")
async def region_cmd(interaction: discord.Interaction, region_id: str):
    await send_endpoint_pages(interaction, "region.getById", {"regionId": region_id}, f"🏔️ Region: {region_id}")

# ==================== MILITARY UNIT COMMANDS ====================

@tree.command(name="topmu", description="🎖️ Top military units")
async def topmu_cmd(interaction: discord.Interaction):
    await safe_defer(interaction)
    res = await war_api.call("mu.getManyPaginated", {"page":1,"limit":200})
    
    items = []
    if isinstance(res, dict) and isinstance(res.get("items"), list): 
        items = res["items"]
    elif isinstance(res, list): 
        items = res
    
    scored = []
    for mu in items:
        if not isinstance(mu, dict): 
            continue
        members = len(mu.get("members", []))
        invest = mu.get("investedMoneyByUsers") or {}
        tot = 0.0
        if isinstance(invest, dict):
            for v in invest.values():
                try: 
                    tot += float(v)
                except: 
                    pass
        score = tot if tot > 0 else members
        scored.append((mu, score))
    
    scored.sort(key=lambda x: x[1], reverse=True)
    
    pages = []
    dev = []
    for idx, (mu, score) in enumerate(scored, start=1):
        name = mu.get("name") or mu.get("_id") or str(idx)
        mid = mu.get("_id") or mu.get("id")
        link = f"[{safe_truncate(name,40)}]({URLS['mu']}{mid})" if mid else safe_truncate(name,40)
        avatar = extract_avatar(mu)
        
        members = len(mu.get("members", []))
        tier = f"Members: {members}"
        
        emb = make_item_embed(idx, len(scored), link, avatar, fmt_num(score), tier, "Top Military Units", ICON_MU)
        pages.append(emb)
        dev.append(json.dumps(mu, default=str))
        
        if idx >= 200: 
            break
    
    if not pages:
        pages.append(discord.Embed(title="Top MUs", description="No data", timestamp=now_utc()))
        dev.append("[]")
    
    view = LeaderboardView(pages, dev)
    await interaction.followup.send(embed=pages[0], view=view)

@tree.command(name="mu", description="🎖️ List military units")
async def mu_cmd(interaction: discord.Interaction):
    await send_endpoint_pages(interaction, "mu.getManyPaginated", {"page":1,"limit":50}, "🎖️ Military Units")

@tree.command(name="mu_details", description="🎖️ Get MU details by ID")
@app_commands.describe(mu_id="Military Unit ID")
async def mu_details_cmd(interaction: discord.Interaction, mu_id: str):
    await send_endpoint_pages(interaction, "mu.getById", {"muId": mu_id}, f"🎖️ MU: {mu_id}")

# ==================== BATTLE COMMANDS ====================

@tree.command(name="battles", description="⚔️ View active battles")
async def battles_cmd(interaction: discord.Interaction):
    await send_endpoint_pages(interaction, "battle.getBattles", None, "⚔️ Active Battles")

@tree.command(name="battle", description="⚔️ Get battle details")
@app_commands.describe(battle_id="Battle ID")
async def battle_cmd(interaction: discord.Interaction, battle_id: str):
    await send_endpoint_pages(interaction, "battle.getById", {"battleId": battle_id}, f"⚔️ Battle: {battle_id}")

# ==================== COMPANY COMMANDS ====================

@tree.command(name="companies", description="🏢 List companies")
async def companies_cmd(interaction: discord.Interaction):
    await send_endpoint_pages(interaction, "company.getCompanies", {"page":1,"limit":50}, "🏢 Companies")

@tree.command(name="company", description="🏢 Get company details")
@app_commands.describe(company_id="Company ID")
async def company_cmd(interaction: discord.Interaction, company_id: str):
    await send_endpoint_pages(interaction, "company.getById", {"companyId": company_id}, f"🏢 Company: {company_id}")

# ==================== ECONOMY COMMANDS ====================

@tree.command(name="prices", description="💰 View item prices")
async def prices_cmd(interaction: discord.Interaction):
    await safe_defer(interaction)
    data = await war_api.call("itemTrading.getPrices")
    
    e = discord.Embed(title="💰 Item Prices", color=discord.Color.gold(), timestamp=now_utc())
    
    if isinstance(data, dict):
        items = sorted(data.items(), key=lambda x: float(x[1]) if isinstance(x[1], (int, float)) else 0, reverse=True)
        for k, v in items[:25]:
            e.add_field(name=safe_truncate(str(k), 30), value=fmt_num(v), inline=True)
    else:
        e.description = safe_truncate(str(data), 1000)
    
    dev_json = [json.dumps(data, default=str)]
    view = LeaderboardView([e], dev_json)
    await interaction.followup.send(embed=e, view=view)

@tree.command(name="transactions", description="💸 Recent transactions")
async def transactions_cmd(interaction: discord.Interaction):
    await send_endpoint_pages(interaction, "transaction.getPaginatedTransactions", {"page":1,"limit":50}, "💸 Transactions")

@tree.command(name="workoffers", description="💼 Available work offers")
async def workoffers_cmd(interaction: discord.Interaction):
    await send_endpoint_pages(interaction, "workOffer.getWorkOffersPaginated", {"page":1,"limit":50}, "💼 Work Offers")

@tree.command(name="workoffer", description="💼 Get work offer details")
@app_commands.describe(offer_id="Work offer ID")
async def workoffer_cmd(interaction: discord.Interaction, offer_id: str):
    await send_endpoint_pages(interaction, "workOffer.getById", {"workOfferId": offer_id}, f"💼 Work Offer: {offer_id}")

# ==================== USER COMMANDS ====================

@tree.command(name="user", description="👤 Get user profile")
@app_commands.describe(user_id="User ID")
async def user_cmd(interaction: discord.Interaction, user_id: str):
    await send_endpoint_pages(interaction, "user.getUserLite", {"userId": user_id}, f"👤 User: {user_id}")

# ==================== ARTICLE COMMANDS ====================

@tree.command(name="articles", description="📰 Latest articles")
async def articles_cmd(interaction: discord.Interaction):
    await send_endpoint_pages(interaction, "article.getArticlesPaginated", {"page":1,"limit":50}, "📰 Latest Articles")

@tree.command(name="article", description="📰 Get article by ID")
@app_commands.describe(article_id="Article ID")
async def article_cmd(interaction: discord.Interaction, article_id: str):
    await send_endpoint_pages(interaction, "article.getArticleById", {"articleId": article_id}, f"📰 Article: {article_id}")

# ==================== SEARCH COMMAND ====================

@tree.command(name="search", description="🔍 Search anything")
@app_commands.describe(query="Search query")
async def search_cmd(interaction: discord.Interaction, query: str):
    await send_endpoint_pages(interaction, "search.searchAnything", {"searchText": query}, f"🔍 Search: {query}")

# ==================== JSON DEBUG ====================

class JsonModal(Modal):
    def __init__(self):
        super().__init__(title="Paste JSON")
        self.input = TextInput(label="JSON", style=discord.TextStyle.long, required=True, max_length=4000)
        self.add_item(self.input)
    
    async def on_submit(self, interaction: discord.Interaction):
        await safe_defer(interaction, ephemeral=True)
        try:
            parsed = json.loads(self.input.value)
            text = json.dumps(parsed, indent=2, default=str)
            if len(text) > 1900: 
                text = text[:1897] + "..."
            await interaction.followup.send(f"```json\n{text}\n```", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Invalid JSON: {e}", ephemeral=True)

@tree.command(name="jsondebug", description="🧪 Format JSON code")
async def jsondebug_cmd(interaction: discord.Interaction):
    modal = JsonModal()
    await interaction.response.send_modal(modal)

# ==================== MONITOR & ALERTS ====================

@dataclass
class Alert:
    ts: str
    level: str
    category: str
    title: str
    message: str
    data: Dict = field(default_factory=dict)

class Monitor:
    def __init__(self, api: WarEraAPI):
        self.api = api
        self.prev = state.get("monitor_prev", {})
        self.alerts = state.get("monitor_alerts", [])
        self.running = False
        self.interval = DEFAULT_DASH_INTERVAL
        self.price_threshold = 20.0
        self.critical = 50.0

    async def scan_once(self) -> List[Alert]:
        out: List[Alert] = []
        
        # Price changes
        prices = await self.api.call("itemTrading.getPrices")
        prev_prices = self.prev.get("itemTrading.getPrices")
        if isinstance(prices, dict) and isinstance(prev_prices, dict):
            for k, v in prices.items():
                if isinstance(v, (int, float)) and isinstance(prev_prices.get(k), (int, float)):
                    old = prev_prices.get(k)
                    if old != 0:
                        change = ((v - old) / abs(old)) * 100.0
                        if abs(change) >= self.price_threshold:
                            lvl = "CRITICAL" if abs(change) >= self.critical else "WARNING"
                            out.append(Alert(
                                now_utc().isoformat(), 
                                lvl, 
                                "ECONOMY", 
                                f"Price {k}", 
                                f"{fmt_num(old)} → {fmt_num(v)} ({change:+.2f}%)", 
                                {"old": old, "new": v, "pct": change}
                            ))
        
        # Battle changes
        battles = await self.api.call("battle.getBattles")
        prev_battles = self.prev.get("battle.getBattles")
        if isinstance(battles, list) and isinstance(prev_battles, list):
            if len(battles) > len(prev_battles):
                out.append(Alert(
                    now_utc().isoformat(), 
                    "WARNING", 
                    "BATTLE", 
                    "New battles", 
                    f"+{len(battles) - len(prev_battles)} battles started"
                ))
        
        # Ranking changes
        ranking = await self.api.call("ranking.getRanking", {"rankingType": "userDamages"})
        prev_rank = self.prev.get("ranking.getRanking.userDamages")
        try:
            new_top = (ranking.get("items") or [None])[0] if isinstance(ranking, dict) else None
            old_top = (prev_rank.get("items") or [None])[0] if isinstance(prev_rank, dict) else None
            if isinstance(new_top, dict) and isinstance(old_top, dict) and new_top.get("_id") != old_top.get("_id"):
                old_name = old_top.get("name") or old_top.get("user") or old_top.get("_id")
                new_name = new_top.get("name") or new_top.get("user") or new_top.get("_id")
                out.append(Alert(
                    now_utc().isoformat(), 
                    "INFO", 
                    "RANKING", 
                    "Top damage changed", 
                    f"{old_name} → {new_name}"
                ))
        except Exception:
            pass

        # Persist
        self.prev["itemTrading.getPrices"] = prices if prices is not None else prev_prices
        self.prev["battle.getBattles"] = battles if battles is not None else prev_battles
        self.prev["ranking.getRanking.userDamages"] = ranking if ranking is not None else prev_rank
        state["monitor_prev"] = self.prev
        
        for a in out:
            state_alert = {
                "ts": a.ts, 
                "level": a.level, 
                "category": a.category, 
                "title": a.title, 
                "message": a.message, 
                "data": a.data
            }
            self.alerts.insert(0, state_alert)
        
        state["monitor_alerts"] = self.alerts[:400]
        await save_state()
        return out

monitor = Monitor(war_api)

@tasks.loop(seconds=DEFAULT_DASH_INTERVAL)
async def monitor_loop():
    try:
        if monitor.interval != monitor_loop.seconds:
            monitor_loop.change_interval(seconds=monitor.interval)
    except Exception:
        pass
    
    if not monitor.running:
        return
    
    try:
        alerts = await monitor.scan_once()
        if alerts:
            # Post to alert channel
            if ALERT_CHANNEL_ID:
                ch = bot.get_channel(int(ALERT_CHANNEL_ID))
                if ch:
                    summary = f"**🚨 WarEra Monitor — {len(alerts)} alerts**\n"
                    by_cat = {}
                    for a in alerts:
                        by_cat.setdefault(a.category, 0)
                        by_cat[a.category] += 1
                    for c, cnt in by_cat.items(): 
                        summary += f"• {c}: {cnt}\n"
                    await ch.send(summary)
                    
                    for a in alerts[:12]:
                        color = (discord.Color.red() if a.level == "CRITICAL" else 
                                (discord.Color.gold() if a.level == "WARNING" else discord.Color.blue()))
                        emb = discord.Embed(
                            title=f"{a.level} {a.category} — {a.title}", 
                            description=a.message, 
                            timestamp=datetime.fromisoformat(a.ts), 
                            color=color
                        )
                        for k, v in (a.data or {}).items():
                            emb.add_field(name=str(k), value=safe_truncate(json.dumps(v, default=str), 256), inline=True)
                        await ch.send(embed=emb)
            
            # DM subscribers
            subs = state.get("alerts_subscribers", [])
            for a in alerts:
                for uid in subs:
                    try:
                        user = await bot.fetch_user(int(uid))
                        if user:
                            await user.send(f"🚨 {a.title}: {a.message}")
                    except Exception:
                        pass
    except Exception as e:
        print("[monitor_loop] error:", e)

@tree.command(name="alerts", description="🔔 Manage alert subscriptions")
@app_commands.describe(action="subscribe, unsubscribe, or list")
@app_commands.choices(action=[
    app_commands.Choice(name="Subscribe", value="subscribe"),
    app_commands.Choice(name="Unsubscribe", value="unsubscribe"),
    app_commands.Choice(name="List Subscribers", value="list"),
])
async def alerts_cmd(interaction: discord.Interaction, action: app_commands.Choice[str]):
    await safe_defer(interaction, ephemeral=True)
    uid = str(interaction.user.id)
    subs = state.get("alerts_subscribers", [])
    
    if action.value == "subscribe":
        if uid in subs:
            await interaction.followup.send("You are already subscribed to alerts.", ephemeral=True)
            return
        subs.append(uid)
        state["alerts_subscribers"] = subs
        await save_state()
        await interaction.followup.send("✅ Subscribed to alerts (DM).", ephemeral=True)
        return
    
    if action.value == "unsubscribe":
        if uid in subs:
            subs.remove(uid)
            state["alerts_subscribers"] = subs
            await save_state()
            await interaction.followup.send("✅ Unsubscribed from alerts.", ephemeral=True)
            return
        await interaction.followup.send("You were not subscribed.", ephemeral=True)
        return
    
    if action.value == "list":
        await interaction.followup.send(f"📊 Total subscribers: {len(subs)}", ephemeral=True)
        return

# ==================== DASHBOARD ====================

class IntervalModal(Modal):
    def __init__(self):
        super().__init__(title="Set Refresh Interval")
        self.input = TextInput(
            label="Seconds", 
            placeholder=str(DEFAULT_DASH_INTERVAL), 
            required=True,
            min_length=1,
            max_length=5
        )
        self.add_item(self.input)
    
    async def on_submit(self, interaction: discord.Interaction):
        val = self.input.value.strip()
        try:
            sec = int(val)
            if sec < 5: 
                raise ValueError("Minimum interval is 5 seconds")
            monitor.interval = sec
            if monitor_loop.is_running(): 
                monitor_loop.change_interval(seconds=sec)
            if dash_loop.is_running(): 
                dash_loop.change_interval(seconds=sec)
            state["dash_interval"] = sec
            await save_state()
            await interaction.response.send_message(f"✅ Interval set to {sec}s", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"❌ Error: {e}", ephemeral=True)

class DashboardControls(View):
    def __init__(self):
        super().__init__(timeout=None)
        self.start = Button(label="▶️ Start", style=discord.ButtonStyle.success)
        self.stop = Button(label="⏸️ Stop", style=discord.ButtonStyle.danger)
        self.refresh = Button(label="🔁 Refresh", style=discord.ButtonStyle.secondary)
        self.interval = Button(label="⏱️ Interval", style=discord.ButtonStyle.secondary)
        self.clear = Button(label="🧹 Clear", style=discord.ButtonStyle.secondary)
        
        self.add_item(self.start)
        self.add_item(self.stop)
        self.add_item(self.refresh)
        self.add_item(self.interval)
        self.add_item(self.clear)
        
        self.start.callback = self.on_start
        self.stop.callback = self.on_stop
        self.refresh.callback = self.on_refresh
        self.interval.callback = self.on_interval
        self.clear.callback = self.on_clear

    async def on_start(self, interaction: discord.Interaction):
        monitor.running = True
        if not monitor_loop.is_running(): 
            monitor_loop.start()
        await interaction.response.send_message("✅ Monitor started", ephemeral=True)

    async def on_stop(self, interaction: discord.Interaction):
        monitor.running = False
        await interaction.response.send_message("⏸️ Monitor stopped", ephemeral=True)

    async def on_refresh(self, interaction: discord.Interaction):
        alerts = await monitor.scan_once()
        await interaction.response.send_message(f"✅ Scanned: {len(alerts)} new alerts", ephemeral=True)

    async def on_interval(self, interaction: discord.Interaction):
        await interaction.response.send_modal(IntervalModal())

    async def on_clear(self, interaction: discord.Interaction):
        monitor.alerts.clear()
        state["monitor_alerts"] = []
        await save_state()
        await interaction.response.send_message("✅ Alerts cleared", ephemeral=True)

@tree.command(name="dashboard", description="📊 Create/update live dashboard")
async def dashboard_cmd(interaction: discord.Interaction):
    await safe_defer(interaction)
    
    # Fetch data
    rank_pages, _ = await render_endpoint_to_pages("ranking.getRanking", {"rankingType": "userDamages"})
    rank_embed = rank_pages[0] if rank_pages else discord.Embed(title="Rankings", timestamp=now_utc())
    
    prices = await war_api.call("itemTrading.getPrices")
    pe = discord.Embed(title="💰 Item Prices", color=discord.Color.gold(), timestamp=now_utc())
    if isinstance(prices, dict):
        items = sorted(prices.items(), key=lambda x: float(x[1]) if isinstance(x[1], (int, float)) else 0, reverse=True)
        for k, v in items[:12]:
            pe.add_field(name=safe_truncate(str(k), 24), value=fmt_num(v), inline=True)
    
    battles = await war_api.call("battle.getBattles")
    be = discord.Embed(title="⚔️ Active Battles", color=discord.Color.red(), timestamp=now_utc())
    if isinstance(battles, list):
        for b in battles[:8]:
            if isinstance(b, dict):
                a = b.get("attackerCountry") or b.get("attacker") or "?"
                d = b.get("defenderCountry") or b.get("defender") or "?"
                s = b.get("status") or b.get("phase") or "Active"
                be.add_field(name=f"{a} vs {d}", value=safe_truncate(str(s), 50), inline=False)
    
    alerts_embed = discord.Embed(title="🚨 Recent Alerts", color=discord.Color.orange(), timestamp=now_utc())
    recent_alerts = monitor.alerts[:6]
    if recent_alerts:
        for a in recent_alerts:
            alerts_embed.add_field(
                name=f"{a.get('level', '')} {a.get('category', '')}", 
                value=safe_truncate(a.get("message", ""), 80), 
                inline=False
            )
    else:
        alerts_embed.description = "No recent alerts"

    game_pages = [rank_embed, pe, be, alerts_embed]
    dev_pages = [
        json.dumps({"endpoint": "ranking.getRanking"}), 
        json.dumps(prices or {}, default=str), 
        json.dumps(battles or {}, default=str), 
        json.dumps(recent_alerts or {}, default=str)
    ]
    
    view = LeaderboardView(game_pages, dev_pages)
    controls = DashboardControls()

    channel = bot.get_channel(int(DASH_CHANNEL_ID)) if DASH_CHANNEL_ID else interaction.channel
    if channel is None:
        await interaction.followup.send("❌ Dashboard channel not configured. Set WARERA_DASH_CHANNEL.", ephemeral=True)
        return
    
    dash = state.get("dash_message")
    posted = None
    
    if dash:
        try:
            msg = await channel.fetch_message(int(dash["message_id"]))
            await msg.edit(embed=game_pages[0], view=view)
            posted = msg
        except Exception:
            posted = await channel.send(embed=game_pages[0], view=view)
            state["dash_message"] = {"channel_id": channel.id, "message_id": posted.id}
            await save_state()
    else:
        posted = await channel.send(embed=game_pages[0], view=view)
        state["dash_message"] = {"channel_id": channel.id, "message_id": posted.id}
        await save_state()
    
    try:
        await interaction.followup.send("⚙️ Dashboard controls:", view=controls, ephemeral=True)
    except Exception:
        pass
    
    if not dash_loop.is_running(): 
        dash_loop.start()
    
    await interaction.followup.send("✅ Dashboard created/updated!", ephemeral=True)

@tasks.loop(seconds=DEFAULT_DASH_INTERVAL)
async def dash_loop():
    dash = state.get("dash_message")
    if not dash: 
        return
    
    try:
        ch = bot.get_channel(int(dash["channel_id"]))
        if ch is None: 
            return
        msg = await ch.fetch_message(int(dash["message_id"]))
        
        # Update embeds
        rank_pages, _ = await render_endpoint_to_pages("ranking.getRanking", {"rankingType": "userDamages"})
        rank_embed = rank_pages[0] if rank_pages else discord.Embed(title="Rankings", timestamp=now_utc())
        
        prices = await war_api.call("itemTrading.getPrices")
        pe = discord.Embed(title="💰 Item Prices", color=discord.Color.gold(), timestamp=now_utc())
        if isinstance(prices, dict):
            items = sorted(prices.items(), key=lambda x: float(x[1]) if isinstance(x[1], (int, float)) else 0, reverse=True)
            for k, v in items[:12]:
                pe.add_field(name=safe_truncate(str(k), 24), value=fmt_num(v), inline=True)
        
        battles = await war_api.call("battle.getBattles")
        be = discord.Embed(title="⚔️ Active Battles", color=discord.Color.red(), timestamp=now_utc())
        if isinstance(battles, list):
            for b in battles[:8]:
                if isinstance(b, dict):
                    a = b.get("attackerCountry") or b.get("attacker") or "?"
                    d = b.get("defenderCountry") or b.get("defender") or "?"
                    s = b.get("status") or b.get("phase") or "Active"
                    be.add_field(name=f"{a} vs {d}", value=safe_truncate(str(s), 50), inline=False)
        
        alerts_embed = discord.Embed(title="🚨 Recent Alerts", color=discord.Color.orange(), timestamp=now_utc())
        recent_alerts = monitor.alerts[:6]
        if recent_alerts:
            for a in recent_alerts:
                alerts_embed.add_field(
                    name=f"{a.get('level', '')} {a.get('category', '')}", 
                    value=safe_truncate(a.get("message", ""), 80), 
                    inline=False
                )
        else:
            alerts_embed.description = "No recent alerts"
        
        pages = [rank_embed, pe, be, alerts_embed]
        dev_pages = [
            json.dumps({"endpoint": "ranking"}), 
            json.dumps(prices or {}, default=str), 
            json.dumps(battles or {}, default=str), 
            json.dumps(recent_alerts or {}, default=str)
        ]
        
        view = LeaderboardView(pages, dev_pages)
        await msg.edit(embed=pages[0], view=view)
    except Exception as e:
        print("[dash_loop] error:", e)

# ==================== BOT LIFECYCLE ====================

@bot.event
async def on_ready():
    print(f"✅ WarEra Bot logged in as {bot.user} (ID: {bot.user.id})")
    print(f"📊 Serving {len(bot.guilds)} guild(s)")
    
    try:
        synced = await tree.sync()
        print(f"✅ Synced {len(synced)} slash command(s)")
    except Exception as e:
        print(f"❌ Failed to sync commands: {e}")
    
    # Load monitor state
    monitor.prev = state.get("monitor_prev", {})
    monitor.alerts = state.get("monitor_alerts", [])
    
    # Start loops
    if not monitor_loop.is_running(): 
        monitor_loop.start()
        print("✅ Monitor loop started")
    
    if state.get("dash_message") and not dash_loop.is_running(): 
        dash_loop.start()
        print("✅ Dashboard loop started")
    
    print("=" * 50)
    print("🎮 WarEra Bot is ready!")
    print("=" * 50)

@bot.event
async def on_error(event, *args, **kwargs):
    print(f"❌ Error in {event}: {args} {kwargs}")

# ==================== MAIN ====================

if __name__ == "__main__":
    if DISCORD_TOKEN == "YOUR_TOKEN_HERE":
        print("=" * 50)
        print("❌ ERROR: Discord bot token not configured!")
        print("=" * 50)
        print("Please set the DISCORD_BOT_TOKEN environment variable.")
        print("Example: export DISCORD_BOT_TOKEN='your_token_here'")
        print("=" * 50)
    else:
        print("=" * 50)
        print("🚀 Starting WarEra Discord Bot...")
        print("=" * 50)
        print(f"📍 API Base: {API_BASE}")
        print(f"📁 State Path: {STATE_PATH}")
        print(f"⏱️  Default Interval: {DEFAULT_DASH_INTERVAL}s")
        print(f"📄 Page Size: {PAGE_SIZE}")
        if DASH_CHANNEL_ID:
            print(f"📊 Dashboard Channel: {DASH_CHANNEL_ID}")
        if ALERT_CHANNEL_ID:
            print(f"🚨 Alert Channel: {ALERT_CHANNEL_ID}")
        print("=" * 50)
        
        try:
            bot.run(DISCORD_TOKEN)
        except KeyboardInterrupt:
            print("\n👋 Bot shutdown requested")
        except Exception as e:
            print(f"\n❌ Fatal error: {e}")
        finally:
            print("✅ Bot stopped")
