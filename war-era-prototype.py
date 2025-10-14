# war-era-dashboard-bot.py
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
DASH_CHANNEL_ID = os.getenv("WARERA_DASH_CHANNEL")       # optional
ALERT_CHANNEL_ID = os.getenv("WARERA_ALERT_CHANNEL")     # optional
REQUEST_TIMEOUT = float(os.getenv("WARERA_REQUEST_TIMEOUT", "10"))
RETRY_ATTEMPTS = int(os.getenv("WARERA_RETRY_ATTEMPTS", "3"))
RETRY_BACKOFF = float(os.getenv("WARERA_RETRY_BACKOFF", "0.7"))
DEFAULT_DASH_INTERVAL = int(os.getenv("WARERA_DASH_INTERVAL", "60"))
PAGE_SIZE = int(os.getenv("WARERA_PAGE_SIZE", "8"))

# Custom emoji/image URLs for game aesthetic (replace with your assets)
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
intents.message_content = False
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)
tree = bot.tree

# Shared aiohttp session
_session: Optional[aiohttp.ClientSession] = None

async def get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT))
    return _session

# ---------------- WarEra API client ----------------
class WarEraAPI:
    def __init__(self, base_url: str = API_BASE):
        self.base_url = base_url.rstrip("/")

    def build_url(self, endpoint: str, params: Optional[Dict] = None) -> str:
        ep = endpoint.strip().lstrip("/")
        url = f"{self.base_url}/{ep}"
        input_json = json.dumps(params or {}, separators=(",", ":"))
        encoded = urllib.parse.quote(input_json, safe="")
        return f"{url}?input={encoded}"

    async def call(self, endpoint: str, params: Optional[Dict] = None) -> Optional[Any]:
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
                    # Unwrap tRPC envelope
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

# ---------------- Alerts system ----------------
class AlertLevel(Enum):
    INFO = "🔵"
    WARNING = "🟡"
    CRITICAL = "🔴"

@dataclass
class Alert:
    timestamp: str
    level: AlertLevel
    category: str
    title: str
    message: str
    data: Dict[str,Any] = field(default_factory=dict)

class WarEraMonitor:
    def __init__(self, api: WarEraAPI):
        self.api = api
        self.prev: Dict[str, Any] = {}
        self.alerts: List[Alert] = []
        self.running = False
        self.interval = DEFAULT_DASH_INTERVAL
        # thresholds (tweak)
        self.price_pct_threshold = 20.0
        self.price_critical_pct = 50.0

        # monitored endpoints (expand as desired)
        self.simple_endpoints = [
            "itemTrading.getPrices",
            "battle.getBattles",
            "country.getAllCountries",
            "gameConfig.getDates",
        ]
        self.paginated_endpoints = [
            "company.getCompanies",
            "workOffer.getWorkOffersPaginated",
            "article.getArticlesPaginated",
            "mu.getManyPaginated",
            "transaction.getPaginatedTransactions",
        ]
        self.special = {
            "ranking.getRanking": {"rankingType": "userDamages"},
            "battleRanking.getRanking": {},
            "tradingOrder.getTopOrders": {"itemType":"FOOD"},
        }

    def _add_alert(self, level: AlertLevel, category: str, title: str, message: str, data: Dict = None):
        a = Alert(datetime.now(timezone.utc).isoformat(), level, category, title, message, data or {})
        self.alerts.insert(0, a)  # newest first
        return a

    async def scan_once(self) -> List[Alert]:
        results: List[Alert] = []
        # simple endpoints
        for ep in self.simple_endpoints:
            data = await self.api.call(ep)
            if data is None: continue
            prev = self.prev.get(ep)
            # detect lists
            if isinstance(prev, list) and isinstance(data, list):
                if len(data) > len(prev):
                    diff = len(data) - len(prev)
                    a = self._add_alert(AlertLevel.WARNING, "BATTLE" if "battle" in ep else "GENERAL", ep, f"+{diff} new items", {"endpoint":ep,"diff":diff})
                    results.append(a)
            # detect price dicts
            if isinstance(prev, dict) and isinstance(data, dict):
                for k, newv in data.items():
                    oldv = prev.get(k)
                    if isinstance(newv, (int,float)) and isinstance(oldv,(int,float)) and oldv != 0:
                        change = ((newv - oldv)/abs(oldv)) * 100.0
                        if abs(change) >= self.price_pct_threshold:
                            lvl = AlertLevel.CRITICAL if abs(change) >= self.price_critical_pct else AlertLevel.WARNING
                            a = self._add_alert(lvl, "ECONOMY", ep, f"{k}: {change:+.1f}% ( {oldv} → {newv} )", {"key":k,"old":oldv,"new":newv,"pct":change})
                            results.append(a)
            self.prev[ep] = data

        # paginated endpoints (just check counts)
        for ep in self.paginated_endpoints:
            data = await self.api.call(ep, {"page":1,"limit":50})
            if data is None: continue
            prev = self.prev.get(ep)
            # if list
            if isinstance(prev, list) and isinstance(data, list):
                if len(data) > len(prev):
                    diff = len(data) - len(prev)
                    a = self._add_alert(AlertLevel.INFO, "PAGINATED", ep, f"+{diff} new items", {"endpoint":ep,"diff":diff})
                    results.append(a)
            self.prev[ep] = data

        # special endpoints
        for ep, params in self.special.items():
            data = await self.api.call(ep, params)
            if data is None: continue
            prev = self.prev.get(ep)
            # if ranking items
            if isinstance(prev, dict) and isinstance(data, dict) and "items" in data and isinstance(data["items"], list):
                # detect top-1 change
                try:
                    prev_top = prev.get("items",[None])[0]
                    new_top = data.get("items",[None])[0]
                    if prev_top and new_top and isinstance(prev_top, dict) and isinstance(new_top, dict):
                        if prev_top.get("_id") != new_top.get("_id"):
                            a = self._add_alert(AlertLevel.WARNING, "RANKINGS", ep, f"Top changed: {prev_top.get('user') or prev_top.get('_id')} → {new_top.get('user') or new_top.get('_id')}", {"prev":prev_top,"new":new_top})
                            results.append(a)
                except Exception:
                    pass
            self.prev[ep] = data

        # store last scan time
        self.prev["_last_scan"] = datetime.now(timezone.utc).isoformat()
        return results

