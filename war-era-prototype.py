# warera_bot_final.py
"""
WarEra full bot — single file.
Slash-only commands (bot.tree). Includes:
- Pretty leaderboards (one embed per item)
- Correct per-entity links & avatars
- /jsondebug returns formatted JSON code block
- Dashboard auto-refresh + controls
- Alerts (channel + DM subscribers)
- Many endpoints covered from API docs
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
}

ICON_DAMAGE = "⚔️"
ICON_WEALTH = "💰"
ICON_MU = "🎖️"
ICON_COMPANY = "🏢"
ICON_COUNTRY = "🌍"
ICON_REGION = "🏔️"

# ---------------- BOT SETUP ----------------
intents = discord.Intents.default()
intents.message_content = False
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)
tree = bot.tree

# aiohttp session
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

def fmt_num(v: Any, decimals: int = 3) -> str:
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
            print("[save_state] ", e)

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

# ---------------- Monitor / Alerts ----------------
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
        # example price change detection
        prices = await self.api.call("itemTrading.getPrices")
        prev_prices = self.prev.get("itemTrading.getPrices")
        if isinstance(prices, dict) and isinstance(prev_prices, dict):
            for k,v in prices.items():
                if isinstance(v,(int,float)) and isinstance(prev_prices.get(k),(int,float)):
                    old = prev_prices.get(k)
                    if old != 0:
                        change = ((v-old)/abs(old))*100.0
                        if abs(change) >= self.price_threshold:
                            lvl = "CRITICAL" if abs(change) >= self.critical else "WARNING"
                            out.append(Alert(now_utc().isoformat(), lvl, "ECONOMY", f"Price {k}", f"{fmt_num(old)} -> {fmt_num(v)} ({change:+.2f}%)", {"old":old,"new":v,"pct":change}))
        # battles increase
        battles = await self.api.call("battle.getBattles")
        prev_battles = self.prev.get("battle.getBattles")
        if isinstance(battles, list) and isinstance(prev_battles, list):
            if len(battles) > len(prev_battles):
                out.append(Alert(now_utc().isoformat(), "WARNING", "BATTLE", "New battles", f"+{len(battles)-len(prev_battles)}"))
        # ranking top change
        ranking = await self.api.call("ranking.getRanking", {"rankingType":"userDamages"})
        prev_rank = self.prev.get("ranking.getRanking.userDamages")
        try:
            new_top = (ranking.get("items") or [None])[0] if isinstance(ranking, dict) else None
            old_top = (prev_rank.get("items") or [None])[0] if isinstance(prev_rank, dict) else None
            if isinstance(new_top, dict) and isinstance(old_top, dict) and new_top.get("_id") != old_top.get("_id"):
                out.append(Alert(now_utc().isoformat(), "INFO", "RANKING", "Top changed", f"{old_top.get('user') or old_top.get('_id')} -> {new_top.get('user') or new_top.get('_id')}"))
        except Exception:
            pass

        # persist prevs and alerts
        self.prev["itemTrading.getPrices"] = prices if prices is not None else prev_prices
        self.prev["battle.getBattles"] = battles if battles is not None else prev_battles
        self.prev["ranking.getRanking.userDamages"] = ranking if ranking is not None else prev_rank
        state["monitor_prev"] = self.prev
        for a in out:
            state_alert = {"ts": a.ts, "level": a.level, "category": a.category, "title": a.title, "message": a.message, "data": a.data}
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
            # post to channel if set, else DM subs
            if ALERT_CHANNEL_ID:
                ch = bot.get_channel(int(ALERT_CHANNEL_ID))
                if ch:
                    summary = f"**🚨 WarEra Monitor — {len(alerts)} alerts**\n"
                    by = {}
                    for a in alerts:
                        by.setdefault(a.category,0)
                        by[a.category]+=1
                    for c,cnt in by.items(): summary += f"• {c}: {cnt}\n"
                    await ch.send(summary)
                    for a in alerts[:12]:
                        emb = discord.Embed(title=f"{a.level} {a.category} — {a.title}", description=a.message, timestamp=datetime.fromisoformat(a.ts), color=(discord.Color.red() if a.level=="CRITICAL" else (discord.Color.gold() if a.level=="WARNING" else discord.Color.blue())))
                        for k,v in (a.data or {}).items():
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

# ---------------- Entity helpers ----------------
def extract_avatar(obj: Dict[str,Any]) -> Optional[str]:
    for k in ("animatedAvatarUrl","avatarUrl","avatar","image","picture"):
        v = obj.get(k)
        if isinstance(v,str) and v.startswith("http"):
            return v
    if isinstance(obj.get("user"), dict):
        return extract_avatar(obj["user"])
    return None

def is_likely_id(s: Any) -> bool:
    return isinstance(s, str) and len(s) in (24,26,36)

def link_for_entity(item: Dict[str,Any]) -> Tuple[str, Optional[str]]:
    """
    Determine the best ([Name](url), avatar) pair for the object.
    Uses per-type URL rules you provided.
    """
    # nested user object
    if isinstance(item.get("user"), dict):
        u = item["user"]
        uid = u.get("_id") or u.get("id")
        name = u.get("name") or u.get("username") or uid
        if uid:
            return (f"[{safe_truncate(name,40)}]({URLS['user']}{uid})", extract_avatar(u))
    # direct user id
    if item.get("user") and is_likely_id(item.get("user")):
        uid = item.get("user")
        name = item.get("name") or item.get("username") or uid
        return (f"[{safe_truncate(name,40)}]({URLS['user']}{uid})", extract_avatar(item))
    # company
    cid = item.get("companyId") or item.get("company")
    if cid:
        name = item.get("name") or item.get("title") or cid
        return (f"[{safe_truncate(name,40)}]({URLS['company']}{cid})", extract_avatar(item))
    # country
    cc = item.get("countryId") or item.get("country")
    if cc:
        name = item.get("name") or item.get("countryName") or cc
        return (f"[{safe_truncate(name,40)}]({URLS['country']}{cc})", extract_avatar(item))
    # region
    rid = item.get("region")
    if rid:
        name = item.get("name") or item.get("regionName") or rid
        return (f"[{safe_truncate(name,40)}]({URLS['region']}{rid})", extract_avatar(item))
    # mu
    if item.get("members") is not None:
        mid = item.get("_id") or item.get("id")
        name = item.get("name") or mid
        if mid:
            return (f"[{safe_truncate(name,40)}]({URLS['mu']}{mid})", extract_avatar(item))
    # battle
    if item.get("battleId") or item.get("_id") and "attacker" in item:
        bid = item.get("battleId") or item.get("_id") or item.get("id")
        name = item.get("title") or bid
        if bid:
            return (f"[{safe_truncate(name,40)}]({URLS['battle']}{bid})", extract_avatar(item))
    # fallback: if _id looks like user id, link user
    mid = item.get("_id") or item.get("id")
    if is_likely_id(mid):
        name = item.get("name") or item.get("title") or mid
        return (f"[{safe_truncate(name,40)}]({URLS['user']}{mid})", extract_avatar(item))
    # final fallback plain name
    name = item.get("name") or item.get("title") or str(mid)
    return (safe_truncate(name,40), extract_avatar(item))

# ---------------- Make per-item embed ----------------
def make_item_embed(idx:int, total:int, name_markup:str, avatar:Optional[str], value_str:str, tier:Optional[str], endpoint_title:str) -> discord.Embed:
    title = f"#{idx} — {name_markup}"
    desc = f"{ICON_DAMAGE} {value_str}"
    if tier:
        desc += f"\n· {safe_truncate(str(tier),20)}"
    emb = discord.Embed(title=title, description=desc, color=discord.Color.dark_gold(), timestamp=now_utc())
    if avatar:
        try: emb.set_thumbnail(url=avatar)
        except: pass
    # footer: show page range for the current block of PAGE_SIZE
    start = ((idx-1)//PAGE_SIZE)*PAGE_SIZE + 1
    end = min(total, start + PAGE_SIZE - 1)
    emb.set_footer(text=f"Showing {start}-{end} of {total} • {endpoint_title}")
    return emb

# ---------------- Render endpoint to one-embed-per-item pages ----------------
async def render_endpoint_to_pages(endpoint:str, params:Optional[Dict]=None) -> Tuple[List[discord.Embed], List[str]]:
    data = await war_api.call(endpoint, params)
    if data is None:
        return [discord.Embed(title=endpoint, description="❌ Failed to fetch", color=discord.Color.red(), timestamp=now_utc())], [json.dumps({"error":"fetch failed"})]
    # if dict with list inside
    if isinstance(data, dict):
        for list_key in ("items","results","data"):
            if list_key in data and isinstance(data[list_key], list):
                items = data[list_key]
                game_pages: List[discord.Embed] = []
                dev_json: List[str] = []
                total = len(items)
                idx = 0
                for it in items:
                    idx += 1
                    if isinstance(it, dict):
                        name_link, avatar = link_for_entity(it)
                        val = it.get("value") or it.get("damage") or it.get("score") or it.get("wealth") or it.get("price") or 0
                        val_s = fmt_num(val)
                        tier = it.get("tier") or it.get("rank") or it.get("title")
                        emb = make_item_embed(idx, total, name_link, avatar, val_s, tier, endpoint)
                        game_pages.append(emb)
                        dev_json.append(json.dumps(it, default=str))
                    else:
                        emb = discord.Embed(title=f"#{idx}", description=safe_truncate(str(it),1000), timestamp=now_utc())
                        game_pages.append(emb)
                        dev_json.append(json.dumps(it, default=str))
                if not game_pages:
                    game_pages.append(discord.Embed(title=endpoint, description="No data", timestamp=now_utc()))
                    dev_json.append(json.dumps(items, default=str))
                return game_pages, dev_json
        # single object -> show key/value compactly
        e = discord.Embed(title=endpoint, timestamp=now_utc(), color=discord.Color.blue())
        small = {k:v for k,v in data.items() if isinstance(v,(str,int,float,bool))}
        if "_id" in small and "name" in data:
            del small["_id"]
        for k,v in list(small.items())[:12]:
            e.add_field(name=str(k), value=safe_truncate(str(v),256), inline=True)
        return [e], [json.dumps(data, default=str)]
    # if list
    if isinstance(data, list):
        game_pages: List[discord.Embed] = []
        dev_json: List[str] = []
        total = len(data)
        idx = 0
        for it in data:
            idx += 1
            if isinstance(it, dict):
                name_link, avatar = link_for_entity(it)
                val = it.get("value") or it.get("damage") or it.get("score") or it.get("wealth") or 0
                val_s = fmt_num(val)
                tier = it.get("tier") or it.get("rank") or None
                emb = make_item_embed(idx, total, name_link, avatar, val_s, tier, endpoint)
                game_pages.append(emb)
                dev_json.append(json.dumps(it, default=str))
            else:
                emb = discord.Embed(title=f"#{idx}", description=safe_truncate(str(it),1000), timestamp=now_utc())
                game_pages.append(emb)
                dev_json.append(json.dumps(it, default=str))
        if not game_pages:
            game_pages.append(discord.Embed(title=endpoint, description="No data", timestamp=now_utc()))
            dev_json.append(json.dumps(data, default=str))
        return game_pages, dev_json
    # primitive fallback
    return [discord.Embed(title=endpoint, description=safe_truncate(str(data),1000), timestamp=now_utc())], [json.dumps(data, default=str)]

# ---------------- Leaderboard View (Game <-> Dev pages) ----------------
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
        self.add_item(self.prev); self.add_item(self.toggle); self.add_item(self.next)
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
            emb = discord.Embed(title=f"🧠 Dev JSON (page {self.idx+1})", description=f"```json\n{j}\n```", timestamp=now_utc(), color=discord.Color.dark_grey())
        self._update_buttons()
        # followup.edit_message requires message id
        try:
            await interaction.followup.edit_message(interaction.message.id, embed=emb, view=self)
        except Exception:
            # sometimes followup not available: try edit original response
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

# ---------------- Slash Commands ----------------
RANKING_CHOICES = [
    app_commands.Choice(name="User Damage", value="userDamages"),
    app_commands.Choice(name="Weekly User Damage", value="weeklyUserDamages"),
    app_commands.Choice(name="Wealth", value="userWealth"),
    app_commands.Choice(name="Level", value="userLevel"),
    app_commands.Choice(name="Referals", value="userReferrals"),
    app_commands.Choice(name="Subscribers", value="userSubscribers"),
    app_commands.Choice(name="Ground", value="userTerrain"),
    app_commands.Choice(name="Premium", value="userPremiumMonths"),
    app_commands.Choice(name="Premium Gifts", value="userPremiumGifts"),
]

@tree.command(name="help", description="Show commands")
async def help_cmd(interaction: discord.Interaction):
    await safe_defer(interaction)
    e = discord.Embed(title="WarEra Bot — Commands", color=discord.Color.gold(), timestamp=now_utc())
    rows = [
        ("/rankings <type>", "Ranking.getRanking (users)"),
        ("/topdamage", "Aggregated top damage (users)"),
        ("/topwealth", "Aggregated top wealth (users)"),
        ("/toplandproducers", "Aggregated land producers (users)"),
        ("/topmu", "Top military units (MUs)"),
        ("/countries", "List countries"),
        ("/country <id>", "Country by id"),
        ("/regions", "List regions"),
        ("/region <id>", "Region by id"),
        ("/companies", "List companies"),
        ("/company <id>", "Company by id"),
        ("/battles", "Active battles"),
        ("/prices", "Item prices"),
        ("/transactions", "Transactions"),
        ("/users <id>", "User by id"),
        ("/jsondebug", "Paste JSON => formatted code block"),
        ("/dashboard", "Post/refresh dashboard (auto-refresh)"),
        ("/alerts <subscribe/unsubscribe/list>", "Manage alerts"),
    ]
    for k,v in rows:
        e.add_field(name=k, value=v, inline=False)
    await interaction.followup.send(embed=e)

@tree.command(name="rankings", description="View ranking.getRanking")
@app_commands.choices(ranking_type=RANKING_CHOICES)
async def rankings_cmd(interaction: discord.Interaction, ranking_type: app_commands.Choice[str]):
    await safe_defer(interaction)
    pages, dev_json = await render_endpoint_to_pages("ranking.getRanking", {"rankingType": ranking_type.value})
    view = LeaderboardView(pages, dev_json)
    await interaction.followup.send(embed=pages[0], view=view)

# Aggregations (users)
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
            if uid is None: continue
            try: sums[str(uid)] = sums.get(str(uid),0.0) + float(val)
            except: pass
    return sorted(sums.items(), key=lambda kv: kv[1], reverse=True)

def ranking_list_to_pages(title:str, ranked:List[Tuple[str,float]], names_map:Optional[Dict[str,str]]=None) -> Tuple[List[discord.Embed], List[str]]:
    pages = []; dev = []; total = len(ranked)
    for idx,(uid,val) in enumerate(ranked, start=1):
        name = (names_map or {}).get(uid) or uid
        name_markup = f"[{safe_truncate(name,40)}]({URLS['user']}{uid})" if uid else safe_truncate(str(name),40)
        emb = make_item_embed(idx, total, name_markup, None, fmt_num(val), None, title)
        pages.append(emb); dev.append(json.dumps({"user":uid,"value":val}, default=str))
        if idx >= 2000: break
    if not pages:
        pages.append(discord.Embed(title=title, description="No data", timestamp=now_utc()))
        dev.append("[]")
    return pages, dev

@tree.command(name="topdamage", description="Top damage aggregated")
async def topdamage_cmd(interaction: discord.Interaction):
    await safe_defer(interaction)
    ranked = await aggregate_users_from_ranking("userDamages")
    names_map={}
    for uid,_ in ranked[:50]:
        try:
            r = await war_api.call("user.getUserLite", {"userId":uid})
            if isinstance(r, dict): names_map[uid] = r.get("name") or r.get("username") or uid
            else: names_map[uid]=uid
        except: names_map[uid]=uid
    pages, dev = ranking_list_to_pages("🔥 Top Damage (aggregated)", ranked[:500], names_map)
    view = LeaderboardView(pages, dev)
    await interaction.followup.send(embed=pages[0], view=view)

@tree.command(name="topwealth", description="Top wealth aggregated")
async def topwealth_cmd(interaction: discord.Interaction):
    await safe_defer(interaction)
    ranked = await aggregate_users_from_ranking("userWealth")
    names_map={}
    for uid,_ in ranked[:50]:
        try:
            r = await war_api.call("user.getUserLite", {"userId":uid})
            if isinstance(r, dict): names_map[uid] = r.get("name") or r.get("username") or uid
            else: names_map[uid]=uid
        except: names_map[uid]=uid
    pages, dev = ranking_list_to_pages("💰 Top Wealth", ranked[:500], names_map)
    view = LeaderboardView(pages, dev)
    await interaction.followup.send(embed=pages[0], view=view)

@tree.command(name="toplandproducers", description="Top land producers aggregated")
async def topland_cmd(interaction: discord.Interaction):
    await safe_defer(interaction)
    ranked = await aggregate_users_from_ranking("userTerrain")
    names_map={}
    for uid,_ in ranked[:50]:
        try:
            r = await war_api.call("user.getUserLite", {"userId":uid})
            if isinstance(r, dict): names_map[uid] = r.get("name") or r.get("username") or uid
            else: names_map[uid]=uid
        except: names_map[uid]=uid
    pages, dev = ranking_list_to_pages("🌾 Top Land Producers", ranked[:500], names_map)
    view = LeaderboardView(pages, dev)
    await interaction.followup.send(embed=pages[0], view=view)

@tree.command(name="topmu", description="Top Military Units (MUs)")
async def topmu_cmd(interaction: discord.Interaction):
    await safe_defer(interaction)
    res = await war_api.call("mu.getManyPaginated", {"page":1,"limit":200})
    items = []
    if isinstance(res, dict) and isinstance(res.get("items"), list): items = res["items"]
    elif isinstance(res, list): items = res
    scored=[]
    for mu in items:
        if not isinstance(mu, dict): continue
        members = len(mu.get("members",[]))
        invest = mu.get("investedMoneyByUsers") or {}
        tot=0.0
        if isinstance(invest, dict):
            for v in invest.values():
                try: tot+=float(v)
                except: pass
        score = tot if tot>0 else members
        scored.append((mu,score))
    scored.sort(key=lambda x:x[1], reverse=True)
    pages=[]; dev=[]
    for idx,(mu,score) in enumerate(scored, start=1):
        name = mu.get("name") or mu.get("_id") or str(idx)
        link = f"[{safe_truncate(name,40)}]({URLS['mu']}{mu.get('_id') or mu.get('id')})" if (mu.get("_id") or mu.get("id")) else safe_truncate(name,40)
        avatar = extract_avatar(mu)
        emb = make_item_embed(idx, len(scored), link, avatar, fmt_num(score), None, "mu.getManyPaginated")
        pages.append(emb); dev.append(json.dumps(mu, default=str))
        if idx>=200: break
    if not pages:
        pages.append(discord.Embed(title="Top MUs", description="No data", timestamp=now_utc()))
        dev.append("[]")
    view = LeaderboardView(pages, dev)
    await interaction.followup.send(embed=pages[0], view=view)

# Generic endpoint wrapper
async def send_endpoint_pages(interaction: discord.Interaction, endpoint: str, params: Optional[Dict]=None):
    await safe_defer(interaction)
    pages, dev = await render_endpoint_to_pages(endpoint, params)
    view = LeaderboardView(pages, dev)
    await interaction.followup.send(embed=pages[0], view=view)

# Commands for endpoints
@tree.command(name="countries", description="List countries")
async def countries_cmd(interaction: discord.Interaction):
    await send_endpoint_pages(interaction, "country.getAllCountries")

@tree.command(name="country", description="Country by id")
@app_commands.describe(country_id="Country id")
async def country_cmd(interaction: discord.Interaction, country_id: str):
    await send_endpoint_pages(interaction, "country.getCountryById", {"countryId": country_id})

@tree.command(name="regions", description="Regions list")
async def regions_cmd(interaction: discord.Interaction):
    await send_endpoint_pages(interaction, "region.getRegionsObject")

@tree.command(name="region", description="Region by id")
@app_commands.describe(region_id="Region id")
async def region_cmd(interaction: discord.Interaction, region_id: str):
    await send_endpoint_pages(interaction, "region.getById", {"regionId": region_id})

@tree.command(name="companies", description="List companies")
async def companies_cmd(interaction: discord.Interaction):
    await send_endpoint_pages(interaction, "company.getCompanies", {"page":1,"limit":50})

@tree.command(name="company", description="Company by id")
@app_commands.describe(company_id="Company id")
async def company_cmd(interaction: discord.Interaction, company_id: str):
    await send_endpoint_pages(interaction, "company.getById", {"companyId": company_id})

@tree.command(name="battles", description="Active battles")
async def battles_cmd(interaction: discord.Interaction):
    await send_endpoint_pages(interaction, "battle.getBattles")

@tree.command(name="battle", description="Battle by id")
@app_commands.describe(battle_id="Battle id")
async def battle_cmd(interaction: discord.Interaction, battle_id: str):
    await send_endpoint_pages(interaction, "battle.getById", {"battleId": battle_id})

@tree.command(name="workoffers", description="Work offers")
async def workoffers_cmd(interaction: discord.Interaction):
    await send_endpoint_pages(interaction, "workOffer.getWorkOffersPaginated", {"page":1,"limit":50})

@tree.command(name="workoffer", description="Work offer by id")
@app_commands.describe(offer_id="Work offer id")
async def workoffer_cmd(interaction: discord.Interaction, offer_id: str):
    await send_endpoint_pages(interaction, "workOffer.getById", {"workOfferId": offer_id})

@tree.command(name="mu", description="List military units (paginated)")
async def mu_cmd(interaction: discord.Interaction):
    await send_endpoint_pages(interaction, "mu.getManyPaginated", {"page":1,"limit":50})

@tree.command(name="mu_by_id", description="MU by id")
@app_commands.describe(mu_id="MU id")
async def mu_by_id_cmd(interaction: discord.Interaction, mu_id: str):
    await send_endpoint_pages(interaction, "mu.getById", {"muId": mu_id})

@tree.command(name="articles", description="List articles")
async def articles_cmd(interaction: discord.Interaction):
    await send_endpoint_pages(interaction, "article.getArticlesPaginated", {"page":1,"limit":50})

@tree.command(name="article", description="Article by id")
@app_commands.describe(article_id="Article id")
async def article_cmd(interaction: discord.Interaction, article_id: str):
    await send_endpoint_pages(interaction, "article.getArticleById", {"articleId": article_id})

@tree.command(name="prices", description="Item prices")
async def prices_cmd(interaction: discord.Interaction):
    await send_endpoint_pages(interaction, "itemTrading.getPrices")

@tree.command(name="transactions", description="Transactions")
async def transactions_cmd(interaction: discord.Interaction):
    await send_endpoint_pages(interaction, "transaction.getPaginatedTransactions", {"page":1,"limit":50})

@tree.command(name="users", description="User by id")
@app_commands.describe(user_id="User id")
async def users_cmd(interaction: discord.Interaction, user_id: str):
    await send_endpoint_pages(interaction, "user.getUserLite", {"userId": user_id})

@tree.command(name="search", description="Search anything")
@app_commands.describe(query="Search text")
async def search_cmd(interaction: discord.Interaction, query: str):
    await send_endpoint_pages(interaction, "search.searchAnything", {"searchText": query})

# ---------------- JSON Debug (modal) ----------------
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
            if len(text) > 1900: text = text[:1897] + "..."
            await interaction.followup.send(f"```json\n{text}\n```", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Invalid JSON: {e}", ephemeral=True)

@tree.command(name="jsondebug", description="Paste JSON and get formatted code block")
async def jsondebug_cmd(interaction: discord.Interaction):
    modal = JsonModal()
    await interaction.response.send_modal(modal)

# ---------------- Alerts subscribe/unsubscribe/list ----------------
@tree.command(name="alerts", description="Manage alerts (subscribe/unsubscribe/list)")
@app_commands.describe(action="subscribe, unsubscribe, list")
async def alerts_cmd(interaction: discord.Interaction, action: str):
    await safe_defer(interaction, ephemeral=True)
    uid = str(interaction.user.id)
    subs = state.get("alerts_subscribers", [])
    a = action.lower()
    if a == "subscribe":
        if uid in subs:
            await interaction.followup.send("You are already subscribed.", ephemeral=True); return
        subs.append(uid); state["alerts_subscribers"]=subs; await save_state()
        await interaction.followup.send("✅ Subscribed to alerts (DM).", ephemeral=True); return
    if a == "unsubscribe":
        if uid in subs: subs.remove(uid); state["alerts_subscribers"]=subs; await save_state(); await interaction.followup.send("✅ Unsubscribed.", ephemeral=True); return
        await interaction.followup.send("You were not subscribed.", ephemeral=True); return
    if a == "list":
        await interaction.followup.send(f"Subscribers: {len(subs)}", ephemeral=True); return
    await interaction.followup.send("Usage: /alerts subscribe|unsubscribe|list", ephemeral=True)

# ---------------- Dashboard & Controls ----------------
class IntervalModal(Modal):
    def __init__(self):
        super().__init__(title="Set interval (seconds)")
        self.input = TextInput(label="Seconds", placeholder=str(DEFAULT_DASH_INTERVAL), required=True)
        self.add_item(self.input)
    async def on_submit(self, interaction: discord.Interaction):
        val = self.input.value.strip()
        try:
            sec = int(val)
            if sec < 5: raise ValueError("min 5s")
            monitor.interval = sec
            if monitor_loop.is_running(): monitor_loop.change_interval(seconds=sec)
            if dash_loop.is_running(): dash_loop.change_interval(seconds=sec)
            state["dash_interval"] = sec; await save_state()
            await interaction.response.send_message(f"✅ Interval set to {sec}s", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)

class DashboardControls(View):
    def __init__(self):
        super().__init__(timeout=None)
        self.start = Button(label="▶️ Start Monitor", style=discord.ButtonStyle.success)
        self.stop = Button(label="⏸️ Stop Monitor", style=discord.ButtonStyle.danger)
        self.refresh = Button(label="🔁 Refresh now", style=discord.ButtonStyle.secondary)
        self.interval = Button(label="⏱️ Set interval", style=discord.ButtonStyle.secondary)
        self.clear = Button(label="🧹 Clear alerts", style=discord.ButtonStyle.secondary)
        self.add_item(self.start); self.add_item(self.stop); self.add_item(self.refresh); self.add_item(self.interval); self.add_item(self.clear)
        self.start.callback=self.on_start; self.stop.callback=self.on_stop; self.refresh.callback=self.on_refresh; self.interval.callback=self.on_interval; self.clear.callback=self.on_clear

    async def on_start(self, interaction: discord.Interaction):
        monitor.running = True
        if not monitor_loop.is_running(): monitor_loop.start()
        await interaction.response.send_message("✅ Monitor started", ephemeral=True)

    async def on_stop(self, interaction: discord.Interaction):
        monitor.running = False
        await interaction.response.send_message("⏸️ Monitor stopped", ephemeral=True)

    async def on_refresh(self, interaction: discord.Interaction):
        alerts = await monitor.scan_once()
        await interaction.response.send_message(f"✅ Scanned: {len(alerts)} alerts.", ephemeral=True)

    async def on_interval(self, interaction: discord.Interaction):
        await interaction.response.send_modal(IntervalModal())

    async def on_clear(self, interaction: discord.Interaction):
        monitor.alerts.clear()
        state["monitor_alerts"]=[]; await save_state()
        await interaction.response.send_message("✅ Alerts cleared", ephemeral=True)

@tree.command(name="dashboard", description="Post/refresh live dashboard (edits same message)")
async def dashboard_cmd(interaction: discord.Interaction):
    await safe_defer(interaction)
    rank_pages, _ = await render_endpoint_to_pages("ranking.getRanking", {"rankingType":"userDamages"})
    rank_embed = rank_pages[0] if rank_pages else discord.Embed(title="Rankings", timestamp=now_utc())
    prices = await war_api.call("itemTrading.getPrices")
    pe = discord.Embed(title="💰 Prices", timestamp=now_utc())
    if isinstance(prices, dict):
        i=0
        for k,v in prices.items():
            if i>=8: break
            pe.add_field(name=safe_truncate(k,24), value=fmt_num(v), inline=True); i+=1
    else:
        pe.description = safe_truncate(str(prices),200)
    battles = await war_api.call("battle.getBattles")
    be = discord.Embed(title="⚔️ Battles", timestamp=now_utc())
    if isinstance(battles, list):
        for b in battles[:6]:
            if isinstance(b, dict):
                a = b.get("attackerCountry") or b.get("attacker"); d = b.get("defenderCountry") or b.get("defender"); s = b.get("status") or b.get("phase")
                be.add_field(name=f"{a} vs {d}", value=safe_truncate(str(s),50), inline=False)
            else:
                be.add_field(name=str(b), value="—", inline=False)
    else:
        be.description = safe_truncate(str(battles),200)
    alerts_embed = discord.Embed(title="🚨 Alerts", timestamp=now_utc())
    recent_alerts = monitor.alerts[:6] if hasattr(monitor,"alerts") else state.get("monitor_alerts", [])[:6]
    if recent_alerts:
        for a in recent_alerts:
            alerts_embed.add_field(name=f"{a.get('level','')} {a.get('category','')}", value=safe_truncate(a.get("message",""),80), inline=False)
    else:
        alerts_embed.description = "No alerts"

    topdamage = await aggregate_users_from_ranking("userDamages")
    damage_pages, damage_dev = ranking_list_to_pages("🔥 Top Damage (agg)", topdamage[:50])
    game_pages = [rank_embed, pe, be, alerts_embed] + (damage_pages[:2] if damage_pages else [])
    dev_pages = [json.dumps({"endpoint":"ranking.getRanking"}), json.dumps(prices or {}, default=str), json.dumps(battles or {}, default=str), json.dumps(recent_alerts or {}, default=str)] + (damage_dev[:2] if damage_dev else [])
    view = LeaderboardView(game_pages, dev_pages)
    controls = DashboardControls()

    channel = bot.get_channel(int(DASH_CHANNEL_ID)) if DASH_CHANNEL_ID else interaction.channel
    if channel is None:
        await interaction.followup.send("❌ Dashboard channel missing. Set WARERA_DASH_CHANNEL or run this command in a channel.", ephemeral=True)
        return
    dash = state.get("dash_message")
    posted = None
    if dash:
        try:
            msg = await channel.fetch_message(int(dash["message_id"]))
            await msg.edit(embed=game_pages[0], view=view)
            posted = msg
        except Exception:
            try:
                posted = await channel.send(embed=game_pages[0], view=view)
                state["dash_message"] = {"channel_id": channel.id, "message_id": posted.id}; await save_state()
            except Exception:
                posted = None
    else:
        posted = await channel.send(embed=game_pages[0], view=view)
        state["dash_message"] = {"channel_id": channel.id, "message_id": posted.id}; await save_state()
    try:
        await interaction.followup.send("Dashboard controls (only you):", view=controls, ephemeral=True)
    except Exception:
        try: await interaction.followup.send("Dashboard created", ephemeral=True)
        except: pass
    if not dash_loop.is_running(): dash_loop.start()
    await interaction.followup.send("✅ Dashboard posted/updated.", ephemeral=True)

@tasks.loop(seconds=DEFAULT_DASH_INTERVAL)
async def dash_loop():
    dash = state.get("dash_message")
    if not dash: return
    try:
        ch = bot.get_channel(int(dash["channel_id"]))
        if ch is None: return
        msg = await ch.fetch_message(int(dash["message_id"]))
        rank_pages, _ = await render_endpoint_to_pages("ranking.getRanking", {"rankingType":"userDamages"})
        rank_embed = rank_pages[0] if rank_pages else discord.Embed(title="Rankings", timestamp=now_utc())
        prices = await war_api.call("itemTrading.getPrices")
        pe = discord.Embed(title="💰 Prices", timestamp=now_utc())
        if isinstance(prices, dict):
            i=0
            for k,v in prices.items():
                if i>=8: break
                pe.add_field(name=safe_truncate(k,24), value=fmt_num(v), inline=True); i+=1
        battles = await war_api.call("battle.getBattles")
        be = discord.Embed(title="⚔️ Battles", timestamp=now_utc())
        if isinstance(battles, list):
            for b in battles[:6]:
                if isinstance(b, dict):
                    a = b.get("attackerCountry") or b.get("attacker"); d = b.get("defenderCountry") or b.get("defender"); s = b.get("status") or b.get("phase")
                    be.add_field(name=f"{a} vs {d}", value=safe_truncate(str(s),50), inline=False)
        alerts_embed = discord.Embed(title="🚨 Alerts", timestamp=now_utc())
        recent_alerts = monitor.alerts[:6] if hasattr(monitor,"alerts") else state.get("monitor_alerts", [])[:6]
        if recent_alerts:
            for a in recent_alerts:
                alerts_embed.add_field(name=f"{a.get('level','')} {a.get('category','')}", value=safe_truncate(a.get("message",""),80), inline=False)
        pages = [rank_embed, pe, be, alerts_embed]
        dev_pages = [json.dumps({"endpoint":"ranking"}), json.dumps(prices or {}, default=str), json.dumps(battles or {}, default=str), json.dumps(recent_alerts or {}, default=str)]
        view = LeaderboardView(pages, dev_pages)
        await msg.edit(embed=pages[0], view=view)
    except Exception as e:
        print("[dash_loop] error:", e)

# ---------------- LIFECYCLE ----------------
@bot.event
async def on_ready():
    print(f"[WarEra Final] Logged in as {bot.user} (id={bot.user.id})")
    try:
        await tree.sync()
        print("[WarEra Final] Slash commands synced.")
    except Exception as e:
        print("Slash sync failed:", e)
    monitor.prev = state.get("monitor_prev", {})
    monitor.alerts = state.get("monitor_alerts", [])
    if not monitor_loop.is_running(): monitor_loop.start()
    if state.get("dash_message") and not dash_loop.is_running(): dash_loop.start()

# ---------------- RUN ----------------
if __name__ == "__main__":
    if DISCORD_TOKEN == "YOUR_TOKEN_HERE":
        print("Set DISCORD_BOT_TOKEN environment variable.")
    else:
        bot.run(DISCORD_TOKEN)
