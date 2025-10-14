# war-era-full.py
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
DEFAULT_DASH_INTERVAL = int(os.getenv("WARERA_DASH_INTERVAL", "60"))  # seconds
PAGE_SIZE = int(os.getenv("WARERA_PAGE_SIZE", "8"))

# Custom emoji/image URLs (replace with your assets)
CUSTOM_EMOJIS = {
    "master": "https://i.imgur.com/8YgXGkX.png",
    "gold": "https://i.imgur.com/4YFQx4y.png",
    "silver": "https://i.imgur.com/1H4Zb6C.png",
    "bronze": "https://i.imgur.com/7h7k8G1.png",
    "mu": "https://i.imgur.com/d6QfFv6.png",
    "country": "https://i.imgur.com/3k8QH8x.png",
    "company": "https://i.imgur.com/9u9p1Yy.png",
    "fire": "https://i.imgur.com/7y2KQ8b.png",
}

# ---------------- Bot setup ----------------
intents = discord.Intents.default()
intents.message_content = False  # slash-only
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)
tree = bot.tree

# Shared aiohttp session
_session: Optional[aiohttp.ClientSession] = None

async def get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT))
    return _session

# ---------------- Utilities (formatting, links, state) ----------------
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
        # Try numeric string
        fv = float(v)
        return f"{fv:,.{decimals}f}"
    except Exception:
        return str(v)

def medal_for_tier(tier: Optional[str]) -> str:
    t = (tier or "").lower()
    if "master" in t or t.startswith("maste"): return CUSTOM_EMOJIS.get("master") or "🥇"
    if "gold" in t: return CUSTOM_EMOJIS.get("gold") or "🥈"
    if "silver" in t: return CUSTOM_EMOJIS.get("silver") or "🥉"
    if "bronze" in t: return CUSTOM_EMOJIS.get("bronze") or "🏅"
    return CUSTOM_EMOJIS.get("mu") or "🏵️"

# URL rules (user requested app.warera.io paths)
def user_url(uid: Optional[str]) -> str:
    if not uid: return ""
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