monitor = WarEraMonitor(war_api)

# monitor loop that can be started/stopped via buttons
@tasks.loop(seconds=DEFAULT_DASH_INTERVAL)
async def monitor_loop():
    if not monitor.running:
        return
    try:
        alerts = await monitor.scan_once()
        if alerts and ALERT_CHANNEL_ID:
            channel = bot.get_channel(int(ALERT_CHANNEL_ID))
            if channel:
                # group by category and send
                summary = f"**🚨 WarEra Monitor — {len(alerts)} alerts**\n"
                by_cat = {}
                for a in alerts:
                    by_cat.setdefault(a.category,0)
                    by_cat[a.category]+=1
                for c,n in by_cat.items():
                    summary += f"• {c}: {n}\n"
                await channel.send(summary)
                for a in alerts[:15]:
                    embed = discord.Embed(title=f"{a.level.value} {a.category} - {a.title}", description=a.message, color=(discord.Color.red() if a.level==AlertLevel.CRITICAL else (discord.Color.gold() if a.level==AlertLevel.WARNING else discord.Color.blue())), timestamp=datetime.fromisoformat(a.timestamp))
                    if a.data:
                        for k,v in a.data.items():
                            try:
                                embed.add_field(name=str(k), value=safe_truncate(json.dumps(v, default=str), 256), inline=True)
                            except:
                                embed.add_field(name=str(k), value=str(v), inline=True)
                    await channel.send(embed=embed)
                if len(alerts) > 15:
                    await channel.send(f"⚠️ {len(alerts)-15} more alerts suppressed")
    except Exception as e:
        print("[monitor_loop] error:", e)

# ---------------- Utilities & formatting ----------------
MAX_DESC = 2048
MAX_FIELD = 1024

def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def safe_truncate(s: Optional[str], n: int) -> str:
    if s is None:
        return ""
    s = str(s)
    return s if len(s) <= n else s[: n - 3] + "..."

def fmt_num(v: Any) -> str:
    try:
        if isinstance(v, (int, float)):
            return f"{int(v):,}"
        return str(v)
    except:
        return str(v)

def medal_for_tier(tier: Optional[str]) -> str:
    t = (tier or "").lower()
    if "master" in t or t.startswith("maste"): return CUSTOM_EMOJIS.get("master") or "🥇"
    if "gold" in t: return CUSTOM_EMOJIS.get("gold") or "🥈"
    if "silver" in t: return CUSTOM_EMOJIS.get("silver") or "🥉"
    if "bronze" in t: return CUSTOM_EMOJIS.get("bronze") or "🏅"
    return CUSTOM_EMOJIS.get("mu") or "🏵️"

def profile_url(user_id: Optional[str]) -> str:
    if not user_id: return ""
    return f"https://warera.io/profile/{user_id}"

def make_game_embed(title: str, description: Optional[str] = None, color: discord.Color = discord.Color.blurple()) -> discord.Embed:
    return discord.Embed(title=title, description=safe_truncate(description, MAX_DESC) if description else None, timestamp=now_utc(), color=color)

