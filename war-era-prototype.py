# war-era-final.py
"""
WarEra Final — single-file slash-only Discord bot

Features:
- Slash commands for many WarEra endpoints (rankings, countries, companies, battles, mu, work offers, articles, transactions, prices, users, search)
- /dashboard auto-refresh (edits saved message every DEFAULT_DASH_INTERVAL seconds)
- Alerts system: posts to ALERT_CHANNEL_ID and DMs subscribed users
- Persisted state (alerts subscribers, dashboard message id, monitor snapshots) in STATE_PATH
- Pretty embeds: clickable links (app.warera.io paths), numbers formatted to 3 decimals, dates formatted, truncation
- Avatars shown in embed thumbnails (when API response provides avatar fields)
- Dev View toggles current page JSON only
- Custom ranking commands (/topdamage, /topwealth, /toplandproducers, /topmu)
- Safe defers, retry/backoff for API calls
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
DASH_CHANNEL_ID = os.getenv("WARERA_DASH_CHANNEL")        # optional channel id
ALERT_CHANNEL_ID = os.getenv("WARERA_ALERT_CHANNEL")      # optional channel id
STATE_PATH = os.getenv("WARERA_STATE_PATH", "state_warera.json")
REQUEST_TIMEOUT = float(os.getenv("WARERA_REQUEST_TIMEOUT", "10"))
RETRY_ATTEMPTS = int(os.getenv("WARERA_RETRY_ATTEMPTS", "3"))
RETRY_BACKOFF = float(os.getenv("WARERA_RETRY_BACKOFF", "0.6"))
DEFAULT_DASH_INTERVAL = int(os.getenv("WARERA_DASH_INTERVAL", "60"))  # seconds
PAGE_SIZE = int(os.getenv("WARERA_PAGE_SIZE", "8"))

# Optional custom theme images (replace if desired)
CUSTOM_EMOJIS = {
    "master": "https://i.imgur.com/8YgXGkX.png",
    "gold": "https://i.imgur.com/4YFQx4y.png",
    "silver": "https://i.imgur.com/1H4Zb6C.png",
    "bronze": "https://i.imgur.com/7h7k8G1.png",
}

# ---------------- Bot setup ----------------
intents = discord.Intents.default()
intents.message_content = False  # slash-only
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)
tree = bot.tree

# shared aiohttp session
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
        if isinstance(v, float):
            return f"{v:,.{decimals}f}"
        if isinstance(v, int):
            return f"{v:,}"
        # try parse numeric strings
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

def medal_for_tier(tier: Optional[str]) -> str:
    t = (tier or "").lower()
    if "master" in t or t.startswith("maste"):
        return CUSTOM_EMOJIS.get("master") or "🥇"
    if "gold" in t:
        return CUSTOM_EMOJIS.get("gold") or "🥈"
    if "silver" in t:
        return CUSTOM_EMOJIS.get("silver") or "🥉"
    if "bronze" in t:
        return CUSTOM_EMOJIS.get("bronze") or "🏅"
    return "🏵️"

# URLs requested
def user_url(uid: Optional[str]) -> str:
    if not uid:
        return ""
    return f"https://app.warera.io/user/{uid}"

def country_url(cid: Optional[str]) -> str:
    if not cid: return ""
    return f"https://app.warera.io/country/{cid}"

def region_url(rid: Optional[str]) -> str:
    if not rid: return ""
    return f"https://app.warera.io/region/{rid}"

def company_url(cid: Optional[str]) -> str:
    if not cid: return ""
    return f"https://app.warera.io/company/{cid}"

def mu_url(mid: Optional[str]) -> str:
    if not mid: return ""
    return f"https://app.warera.io/mu/{mid}"

# ---------------- Persistent State ----------------
_state_lock = asyncio.Lock()
DEFAULT_STATE = {
    "alerts_subscribers": [],   # discord user ids as strings
    "monitor_prev": {},
    "monitor_alerts": [],
    "dash_message": None,       # {channel_id, message_id}
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

# ---------------- WarEra API client (GET ?input=...) ----------------
class WarEraAPI:
    def __init__(self, base_url: str = API_BASE):
        self.base_url = base_url.rstrip("/")

    def _build_url(self, endpoint: str, params: Optional[dict]) -> str:
        ep = endpoint.strip().lstrip("/")
        url = f"{self.base_url}/{ep}"
        input_json = json.dumps(params or {}, separators=(",", ":"))
        encoded = urllib.parse.quote(input_json, safe="")
        return f"{url}?input={encoded}"

    async def call(self, endpoint: str, params: Optional[Dict] = None) -> Optional[Any]:
        url = self._build_url(endpoint, params)
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
                    # unwrap tRPC envelope
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
class AlertLevel(Enum):
    INFO = "🔵"
    WARNING = "🟡"
    CRITICAL = "🔴"

@dataclass
class Alert:
    ts: str
    level: str
    category: str
    title: str
    message: str
    data: dict = field(default_factory=dict)

class WarEraMonitor:
    def __init__(self, api: WarEraAPI):
        self.api = api
        self.running = False
        self.interval = DEFAULT_DASH_INTERVAL
        self.prev = state.get("monitor_prev", {})
        self.alerts: List[Dict] = state.get("monitor_alerts", [])
        self.price_threshold = 20.0
        self.price_critical = 50.0

    async def scan_once(self) -> List[Alert]:
        alerts: List[Alert] = []
        # prices
        prices = await self.api.call("itemTrading.getPrices")
        prev_prices = self.prev.get("itemTrading.getPrices")
        if isinstance(prices, dict) and isinstance(prev_prices, dict):
            for k, v in prices.items():
                if isinstance(v, (int, float)) and isinstance(prev_prices.get(k), (int, float)):
                    old = prev_prices.get(k)
                    if old != 0:
                        change = ((v - old) / abs(old)) * 100.0
                        if abs(change) >= self.price_threshold:
                            lvl = AlertLevel.CRITICAL.value if abs(change) >= self.price_critical else AlertLevel.WARNING.value
                            a = Alert(now_utc().isoformat(), lvl, "ECONOMY", f"Price {k}", f"{fmt_num(old)} → {fmt_num(v)} ({change:+.2f}%)", {"old": old, "new": v, "pct": change})
                            alerts.append(a)
        # battles new
        battles = await self.api.call("battle.getBattles")
        prev_battles = self.prev.get("battle.getBattles")
        if isinstance(battles, list) and isinstance(prev_battles, list):
            if len(battles) > len(prev_battles):
                diff = len(battles) - len(prev_battles)
                alerts.append(Alert(now_utc().isoformat(), AlertLevel.WARNING.value, "BATTLE", "New battles", f"+{diff} new battles", {"diff": diff}))
        # ranking top change
        ranking = await self.api.call("ranking.getRanking", {"rankingType": "userDamages"})
        prev_rank = self.prev.get("ranking.getRanking.userDamages")
        try:
            if isinstance(ranking, dict) and isinstance(prev_rank, dict):
                ni = (ranking.get("items") or [None])[0]
                pi = (prev_rank.get("items") or [None])[0]
                if isinstance(ni, dict) and isinstance(pi, dict) and ni.get("_id") != pi.get("_id"):
                    alerts.append(Alert(now_utc().isoformat(), AlertLevel.INFO.value, "RANKING", "Top Damage changed", f"{pi.get('user') or pi.get('_id')} → {ni.get('user') or ni.get('_id')}", {"old": pi, "new": ni}))
        except Exception:
            pass

        # persist previous
        self.prev["itemTrading.getPrices"] = prices if prices is not None else prev_prices
        self.prev["battle.getBattles"] = battles if battles is not None else prev_battles
        self.prev["ranking.getRanking.userDamages"] = ranking if ranking is not None else prev_rank
        state["monitor_prev"] = self.prev

        # persist alerts list (simplified)
        for a in alerts:
            state_alert = {"ts": a.ts, "level": a.level, "category": a.category, "title": a.title, "message": a.message, "data": a.data}
            self.alerts.insert(0, state_alert)
        state["monitor_alerts"] = self.alerts[:400]
        await save_state()
        return alerts

monitor = WarEraMonitor(war_api)

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
        if alerts and ALERT_CHANNEL_ID:
            ch = bot.get_channel(int(ALERT_CHANNEL_ID))
            if ch:
                summary = f"**🚨 WarEra Monitor — {len(alerts)} alerts**\n"
                by = {}
                for a in alerts:
                    by.setdefault(a.category, 0)
                    by[a.category] += 1
                for c, cnt in by.items():
                    summary += f"• {c}: {cnt}\n"
                await ch.send(summary)
                # DM subscribers
                subs = state.get("alerts_subscribers", [])
                for a in alerts[:12]:
                    emb = discord.Embed(title=f"{a.level} {a.category} — {a.title}", description=a.message, timestamp=datetime.fromisoformat(a.ts), color=(discord.Color.red() if a.level==AlertLevel.CRITICAL.value else discord.Color.gold()))
                    for k, v in a.data.items():
                        emb.add_field(name=str(k), value=safe_truncate(json.dumps(v, default=str), 256), inline=True)
                    await ch.send(embed=emb)
                    # DM subscribers (best-effort)
                    for uid in subs:
                        try:
                            user = await bot.fetch_user(int(uid))
                            if user:
                                await user.send(f"🚨 {a.title}: {a.message}")
                                await user.send(embed=emb)
                        except Exception:
                            pass
                if len(alerts) > 12:
                    await ch.send(f"⚠️ {len(alerts)-12} more suppressed")
    except Exception as e:
        print("[monitor_loop] error:", e)

# ---------------- UI helpers & JSON dev embed ----------------
MAX_DESC = 2048
MAX_FIELD = 1024

def make_game_embed(title: str, description: Optional[str] = None, color: discord.Color = discord.Color.blurple()) -> discord.Embed:
    return discord.Embed(title=title, description=safe_truncate(description, MAX_DESC) if description else None, timestamp=now_utc(), color=color)

def json_dev_embed(endpoint: str, data: Any, meta: Optional[Dict]=None) -> discord.Embed:
    try:
        j = json.dumps(data, indent=2, default=str)
    except Exception:
        j = str(data)
    if len(j) > 1900:
        j = j[:1897] + "..."
    title = f"🧠 DEV — {endpoint}"
    desc = ""
    if meta:
        desc += " • ".join([f"{k}:{v}" for k,v in meta.items()]) + "\n\n"
    desc += f"```json\n{j}\n```"
    return discord.Embed(title=title, description=desc, timestamp=now_utc(), color=discord.Color.dark_grey())

def add_small_fields(embed: discord.Embed, d: Dict[str,Any], limit: int = 8):
    added = 0
    for k, v in d.items():
        if added >= limit: break
        val = v if isinstance(v,(str,int,float,bool)) else json.dumps(v, default=str)
        embed.add_field(name=safe_truncate(str(k),64), value=safe_truncate(str(val), MAX_FIELD), inline=True)
        added += 1
    if len(d) > limit:
        embed.add_field(name="…", value=f"+{len(d)-limit} more", inline=False)

# ---------------- PageView (Game <-> Dev toggle, current page JSON only) ----------------
class GameDevPageView(View):
    def __init__(self, game_pages: List[discord.Embed], dev_pages_json: Optional[List[str]] = None, *, timeout: int = 180):
        super().__init__(timeout=timeout)
        self.game_pages = game_pages
        # dev_pages_json: list of JSON strings corresponding to game_pages
        self.dev_pages_json = dev_pages_json or []
        self.mode = "game"
        self.index = 0
        self.prev = Button(emoji="◀️", style=discord.ButtonStyle.secondary)
        self.toggle = Button(label="🧠 Dev View", style=discord.ButtonStyle.primary)
        self.next = Button(emoji="▶️", style=discord.ButtonStyle.secondary)
        self.add_item(self.prev); self.add_item(self.toggle); self.add_item(self.next)
        self.prev.callback = self.on_prev
        self.next.callback = self.on_next
        self.toggle.callback = self.on_toggle
        self._update_buttons()

    def _update_buttons(self):
        length = len(self.game_pages) if self.mode == "game" else len(self.dev_pages_json)
        self.prev.disabled = self.index <= 0 or length <= 1
        self.next.disabled = self.index >= (length - 1) or length <= 1
        self.toggle.label = "🧠 Dev View" if self.mode == "game" else "🎮 Game View"

    async def on_prev(self, interaction: discord.Interaction):
        await interaction.response.defer()
        self.index = max(0, self.index - 1)
        await self._refresh(interaction)

    async def on_next(self, interaction: discord.Interaction):
        await interaction.response.defer()
        length = len(self.game_pages) if self.mode == "game" else len(self.dev_pages_json)
        self.index = min(length - 1, self.index + 1)
        await self._refresh(interaction)

    async def on_toggle(self, interaction: discord.Interaction):
        await interaction.response.defer()
        self.mode = "dev" if self.mode == "game" else "game"
        # if dev view but no JSON for that page, show full API dev embed instead
        if self.mode == "dev" and (self.index >= len(self.dev_pages_json) or not self.dev_pages_json):
            await interaction.followup.send("No dev JSON for this page.", ephemeral=True)
            self.mode = "game"
            return
        await self._refresh(interaction)

    async def _refresh(self, interaction: discord.Interaction):
        if self.mode == "game":
            embed = self.game_pages[self.index]
        else:
            # dev: show only current page JSON inside a codeblock embed; limit size
            j = self.dev_pages_json[self.index] if self.index < len(self.dev_pages_json) else "{}"
            if len(j) > 1900:
                j = j[:1897] + "..."
            embed = discord.Embed(title=f"🧠 Dev JSON (page {self.index+1})", description=f"```json\n{j}\n```", timestamp=now_utc(), color=discord.Color.dark_grey())
        self._update_buttons()
        await interaction.followup.edit_message(interaction.message.id, embed=embed, view=self)

# JSON modal + button (for arbitrary endpoint)
class ViewJsonModal(Modal):
    def __init__(self, endpoint: str):
        super().__init__(title=f"View JSON — {endpoint}")
        self.endpoint = endpoint
        self.key = TextInput(label="Optional key (dot path)", required=False, placeholder="items.0")
        self.add_item(self.key)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        key = self.key.value.strip()
        data = await war_api.call(self.endpoint)
        if data is None:
            await interaction.followup.send("❌ Fetch failed", ephemeral=True)
            return
        if key:
            parts = key.split(".")
            try:
                sub = data
                for p in parts:
                    if isinstance(sub, dict):
                        sub = sub.get(p, f"❌ Key '{p}' missing")
                    elif isinstance(sub, list) and p.isdigit():
                        sub = sub[int(p)]
                    else:
                        sub = f"❌ Can't traverse '{p}'"
                data = sub
            except Exception as e:
                await interaction.followup.send(f"❌ Key error: {e}", ephemeral=True)
                return
        await interaction.followup.send(embed=json_dev_embed(self.endpoint, data, {"key": key or None}), ephemeral=True)

class ViewJsonButton(Button):
    def __init__(self, endpoint: str):
        super().__init__(label="🔍 View JSON", style=discord.ButtonStyle.secondary)
        self.endpoint = endpoint
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(ViewJsonModal(self.endpoint))

# ---------------- Entity formatting helpers (names -> links, avatars) ----------------
# Attempt to extract avatar url from entity dict
def extract_avatar(entity: dict) -> Optional[str]:
    for k in ("animatedAvatarUrl","avatarUrl","avatar","image","picture"):
        if k in entity and isinstance(entity[k], str) and entity[k].startswith("http"):
            return entity[k]
    # fallback: maybe user object nested
    if "user" in entity and isinstance(entity["user"], dict):
        return extract_avatar(entity["user"])
    return None

def entity_name_link(entity: dict) -> Tuple[str, Optional[str]]:
    """
    Returns (markup, avatar_url) where markup is [Name](url)
    It tries to produce user/company/region links preferentially.
    """
    # pick id/name fields
    name = entity.get("name") or entity.get("title") or entity.get("username") or str(entity.get("_id") or entity.get("id") or "")
    # user-like detection
    uid = entity.get("user") or entity.get("userId") or entity.get("id") or entity.get("_id")
    avatar = extract_avatar(entity)
    # If entity has 'user' field referencing id string
    if isinstance(uid, str) and len(uid) == 24:
        return (f"[{safe_truncate(name,40)}]({user_url(uid)})", avatar)
    # If entity has 'user' nested dict
    if isinstance(entity.get("user"), dict):
        nested = entity["user"]
        nested_id = nested.get("_id") or nested.get("id")
        nested_name = nested.get("name") or nested.get("username")
        av = extract_avatar(nested) or avatar
        if nested_id:
            return (f"[{safe_truncate(nested_name or nested_id,40)}]({user_url(nested_id)})", av)
    # company
    if entity.get("companyId") or entity.get("company"):
        cid = entity.get("companyId") or entity.get("company")
        return (f"[{safe_truncate(name,40)}]({company_url(cid)})", avatar)
    # region
    if entity.get("region"):
        rid = entity.get("region")
        return (f"[{safe_truncate(name,40)}]({region_url(rid)})", avatar)
    # country
    if entity.get("countryId") or entity.get("country"):
        cid = entity.get("countryId") or entity.get("country")
        return (f"[{safe_truncate(name,40)}]({country_url(cid)})", avatar)
    # mu
    if entity.get("_id") and "members" in entity:
        mid = entity.get("_id")
        return (f"[{safe_truncate(name,40)}]({mu_url(mid)})", avatar)
    # fallback: no link (return plain)
    return (safe_truncate(name,40), avatar)

# ---------------- Render endpoint -> game pages & dev JSON pages ----------------
async def render_endpoint(endpoint: str, params: Optional[Dict] = None) -> Tuple[List[discord.Embed], List[str]]:
    """
    Returns (game_embeds, dev_pages_json_strings)
    dev_pages_json_strings is a list of JSON strings per page (so Dev View shows current page json only)
    """
    data = await war_api.call(endpoint, params)
    if data is None:
        return [make_game_embed(endpoint, "❌ Failed to fetch", color=discord.Color.red())], [json.dumps({"error":"fetch failed"})]

    # dict responses that contain list of items
    if isinstance(data, dict):
        # find list keys
        list_keys = ["items","results","data"]
        for k in list_keys:
            if k in data and isinstance(data[k], list):
                items = data[k]
                # ranking-like?
                is_ranking = any(isinstance(it, dict) and any(key in it for key in ("tier","value","damage","score")) for it in items)
                # produce pages
                game_pages = []
                dev_json_pages = []
                total = len(items)
                for i in range(0, total, PAGE_SIZE):
                    chunk = items[i:i+PAGE_SIZE]
                    # build embed; set embed thumbnail to first avatar if present
                    embed = make_game_embed(f"📡 {endpoint}", f"Showing {i+1}-{i+len(chunk)} of {total}", color=discord.Color.dark_gold())
                    # if chunk provides first avatar -> use as thumbnail
                    thumb = None
                    if chunk:
                        first = chunk[0]
                        if isinstance(first, dict):
                            _, thumb = entity_name_link(first)
                    if thumb:
                        embed.set_thumbnail(url=thumb)
                    # add fields
                    for idx, it in enumerate(chunk, start=i+1):
                        if isinstance(it, dict):
                            name_markup, av = entity_name_link(it)
                            # prefer real name fields
                            val = it.get("value") or it.get("damage") or it.get("score") or it.get("wealth") or 0
                            tier = it.get("tier") or ""
                            val_s = fmt_num(val)
                            line = f"⚔️ **{val_s}**"
                            if tier:
                                line += f"\n· {safe_truncate(str(tier), 20)}"
                            embed.add_field(name=f"#{idx} — {name_markup}", value=line, inline=False)
                        else:
                            embed.add_field(name=f"#{idx}", value=safe_truncate(str(it), 200), inline=False)
                    game_pages.append(embed)
                    dev_json_pages.append(json.dumps(chunk, default=str))
                return game_pages, dev_json_pages

        # single object - show scalars
        if any(k in data for k in ("name","id","members","region","user","title")):
            e = make_game_embed(endpoint)
            small = {k:v for k,v in data.items() if isinstance(v,(str,int,float,bool))}
            add_small_fields(e, small, limit=12)
            return [e], [json.dumps(data, default=str)]

        # fallback: scalar dict: show key summary + dev
        e = make_game_embed(endpoint)
        small = {k:v for k,v in data.items() if isinstance(v,(str,int,float,bool))}
        add_small_fields(e, small, limit=10)
        return [e], [json.dumps(data, default=str)]

    # list responses
    if isinstance(data, list):
        # produce summary pages
        game_pages = []
        dev_json_pages = []
        total = len(data)
        for i in range(0, total, PAGE_SIZE):
            chunk = data[i:i+PAGE_SIZE]
            embed = make_game_embed(f"📡 {endpoint}", f"Showing {i+1}-{i+len(chunk)} of {total}")
            # thumbnail = first avatar if present
            thumb = None
            if chunk and isinstance(chunk[0], dict):
                _, thumb = entity_name_link(chunk[0])
            if thumb:
                embed.set_thumbnail(url=thumb)
            for idx, it in enumerate(chunk, start=i+1):
                if isinstance(it, dict):
                    name_markup, av = entity_name_link(it)
                    keys_shown = []
                    for k in ("region","status","price","value","rank","company","members"):
                        if k in it:
                            keys_shown.append(f"{k}:{fmt_num(it[k])}")
                    embed.add_field(name=f"#{idx} — {name_markup}", value=", ".join(keys_shown) or "—", inline=False)
                else:
                    embed.add_field(name=f"#{idx}", value=safe_truncate(str(it),200), inline=False)
            game_pages.append(embed)
            dev_json_pages.append(json.dumps(chunk, default=str))
        if not game_pages:
            game_pages.append(make_game_embed(endpoint, "No data"))
            dev_json_pages.append(json.dumps(data, default=str))
        return game_pages, dev_json_pages

    # fallback primitive
    return [make_game_embed(endpoint, safe_truncate(str(data), 1000))], [json.dumps(data, default=str)]

# ---------------- Aggregations for custom ranking commands ----------------
_user_cache: Dict[str,str] = {}  # uid -> display name cache

async def fetch_user_name(uid: str) -> str:
    # try cache
    if uid in _user_cache:
        return _user_cache[uid]
    # call user.getUserLite
    res = await war_api.call("user.getUserLite", {"userId": uid})
    name = None
    if isinstance(res, dict):
        name = res.get("name") or res.get("username") or uid
    else:
        name = uid
    _user_cache[uid] = name
    return name

async def aggregate_ranking_by_user(ranking_type: str) -> List[Tuple[str, float]]:
    # best-effort: call ranking.getRanking and sum values per user
    sums: Dict[str, float] = {}
    data = await war_api.call("ranking.getRanking", {"rankingType": ranking_type})
    items = []
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        items = data["items"]
    elif isinstance(data, list):
        items = data
    for it in items:
        if isinstance(it, dict):
            uid = it.get("user") or it.get("_id") or it.get("id")
            if uid is None: continue
            val = it.get("value") or it.get("damage") or it.get("score") or it.get("wealth") or 0
            try:
                sums[str(uid)] = sums.get(str(uid), 0.0) + float(val)
            except:
                pass
    # sort
    sorted_list = sorted(sums.items(), key=lambda kv: kv[1], reverse=True)
    return sorted_list

def ranking_to_embeds(title: str, ranked: List[Tuple[str,float]], names_map: Optional[Dict[str,str]] = None) -> Tuple[List[discord.Embed], List[str]]:
    pages = []
    dev_json_pages = []
    total = len(ranked)
    for i in range(0, total, PAGE_SIZE):
        chunk = ranked[i:i+PAGE_SIZE]
        embed = make_game_embed(title, f"Showing {i+1}-{i+len(chunk)} of {total}", color=discord.Color.dark_gold())
        # set thumbnail to generic (if you prefer)
        for idx, (uid, val) in enumerate(chunk, start=i+1):
            name = (names_map or {}).get(uid) or uid
            link = f"[{safe_truncate(name,36)}]({user_url(uid)})" if uid else safe_truncate(name,36)
            embed.add_field(name=f"#{idx} — {link}", value=f"⚔️ **{fmt_num(val)}**", inline=False)
        pages.append(embed)
        dev_json_pages.append(json.dumps(chunk, default=str))
    if not pages:
        pages.append(make_game_embed(title,"No data"))
        dev_json_pages.append(json.dumps(ranked, default=str))
    return pages, dev_json_pages

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
    e = make_game_embed("🎮 WarEra Commands", color=discord.Color.gold())
    cmds = [
        ("/rankings <type>", "View ranking.getRanking (choose type)"),
        ("/topdamage", "Aggregated top damage"),
        ("/topwealth", "Aggregated top wealth"),
        ("/toplandproducers", "Aggregated land producers"),
        ("/topmu", "Top military units"),
        ("/dashboard", "Post/refresh dashboard (auto-refresh)"),
        ("/alerts subscribe/unsubscribe/list", "Manage alerts subscription"),
        ("/jsondebug", "Paste JSON to pretty-print"),
        ("/countries /companies /battles /prices /transactions /users <id> /search <q>", "Other endpoints"),
    ]
    for c,d in cmds:
        e.add_field(name=c, value=d, inline=False)
    await interaction.followup.send(embed=e)

@tree.command(name="rankings", description="View WarEra ranking")
@app_commands.choices(ranking_type=RANKING_CHOICES)
async def rankings_cmd(interaction: discord.Interaction, ranking_type: app_commands.Choice[str]):
    await safe_defer(interaction)
    game_pages, dev_pages_json = await render_endpoint("ranking.getRanking", {"rankingType": ranking_type.value})
    view = GameDevPageView(game_pages, dev_pages_json)
    await interaction.followup.send(embed=game_pages[0], view=view)

# Custom aggregated commands
@tree.command(name="topdamage", description="Top damage (aggregated)")
async def topdamage_cmd(interaction: discord.Interaction):
    await safe_defer(interaction)
    ranked = await aggregate_ranking_by_user("userDamages")
    # fetch a few user names for display
    names_map = {}
    for uid, _ in ranked[:30]:
        names_map[uid] = await fetch_user_name(uid)
    pages, dev_json = ranking_to_embeds("🔥 Top Damage (aggregated)", ranked, names_map)
    view = GameDevPageView(pages, dev_json)
    await interaction.followup.send(embed=pages[0], view=view)

@tree.command(name="topwealth", description="Top wealth (aggregated)")
async def topwealth_cmd(interaction: discord.Interaction):
    await safe_defer(interaction)
    ranked = await aggregate_ranking_by_user("userWealth")
    names_map = {}
    for uid,_ in ranked[:30]:
        names_map[uid] = await fetch_user_name(uid)
    pages, dev_json = ranking_to_embeds("💰 Top Wealth", ranked, names_map)
    view = GameDevPageView(pages, dev_json)
    await interaction.followup.send(embed=pages[0], view=view)

@tree.command(name="toplandproducers", description="Top land producers")
async def topland_cmd(interaction: discord.Interaction):
    await safe_defer(interaction)
    ranked = await aggregate_ranking_by_user("userTerrain")
    names_map = {}
    for uid,_ in ranked[:30]:
        names_map[uid] = await fetch_user_name(uid)
    pages, dev_json = ranking_to_embeds("🌾 Top Land Producers", ranked, names_map)
    view = GameDevPageView(pages, dev_json)
    await interaction.followup.send(embed=pages[0], view=view)

@tree.command(name="topmu", description="Top military units")
async def topmu_cmd(interaction: discord.Interaction):
    await safe_defer(interaction)
    data = await war_api.call("mu.getManyPaginated", {"page":1,"limit":200})
    items = []
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        items = data["items"]
    elif isinstance(data, list):
        items = data
    scored = []
    for mu in items:
        if not isinstance(mu, dict): continue
        members = len(mu.get("members", []))
        invested = mu.get("investedMoneyByUsers") or {}
        total_invest = 0.0
        if isinstance(invested, dict):
            for v in invested.values():
                try:
                    total_invest += float(v)
                except:
                    pass
        score = total_invest if total_invest>0 else members
        scored.append((mu.get("_id") or mu.get("id") or mu.get("name"), score, mu))
    scored.sort(key=lambda t:t[1], reverse=True)
    pages = []
    dev_json = []
    for i in range(0, len(scored), PAGE_SIZE):
        chunk = scored[i:i+PAGE_SIZE]
        embed = make_game_embed("🎖️ Top MUs", f"Showing {i+1}-{i+len(chunk)} of {len(scored)}", color=discord.Color.dark_teal())
        for idx, (mid, score, muobj) in enumerate(chunk, start=i+1):
            name = safe_truncate(muobj.get("name") or str(mid), 36)
            link = f"[{name}]({mu_url(mid)})" if mu_url(mid) else name
            embed.add_field(name=f"#{idx} {link}", value=f"Members: {len(muobj.get('members',[]))}\nScore: {fmt_num(score)}", inline=False)
        pages.append(embed)
        dev_json.append(json.dumps([m for (_,_,m) in chunk], default=str))
    if not pages:
        pages.append(make_game_embed("Top MUs", "No data"))
        dev_json.append(json.dumps(scored, default=str))
    view = GameDevPageView(pages, dev_json)
    await interaction.followup.send(embed=pages[0], view=view)

# Common endpoint commands (many)
@tree.command(name="countries", description="List countries")
async def countries_cmd(interaction: discord.Interaction):
    await safe_defer(interaction)
    game, dev_json = await render_endpoint("country.getAllCountries")
    await interaction.followup.send(embed=game[0], view=GameDevPageView(game, dev_json))

@tree.command(name="companies", description="List companies")
async def companies_cmd(interaction: discord.Interaction):
    await safe_defer(interaction)
    game, dev_json = await render_endpoint("company.getCompanies", {"page":1,"limit":50})
    await interaction.followup.send(embed=game[0], view=GameDevPageView(game, dev_json))

@tree.command(name="company", description="Company by id")
@app_commands.describe(company_id="Company id")
async def company_cmd(interaction: discord.Interaction, company_id: str):
    await safe_defer(interaction)
    game, dev_json = await render_endpoint("company.getById", {"companyId": company_id})
    await interaction.followup.send(embed=game[0], view=GameDevPageView(game, dev_json))

@tree.command(name="battles", description="Active battles")
async def battles_cmd(interaction: discord.Interaction):
    await safe_defer(interaction)
    game, dev_json = await render_endpoint("battle.getBattles")
    await interaction.followup.send(embed=game[0], view=GameDevPageView(game, dev_json))

@tree.command(name="battle", description="Battle by id")
@app_commands.describe(battle_id="Battle id")
async def battle_cmd(interaction: discord.Interaction, battle_id: str):
    await safe_defer(interaction)
    game, dev_json = await render_endpoint("battle.getById", {"battleId": battle_id})
    await interaction.followup.send(embed=game[0], view=GameDevPageView(game, dev_json))

@tree.command(name="workoffers", description="Work offers")
async def workoffers_cmd(interaction: discord.Interaction):
    await safe_defer(interaction)
    game, dev_json = await render_endpoint("workOffer.getWorkOffersPaginated", {"page":1,"limit":50})
    await interaction.followup.send(embed=game[0], view=GameDevPageView(game, dev_json))

@tree.command(name="workoffer", description="Work offer by id")
@app_commands.describe(offer_id="WorkOffer id")
async def workoffer_cmd(interaction: discord.Interaction, offer_id: str):
    await safe_defer(interaction)
    game, dev_json = await render_endpoint("workOffer.getById", {"workOfferId": offer_id})
    await interaction.followup.send(embed=game[0], view=GameDevPageView(game, dev_json))

@tree.command(name="mu", description="List military units")
async def mu_cmd(interaction: discord.Interaction):
    await safe_defer(interaction)
    game, dev_json = await render_endpoint("mu.getManyPaginated", {"page":1,"limit":50})
    await interaction.followup.send(embed=game[0], view=GameDevPageView(game, dev_json))

@tree.command(name="mu_by_id", description="Get MU by id")
@app_commands.describe(mu_id="MU id")
async def mu_by_id_cmd(interaction: discord.Interaction, mu_id: str):
    await safe_defer(interaction)
    game, dev_json = await render_endpoint("mu.getById", {"muId": mu_id})
    await interaction.followup.send(embed=game[0], view=GameDevPageView(game, dev_json))

@tree.command(name="articles", description="List articles")
async def articles_cmd(interaction: discord.Interaction):
    await safe_defer(interaction)
    game, dev_json = await render_endpoint("article.getArticlesPaginated", {"page":1,"limit":50})
    await interaction.followup.send(embed=game[0], view=GameDevPageView(game, dev_json))

@tree.command(name="article", description="Article by id")
@app_commands.describe(article_id="Article id")
async def article_cmd(interaction: discord.Interaction, article_id: str):
    await safe_defer(interaction)
    game, dev_json = await render_endpoint("article.getArticleById", {"articleId": article_id})
    await interaction.followup.send(embed=game[0], view=GameDevPageView(game, dev_json))

@tree.command(name="prices", description="Item prices")
async def prices_cmd(interaction: discord.Interaction):
    await safe_defer(interaction)
    game, dev_json = await render_endpoint("itemTrading.getPrices")
    await interaction.followup.send(embed=game[0], view=GameDevPageView(game, dev_json))

@tree.command(name="transactions", description="Transactions")
async def transactions_cmd(interaction: discord.Interaction):
    await safe_defer(interaction)
    game, dev_json = await render_endpoint("transaction.getPaginatedTransactions", {"page":1,"limit":50})
    await interaction.followup.send(embed=game[0], view=GameDevPageView(game, dev_json))

@tree.command(name="users", description="User by id")
@app_commands.describe(user_id="User id")
async def users_cmd(interaction: discord.Interaction, user_id: str):
    await safe_defer(interaction)
    game, dev_json = await render_endpoint("user.getUserLite", {"userId": user_id})
    await interaction.followup.send(embed=game[0], view=GameDevPageView(game, dev_json))

@tree.command(name="search", description="Search anything")
@app_commands.describe(query="Search text")
async def search_cmd(interaction: discord.Interaction, query: str):
    await safe_defer(interaction)
    game, dev_json = await render_endpoint("search.searchAnything", {"searchText": query})
    await interaction.followup.send(embed=game[0], view=GameDevPageView(game, dev_json))

# JSON paste debugger
class ManualJsonModal(Modal):
    def __init__(self):
        super().__init__(title="JSON Debugger")
        self.input = TextInput(label="Paste JSON", style=discord.TextStyle.long, required=True, max_length=4000)
        self.add_item(self.input)
    async def on_submit(self, interaction: discord.Interaction):
        await safe_defer(interaction, ephemeral=True)
        try:
            parsed = json.loads(self.input.value)
        except Exception as e:
            await interaction.followup.send(f"❌ Invalid JSON: {e}", ephemeral=True)
            return
        await interaction.followup.send(embed=json_dev_embed("manual", parsed, {"source":"paste"}), ephemeral=True)

@tree.command(name="jsondebug", description="Paste JSON and get dev embed")
async def jsondebug_cmd(interaction: discord.Interaction):
    await interaction.response.send_modal(ManualJsonModal())

# ---------------- Alerts subscribe/unsubscribe ----------------
@tree.command(name="alerts", description="Manage alerts subscription (subscribe/unsubscribe/list)")
@app_commands.describe(action="subscribe, unsubscribe, or list")
async def alerts_cmd(interaction: discord.Interaction, action: str):
    await safe_defer(interaction, ephemeral=True)
    uid = str(interaction.user.id)
    subs = state.get("alerts_subscribers", [])
    act = action.lower()
    if act == "subscribe":
        if uid in subs:
            await interaction.followup.send("You are already subscribed to DMs.", ephemeral=True)
            return
        subs.append(uid)
        state["alerts_subscribers"] = subs
        await save_state()
        await interaction.followup.send("✅ Subscribed to alerts via DM.", ephemeral=True)
        return
    if act == "unsubscribe":
        if uid in subs:
            subs.remove(uid)
            state["alerts_subscribers"] = subs
            await save_state()
            await interaction.followup.send("✅ Unsubscribed.", ephemeral=True)
            return
        await interaction.followup.send("You weren't subscribed.", ephemeral=True)
        return
    if act == "list":
        await interaction.followup.send(f"Subscribers: {len(subs)}", ephemeral=True)
        return
    await interaction.followup.send("Usage: /alerts subscribe|unsubscribe|list", ephemeral=True)

# ---------------- Dashboard & controls (auto-refreshing) ----------------
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
            if monitor_loop.is_running():
                monitor_loop.change_interval(seconds=sec)
            if dash_loop.is_running():
                dash_loop.change_interval(seconds=sec)
            state["dash_interval"] = sec
            await save_state()
            await interaction.response.send_message(f"✅ Interval set to {sec}s", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"❌ Invalid: {e}", ephemeral=True)

class DashboardControlsView(View):
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

@tree.command(name="dashboard", description="Post or refresh live dashboard (auto-refresh edits same message)")
async def dashboard_cmd(interaction: discord.Interaction):
    await safe_defer(interaction)
    # build dashboard pages: ranking, prices, battles, alerts, custom rankings
    rank_pages, rank_dev = await render_endpoint("ranking.getRanking", {"rankingType":"userDamages"})
    rank_embed = rank_pages[0] if rank_pages else make_game_embed("Rankings")
    prices_data = await war_api.call("itemTrading.getPrices")
    pe = make_game_embed("💰 Prices")
    if isinstance(prices_data, dict):
        i=0
        for k,v in prices_data.items():
            if i>=8: break
            pe.add_field(name=safe_truncate(k,24), value=fmt_num(v), inline=True)
            i+=1
    else:
        pe.description = safe_truncate(str(prices_data),200)
    battles_data = await war_api.call("battle.getBattles")
    be = make_game_embed("⚔️ Battles")
    if isinstance(battles_data, list):
        for b in battles_data[:6]:
            if isinstance(b, dict):
                a = b.get("attackerCountry") or b.get("attacker")
                d = b.get("defenderCountry") or b.get("defender")
                s = b.get("status") or b.get("phase")
                be.add_field(name=f"{a} vs {d}", value=safe_truncate(str(s),50), inline=False)
            else:
                be.add_field(name=str(b), value="—", inline=False)
    else:
        be.description = safe_truncate(str(battles_data),200)
    alerts_embed = make_game_embed("🚨 Alerts Summary")
    recent_alerts = monitor.alerts[:6] if hasattr(monitor, "alerts") else state.get("monitor_alerts", [])[:6]
    if recent_alerts:
        for a in recent_alerts:
            alerts_embed.add_field(name=f"{a.get('level','')} {a.get('category','')}", value=safe_truncate(a.get("message",""),80), inline=False)
    else:
        alerts_embed.description = "No alerts"

    # Custom ranking: topdamage
    topdamage_list = await aggregate_ranking_by_user("userDamages")
    topdamage_pages, topdamage_dev = ranking_to_embeds("🔥 Top Damage (aggregated)", topdamage_list[:40], {})
    # assemble pages & dev JSON pages
    game_pages = [rank_embed, pe, be, alerts_embed] + topdamage_pages[:2]
    dev_pages_json = [json.dumps({"endpoint":"ranking.getRanking"}), json.dumps(prices_data, default=str), json.dumps(battles_data, default=str), json.dumps(recent_alerts, default=str)] + topdamage_dev[:2]
    view = GameDevPageView(game_pages, dev_pages_json)
    controls = DashboardControlsView()

    # post or edit existing dashboard msg (persist in state)
    channel = bot.get_channel(int(DASH_CHANNEL_ID)) if DASH_CHANNEL_ID else interaction.channel
    if channel is None:
        await interaction.followup.send("❌ Dashboard channel not found. Set WARERA_DASH_CHANNEL or run this command in a channel.", ephemeral=True)
        return
    dash_info = state.get("dash_message")
    posted_msg = None
    if dash_info:
        try:
            # try edit
            msg = await channel.fetch_message(int(dash_info["message_id"]))
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

    # ephemeral controls for the caller
    try:
        await interaction.followup.send("Dashboard controls (only you):", view=controls, ephemeral=True)
    except Exception:
        await interaction.followup.send("Dashboard controls:", view=controls)

    # ensure dash_loop runs
    if not dash_loop.is_running():
        dash_loop.start()
    await interaction.followup.send("✅ Dashboard posted/updated.", ephemeral=True)

# dash loop edits saved message (auto-refresh)
@tasks.loop(seconds=DEFAULT_DASH_INTERVAL)
async def dash_loop():
    dash_info = state.get("dash_message")
    if not dash_info:
        return
    try:
        ch = bot.get_channel(int(dash_info["channel_id"]))
        if ch is None:
            return
        msg = await ch.fetch_message(int(dash_info["message_id"]))
        # rebuild simple dashboard embeds (same as above but minimal)
        rank_pages, _ = await render_endpoint("ranking.getRanking", {"rankingType":"userDamages"})
        rank_embed = rank_pages[0] if rank_pages else make_game_embed("Rankings")
        prices_data = await war_api.call("itemTrading.getPrices")
        pe = make_game_embed("💰 Prices")
        if isinstance(prices_data, dict):
            i=0
            for k,v in prices_data.items():
                if i>=8: break
                pe.add_field(name=safe_truncate(k,24), value=fmt_num(v), inline=True)
                i+=1
        battles_data = await war_api.call("battle.getBattles")
        be = make_game_embed("⚔️ Battles")
        if isinstance(battles_data, list):
            for b in battles_data[:6]:
                if isinstance(b, dict):
                    a = b.get("attackerCountry") or b.get("attacker")
                    d = b.get("defenderCountry") or b.get("defender")
                    s = b.get("status") or b.get("phase")
                    be.add_field(name=f"{a} vs {d}", value=safe_truncate(str(s),50), inline=False)
        alerts_embed = make_game_embed("🚨 Alerts Summary")
        recent_alerts = monitor.alerts[:6] if hasattr(monitor, "alerts") else state.get("monitor_alerts", [])[:6]
        if recent_alerts:
            for a in recent_alerts:
                alerts_embed.add_field(name=f"{a.get('level','')} {a.get('category','')}", value=safe_truncate(a.get("message",""),80), inline=False)
        pages = [rank_embed, pe, be, alerts_embed]
        dev_pages_json = [json.dumps({"endpoint":"ranking"}), json.dumps(prices_data or {}, default=str), json.dumps(battles_data or {}, default=str), json.dumps(recent_alerts or {}, default=str)]
        view = GameDevPageView(pages, dev_pages_json)
        await msg.edit(embed=pages[0], view=view)
    except Exception as e:
        # log and continue
        print("[dash_loop] error:", e)

# ---------------- Lifecycle ----------------
@bot.event
async def on_ready():
    print(f"[WarEra Final] Logged in as {bot.user} (id={bot.user.id})")
    try:
        await tree.sync()
        print("[WarEra Final] Slash commands synced.")
    except Exception as e:
        print("[WarEra Final] Slash sync failed:", e)
    # restore monitor prev/alerts
    monitor.prev = state.get("monitor_prev", {})
    monitor.alerts = state.get("monitor_alerts", [])
    # start monitor loop (only acts if monitor.running)
    if not monitor_loop.is_running():
        monitor_loop.start()
    # start dash loop if dash message exists
    if state.get("dash_message") and not dash_loop.is_running():
        dash_loop.start()

# ---------------- Run ----------------
if __name__ == "__main__":
    if DISCORD_TOKEN == "YOUR_TOKEN_HERE":
        print("Set DISCORD_BOT_TOKEN environment variable and restart.")
    else:
        bot.run(DISCORD_TOKEN)