# ---------------- Simple persistent state ----------------
_state_lock = asyncio.Lock()
DEFAULT_STATE = {
    "alerts_subscribers": [],   # list of user ids
    "monitor_prev": {},         # previous snapshots for monitor
    "monitor_alerts": [],       # last alerts list
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

# load at start
load_state()

# ---------------- WarEra API client ----------------
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
                    # Unwrap tRPC envelope if present
                    if isinstance(data, dict) and "result" in data:
                        res = data["result"]
                        if isinstance(res, dict) and "data" in res:
                            return res["data"]
                        return res
                    return data
            except Exception as e:
                last_exc = e
                await asyncio.sleep(RETRY_BACKOFF * (2 ** attempt))
        print(f"[WarEraAPI] failed {endpoint} params={params} -> {last_exc}")
        return None

war_api = WarEraAPI()

# ---------------- Alerts / Monitor ----------------
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
        # thresholds
        self.price_threshold = 20.0
        self.critical_price = 50.0

    async def scan_once(self) -> List[Alert]:
        alerts: List[Alert] = []
        # check prices
        prices = await self.api.call("itemTrading.getPrices")
        prev_prices = self.prev.get("itemTrading.getPrices")
        if isinstance(prices, dict) and isinstance(prev_prices, dict):
            for k, v in prices.items():
                if isinstance(v, (int, float)) and k in prev_prices and isinstance(prev_prices[k], (int, float)):
                    old = prev_prices[k]
                    if old != 0:
                        change = ((v - old) / abs(old)) * 100.0
                        if abs(change) >= self.price_threshold:
                            level = AlertLevel.CRITICAL if abs(change) >= self.critical_price else AlertLevel.WARNING
                            a = Alert(now_utc().isoformat(), level.value, "ECONOMY", f"Price {k}", f"{old} → {v} ({change:+.2f}%)", {"old": old, "new": v, "pct": change})
                            alerts.append(a)
        # battles new
        battles = await self.api.call("battle.getBattles")
        prev_battles = self.prev.get("battle.getBattles")
        if isinstance(battles, list) and isinstance(prev_battles, list):
            if len(battles) > len(prev_battles):
                diff = len(battles) - len(prev_battles)
                a = Alert(now_utc().isoformat(), AlertLevel.WARNING.value, "BATTLE", "New battles", f"+{diff} new battles", {"diff": diff})
                alerts.append(a)
        # ranking top changed
        ranking = await self.api.call("ranking.getRanking", {"rankingType":"userDamages"})
        prev_ranking = self.prev.get("ranking.getRanking.userDamages")
        try:
            if isinstance(ranking, dict) and isinstance(prev_ranking, dict):
                new_top = (ranking.get("items") or [None])[0]
                old_top = (prev_ranking.get("items") or [None])[0]
                if isinstance(new_top, dict) and isinstance(old_top, dict):
                    if new_top.get("_id") != old_top.get("_id"):
                        a = Alert(now_utc().isoformat(), AlertLevel.INFO.value, "RANKING", "Top Damage changed", f"{old_top.get('user') or old_top.get('_id')} → {new_top.get('user') or new_top.get('_id')}", {"old": old_top, "new": new_top})
                        alerts.append(a)
        except Exception:
            pass

        # save prev snapshots
        self.prev["itemTrading.getPrices"] = prices if prices is not None else prev_prices
        self.prev["battle.getBattles"] = battles if battles is not None else prev_battles
        self.prev["ranking.getRanking.userDamages"] = ranking if ranking is not None else prev_ranking
        # persist
        state["monitor_prev"] = self.prev
        # convert alerts to serializable and persist the last alerts
        for a in alerts:
            state_alert = {"ts": a.ts, "level": a.level, "category": a.category, "title": a.title, "message": a.message, "data": a.data}
            self.alerts.insert(0, state_alert)
        state["monitor_alerts"] = self.alerts[:200]  # cap
        await save_state()
        return alerts

monitor = WarEraMonitor(war_api)

# Monitor loop (runs but only acts when monitor.running)
@tasks.loop(seconds=DEFAULT_DASH_INTERVAL)
async def monitor_loop():
    # adjust interval dynamically
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
                for a in alerts[:15]:
                    emb = discord.Embed(title=f"{a.level} {a.category} — {a.title}", description=a.message, timestamp=datetime.fromisoformat(a.ts), color=(discord.Color.red() if a.level==AlertLevel.CRITICAL.value else (discord.Color.gold() if a.level==AlertLevel.WARNING.value else discord.Color.blue())))
                    for k, v in (a.data or {}).items():
                        try:
                            emb.add_field(name=str(k), value=safe_truncate(json.dumps(v, default=str), 256), inline=True)
                        except:
                            emb.add_field(name=str(k), value=str(v), inline=True)
                    await ch.send(embed=emb)
                if len(alerts) > 15:
                    await ch.send(f"⚠️ {len(alerts)-15} more alerts suppressed")
    except Exception as e:
        print("[monitor_loop] error:", e)

# ---------------- UI building blocks ----------------
MAX_DESC = 2048
MAX_FIELD = 1024

def make_game_embed(title: str, description: Optional[str] = None, color: discord.Color = discord.Color.blurple()) -> discord.Embed:
    return discord.Embed(title=title, description=safe_truncate(description, MAX_DESC) if description else None, timestamp=now_utc(), color=color)

def json_dev_embed(endpoint: str, data: Any, meta: Optional[Dict] = None) -> discord.Embed:
    try:
        j = json.dumps(data, indent=2, default=str)
    except Exception:
        j = str(data)
    if len(j) > 1900:
        j = j[:1897] + "..."
    title = f"🧠 DEV — {endpoint}"
    desc = ""
    if meta:
        desc += " • ".join([f"{k}:{v}" for k,v in (meta.items())]) + "\n\n"
    desc += f"```json\n{j}\n```"
    return discord.Embed(title=title, description=desc, timestamp=now_utc(), color=discord.Color.dark_grey())

def add_small_fields(embed: discord.Embed, d: Dict[str, Any], limit: int = 10):
    added = 0
    for k, v in d.items():
        if added >= limit: break
        val = v if isinstance(v, (str, int, float)) else json.dumps(v, default=str)
        embed.add_field(name=safe_truncate(str(k), 64), value=safe_truncate(str(val), MAX_FIELD), inline=True)
        added += 1
    if len(d) > limit:
        embed.add_field(name="…", value=f"+{len(d)-limit} more", inline=False)

# ---------------- Page View with Game/Dev toggle ----------------
class GameDevPageView(View):
    def __init__(self, game_pages: List[discord.Embed], dev_pages: Optional[List[discord.Embed]] = None, *, timeout: int = 180):
        super().__init__(timeout=timeout)
        self.game_pages = game_pages
        self.dev_pages = dev_pages or []
        self.mode = "game"
        self.current = 0
        self.prev = Button(label="◀️", style=discord.ButtonStyle.secondary)
        self.toggle = Button(label="🧠 Dev View", style=discord.ButtonStyle.primary)
        self.next = Button(label="▶️", style=discord.ButtonStyle.secondary)
        self.add_item(self.prev)
        self.add_item(self.toggle)
        self.add_item(self.next)
        self.prev.callback = self.on_prev
        self.next.callback = self.on_next
        self.toggle.callback = self.on_toggle
        self._update_buttons()

    def _update_buttons(self):
        length = len(self.game_pages) if self.mode == "game" else len(self.dev_pages)
        self.prev.disabled = self.current <= 0 or length <= 1
        self.next.disabled = self.current >= (length - 1) or length <= 1
        self.toggle.label = "🧠 Dev View" if self.mode == "game" else "🎮 Game View"

    async def on_prev(self, interaction: discord.Interaction):
        await interaction.response.defer()
        self.current = max(0, self.current - 1)
        await self._refresh(interaction)

    async def on_next(self, interaction: discord.Interaction):
        await interaction.response.defer()
        length = len(self.game_pages) if self.mode == "game" else len(self.dev_pages)
        self.current = min(length - 1, self.current + 1)
        await self._refresh(interaction)

    async def on_toggle(self, interaction: discord.Interaction):
        await interaction.response.defer()
        self.mode = "dev" if self.mode == "game" else "game"
        length = len(self.game_pages) if self.mode == "game" else len(self.dev_pages)
        if length == 0:
            self.mode = "game" if self.mode == "dev" else "dev"
            await interaction.followup.send("No data for that view.", ephemeral=True)
            return
        if self.current >= length:
            self.current = length - 1
        await self._refresh(interaction)

    async def _refresh(self, interaction: discord.Interaction):
        if self.mode == "game":
            embed = self.game_pages[self.current]
        else:
            embed = self.dev_pages[self.current] if self.current < len(self.dev_pages) else discord.Embed(title="No dev data")
        self._update_buttons()
        await interaction.followup.edit_message(interaction.message.id, embed=embed, view=self)

# JSON modal + button
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

# ---------------- Renderers (entities -> nice embeds) ----------------
def entity_name_link(entity: dict) -> str:
    """
    returns [Name](url) where url depends on entity fields.
    expects entity possibly containing user, _id, id, name, companyId, region, etc.
    """
    # user
    uid = entity.get("user") or entity.get("userId") or entity.get("id") or entity.get("_id")
    name = entity.get("name") or entity.get("username") or entity.get("title") or str(uid)
    # if user-like -> user url
    if entity.get("user") or entity.get("username") or (isinstance(uid, str) and len(str(uid))==24):
        url = user_url(uid)
        return f"[{safe_truncate(name, 40)}]({url})"
    # company
    if entity.get("companyId") or entity.get("company"):
        cid = entity.get("companyId") or entity.get("company")
        url = company_url(cid)
        return f"[{safe_truncate(name,40)}]({url})"
    # region/country
    if entity.get("region"):
        rid = entity.get("region")
        return f"[{safe_truncate(name,40)}]({region_url(rid)})"
    if entity.get("countryId") or entity.get("country"):
        cid = entity.get("countryId") or entity.get("country")
        return f"[{safe_truncate(name,40)}]({country_url(cid)})"
    # MU
    if entity.get("_id") and entity.get("members") is not None:
        mid = entity.get("_id")
        return f"[{safe_truncate(name,40)}]({mu_url(mid)})"
    # fallback: no link
    return safe_truncate(name, 40)

async def render_endpoint(endpoint: str, params: Optional[Dict] = None) -> Tuple[List[discord.Embed], List[discord.Embed]]:
    data = await war_api.call(endpoint, params)
    if data is None:
        return [make_game_embed(endpoint, "❌ Failed to fetch", color=discord.Color.red())], [json_dev_embed(endpoint, {}, {})]
    # Ranking style
    if isinstance(data, dict):
        # items lists
        items = None
        for k in ("items","results","data"):
            if k in data and isinstance(data[k], list):
                items = data[k]; break
        if items is not None:
            # detect ranking by 'tier' or 'value' -> pretty ranking
            if any(isinstance(it, dict) and ("tier" in it or "value" in it or "damage" in it or "score" in it) for it in items):
                # pretty pages
                game_pages = []
                dev_pages = []
                total = len(items)
                for i in range(0, total, PAGE_SIZE):
                    chunk = items[i : i + PAGE_SIZE]
                    e = make_game_embed(f"🏆 {endpoint}", f"Showing {i+1}-{i+len(chunk)} of {total}", color=discord.Color.dark_gold())
                    for idx, it in enumerate(chunk, start=i+1):
                        if isinstance(it, dict):
                            name_link = entity_name_link(it)
                            uid = it.get("user") or it.get("_id") or it.get("id")
                            val = it.get("value") or it.get("damage") or it.get("score") or it.get("wealth") or 0
                            val_s = fmt_num(val)
                            tier = it.get("tier","")
                            medal = medal_for_tier(tier)
                            field = f"{medal} **{name_link}**\n⚔️ **{val_s}**"
                            if tier:
                                field += f"\n· {safe_truncate(str(tier), 20)}"
                            e.add_field(name=f"#{idx}", value=field, inline=False)
                        else:
                            e.add_field(name=f"#{idx}", value=safe_truncate(str(it), 200), inline=False)
                    game_pages.append(e)
                    dev_pages.append(json_dev_embed(endpoint + " (dev)", chunk, {"chunk_start": i+1}))
                return game_pages, dev_pages
            # generic list pages
            return list_summary_pages(endpoint, items)
        # single-object-like
        if any(k in data for k in ("name","id","members","region","user","title")):
            # single object pretty
            e = make_game_embed(endpoint)
            small = {k:v for k,v in data.items() if isinstance(v, (str,int,float,bool))}
            add_small_fields(e, small, limit=12)
            return [e], [json_dev_embed(endpoint + " (dev)", data, {})]
        # fallback dict
        e = make_game_embed(endpoint)
        small = {k:v for k,v in data.items() if isinstance(v, (str,int,float,bool))}
        add_small_fields(e, small, limit=10)
        return [e], [json_dev_embed(endpoint + " (dev)", data, {})]

    if isinstance(data, list):
        return list_summary_pages(endpoint, data)

    # fallback primitive
    return [make_game_embed(endpoint, safe_truncate(str(data), 1000))], [json_dev_embed(endpoint + " (dev)", data, {})]

def list_summary_pages(endpoint: str, items: List[Any]) -> Tuple[List[discord.Embed], List[discord.Embed]]:
    game_pages = []
    dev_pages = []
    total = len(items)
    for i in range(0, total, PAGE_SIZE):
        chunk = items[i : i + PAGE_SIZE]
        e = make_game_embed(endpoint, f"Showing {i+1}-{i+len(chunk)} of {total}")
        for idx, it in enumerate(chunk, start=i+1):
            if isinstance(it, dict):
                name = it.get("name") or it.get("title") or it.get("id") or str(idx)
                keys = []
                for k in ("region","status","price","value","rank","company","members"):
                    if k in it:
                        keys.append(f"{k}:{fmt_num(it[k])}")
                e.add_field(name=f"#{idx} {safe_truncate(name,36)}", value=", ".join(keys) or "—", inline=False)
            else:
                e.add_field(name=f"#{idx}", value=safe_truncate(str(it),200), inline=False)
        game_pages.append(e)
        dev_pages.append(json_dev_embed(endpoint + " (dev)", chunk, {"chunk_start": i+1}))
    if not game_pages:
        game_pages.append(make_game_embed(endpoint, "No data"))
        dev_pages.append(json_dev_embed(endpoint + " (dev)", items, {}))
    return game_pages, dev_pages

# ---------------- Custom Rankings (aggregations) ----------------
async def aggregate_ranking_by_user(ranking_type: str, max_pages: int = 5) -> List[Tuple[str, float]]:
    """
    Fetch multiple pages from ranking.getRanking and sum 'value' per user.
    Returns list of (user_id, total_value) sorted desc.
    max_pages controls how many pages (page size depends on API; we request paging via params if supported)
    """
    sums: Dict[str, float] = {}
    # The API shape for ranking.getRanking often returns items in a single call (no page param).
    # We'll call once, then try to call multiple times with an offset if API supports (best-effort).
    data = await war_api.call("ranking.getRanking", {"rankingType": ranking_type})
    items = []
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        items = data["items"]
    elif isinstance(data, list):
        items = data
    # If items are many, we use them as available
    for it in items:
        if isinstance(it, dict):
            uid = it.get("user") or it.get("_id") or it.get("id")
            val = it.get("value") or it.get("damage") or it.get("score") or it.get("wealth") or 0
            try:
                sums[str(uid)] = sums.get(str(uid), 0.0) + float(val)
            except:
                pass
    # If not many results, try other endpoints? For now return what we have
    # Convert to sorted list
    sorted_list = sorted(sums.items(), key=lambda kv: kv[1], reverse=True)
    return sorted_list

# Top damage command format
def ranking_to_embeds(title: str, ranked: List[Tuple[str, float]], name_map: Optional[Dict[str,str]] = None) -> List[discord.Embed]:
    pages = []
    total = len(ranked)
    for i in range(0, total, PAGE_SIZE):
        chunk = ranked[i : i + PAGE_SIZE]
        e = make_game_embed(title, f"Showing {i+1}-{i+len(chunk)} of {total}", color=discord.Color.dark_gold())
        for idx, (uid, val) in enumerate(chunk, start=i+1):
            name = (name_map or {}).get(uid) or uid
            url = user_url(uid)
            link = f"[{safe_truncate(name,36)}]({url})"
            e.add_field(name=f"#{idx}", value=f"**{link}** — ⚔️ {fmt_num(val)}", inline=False)
        pages.append(e)
    if not pages:
        pages.append(make_game_embed(title, "No results"))
    return pages

# ---------------- Interaction helpers ----------------
async def safe_defer(interaction: discord.Interaction, ephemeral: bool=False):
    if not interaction.response.is_done():
        try:
            await interaction.response.defer(ephemeral=ephemeral)
        except Exception:
            pass

# ---------------- Slash commands (main) ----------------

# Ranking choices
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

@tree.command(name="help", description="Show available WarEra commands")
async def help_cmd(interaction: discord.Interaction):
    await safe_defer(interaction)
    e = make_game_embed("🎮 WarEra — Commands", color=discord.Color.gold())
    cmds = [
        ("/rankings <type>", "Leaderboards"),
        ("/topdamage", "Top damage (aggregated)"),
        ("/toplandproducers", "Top land producers (aggregated)"),
        ("/topwealth", "Top wealth"),
        ("/topmu", "Top MUs (members/avg)"),
        ("/countries", "List countries"),
        ("/companies", "List companies"),
        ("/company <id>", "Company by ID"),
        ("/battles", "Active battles"),
        ("/workoffers", "Work offers"),
        ("/mu", "Military units"),
        ("/articles", "Articles"),
        ("/prices", "Item prices"),
        ("/transactions", "Transactions"),
        ("/users <id>", "User info"),
        ("/search <q>", "Search anything"),
        ("/dashboard", "Post/refresh dashboard"),
        ("/alerts subscribe/unsubscribe", "Manage alerts subscription"),
        ("/jsondebug", "Paste JSON to debug"),
    ]
    for k, d in cmds:
        e.add_field(name=k, value=d, inline=False)
    await interaction.followup.send(embed=e)

@tree.command(name="rankings", description="View raw ranking from API")
@app_commands.choices(ranking_type=RANKING_CHOICES)
async def rankings_cmd(interaction: discord.Interaction, ranking_type: app_commands.Choice[str]):
    await safe_defer(interaction)
    game, dev = await render_endpoint("ranking.getRanking", {"rankingType": ranking_type.value})
    view = GameDevPageView(game, dev)
    await interaction.followup.send(embed=game[0], view=view)

# Custom aggregated ranking commands
@tree.command(name="topdamage", description="Top damage aggregated (best-effort)")
async def topdamage_cmd(interaction: discord.Interaction):
    await safe_defer(interaction)
    ranked = await aggregate_ranking_by_user("userDamages")
    pages = ranking_to_embeds("🔥 Top Damage (aggregated)", ranked)
    view = GameDevPageView(pages, [json_dev_embed("topdamage (dev)", ranked, {})])
    await interaction.followup.send(embed=pages[0], view=view)

@tree.command(name="toplandproducers", description="Top land producers (best-effort using userTerrain)")
async def topland_cmd(interaction: discord.Interaction):
    await safe_defer(interaction)
    ranked = await aggregate_ranking_by_user("userTerrain")
    pages = ranking_to_embeds("🌾 Top Land Producers", ranked)
    view = GameDevPageView(pages, [json_dev_embed("topland (dev)", ranked, {})])
    await interaction.followup.send(embed=pages[0], view=view)

@tree.command(name="topwealth", description="Top wealth (aggregated)")
async def topwealth_cmd(interaction: discord.Interaction):
    await safe_defer(interaction)
    ranked = await aggregate_ranking_by_user("userWealth")
    pages = ranking_to_embeds("💰 Top Wealth", ranked)
    view = GameDevPageView(pages, [json_dev_embed("topwealth (dev)", ranked, {})])
    await interaction.followup.send(embed=pages[0], view=view)

@tree.command(name="topmu", description="Top military units (by member count / stats)")
async def topmu_cmd(interaction: discord.Interaction):
    await safe_defer(interaction)
    # Fetch paginated MUs and rank by members count or invested money if present
    data = await war_api.call("mu.getManyPaginated", {"page":1,"limit":200})
    items = []
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict) and isinstance(data.get("items"), list):
        items = data["items"]
    # compute metric: prefer average invested or members count
    scores = []
    for mu in items:
        if not isinstance(mu, dict):
            continue
        members = len(mu.get("members", []))
        invested = mu.get("investedMoneyByUsers") or {}
        total_invest = 0
        if isinstance(invested, dict):
            for v in invested.values():
                try:
                    total_invest += float(v)
                except:
                    pass
        score = total_invest if total_invest > 0 else members
        scores.append((mu.get("_id") or mu.get("id") or mu.get("name"), score, mu))
    scores.sort(key=lambda t: t[1], reverse=True)
    # build embeds
    pages = []
    dev_pages = []
    for i in range(0, len(scores), PAGE_SIZE):
        chunk = scores[i : i + PAGE_SIZE]
        e = make_game_embed("🎖️ Top MUs", f"Showing {i+1}-{i+len(chunk)} of {len(scores)}", color=discord.Color.dark_teal())
        for idx, (mid, score, muobj) in enumerate(chunk, start=i+1):
            name_link = f"[{safe_truncate(muobj.get('name') or str(mid),36)}]({mu_url(mid)})" if mu_url(mid) else safe_truncate(muobj.get('name') or str(mid),36)
            e.add_field(name=f"#{idx} {name_link}", value=f"Members: {len(muobj.get('members',[]))}\nScore: {fmt_num(score)}", inline=False)
        pages.append(e)
        dev_pages.append(json_dev_embed("topmu (dev)", [m for (_,_,m) in chunk], {}))
    if not pages:
        pages.append(make_game_embed("Top MUs","No data"))
    view = GameDevPageView(pages, dev_pages)
    await interaction.followup.send(embed=pages[0], view=view)

