# war-era-final.py
"""
WarEra final single-file bot
- Slash-only (bot.tree)
- Pretty leaderboards: one entry per embed page, with avatar thumbnails
- Correct per-entity URL linking (user/company/country/region/mu)
- Dashboard auto-refresh (edits same message for everyone)
- Alerts posted to channel + DM subscribers
- jsondebug returns formatted JSON as a plain codeblock message
- Persistent state in state_warera.json (configurable)
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
from enum import Enum

# ---------------- Config ----------------
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

# optional small icons (can be URLs or emoji text)
ICON_DAMAGE = "⚔️"
ICON_WEALTH = "💰"
ICON_MU = "🎖️"
ICON_COMPANY = "🏢"
ICON_COUNTRY = "🌍"
ICON_REGION = "🏔️"

# ---------------- Bot setup ----------------
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

# ---------------- Utilities ----------------
def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def safe_truncate(s: Optional[str], n: int) -> str:
    if s is None:
        return ""
    s = str(s)
    return s if len(s) <= n else s[: n-3] + "..."

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

# URL mapping per your spec
def user_url(uid: Optional[str]) -> str:
    if not uid: return ""
    return f"https://app.warera.io/user/{uid}"

def company_url(cid: Optional[str]) -> str:
    if not cid: return ""
    return f"https://app.warera.io/company/{cid}"

def country_url(cid: Optional[str]) -> str:
    if not cid: return ""
    return f"https://app.warera.io/country/{cid}"

def region_url(rid: Optional[str]) -> str:
    if not rid: return ""
    return f"https://app.warera.io/region/{rid}"

def mu_url(mid: Optional[str]) -> str:
    if not mid: return ""
    return f"https://app.warera.io/mu/{mid}"

# ---------------- Persistent state ----------------
_state_lock = asyncio.Lock()
DEFAULT_STATE = {
    "alerts_subscribers": [],
    "monitor_prev": {},
    "monitor_alerts": [],
    "dash_message": None
}
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
            print("[save_state] error:", e)

load_state()

# ---------------- API Client ----------------
class WarEraAPI:
    def __init__(self, base_url: str = API_BASE):
        self.base_url = base_url.rstrip("/")

    def build_url(self, endpoint: str, params: Optional[Dict]=None) -> str:
        ep = endpoint.strip().lstrip("/")
        base = f"{self.base_url}/{ep}"
        input_json = json.dumps(params or {}, separators=(",", ":"))
        encoded = urllib.parse.quote(input_json, safe='')
        return f"{base}?input={encoded}"

    async def call(self, endpoint: str, params: Optional[Dict]=None) -> Optional[Any]:
        url = self.build_url(endpoint, params)
        sess = await get_session()
        last_exc = None
        for attempt in range(RETRY_ATTEMPTS):
            try:
                async with sess.get(url) as resp:
                    text = await resp.text()
                    if resp.status != 200:
                        last_exc = Exception(f"HTTP {resp.status}: {text[:200]}")
                        await asyncio.sleep(RETRY_BACKOFF * (2 ** attempt))
                        continue
                    data = json.loads(text)
                    if isinstance(data, dict) and "result" in data:
                        res = data["result"]
                        if isinstance(res, dict) and "data" in res:
                            return res["data"]
                        return res
                    return data
            except Exception as e:
                last_exc = e
                await asyncio.sleep(RETRY_BACKOFF * (2 ** attempt))
        print(f"[WarEraAPI] call failed {endpoint} params={params} -> {last_exc}")
        return None

war_api = WarEraAPI()

# ---------------- Monitor / Alerts ----------------
class Alert:
    def __init__(self, level: str, category: str, title: str, message: str, data: Dict=None):
        self.ts = now_utc().isoformat()
        self.level = level
        self.category = category
        self.title = title
        self.message = message
        self.data = data or {}

class Monitor:
    def __init__(self, api: WarEraAPI):
        self.api = api
        self.prev = state.get("monitor_prev", {})
        self.alerts = state.get("monitor_alerts", [])
        self.running = False
        self.interval = DEFAULT_DASH_INTERVAL
        self.price_threshold = 20.0
        self.price_critical = 50.0

    async def scan_once(self) -> List[Alert]:
        new_alerts: List[Alert] = []
        # prices
        prices = await self.api.call("itemTrading.getPrices")
        prev_prices = self.prev.get("itemTrading.getPrices")
        if isinstance(prices, dict) and isinstance(prev_prices, dict):
            for k,v in prices.items():
                if isinstance(v, (int,float)) and isinstance(prev_prices.get(k),(int,float)):
                    old = prev_prices.get(k)
                    if old != 0:
                        change = ((v-old)/abs(old))*100.0
                        if abs(change) >= self.price_threshold:
                            lvl = "CRITICAL" if abs(change) >= self.price_critical else "WARNING"
                            new_alerts.append(Alert(lvl, "ECONOMY", f"Price {k}", f"{fmt_num(old)} → {fmt_num(v)} ({change:+.2f}%)", {"old":old,"new":v,"pct":change}))
        # battles new
        battles = await self.api.call("battle.getBattles")
        prev_battles = self.prev.get("battle.getBattles")
        if isinstance(battles, list) and isinstance(prev_battles, list):
            if len(battles) > len(prev_battles):
                new_alerts.append(Alert("WARNING","BATTLE","New battles",f"+{len(battles)-len(prev_battles)} new battles",{"diff":len(battles)-len(prev_battles)}))
        # rankings top changed
        ranking = await self.api.call("ranking.getRanking", {"rankingType":"userDamages"})
        prev_ranking = self.prev.get("ranking.getRanking.userDamages")
        try:
            new_top = (ranking.get("items") or [None])[0] if isinstance(ranking, dict) else None
            old_top = (prev_ranking.get("items") or [None])[0] if isinstance(prev_ranking, dict) else None
            if isinstance(new_top, dict) and isinstance(old_top, dict) and new_top.get("_id") != old_top.get("_id"):
                new_alerts.append(Alert("INFO","RANKING","Top changed",f"{old_top.get('user') or old_top.get('_id')} → {new_top.get('user') or new_top.get('_id')}",{"old":old_top,"new":new_top}))
        except Exception:
            pass

        # persist prevs
        self.prev["itemTrading.getPrices"] = prices if prices is not None else prev_prices
        self.prev["battle.getBattles"] = battles if battles is not None else prev_battles
        self.prev["ranking.getRanking.userDamages"] = ranking if ranking is not None else prev_ranking
        state["monitor_prev"] = self.prev

        # persist and push alerts
        for a in new_alerts:
            state_alert = {"ts": a.ts, "level": a.level, "category": a.category, "title": a.title, "message": a.message, "data": a.data}
            self.alerts.insert(0, state_alert)
        state["monitor_alerts"] = self.alerts[:400]
        await save_state()
        return new_alerts

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
            # post to channel if configured
            if ALERT_CHANNEL_ID:
                ch = bot.get_channel(int(ALERT_CHANNEL_ID))
                if ch:
                    summary = f"**🚨 WarEra Monitor — {len(alerts)} alerts**\n"
                    by = {}
                    for a in alerts:
                        by.setdefault(a.category,0)
                        by[a.category]+=1
                    for c,cnt in by.items():
                        summary += f"• {c}: {cnt}\n"
                    await ch.send(summary)
                    for a in alerts[:12]:
                        emb = discord.Embed(title=f"{a.level} {a.category} — {a.title}", description=a.message, timestamp=datetime.fromisoformat(a.ts), color=(discord.Color.red() if a.level=="CRITICAL" else (discord.Color.gold() if a.level=="WARNING" else discord.Color.blue())))
                        for k,v in (a.data or {}).items():
                            emb.add_field(name=str(k), value=safe_truncate(json.dumps(v, default=str),256), inline=True)
                        await ch.send(embed=emb)
                        # DM subscribers
                        subs = state.get("alerts_subscribers", [])
                        for uid in subs:
                            try:
                                user = await bot.fetch_user(int(uid))
                                if user:
                                    try:
                                        await user.send(f"🚨 {a.title}: {a.message}")
                                        await user.send(embed=emb)
                                    except Exception:
                                        pass
                            except Exception:
                                pass
                    if len(alerts) > 12 and ch:
                        await ch.send(f"⚠️ {len(alerts)-12} more suppressed")
            else:
                # no channel: DM subscribers only
                subs = state.get("alerts_subscribers", [])
                for a in alerts:
                    subs = state.get("alerts_subscribers", [])
                    for uid in subs:
                        try:
                            user = await bot.fetch_user(int(uid))
                            if user:
                                await user.send(f"🚨 {a.title}: {a.message}")
                        except Exception:
                            pass
    except Exception as e:
        print("[monitor_loop] error:", e)

# ---------------- UI helpers ----------------
def make_embed_single_entry(rank_idx: int, name_markup: str, avatar: Optional[str], value_str: str, tier: Optional[str], endpoint_title: str, total: Optional[int]=None) -> discord.Embed:
    title = f"#{rank_idx} — {name_markup}"
    desc = f"{ICON_DAMAGE} {value_str}"
    if tier:
        desc += f"\n· {safe_truncate(str(tier), 20)}"
    if total:
        footer = f"Showing {rank_idx} of {total}"
    else:
        footer = endpoint_title
    emb = discord.Embed(title=title, description=desc, color=discord.Color.dark_gold(), timestamp=now_utc())
    if avatar:
        try:
            emb.set_thumbnail(url=avatar)
        except Exception:
            pass
    emb.set_footer(text=footer)
    return emb

def json_codeblock(some) -> str:
    try:
        j = json.dumps(some, indent=2, default=str)
    except Exception:
        j = str(some)
    if len(j) > 1900:
        j = j[:1897]+"..."
    return f"```json\n{j}\n```"

# Detect avatar fields
def extract_avatar(obj: Dict[str,Any]) -> Optional[str]:
    for k in ("animatedAvatarUrl","avatarUrl","avatar","image","picture"):
        v = obj.get(k)
        if isinstance(v, str) and v.startswith("http"):
            return v
    # nested user object
    if isinstance(obj.get("user"), dict):
        return extract_avatar(obj["user"])
    return None

# Best effort: get display name and link + avatar depending on object fields
def determine_entity_link_and_avatar(item: Dict[str,Any]) -> Tuple[str, Optional[str]]:
    """
    returns (markup, avatar_url) where markup is [Name](url) or plain name.
    Rules:
      - If item has 'user' field or 'username'/'name' with 24-char id -> user link
      - If item has 'companyId' or 'company' -> company link
      - If item has 'countryId' or 'country' -> country link
      - If item has 'region' -> region link
      - If item has members -> mu link
      - else fallback to name or id -> user link only if looks like user id
    """
    # try nested user obj
    if isinstance(item.get("user"), dict):
        u = item["user"]
        uid = u.get("_id") or u.get("id")
        name = u.get("name") or u.get("username") or str(uid)
        if uid:
            return (f"[{safe_truncate(name,40)}]({user_url(uid)})", extract_avatar(u))
    # user id string
    uid = item.get("user") or item.get("userId")
    if isinstance(uid, str) and len(uid) in (24, 26, 36):  # heuristic lengths
        name = item.get("name") or item.get("username") or uid
        return (f"[{safe_truncate(name,40)}]({user_url(uid)})", extract_avatar(item))
    # company
    cid = item.get("companyId") or item.get("company")
    if cid:
        name = item.get("name") or item.get("title") or cid
        return (f"[{safe_truncate(name,40)}]({company_url(cid)})", extract_avatar(item))
    # country
    cc = item.get("countryId") or item.get("country")
    if cc:
        name = item.get("name") or item.get("countryName") or cc
        return (f"[{safe_truncate(name,40)}]({country_url(cc)})", extract_avatar(item))
    # region
    rid = item.get("region")
    if rid:
        name = item.get("name") or item.get("regionName") or rid
        return (f"[{safe_truncate(name,40)}]({region_url(rid)})", extract_avatar(item))
    # military unit
    if item.get("members") is not None:
        mid = item.get("_id") or item.get("id")
        name = item.get("name") or mid
        if mid:
            return (f"[{safe_truncate(name,40)}]({mu_url(mid)})", extract_avatar(item))
    # fallback: if has _id and length looks like user id, treat as user
    mid = item.get("_id") or item.get("id")
    if isinstance(mid, str) and len(mid) in (24,26,36):
        name = item.get("name") or mid
        return (f"[{safe_truncate(name,40)}]({user_url(mid)})", extract_avatar(item))
    # final fallback
    name = item.get("name") or item.get("title") or str(mid)
    return (safe_truncate(name,40), extract_avatar(item))

# ---------------- Render endpoint into one-embed-per-item pages ----------------
async def render_endpoint_to_pages(endpoint: str, params: Optional[Dict]=None) -> Tuple[List[discord.Embed], List[str]]:
    """
    Returns (game_pages: List[Embed], dev_pages_json: List[str]) where dev_pages_json[i] is JSON for that page.
    For ranking-like lists we produce one embed per item (matches the example style).
    """
    data = await war_api.call(endpoint, params)
    if data is None:
        return [discord.Embed(title=endpoint, description="❌ Failed to fetch", color=discord.Color.red(), timestamp=now_utc())], [json.dumps({"error":"fetch failed"})]

    # If dict with items/results/data
    if isinstance(data, dict):
        # common listing keys
        for k in ("items","results","data"):
            if k in data and isinstance(data[k], list):
                items = data[k]
                game_pages = []
                dev_json_pages = []
                total = len(items)
                # create per-item embeds
                idx = 0
                for it in items:
                    idx += 1
                    if isinstance(it, dict):
                        name_markup, avatar = determine_entity_link_and_avatar(it)
                        # pick value
                        val = it.get("value") or it.get("damage") or it.get("score") or it.get("wealth") or it.get("price") or 0
                        val_s = fmt_num(val)
                        tier = it.get("tier") or it.get("rank") or it.get("title")
                        emb = make_embed_for_item(idx, total, name_markup, avatar, val_s, tier, endpoint)
                        game_pages.append(emb)
                        dev_json_pages.append(json.dumps(it, default=str))
                    else:
                        # primitive item - simple embed
                        emb = discord.Embed(title=f"#{idx}", description=safe_truncate(str(it), 1000), timestamp=now_utc())
                        game_pages.append(emb)
                        dev_json_pages.append(json.dumps(it, default=str))
                if not game_pages:
                    game_pages.append(discord.Embed(title=endpoint, description="No data", timestamp=now_utc()))
                    dev_json_pages.append(json.dumps(items, default=str))
                return game_pages, dev_json_pages
        # single-object dict (show compact fields as embed)
        e = discord.Embed(title=endpoint, timestamp=now_utc(), color=discord.Color.blue())
        small = {k:v for k,v in data.items() if isinstance(v,(str,int,float,bool))}
        # remove __v and raw _id if name exists:
        if "_id" in small and "name" in data:
            del small["_id"]
        for k,v in list(small.items())[:12]:
            e.add_field(name=str(k), value=safe_truncate(str(v), 1024), inline=True)
        return [e], [json.dumps(data, default=str)]

    # list
    if isinstance(data, list):
        game_pages = []
        dev_json_pages = []
        total = len(data)
        idx = 0
        for it in data:
            idx += 1
            if isinstance(it, dict):
                name_markup, avatar = determine_entity_link_and_avatar(it)
                val = it.get("value") or it.get("damage") or it.get("score") or it.get("wealth") or 0
                val_s = fmt_num(val)
                tier = it.get("tier") or it.get("rank") or None
                emb = make_embed_for_item(idx, total, name_markup, avatar, val_s, tier, endpoint)
                game_pages.append(emb)
                dev_json_pages.append(json.dumps(it, default=str))
            else:
                emb = discord.Embed(title=f"#{idx}", description=safe_truncate(str(it),1000), timestamp=now_utc())
                game_pages.append(emb)
                dev_json_pages.append(json.dumps(it, default=str))
        if not game_pages:
            game_pages.append(discord.Embed(title=endpoint, description="No data", timestamp=now_utc()))
            dev_json_pages.append(json.dumps(data, default=str))
        return game_pages, dev_json_pages

    # primitive fallback
    return [discord.Embed(title=endpoint, description=safe_truncate(str(data),1000), timestamp=now_utc())], [json.dumps(data, default=str)]

def make_embed_for_item(idx: int, total: Optional[int], name_markup: str, avatar: Optional[str], value_s: str, tier: Optional[str], endpoint: str) -> discord.Embed:
    """
    Make a single-embed leaderboard row like your requested format.
    """
    title = f"#{idx} — {name_markup}"
    desc = f"{ICON_DAMAGE} {value_s}"
    if tier:
        desc += f"\n· {safe_truncate(str(tier), 20)}"
    emb = discord.Embed(title=title, description=desc, color=discord.Color.dark_gold(), timestamp=now_utc())
    if avatar:
        try:
            emb.set_thumbnail(url=avatar)
        except Exception:
            pass
    if total:
        emb.set_footer(text=f"Showing {max(1, idx - ((idx-1)//PAGE_SIZE)*PAGE_SIZE)}-{min(total, ((idx-1)//PAGE_SIZE+1)*PAGE_SIZE} of {total}")
    else:
        emb.set_footer(text=endpoint)
    return emb

# ---------------- Dev View / Game View Page navigation ----------------
class LeaderboardView(View):
    def __init__(self, pages: List[discord.Embed], dev_json_pages: List[str], *, timeout: int = 300):
        super().__init__(timeout=timeout)
        self.pages = pages
        self.dev_json = dev_json_pages
        self.idx = 0
        self.mode = "game"
        self.prev_btn = Button(emoji="◀️", style=discord.ButtonStyle.secondary)
        self.toggle_btn = Button(label="🧠 Dev View", style=discord.ButtonStyle.primary)
        self.next_btn = Button(emoji="▶️", style=discord.ButtonStyle.secondary)
        self.add_item(self.prev_btn)
        self.add_item(self.toggle_btn)
        self.add_item(self.next_btn)
        self.prev_btn.callback = self.on_prev
        self.next_btn.callback = self.on_next
        self.toggle_btn.callback = self.on_toggle
        self._update()

    def _update(self):
        length = len(self.pages) if self.mode == "game" else len(self.dev_json)
        self.prev_btn.disabled = self.idx <= 0 or length <= 1
        self.next_btn.disabled = self.idx >= (length - 1) or length <= 1
        self.toggle_btn.label = "🧠 Dev View" if self.mode == "game" else "🎮 Game View"

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
            await interaction.followup.send("No dev JSON available for this page.", ephemeral=True)
            self.mode = "game"
            return
        await self._refresh(interaction)

    async def _refresh(self, interaction: discord.Interaction):
        if self.mode == "game":
            emb = self.pages[self.idx]
        else:
            j = self.dev_json[self.idx] if self.idx < len(self.dev_json) else "{}"
            emb = discord.Embed(title=f"🧠 Dev JSON (page {self.idx+1})", description=f"```json\n{j}\n```", color=discord.Color.dark_grey(), timestamp=now_utc())
        self._update()
        await interaction.followup.edit_message(interaction.message.id, embed=emb, view=self)

# ---------------- Safe defer helper ----------------
async def safe_defer(interaction: discord.Interaction, ephemeral: bool=False):
    if not interaction.response.is_done():
        try:
            await interaction.response.defer(ephemeral=ephemeral)
        except Exception:
            pass

# ---------------- Slash commands ----------------
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

@tree.command(name="help", description="Show WarEra commands")
async def help_cmd(interaction: discord.Interaction):
    await safe_defer(interaction)
    e = discord.Embed(title="WarEra — Commands", color=discord.Color.gold(), timestamp=now_utc())
    rows = [
        ("/rankings <type>", "Get ranking.getRanking"),
        ("/topdamage", "Aggregated top damage (users)"),
        ("/topwealth", "Aggregated top wealth (users)"),
        ("/toplandproducers", "Aggregated land production (users)"),
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
        ("/alerts subscribe/unsubscribe/list", "Manage alerts (DMs & channel)"),
    ]
    for k,v in rows:
        e.add_field(name=k, value=v, inline=False)
    await interaction.followup.send(embed=e)

# /rankings
@tree.command(name="rankings", description="Fetch ranking.getRanking")
@app_commands.choices(ranking_type=RANKING_CHOICES)
async def rankings_cmd(interaction: discord.Interaction, ranking_type: app_commands.Choice[str]):
    await safe_defer(interaction)
    pages, dev_json = await render_endpoint_to_pages("ranking.getRanking", {"rankingType": ranking_type.value})
    view = LeaderboardView(pages, dev_json)
    await interaction.followup.send(embed=pages[0], view=view)

# Aggregations
async def aggregate_users_from_ranking(ranking_type: str, limit:int=1000) -> List[Tuple[str, float]]:
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
                sums[str(uid)] = sums.get(str(uid), 0.0) + float(val)
            except:
                pass
    sorted_list = sorted(sums.items(), key=lambda kv: kv[1], reverse=True)
    return sorted_list

def ranking_list_to_pages(title: str, ranked: List[Tuple[str,float]], names_map: Optional[Dict[str,str]]=None) -> Tuple[List[discord.Embed], List[str]]:
    pages = []
    dev_json = []
    total = len(ranked)
    for idx,(uid,val) in enumerate(ranked, start=1):
        if idx > 2000: break
        name = (names_map or {}).get(uid) or uid
        name_markup = f"[{safe_truncate(name,40)}]({user_url(uid)})" if uid else safe_truncate(str(name),40)
        avatar = None
        # we could fetch avatar via user.getUserLite but avoid extra calls; leave None
        emb = make_embed_for_item(idx, total, name_markup, avatar, fmt_num(val), None, title)
        pages.append(emb)
        dev_json.append(json.dumps({"user":uid,"value":val}, default=str))
    if not pages:
        pages.append(discord.Embed(title=title, description="No data", timestamp=now_utc()))
        dev_json.append("[]")
    return pages, dev_json

@tree.command(name="topdamage", description="Top damage (aggregated from ranking.getRanking)")
async def topdamage_cmd(interaction: discord.Interaction):
    await safe_defer(interaction)
    ranked = await aggregate_users_from_ranking("userDamages")
    # optionally fetch names for top 50
    names_map = {}
    for uid,val in ranked[:50]:
        try:
            res = await war_api.call("user.getUserLite", {"userId": uid})
            if isinstance(res, dict):
                names_map[uid] = res.get("name") or res.get("username") or uid
        except:
            names_map[uid] = uid
    pages, dev_json = ranking_list_to_pages("🔥 Top Damage (aggregated)", ranked[:200], names_map)
    view = LeaderboardView(pages, dev_json)
    await interaction.followup.send(embed=pages[0], view=view)

@tree.command(name="topwealth", description="Top wealth (aggregated)")
async def topwealth_cmd(interaction: discord.Interaction):
    await safe_defer(interaction)
    ranked = await aggregate_users_from_ranking("userWealth")
    names_map = {}
    for uid,val in ranked[:50]:
        try:
            res = await war_api.call("user.getUserLite", {"userId": uid})
            if isinstance(res, dict):
                names_map[uid] = res.get("name") or res.get("username") or uid
        except:
            names_map[uid] = uid
    pages, dev_json = ranking_list_to_pages("💰 Top Wealth", ranked[:200], names_map)
    view = LeaderboardView(pages, dev_json)
    await interaction.followup.send(embed=pages[0], view=view)

@tree.command(name="toplandproducers", description="Top land producers (aggregated)")
async def topland_cmd(interaction: discord.Interaction):
    await safe_defer(interaction)
    ranked = await aggregate_users_from_ranking("userTerrain")
    names_map = {}
    for uid,val in ranked[:50]:
        try:
            res = await war_api.call("user.getUserLite", {"userId": uid})
            if isinstance(res, dict):
                names_map[uid] = res.get("name") or res.get("username") or uid
        except:
            names_map[uid] = uid
    pages, dev_json = ranking_list_to_pages("🌾 Top Land Producers", ranked[:200], names_map)
    view = LeaderboardView(pages, dev_json)
    await interaction.followup.send(embed=pages[0], view=view)

# topmu: list military units (MUs) — these are not users; link as MUs
@tree.command(name="topmu", description="Top military units (by invested/size)")
async def topmu_cmd(interaction: discord.Interaction):
    await safe_defer(interaction)
    # fetch many MUs
    res = await war_api.call("mu.getManyPaginated", {"page":1,"limit":200})
    items = []
    if isinstance(res, dict) and isinstance(res.get("items"), list):
        items = res["items"]
    elif isinstance(res, list):
        items = res
    scored = []
    for mu in items:
        if not isinstance(mu, dict): continue
        members = len(mu.get("members",[]))
        invested = mu.get("investedMoneyByUsers") or {}
        total_invest = 0.0
        if isinstance(invested, dict):
            for v in invested.values():
                try: total_invest += float(v)
                except: pass
        score = total_invest if total_invest>0 else members
        scored.append((mu, score))
    scored.sort(key=lambda x:x[1], reverse=True)
    pages = []
    dev_json = []
    for idx, (mu,score) in enumerate(scored, start=1):
        name = mu.get("name") or mu.get("_id") or str(idx)
        link = f"[{safe_truncate(name,40)}]({mu_url(mu.get('_id') or mu.get('id'))})" if mu.get("_id") or mu.get("id") else safe_truncate(name,40)
        avatar = extract_avatar(mu)
        emb = make_embed_for_item(idx, len(scored), link, avatar, fmt_num(score), None, "mu.getManyPaginated")
        pages.append(emb)
        dev_json.append(json.dumps(mu, default=str))
        if idx >= 200: break
    if not pages:
        pages.append(discord.Embed(title="Top MUs", description="No data", timestamp=now_utc()))
        dev_json.append("[]")
    view = LeaderboardView(pages, dev_json)
    await interaction.followup.send(embed=pages[0], view=view)

# ---------------- Standard endpoints (countries, regions, companies, etc) ----------------
async def send_pages_for_endpoint(interaction: discord.Interaction, endpoint: str, params: Optional[Dict]=None):
    await safe_defer(interaction)
    pages, dev_json = await render_endpoint_to_pages(endpoint, params)
    view = LeaderboardView(pages, dev_json)
    await interaction.followup.send(embed=pages[0], view=view)

@tree.command(name="countries", description="List countries")
async def countries_cmd(interaction: discord.Interaction):
    await send_pages_for_endpoint(interaction, "country.getAllCountries")

@tree.command(name="country", description="Get country by id")
@app_commands.describe(country_id="Country id")
async def country_cmd(interaction: discord.Interaction, country_id: str):
    await send_pages_for_endpoint(interaction, "country.getCountryById", {"countryId": country_id})

@tree.command(name="regions", description="Regions list")
async def regions_cmd(interaction: discord.Interaction):
    await send_pages_for_endpoint(interaction, "region.getRegionsObject")

@tree.command(name="region", description="Get region by id")
@app_commands.describe(region_id="Region id")
async def region_cmd(interaction: discord.Interaction, region_id: str):
    await send_pages_for_endpoint(interaction, "region.getById", {"regionId": region_id})

@tree.command(name="companies", description="List companies")
async def companies_cmd(interaction: discord.Interaction):
    await send_pages_for_endpoint(interaction, "company.getCompanies", {"page":1,"limit":50})

@tree.command(name="company", description="Get company by id")
@app_commands.describe(company_id="Company id")
async def company_cmd(interaction: discord.Interaction, company_id: str):
    await send_pages_for_endpoint(interaction, "company.getById", {"companyId": company_id})

@tree.command(name="battles", description="Active battles")
async def battles_cmd(interaction: discord.Interaction):
    await send_pages_for_endpoint(interaction, "battle.getBattles")

@tree.command(name="battle", description="Battle by id")
@app_commands.describe(battle_id="Battle id")
async def battle_cmd(interaction: discord.Interaction, battle_id: str):
    await send_pages_for_endpoint(interaction, "battle.getById", {"battleId": battle_id})

@tree.command(name="workoffers", description="Work offers (paginated)")
async def workoffers_cmd(interaction: discord.Interaction):
    await send_pages_for_endpoint(interaction, "workOffer.getWorkOffersPaginated", {"page":1,"limit":50})

@tree.command(name="workoffer", description="Work offer by id")
@app_commands.describe(offer_id="Workoffer id")
async def workoffer_cmd(interaction: discord.Interaction, offer_id: str):
    await send_pages_for_endpoint(interaction, "workOffer.getById", {"workOfferId": offer_id})

@tree.command(name="mu_list", description="List military units (paginated)")
async def mu_list_cmd(interaction: discord.Interaction):
    await send_pages_for_endpoint(interaction, "mu.getManyPaginated", {"page":1,"limit":50})

@tree.command(name="mu_by_id", description="MU by id")
@app_commands.describe(mu_id="MU id")
async def mu_by_id_cmd(interaction: discord.Interaction, mu_id: str):
    await send_pages_for_endpoint(interaction, "mu.getById", {"muId": mu_id})

@tree.command(name="articles", description="List articles")
async def articles_cmd(interaction: discord.Interaction):
    await send_pages_for_endpoint(interaction, "article.getArticlesPaginated", {"page":1,"limit":50})

@tree.command(name="article", description="Get article by id")
@app_commands.describe(article_id="Article id")
async def article_cmd(interaction: discord.Interaction, article_id: str):
    await send_pages_for_endpoint(interaction, "article.getArticleById", {"articleId": article_id})

@tree.command(name="prices", description="Item prices")
async def prices_cmd(interaction: discord.Interaction):
    await send_pages_for_endpoint(interaction, "itemTrading.getPrices")

@tree.command(name="transactions", description="Transactions list")
async def transactions_cmd(interaction: discord.Interaction):
    await send_pages_for_endpoint(interaction, "transaction.getPaginatedTransactions", {"page":1,"limit":50})

@tree.command(name="users", description="User by id")
@app_commands.describe(user_id="User id")
async def users_cmd(interaction: discord.Interaction, user_id: str):
    await send_pages_for_endpoint(interaction, "user.getUserLite", {"userId": user_id})

@tree.command(name="search", description="Search API")
@app_commands.describe(query="Search text")
async def search_cmd(interaction: discord.Interaction, query: str):
    await send_pages_for_endpoint(interaction, "search.searchAnything", {"searchText": query})

# ---------------- JSON debug (returns formatted code block, not embed) ----------------
class JsonPasteModal(Modal):
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

@tree.command(name="jsondebug", description="Paste JSON and get formatted code block")
async def jsondebug_cmd(interaction: discord.Interaction):
    modal = JsonPasteModal()
    await interaction.response.send_modal(modal)

# ---------------- Dashboard & controls ----------------
class IntervalModal(Modal):
    def __init__(self):
        super().__init__(title="Dashboard: Set interval (seconds)")
        self.input = TextInput(label="Seconds", placeholder=str(DEFAULT_DASH_INTERVAL), required=True)
        self.add_item(self.input)
    async def on_submit(self, interaction: discord.Interaction):
        val = self.input.value.strip()
        try:
            sec = int(val)
            if sec < 5:
                raise ValueError("min 5s")
            monitor.interval = sec
            if monitor_loop.is_running():
                monitor_loop.change_interval(seconds=sec)
            if dash_loop.is_running():
                dash_loop.change_interval(seconds=sec)
            state["dash_interval"] = sec
            await save_state()
            await interaction.response.send_message(f"✅ Interval set to {sec}s", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)

class DashboardControlView(View):
    def __init__(self):
        super().__init__(timeout=None)
        self.start = Button(label="▶️ Start Monitor", style=discord.ButtonStyle.success)
        self.stop = Button(label="⏸️ Stop Monitor", style=discord.ButtonStyle.danger)
        self.refresh = Button(label="🔁 Refresh Now", style=discord.ButtonStyle.secondary)
        self.interval = Button(label="⏱️ Set Interval", style=discord.ButtonStyle.secondary)
        self.clear = Button(label="🧹 Clear Alerts", style=discord.ButtonStyle.secondary)
        self.add_item(self.start); self.add_item(self.stop); self.add_item(self.refresh); self.add_item(self.interval); self.add_item(self.clear)
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
        await interaction.response.send_message(f"✅ Scanned: {len(alerts)} alerts.", ephemeral=True)

    async def on_interval(self, interaction: discord.Interaction):
        await interaction.response.send_modal(IntervalModal())

    async def on_clear(self, interaction: discord.Interaction):
        monitor.alerts.clear()
        state["monitor_alerts"] = []
        await save_state()
        await interaction.response.send_message("✅ Alerts cleared", ephemeral=True)

@tree.command(name="dashboard", description="Post or refresh live dashboard (auto-refresh edits same message for everyone)")
async def dashboard_cmd(interaction: discord.Interaction):
    await safe_defer(interaction)
    # build dashboard segments
    rank_pages, _ = await render_endpoint_to_pages("ranking.getRanking", {"rankingType":"userDamages"})
    rank_embed = rank_pages[0] if rank_pages else discord.Embed(title="Rankings", timestamp=now_utc())
    prices = await war_api.call("itemTrading.getPrices")
    pe = discord.Embed(title="💰 Prices", timestamp=now_utc())
    if isinstance(prices, dict):
        i=0
        for k,v in prices.items():
            if i>=8: break
            pe.add_field(name=safe_truncate(k,24), value=fmt_num(v), inline=True)
            i+=1
    else:
        pe.description = safe_truncate(str(prices),200)
    battles = await war_api.call("battle.getBattles")
    be = discord.Embed(title="⚔️ Battles", timestamp=now_utc())
    if isinstance(battles, list):
        for b in battles[:6]:
            if isinstance(b, dict):
                a = b.get("attackerCountry") or b.get("attacker")
                d = b.get("defenderCountry") or b.get("defender")
                s = b.get("status") or b.get("phase")
                be.add_field(name=f"{a} vs {d}", value=safe_truncate(str(s),50), inline=False)
            else:
                be.add_field(name=str(b), value="—", inline=False)
    else:
        be.description = safe_truncate(str(battles),200)
    alerts_embed = discord.Embed(title="🚨 Alerts", timestamp=now_utc())
    recent_alerts = monitor.alerts[:6] if hasattr(monitor, "alerts") else state.get("monitor_alerts", [])[:6]
    if recent_alerts:
        for a in recent_alerts:
            alerts_embed.add_field(name=f"{a.get('level','')} {a.get('category','')}", value=safe_truncate(a.get("message",""),80), inline=False)
    else:
        alerts_embed.description = "No alerts"

    # simple custom ranking (topdamage top2 pages)
    topdamage = await aggregate_users_from_ranking("userDamages")
    topdamage_pages, topdamage_dev = ranking_list_to_pages("🔥 Top Damage (agg)", topdamage[:50])
    game_pages = [rank_embed, pe, be, alerts_embed] + topdamage_pages[:2]
    dev_json_pages = [json.dumps({"endpoint":"ranking.getRanking"}), json.dumps(prices or {}, default=str), json.dumps(battles or {}, default=str), json.dumps(recent_alerts or {}, default=str)] + topdamage_dev[:2]
    view = LeaderboardView(game_pages, dev_json_pages)
    controls = DashboardControlView()

    # post or edit the saved dashboard message (channel chosen by env or command channel)
    channel = bot.get_channel(int(DASH_CHANNEL_ID)) if DASH_CHANNEL_ID else interaction.channel
    if channel is None:
        await interaction.followup.send("❌ Dashboard channel missing. Set WARERA_DASH_CHANNEL or run this command in a channel.", ephemeral=True)
        return

    dash = state.get("dash_message")
    posted_msg = None
    if dash:
        try:
            msg = await channel.fetch_message(int(dash["message_id"]))
            await msg.edit(embed=game_pages[0], view=view)
            posted_msg = msg
        except Exception:
            try:
                posted_msg = await channel.send(embed=game_pages[0], view=view)
                state["dash_message"] = {"channel_id": channel.id, "message_id": posted_msg.id}
                await save_state()
            except Exception:
                posted_msg = None
    else:
        posted_msg = await channel.send(embed=game_pages[0], view=view)
        state["dash_message"] = {"channel_id": channel.id, "message_id": posted_msg.id}
        await save_state()

    try:
        await interaction.followup.send("Dashboard controls (only you):", view=controls, ephemeral=True)
    except Exception:
        await interaction.followup.send("Dashboard controls:", view=controls)

    if not dash_loop.is_running():
        dash_loop.start()
    await interaction.followup.send("✅ Dashboard posted/updated.", ephemeral=True)

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
        rank_pages, _ = await render_endpoint_to_pages("ranking.getRanking", {"rankingType":"userDamages"})
        rank_embed = rank_pages[0] if rank_pages else discord.Embed(title="Rankings", timestamp=now_utc())
        prices = await war_api.call("itemTrading.getPrices")
        pe = discord.Embed(title="💰 Prices", timestamp=now_utc())
        if isinstance(prices, dict):
            i=0
            for k,v in prices.items():
                if i>=8: break
                pe.add_field(name=safe_truncate(k,24), value=fmt_num(v), inline=True)
                i+=1
        battles = await war_api.call("battle.getBattles")
        be = discord.Embed(title="⚔️ Battles", timestamp=now_utc())
        if isinstance(battles, list):
            for b in battles[:6]:
                if isinstance(b, dict):
                    a = b.get("attackerCountry") or b.get("attacker")
                    d = b.get("defenderCountry") or b.get("defender")
                    s = b.get("status") or b.get("phase")
                    be.add_field(name=f"{a} vs {d}", value=safe_truncate(str(s),50), inline=False)
        alerts_embed = discord.Embed(title="🚨 Alerts", timestamp=now_utc())
        recent_alerts = monitor.alerts[:6] if hasattr(monitor, "alerts") else state.get("monitor_alerts", [])[:6]
        if recent_alerts:
            for a in recent_alerts:
                alerts_embed.add_field(name=f"{a.get('level','')} {a.get('category','')}", value=safe_truncate(a.get("message",""),80), inline=False)
        pages = [rank_embed, pe, be, alerts_embed]
        dev_pages_json = [json.dumps({"endpoint":"ranking"}), json.dumps(prices or {}, default=str), json.dumps(battles or {}, default=str), json.dumps(recent_alerts or {}, default=str)]
        view = LeaderboardView(pages, dev_pages_json)
        await msg.edit(embed=pages[0], view=view)
    except Exception as e:
        print("[dash_loop] error:", e)

# ---------------- Lifecycle ----------------
@bot.event
async def on_ready():
    print(f"[WarEra Final] Logged in as {bot.user} (id={bot.user.id})")
    try:
        await tree.sync()
        print("[WarEra Final] slash commands synced")
    except Exception as e:
        print("[WarEra Final] sync error:", e)
    monitor.prev = state.get("monitor_prev", {})
    monitor.alerts = state.get("monitor_alerts", [])
    if not monitor_loop.is_running():
        monitor_loop.start()
    if state.get("dash_message") and not dash_loop.is_running():
        dash_loop.start()

# ---------------- Run ----------------
if __name__ == "__main__":
    if DISCORD_TOKEN == "YOUR_TOKEN_HERE":
        print("Set DISCORD_BOT_TOKEN env var and restart")
    else:
        bot.run(DISCORD_TOKEN)