def json_dev_embed(endpoint: str, data: Any, meta: Optional[Dict[str, Any]] = None) -> discord.Embed:
    try:
        j = json.dumps(data, indent=2, default=str)
    except Exception:
        j = str(data)
    if len(j) > 1900:
        j = j[:1897] + "..."
    title = f"🧠 DEV — {endpoint}"
    desc = ""
    if meta:
        meta_parts = [f"{k}: {v}" for k, v in meta.items()]
        desc += " • ".join(meta_parts) + "\n\n"
    desc += f"```json\n{j}\n```"
    return discord.Embed(title=title, description=desc, timestamp=now_utc(), color=discord.Color.dark_grey())

def add_small_fields(embed: discord.Embed, d: Dict[str, Any], limit: int = 8):
    added = 0
    for k, v in d.items():
        if added >= limit: break
        val = v if isinstance(v, (str, int, float)) else json.dumps(v, default=str)
        embed.add_field(name=safe_truncate(str(k), 64), value=safe_truncate(str(val), MAX_FIELD), inline=True)
        added += 1
    if len(d) > limit:
        embed.add_field(name="…", value=f"+{len(d)-limit} more", inline=False)

# ---------------- PageView with toggle (Game <-> Dev) ----------------
class GameDevPageView(View):
    def __init__(self, game_pages: List[discord.Embed], dev_pages: Optional[List[discord.Embed]] = None, *, timeout: int = 180):
        super().__init__(timeout=timeout)
        self.game_pages = game_pages
        self.dev_pages = dev_pages or []
        self.mode = "game"
        self.current = 0
        self.prev_btn = Button(emoji="◀️", style=discord.ButtonStyle.secondary)
        self.toggle_btn = Button(label="🧠 Dev View", style=discord.ButtonStyle.primary)
        self.next_btn = Button(emoji="▶️", style=discord.ButtonStyle.secondary)
        self.add_item(self.prev_btn)
        self.add_item(self.toggle_btn)
        self.add_item(self.next_btn)
        self.prev_btn.callback = self.on_prev
        self.next_btn.callback = self.on_next
        self.toggle_btn.callback = self.on_toggle
        self._update_state()

    def _update_state(self):
        length = len(self.game_pages) if self.mode == "game" else len(self.dev_pages)
        self.prev_btn.disabled = self.current <= 0 or length <= 1
        self.next_btn.disabled = self.current >= (length - 1) or length <= 1
        self.toggle_btn.label = "🧠 Dev View" if self.mode == "game" else "🎮 Game View"

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
        # flip modes
        self.mode = "dev" if self.mode == "game" else "game"
        length = len(self.game_pages) if self.mode == "game" else len(self.dev_pages)
        if length == 0:
            # flip back
            self.mode = "game" if self.mode == "dev" else "dev"
            await interaction.followup.send("No data available for that view.", ephemeral=True)
            return
        if self.current >= length:
            self.current = length - 1
        await self._refresh(interaction)

    async def _refresh(self, interaction: discord.Interaction):
        if self.mode == "game":
            embed = self.game_pages[self.current] if self.current < len(self.game_pages) else discord.Embed(title="No data")
        else:
            embed = self.dev_pages[self.current] if self.current < len(self.dev_pages) else discord.Embed(title="No dev data")
        self._update_state()
        await interaction.followup.edit_message(interaction.message.id, embed=embed, view=self)