# Regular endpoint wrappers (many)
@tree.command(name="countries", description="List countries")
async def countries_cmd(interaction: discord.Interaction):
    await safe_defer(interaction)
    game, dev = await render_endpoint("country.getAllCountries")
    await interaction.followup.send(embed=game[0], view=GameDevPageView(game, dev))

@tree.command(name="companies", description="List companies")
async def companies_cmd(interaction: discord.Interaction):
    await safe_defer(interaction)
    game, dev = await render_endpoint("company.getCompanies", {"page":1,"limit":50})
    await interaction.followup.send(embed=game[0], view=GameDevPageView(game, dev))

@tree.command(name="company", description="Get company by ID")
@app_commands.describe(company_id="Company ID")
async def company_cmd(interaction: discord.Interaction, company_id: str):
    await safe_defer(interaction)
    game, dev = await render_endpoint("company.getById", {"companyId": company_id})
    await interaction.followup.send(embed=game[0], view=GameDevPageView(game, dev))

@tree.command(name="battles", description="Active battles")
async def battles_cmd(interaction: discord.Interaction):
    await safe_defer(interaction)
    game, dev = await render_endpoint("battle.getBattles")
    await interaction.followup.send(embed=game[0], view=GameDevPageView(game, dev))

@tree.command(name="battle", description="Get battle by ID")
@app_commands.describe(battle_id="Battle ID")
async def battle_cmd(interaction: discord.Interaction, battle_id: str):
    await safe_defer(interaction)
    game, dev = await render_endpoint("battle.getById", {"battleId": battle_id})
    await interaction.followup.send(embed=game[0], view=GameDevPageView(game, dev))

@tree.command(name="workoffers", description="Work offers")
async def workoffers_cmd(interaction: discord.Interaction):
    await safe_defer(interaction)
    game, dev = await render_endpoint("workOffer.getWorkOffersPaginated", {"page":1,"limit":50})
    await interaction.followup.send(embed=game[0], view=GameDevPageView(game, dev))

@tree.command(name="workoffer", description="Get work offer by ID")
@app_commands.describe(offer_id="Work offer ID")
async def workoffer_cmd(interaction: discord.Interaction, offer_id: str):
    await safe_defer(interaction)
    game, dev = await render_endpoint("workOffer.getById", {"workOfferId": offer_id})
    await interaction.followup.send(embed=game[0], view=GameDevPageView(game, dev))

@tree.command(name="mu", description="List military units")
async def mu_cmd(interaction: discord.Interaction):
    await safe_defer(interaction)
    game, dev = await render_endpoint("mu.getManyPaginated", {"page":1,"limit":50})
    await interaction.followup.send(embed=game[0], view=GameDevPageView(game, dev))

@tree.command(name="mu_by_id", description="Get MU by ID")
@app_commands.describe(mu_id="MU ID")
async def mu_by_id_cmd(interaction: discord.Interaction, mu_id: str):
    await safe_defer(interaction)
    game, dev = await render_endpoint("mu.getById", {"muId": mu_id})
    await interaction.followup.send(embed=game[0], view=GameDevPageView(game, dev))