# ---------------- View JSON Modal & Button ----------------
class ViewJsonModal(Modal):
    def __init__(self, endpoint: str):
        super().__init__(title=f"View JSON — {endpoint}")
        self.endpoint = endpoint
        self.key_input = TextInput(label="Optional key (dot path)", required=False, placeholder="items.0 or id")
        self.add_item(self.key_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        key = self.key_input.value.strip()
        data = await war_api.call(self.endpoint)
        if data is None:
            await interaction.followup.send("❌ Failed to fetch data", ephemeral=True)
            return
        if key:
            parts = key.split(".")
            sub = data
            try:
                for p in parts:
                    if isinstance(sub, dict):
                        sub = sub.get(p, f"❌ Key '{p}' missing")
                    elif isinstance(sub, list) and p.isdigit():
                        sub = sub[int(p)]
                    else:
                        sub = f"❌ Can't traverse key '{p}'"
                data = sub
            except Exception as e:
                await interaction.followup.send(f"❌ Extraction error: {e}", ephemeral=True)
                return
        embed = json_dev_embed(self.endpoint, data, {"key": key or None})
        await interaction.followup.send(embed=embed, ephemeral=True)

class ViewJsonButton(Button):
    def __init__(self, endpoint: str):
        super().__init__(label="🔍 View JSON", style=discord.ButtonStyle.secondary)
        self.endpoint = endpoint

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(ViewJsonModal(self.endpoint))

# ---------------- Render helpers ----------------
def make_pages_from_items(endpoint_label: str, items: List[Any]) -> Tuple[List[discord.Embed], List[discord.Embed]]:
    # choose presentation: ranking vs summary
    # try to detect 'tier' or 'value' to render ranking
    if any(isinstance(it, dict) and ("tier" in it or "value" in it or "damage" in it) for it in items):
        return pretty_ranking_pages(endpoint_label, items)
    # fallback to list summary
    return list_summary_pages(endpoint_label, items)

def pretty_ranking_pages(endpoint_title: str, items: List[Any]) -> Tuple[List[discord.Embed], List[discord.Embed]]:
    game_pages: List[discord.Embed] = []
    dev_pages: List[discord.Embed] = []
    total = len(items)
    for i in range(0, total, PAGE_SIZE):
        chunk = items[i : i + PAGE_SIZE]
        e = make_game_embed(endpoint_title, f"Showing {i+1}-{i+len(chunk)} of {total}", color=discord.Color.dark_gold())
        for idx, it in enumerate(chunk, start=1 + i):
            if isinstance(it, dict):
                name = it.get("name") or it.get("username") or it.get("user") or it.get("_id") or str(idx)
                uid = it.get("user") or it.get("_id") or it.get("id")
                url = profile_url(uid) if uid else None
                tier = it.get("tier", "")
                medal = medal_for_tier(tier)
                val = it.get("value") or it.get("damage") or it.get("score") or it.get("wealth") or 0
                val_s = fmt_num(val)
                name_display = f"[{safe_truncate(name,36)}]({url})" if url else safe_truncate(name,36)
                field_val = f"{medal} **{name_display}**\n⚔️ **{val_s}**"
                if tier:
                    field_val += f"\n· {safe_truncate(str(tier), 40)}"
                e.add_field(name=f"#{idx}", value=field_val, inline=False)
            else:
                e.add_field(name=f"#{idx}", value=safe_truncate(str(it), 200), inline=False)
        game_pages.append(e)
        dev_pages.append(json_dev_embed(endpoint_title + " (dev)", chunk, {"chunk_start": i+1}))
    if not game_pages:
        game_pages.append(make_game_embed(endpoint_title, "No results", color=discord.Color.greyple()))
        dev_pages.append(json_dev_embed(endpoint_title + " (dev)", items, {}))
    return game_pages, dev_pages

def list_summary_pages(endpoint_title: str, items: List[Any]) -> Tuple[List[discord.Embed], List[discord.Embed]]:
    game_pages: List[discord.Embed] = []
    dev_pages: List[discord.Embed] = []
    total = len(items)
    for i in range(0, total, PAGE_SIZE):
        chunk = items[i : i + PAGE_SIZE]
        e = make_game_embed(endpoint_title, f"Showing {i+1}-{i+len(chunk)} of {total}")
        for idx, it in enumerate(chunk, start=1 + i):
            if isinstance(it, dict):
                name = it.get("name") or it.get("title") or it.get("id") or str(idx)
                keys = []
                for k in ("region","status","price","value","rank","user","company","title","members"):
                    if k in it:
                        keys.append(f"{k}:{fmt_num(it[k])}")
                e.add_field(name=f"{idx}. {safe_truncate(str(name),36)}", value=", ".join(keys) or "—", inline=False)
            else:
                e.add_field(name=f"{idx}", value=safe_truncate(str(it), 200), inline=False)
        game_pages.append(e)
        dev_pages.append(json_dev_embed(endpoint_title + " (dev)", chunk, {"chunk_start": i+1}))
    if not game_pages:
        game_pages.append(make_game_embed(endpoint_title, "No results"))
        dev_pages.append(json_dev_embed(endpoint_title + " (dev)", items, {}))
    return game_pages, dev_pages

def single_object_pages(title: str, obj: Dict[str, Any]) -> Tuple[List[discord.Embed], List[discord.Embed]]:
    game_e = make_game_embed(title)
    small = {k:v for k,v in obj.items() if isinstance(v,(str,int,float,bool))}
    add_small_fields(game_e, small, limit=12)
    dev_e = json_dev_embed(title + " (dev)", obj, {"single": True})
    return [game_e], [dev_e]

# ---------------- Comprehensive endpoint renderer ----------------
async def render_endpoint_full(endpoint: str, params: Optional[Dict] = None) -> Tuple[List[discord.Embed], List[discord.Embed]]:
    data = await war_api.call(endpoint, params)
    if data is None:
        return [make_game_embed(endpoint, "❌ Failed to fetch", color=discord.Color.red())], [json_dev_embed(endpoint, {"error":"fetch failed"}, {})]
    # if dict and contains items array
    if isinstance(data, dict):
        if "items" in data and isinstance(data["items"], list):
            return pretty_ranking_pages(endpoint, data["items"])
        if "results" in data and isinstance(data["results"], list):
            return list_summary_pages(endpoint, data["results"])
        if "data" in data and isinstance(data["data"], list):
            return list_summary_pages(endpoint, data["data"])
        # treat as single object if it has common fields
        if any(k in data for k in ("name","id","members","region","user","title")):
            return single_object_pages(endpoint, data)
        # fallback show small scalars + dev dump
        game_e = make_game_embed(endpoint)
        small = {k:v for k,v in data.items() if isinstance(v,(str,int,float,bool))}
        add_small_fields(game_e, small, limit=10)
        dev_e = json_dev_embed(endpoint + " (dev)", data, {"keys": list(small.keys())})
        return [game_e], [dev_e]
    # If list
    if isinstance(data, list):
        # detect MUs
        low = endpoint.lower()
        if "mu" in low or "military" in low:
            game_pages = []
            dev_pages = []
            total = len(data)
            for i in range(0, total, PAGE_SIZE):
                chunk = data[i : i + PAGE_SIZE]
                page = make_game_embed("🎖️ Military Units", f"Showing {i+1}-{i+len(chunk)} of {total}", color=discord.Color.dark_teal())
                for ent in chunk:
                    if isinstance(ent, dict):
                        name = ent.get("name") or ent.get("_id")
                        members = len(ent.get("members", []))
                        region = ent.get("region", "—")
                        page.add_field(name=safe_truncate(name, 36), value=f"Members: {members}\nRegion: {region}", inline=False)
                    else:
                        page.add_field(name=str(ent), value="—", inline=False)
                game_pages.append(page)
                dev_pages.append(json_dev_embed(endpoint + " (dev)", chunk, {"chunk_start": i+1}))
            return game_pages, dev_pages
        # generic list
        return list_summary_pages(endpoint, data)
    # fallback
    return [make_game_embed(endpoint, safe_truncate(str(data), 1000))], [json_dev_embed(endpoint + " (dev)", data, {})]

# ---------------- Interaction helpers ----------------
async def safe_defer(interaction: discord.Interaction, ephemeral: bool = False):
    if not interaction.response.is_done():
        try:
            await interaction.response.defer(ephemeral=ephemeral)
        except Exception:
            pass

async def send_game_dev(interaction: discord.Interaction, game_pages: List[discord.Embed], dev_pages: Optional[List[discord.Embed]] = None):
    await safe_defer(interaction)
    view = GameDevPageView(game_pages, dev_pages or [])
    msg = await interaction.followup.send(embed=game_pages[0], view=view)
    return msg

# ---------------- Ranking choices ----------------
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

# ---------------- Slash Commands (large set) ----------------
@tree.command(name="help", description="Show WarEra Dashboard commands")
async def help_cmd(interaction: discord.Interaction):
    await safe_defer(interaction)
    e = make_game_embed("🎮 WarEra Dashboard — Commands", color=discord.Color.gold())
    cmds = [
        ("/rankings <type>", "Leaderboards"),
        ("/countries", "List countries"),
        ("/companies", "List companies"),
        ("/company <id>", "Company by ID"),
        ("/battles", "Active battles"),
        ("/battle <id>", "Battle by ID"),
        ("/workoffers", "Work offers"),
        ("/mu", "Military units"),
        ("/articles", "Articles"),
        ("/prices", "Item prices"),
        ("/transactions", "Transactions"),
        ("/users <id>", "User info"),
        ("/search <q>", "Search anything"),
        ("/dashboard", "Post/refresh live dashboard"),
        ("/jsondebug", "Paste JSON to pretty-print"),
    ]
    for c,d in cmds:
        e.add_field(name=c, value=d, inline=False)
    await interaction.followup.send(embed=e)

@tree.command(name="rankings", description="View WarEra rankings (leaderboards)")
@app_commands.choices(ranking_type=RANKING_CHOICES)
@app_commands.describe(ranking_type="Choose a ranking type")
async def rankings_cmd(interaction: discord.Interaction, ranking_type: app_commands.Choice[str]):
    await safe_defer(interaction)
    game, dev = await render_endpoint_full("ranking.getRanking", {"rankingType": ranking_type.value})
    await send_game_dev(interaction, game, dev)

@tree.command(name="countries", description="List countries")
async def countries_cmd(interaction: discord.Interaction):
    await safe_defer(interaction)
    game, dev = await render_endpoint_full("country.getAllCountries")
    await send_game_dev(interaction, game, dev)

@tree.command(name="companies", description="List companies")
async def companies_cmd(interaction: discord.Interaction):
    await safe_defer(interaction)
    game, dev = await render_endpoint_full("company.getCompanies", {"page":1, "limit":50})
    await send_game_dev(interaction, game, dev)

@tree.command(name="company", description="Get company by ID")
@app_commands.describe(company_id="Company ID")
async def company_cmd(interaction: discord.Interaction, company_id: str):
    await safe_defer(interaction)
    game, dev = await render_endpoint_full("company.getById", {"companyId": company_id})
    await send_game_dev(interaction, game, dev)

@tree.command(name="battles", description="Active battles")
async def battles_cmd(interaction: discord.Interaction):
    await safe_defer(interaction)
    game, dev = await render_endpoint_full("battle.getBattles")
    await send_game_dev(interaction, game, dev)

@tree.command(name="battle", description="Get battle by ID")
@app_commands.describe(battle_id="Battle ID")
async def battle_cmd(interaction: discord.Interaction, battle_id: str):
    await safe_defer(interaction)
    game, dev = await render_endpoint_full("battle.getById", {"battleId": battle_id})
    await send_game_dev(interaction, game, dev)

@tree.command(name="workoffers", description="Work offers")
async def workoffers_cmd(interaction: discord.Interaction):
    await safe_defer(interaction)
    game, dev = await render_endpoint_full("workOffer.getWorkOffersPaginated", {"page":1,"limit":50})
    await send_game_dev(interaction, game, dev)

@tree.command(name="workoffer", description="Get work offer by ID")
@app_commands.describe(offer_id="Work offer ID")
async def workoffer_cmd(interaction: discord.Interaction, offer_id: str):
    await safe_defer(interaction)
    game, dev = await render_endpoint_full("workOffer.getById", {"workOfferId": offer_id})
    await send_game_dev(interaction, game, dev)

@tree.command(name="mu", description="List military units (MU)")
async def mu_cmd(interaction: discord.Interaction):
    await safe_defer(interaction)
    game, dev = await render_endpoint_full("mu.getManyPaginated", {"page":1,"limit":50})
    await send_game_dev(interaction, game, dev)

@tree.command(name="mu_by_id", description="Get MU by ID")
@app_commands.describe(mu_id="MU ID")
async def mu_by_id_cmd(interaction: discord.Interaction, mu_id: str):
    await safe_defer(interaction)
    game, dev = await render_endpoint_full("mu.getById", {"muId": mu_id})
    await send_game_dev(interaction, game, dev)

@tree.command(name="articles", description="List articles")
async def articles_cmd(interaction: discord.Interaction):
    await safe_defer(interaction)
    game, dev = await render_endpoint_full("article.getArticlesPaginated", {"page":1,"limit":50})
    await send_game_dev(interaction, game, dev)

@tree.command(name="article", description="Get article by ID")
@app_commands.describe(article_id="Article ID")
async def article_cmd(interaction: discord.Interaction, article_id: str):
    await safe_defer(interaction)
    game, dev = await render_endpoint_full("article.getArticleById", {"articleId": article_id})
    await send_game_dev(interaction, game, dev)

@tree.command(name="prices", description="Item prices")
async def prices_cmd(interaction: discord.Interaction):
    await safe_defer(interaction)
    game, dev = await render_endpoint_full("itemTrading.getPrices")
    await send_game_dev(interaction, game, dev)

@tree.command(name="transactions", description="List recent transactions")
async def transactions_cmd(interaction: discord.Interaction):
    await safe_defer(interaction)
    game, dev = await render_endpoint_full("transaction.getPaginatedTransactions", {"page":1,"limit":50})
    await send_game_dev(interaction, game, dev)

@tree.command(name="users", description="Get user info by ID")
@app_commands.describe(user_id="User ID")
async def users_cmd(interaction: discord.Interaction, user_id: str):
    await safe_defer(interaction)
    game, dev = await render_endpoint_full("user.getUserLite", {"userId": user_id})
    await send_game_dev(interaction, game, dev)

@tree.command(name="search", description="Search WarEra")
@app_commands.describe(query="Search text")
async def search_cmd(interaction: discord.Interaction, query: str):
    await safe_defer(interaction)
    game, dev = await render_endpoint_full("search.searchAnything", {"searchText": query})
    await send_game_dev(interaction, game, dev)

# JSON debugger (paste)
class JsonPasteModal(Modal):
    def __init__(self):
        super().__init__(title="JSON Debugger")
        self.input = TextInput(label="Paste JSON", style=discord.TextStyle.long, placeholder='{"id":"..."}', required=True, max_length=4000)
        self.add_item(self.input)
    async def on_submit(self, interaction: discord.Interaction):
        await safe_defer(interaction, ephemeral=True)
        try:
            parsed = json.loads(self.input.value)
        except Exception as e:
            await interaction.followup.send(f"❌ Invalid JSON: {e}", ephemeral=True)
            return
        embed = json_dev_embed("manual", parsed, {"source": "paste"})
        await interaction.followup.send(embed=embed, ephemeral=True)

@tree.command(name="jsondebug", description="Paste JSON to pretty-print (dev view)")
async def jsondebug_cmd(interaction: discord.Interaction):
    await interaction.response.send_modal(JsonPasteModal())

# ---------------- Alerts & Dashboard Controls (buttons/modals) ----------------
class IntervalModal(Modal):
    def __init__(self):
        super().__init__(title="Set Monitor Interval (seconds)")
        self.interval = TextInput(label="Interval seconds", placeholder=str(DEFAULT_DASH_INTERVAL), required=True, max_length=6)
        self.add_item(self.interval)
    async def on_submit(self, interaction: discord.Interaction):
        await safe_defer(interaction, ephemeral=True)
        try:
            val = int(self.interval.value.strip())
            if val < 5:
                raise ValueError("Min 5s")
            monitor.interval = val
            # restart loop with new interval
            if monitor_loop.is_running():
                monitor_loop.change_interval(seconds=val)
            await interaction.followup.send(f"✅ Monitor interval set to {val}s", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Invalid value: {e}", ephemeral=True)

class DashboardControlView(View):
    def __init__(self):
        super().__init__(timeout=None)
        self.start = Button(label="▶️ Start Monitor", style=discord.ButtonStyle.success)
        self.stop = Button(label="⏸️ Stop Monitor", style=discord.ButtonStyle.danger)
        self.refresh = Button(label="🔁 Manual Refresh", style=discord.ButtonStyle.secondary)
        self.interval_btn = Button(label="⏱️ Set Interval", style=discord.ButtonStyle.secondary)
        self.clear_alerts = Button(label="🧹 Clear Alerts", style=discord.ButtonStyle.secondary)
        self.add_item(self.start)
        self.add_item(self.stop)
        self.add_item(self.refresh)
        self.add_item(self.interval_btn)
        self.add_item(self.clear_alerts)
        self.start.callback = self._on_start
        self.stop.callback = self._on_stop
        self.refresh.callback = self._on_refresh
        self.interval_btn.callback = self._on_interval
        self.clear_alerts.callback = self._on_clear_alerts

    async def _on_start(self, interaction: discord.Interaction):
        await safe_defer(interaction, ephemeral=True)
        monitor.running = True
        if not monitor_loop.is_running():
            monitor_loop.start()
        await interaction.followup.send("✅ Monitor started", ephemeral=True)

    async def _on_stop(self, interaction: discord.Interaction):
        await safe_defer(interaction, ephemeral=True)
        monitor.running = False
        await interaction.followup.send("⏸️ Monitor stopped", ephemeral=True)

    async def _on_refresh(self, interaction: discord.Interaction):
        await safe_defer(interaction, ephemeral=True)
        alerts = await monitor.scan_once()
        text = f"✅ Scanned. {len(alerts)} alerts generated." if alerts else "✅ Scanned. No alerts."
        await interaction.followup.send(text, ephemeral=True)

    async def _on_interval(self, interaction: discord.Interaction):
        await interaction.response.send_modal(IntervalModal())

    async def _on_clear_alerts(self, interaction: discord.Interaction):
        await safe_defer(interaction, ephemeral=True)
        n = len(monitor.alerts)
        monitor.alerts.clear()
        await interaction.followup.send(f"✅ Cleared {n} alerts", ephemeral=True)

# ---------------- Monitor loop (uses monitor.interval) ----------------
@tasks.loop(seconds=DEFAULT_DASH_INTERVAL)
async def monitor_loop():
    # dynamic interval: change if monitor.interval differs
    try:
        if monitor.interval != monitor_loop.seconds:
            monitor_loop.change_interval(seconds=monitor.interval)
    except Exception:
        pass
    if not monitor.running:
        return
    try:
        alerts = await monitor.scan_once()
        # send to alert channel if configured
        if alerts and ALERT_CHANNEL_ID:
            ch = bot.get_channel(int(ALERT_CHANNEL_ID))
            if ch:
                summary = f"**🚨 WarEra Monitor — {len(alerts)} alerts**\n"
                by_cat = {}
                for a in alerts:
                    by_cat.setdefault(a.category,0)
                    by_cat[a.category]+=1
                for c,n in by_cat.items():
                    summary += f"• {c}: {n}\n"
                await ch.send(summary)
                for a in alerts[:12]:
                    embed = discord.Embed(title=f"{a.level.value} {a.category} — {a.title}", description=a.message, color=(discord.Color.red() if a.level==AlertLevel.CRITICAL else (discord.Color.gold() if a.level==AlertLevel.WARNING else discord.Color.blue())), timestamp=datetime.fromisoformat(a.timestamp))
                    if a.data:
                        for k,v in a.data.items():
                            try:
                                embed.add_field(name=str(k), value=safe_truncate(json.dumps(v, default=str), 256), inline=True)
                            except:
                                embed.add_field(name=str(k), value=str(v), inline=True)
                    await ch.send(embed=embed)
                if len(alerts) > 12:
                    await ch.send(f"⚠️ {len(alerts)-12} more alerts suppressed")
    except Exception as e:
        print("[monitor_loop] error:", e)

# ---------------- Dashboard command: full controls and combined view ----------------
@tree.command(name="dashboard", description="Post a combined dashboard with controls (Game view + Alerts)")
async def dashboard_cmd(interaction: discord.Interaction):
    await safe_defer(interaction)
    # Build several widgets: top rankings + prices + active battles + alerts summary
    pages = []
    # 1) rankings top (userDamages)
    r_game, r_dev = await render_endpoint_full("ranking.getRanking", {"rankingType":"userDamages"})
    # pick first page for combined dashboard
    rank_embed = r_game[0] if r_game else make_game_embed("Rankings", "No data")
    # 2) prices small
    prices_data = await war_api.call("itemTrading.getPrices")
    p_embed = make_game_embed("💰 Prices")
    if isinstance(prices_data, dict):
        # top 6 items
        i=0
        for k,v in prices_data.items():
            if i>=6: break
            p_embed.add_field(name=safe_truncate(k,32), value=fmt_num(v), inline=True)
            i+=1
    else:
        p_embed.description = safe_truncate(str(prices_data),200)
    # 3) battles snippet
    battles_data = await war_api.call("battle.getBattles")
    b_embed = make_game_embed("⚔️ Battles")
    if isinstance(battles_data, list):
        for b in battles_data[:6]:
            if isinstance(b, dict):
                a_country = b.get("attackerCountry") or b.get("attacker")
                d_country = b.get("defenderCountry") or b.get("defender")
                status = b.get("status") or b.get("phase")
                b_embed.add_field(name=f"{a_country} vs {d_country}", value=safe_truncate(str(status),64), inline=False)
            else:
                b_embed.add_field(name=str(b), value="—", inline=False)
    else:
        b_embed.description = safe_truncate(str(battles_data),200)

    # 4) alerts summary
    alert_embed = make_game_embed("🚨 Alerts Summary")
    if monitor.alerts:
        # show latest 6 alerts
        for a in monitor.alerts[:6]:
            alert_embed.add_field(name=f"{a.level.value} {a.category}", value=safe_truncate(a.message, 80), inline=False)
    else:
        alert_embed.description = "No alerts"

    # assemble game_pages as multiple pages user can flip through
    game_pages = [rank_embed, p_embed, b_embed, alert_embed]
    dev_pages = [json_dev_embed("ranking.getRanking (dev)", {}, {}), json_dev_embed("itemTrading.getPrices (dev)", prices_data or {}, {}), json_dev_embed("battle.getBattles (dev)", battles_data or {}, {}), json_dev_embed("alerts (dev)", [a.__dict__ for a in monitor.alerts], {})]
    view = GameDevPageView(game_pages, dev_pages)
    # add DashboardControlView below as second view (can't display two views on same message easily)
    # Instead include a grouped control view with both sets of buttons
    control_view = DashboardControlView()
    # combine controls into one message: first embed + controls
    msg = await interaction.followup.send(embed=game_pages[0], view=view)
    # Also send controls message (ephemeral to user), with persistent control view so admins can use
    try:
        await interaction.followup.send("Dashboard controls (only you can see this). Start/Stop and manage monitor here:", view=control_view, ephemeral=True)
    except Exception:
        # fallback: non-ephemeral if ephemeral fails
        await interaction.followup.send("Dashboard controls (public):", view=control_view)
    return msg

# ---------------- Bot events & run ----------------
@bot.event
async def on_ready():
    print(f"[WarEraBot] Logged in as {bot.user} (id={bot.user.id})")
    try:
        await tree.sync()
        print("[WarEraBot] Slash commands synced.")
    except Exception as e:
        print("[WarEraBot] Slash sync failed:", e)
    # don't auto-start monitor unless user starts it; but start loop so it can be used
    if not monitor_loop.is_running():
        monitor_loop.start()

# ---------------- Run ----------------
if __name__ == "__main__":
    if DISCORD_TOKEN == "YOUR_TOKEN_HERE":
        print("Set DISCORD_BOT_TOKEN environment variable and restart.")
    else:
        bot.run(DISCORD_TOKEN)