@tree.command(name="articles", description="List articles")
async def articles_cmd(interaction: discord.Interaction):
    await safe_defer(interaction)
    game, dev = await render_endpoint("article.getArticlesPaginated", {"page":1,"limit":50})
    await interaction.followup.send(embed=game[0], view=GameDevPageView(game, dev))

@tree.command(name="article", description="Get article by ID")
@app_commands.describe(article_id="Article ID")
async def article_cmd(interaction: discord.Interaction, article_id: str):
    await safe_defer(interaction)
    game, dev = await render_endpoint("article.getArticleById", {"articleId": article_id})
    await interaction.followup.send(embed=game[0], view=GameDevPageView(game, dev))

@tree.command(name="prices", description="Item prices")
async def prices_cmd(interaction: discord.Interaction):
    await safe_defer(interaction)
    game, dev = await render_endpoint("itemTrading.getPrices")
    await interaction.followup.send(embed=game[0], view=GameDevPageView(game, dev))

@tree.command(name="transactions", description="List transactions")
async def transactions_cmd(interaction: discord.Interaction):
    await safe_defer(interaction)
    game, dev = await render_endpoint("transaction.getPaginatedTransactions", {"page":1,"limit":50})
    await interaction.followup.send(embed=game[0], view=GameDevPageView(game, dev))

@tree.command(name="users", description="Get user by ID")
@app_commands.describe(user_id="User ID")
async def users_cmd(interaction: discord.Interaction, user_id: str):
    await safe_defer(interaction)
    game, dev = await render_endpoint("user.getUserLite", {"userId": user_id})
    await interaction.followup.send(embed=game[0], view=GameDevPageView(game, dev))

@tree.command(name="search", description="Search WarEra")
@app_commands.describe(query="Search text")
async def search_cmd(interaction: discord.Interaction, query: str):
    await safe_defer(interaction)
    game, dev = await render_endpoint("search.searchAnything", {"searchText": query})
    await interaction.followup.send(embed=game[0], view=GameDevPageView(game, dev))

@tree.command(name="jsondebug", description="Paste JSON and get a dev embed")
async def jsondebug_cmd(interaction: discord.Interaction):
    modal = ViewJsonModal("manual")
    await interaction.response.send_modal(modal)

# ---------------- Alerts subscription commands ----------------
@tree.command(name="alerts", description="Manage alerts subscriptions / view")
@app_commands.describe(action="subscribe, unsubscribe, list")
async def alerts_cmd(interaction: discord.Interaction, action: str):
    await safe_defer(interaction, ephemeral=True)
    uid = str(interaction.user.id)
    if action.lower() == "subscribe":
        subs = state.get("alerts_subscribers", [])
        if uid in subs:
            await interaction.followup.send("You are already subscribed to alerts.", ephemeral=True)
            return
        subs.append(uid)
        state["alerts_subscribers"] = subs
        await save_state()
        await interaction.followup.send("✅ Subscribed to alerts.", ephemeral=True)
        return
    if action.lower() == "unsubscribe":
        subs = state.get("alerts_subscribers", [])
        if uid in subs:
            subs.remove(uid)
            state["alerts_subscribers"] = subs
            await save_state()
            await interaction.followup.send("✅ Unsubscribed.", ephemeral=True)
            return
        await interaction.followup.send("You were not subscribed.", ephemeral=True)
        return
    if action.lower() == "list":
        subs = state.get("alerts_subscribers", [])
        await interaction.followup.send(f"Subscribers: {len(subs)}", ephemeral=True)
        return
    await interaction.followup.send("Usage: /alerts subscribe|unsubscribe|list", ephemeral=True)

# ---------------- Dashboard (combined) ----------------
class IntervalModal(Modal):
    def __init__(self):
        super().__init__(title="Set Dashboard interval (seconds)")
        self.input = TextInput(label="Seconds", placeholder=str(DEFAULT_DASH_INTERVAL), required=True)
        self.add_item(self.input)
    async def on_submit(self, interaction: discord.Interaction):
        val = self.input.value.strip()
        try:
            sec = int(val)
            if sec < 5:
                raise ValueError("minimum 5s")
            # change monitor and dash loops
            monitor.interval = sec
            if monitor_loop.is_running():
                monitor_loop.change_interval(seconds=sec)
            if dash_loop.is_running():
                dash_loop.change_interval(seconds=sec)
            await interaction.response.send_message(f"✅ Interval set to {sec}s", ephemeral=True)
            # persist
            state["dash_interval"] = sec
            await save_state()
        except Exception as e:
            await interaction.response.send_message(f"❌ Invalid: {e}", ephemeral=True)

class DashboardControlsView(View):
    def __init__(self):
        super().__init__(timeout=None)
        self.start = Button(label="▶️ Start Monitor", style=discord.ButtonStyle.success)
        self.stop = Button(label="⏸️ Stop Monitor", style=discord.ButtonStyle.danger)
        self.refresh = Button(label="🔁 Refresh Now", style=discord.ButtonStyle.secondary)
        self.interval = Button(label="⏱️ Set Interval", style=discord.ButtonStyle.secondary)
        self.clear_alerts = Button(label="🧹 Clear Alerts", style=discord.ButtonStyle.secondary)
        self.add_item(self.start); self.add_item(self.stop); self.add_item(self.refresh); self.add_item(self.interval); self.add_item(self.clear_alerts)
        self.start.callback = self.on_start
        self.stop.callback = self.on_stop
        self.refresh.callback = self.on_refresh
        self.interval.callback = self.on_interval
        self.clear_alerts.callback = self.on_clear

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
        await interaction.response.send_message(f"✅ Scanned. {len(alerts)} alerts.", ephemeral=True)

    async def on_interval(self, interaction: discord.Interaction):
        await interaction.response.send_modal(IntervalModal())

    async def on_clear(self, interaction: discord.Interaction):
        monitor.alerts.clear()
        state["monitor_alerts"] = []
        await save_state()
        await interaction.response.send_message("✅ Alerts cleared", ephemeral=True)

@tree.command(name="dashboard", description="Post/refresh combined dashboard (auto-updates)")
async def dashboard_cmd(interaction: discord.Interaction):
    await safe_defer(interaction)
    # build pieces
    # userDamages ranking first page
    game_rank, dev_rank = await render_endpoint("ranking.getRanking", {"rankingType":"userDamages"})
    rank_embed = game_rank[0] if game_rank else make_game_embed("Rankings")
    # prices
    prices = await war_api.call("itemTrading.getPrices")
    price_embed = make_game_embed("💰 Prices")
    if isinstance(prices, dict):
        i=0
        for k,v in prices.items():
            if i>=8: break
            price_embed.add_field(name=safe_truncate(k,32), value=fmt_num(v), inline=True)
            i+=1
    else:
        price_embed.description = safe_truncate(str(prices),200)
    # battles
    battles = await war_api.call("battle.getBattles")
    battle_embed = make_game_embed("⚔️ Battles")
    if isinstance(battles, list):
        for b in battles[:6]:
            if isinstance(b, dict):
                a = b.get("attackerCountry") or b.get("attacker")
                d = b.get("defenderCountry") or b.get("defender")
                s = b.get("status") or b.get("phase")
                battle_embed.add_field(name=f"{a} vs {d}", value=safe_truncate(str(s),50), inline=False)
            else:
                battle_embed.add_field(name=str(b), value="—", inline=False)
    else:
        battle_embed.description = safe_truncate(str(battles),200)
    # alerts summary
    alerts_embed = make_game_embed("🚨 Alerts Summary")
    recent_alerts = monitor.alerts[:6] if hasattr(monitor, "alerts") else state.get("monitor_alerts", [])[:6]
    if recent_alerts:
        for a in recent_alerts:
            alerts_embed.add_field(name=f"{a.get('level','') } {a.get('category','')}", value=safe_truncate(a.get("message",""),80), inline=False)
    else:
        alerts_embed.description = "No alerts"

    pages = [rank_embed, price_embed, battle_embed, alerts_embed]
    dev_pages = [json_dev_embed("ranking (dev)", {}, {}), json_dev_embed("prices (dev)", prices or {}, {}), json_dev_embed("battles (dev)", battles or {}, {}), json_dev_embed("alerts (dev)", recent_alerts, {})]
    view = GameDevPageView(pages, dev_pages)
    controls = DashboardControlsView()

    # post or edit existing dashboard message if saved in state
    dash_info = state.get("dash_message")
    channel = bot.get_channel(int(DASH_CHANNEL_ID)) if DASH_CHANNEL_ID else interaction.channel
    posted_msg = None
    # send main dashboard
    if dash_info and channel:
        try:
            msg = await channel.fetch_message(int(dash_info["message_id"]))
            await msg.edit(embed=pages[0], view=view)
            posted_msg = msg
        except Exception:
            try:
                posted_msg = await channel.send(embed=pages[0], view=view)
                state["dash_message"] = {"channel_id": channel.id, "message_id": posted_msg.id}
                await save_state()
            except Exception:
                posted_msg = None
    else:
        # send new
        posted_msg = await channel.send(embed=pages[0], view=view)
        state["dash_message"] = {"channel_id": channel.id, "message_id": posted_msg.id}
        await save_state()

    # send ephemeral control panel to the calling user
    try:
        await interaction.followup.send("Dashboard controls (only you can see this):", view=controls, ephemeral=True)
    except Exception:
        # fallback public
        await interaction.followup.send("Dashboard controls:", view=controls)

    # ensure auto-refresh loop is running
    if not dash_loop.is_running():
        dash_loop.start()

    await interaction.followup.send("✅ Dashboard posted/updated.", ephemeral=True)

# auto refresh for dashboard: edits the saved message
@tasks.loop(seconds=DEFAULT_DASH_INTERVAL)
async def dash_loop():
    # uses state["dash_message"] to find message; updates content
    dash_info = state.get("dash_message")
    if not dash_info:
        return
    try:
        ch = bot.get_channel(int(dash_info["channel_id"]))
        if ch is None:
            return
        msg = await ch.fetch_message(int(dash_info["message_id"]))
        # generate updated embeds (same as dashboard)
        game_rank, _ = await render_endpoint("ranking.getRanking", {"rankingType":"userDamages"})
        rank_embed = game_rank[0] if game_rank else make_game_embed("Rankings")
        prices = await war_api.call("itemTrading.getPrices")
        price_embed = make_game_embed("💰 Prices")
        if isinstance(prices, dict):
            i=0
            for k,v in prices.items():
                if i>=8: break
                price_embed.add_field(name=safe_truncate(k,32), value=fmt_num(v), inline=True)
                i+=1
        battles = await war_api.call("battle.getBattles")
        battle_embed = make_game_embed("⚔️ Battles")
        if isinstance(battles, list):
            for b in battles[:6]:
                if isinstance(b, dict):
                    a = b.get("attackerCountry") or b.get("attacker")
                    d = b.get("defenderCountry") or b.get("defender")
                    s = b.get("status") or b.get("phase")
                    battle_embed.add_field(name=f"{a} vs {d}", value=safe_truncate(str(s),50), inline=False)
        alerts_embed = make_game_embed("🚨 Alerts Summary")
        recent_alerts = monitor.alerts[:6] if hasattr(monitor, "alerts") else state.get("monitor_alerts", [])[:6]
        if recent_alerts:
            for a in recent_alerts:
                alerts_embed.add_field(name=f"{a.get('level','')} {a.get('category','')}", value=safe_truncate(a.get("message",""),80), inline=False)
        pages = [rank_embed, price_embed, battle_embed, alerts_embed]
        dev_pages = [json_dev_embed("ranking (dev)", {}, {}), json_dev_embed("prices (dev)", prices or {}, {}), json_dev_embed("battles (dev)", battles or {}, {}), json_dev_embed("alerts (dev)", recent_alerts, {})]
        view = GameDevPageView(pages, dev_pages)
        await msg.edit(embed=pages[0], view=view)
    except Exception as e:
        # don't crash; log and continue
        print("[dash_loop] error:", e)

# ---------------- JSON Debugger modal helper ----------------
class ManualJsonModal(Modal):
    def __init__(self):
        super().__init__(title="JSON Debugger")
        self.input = TextInput(label="Paste JSON", style=discord.TextStyle.long, required=True, placeholder='{"id":"..."}', max_length=4000)
        self.add_item(self.input)
    async def on_submit(self, interaction: discord.Interaction):
        await safe_defer(interaction, ephemeral=True)
        try:
            parsed = json.loads(self.input.value)
        except Exception as e:
            await interaction.followup.send(f"❌ Invalid JSON: {e}", ephemeral=True)
            return
        await interaction.followup.send(embed=json_dev_embed("manual", parsed, {"source":"paste"}), ephemeral=True)

@tree.command(name="jsondebug", description="Paste JSON to pretty-print")
async def jsondebug_cmd(interaction: discord.Interaction):
    modal = ManualJsonModal()
    await interaction.response.send_modal(modal)

# ---------------- Bot events ----------------
@bot.event
async def on_ready():
    print(f"[WarEra] Bot logged in as {bot.user} (ID {bot.user.id})")
    try:
        await tree.sync()
        print("[WarEra] Slash commands synced.")
    except Exception as e:
        print("[WarEra] Sync error:", e)
    # restore monitor state from saved state
    monitor.prev = state.get("monitor_prev", {})
    monitor.alerts = state.get("monitor_alerts", [])
    # start monitor loop but it only acts when monitor.running==True
    if not monitor_loop.is_running():
        monitor_loop.start()
    # start dash loop if dash message exists
    if state.get("dash_message") and not dash_loop.is_running():
        dash_loop.start()

# ---------------- Run ----------------
if __name__ == "__main__":
    if DISCORD_TOKEN == "YOUR_TOKEN_HERE":
        print("Set DISCORD_BOT_TOKEN env var and restart.")
    else:
        bot.run(DISCORD_TOKEN)
