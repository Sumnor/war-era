# war-era-complete.py
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
REQUEST_TIMEOUT = float(os.getenv("WARERA_REQUEST_TIMEOUT", "10"))
RETRY_ATTEMPTS = int(os.getenv("WARERA_RETRY_ATTEMPTS", "3"))
RETRY_BACKOFF = float(os.getenv("WARERA_RETRY_BACKOFF", "0.7"))
DEFAULT_DASH_INTERVAL = int(os.getenv("WARERA_DASH_INTERVAL", "60"))
PAGE_SIZE = int(os.getenv("WARERA_PAGE_SIZE", "8"))

# Custom emoji/image URLs
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

_session: Optional[aiohttp.ClientSession] = None

async def get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT))
    return _session

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
    if not user_id:
        return ""
    return f"https://warera.io/profile/{user_id}"

def company_url(comp_id: Optional[str]) -> str:
    if not comp_id:
        return ""
    return f"https://warera.io/company/{comp_id}"

def region_url(reg_id: Optional[str]) -> str:
    if not reg_id:
        return ""
    return f"https://warera.io/region/{reg_id}"

# ---------------- API Client ----------------
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
    data: Dict[str, Any] = field(default_factory=dict)

class WarEraMonitor:
    def __init__(self, api: WarEraAPI):
        self.api = api
        self.prev: Dict[str, Any] = {}
        self.alerts: List[Alert] = []
        self.running = False
        self.interval = DEFAULT_DASH_INTERVAL
        self.simple = ["itemTrading.getPrices", "battle.getBattles", "country.getAllCountries"]
        self.paginated = ["company.getCompanies", "workOffer.getWorkOffersPaginated", "article.getArticlesPaginated", "mu.getManyPaginated", "transaction.getPaginatedTransactions"]
        self.special = {"ranking.getRanking": {"rankingType": "userDamages"}}
        self.price_threshold = 20.0
        self.price_critical = 50.0

    def _make_alert(self, level: AlertLevel, cat: str, title: str, msg: str, data: Dict = None) -> Alert:
        a = Alert(datetime.now(timezone.utc).isoformat(), level, cat, title, msg, data or {})
        self.alerts.insert(0, a)
        return a

    async def scan(self) -> List[Alert]:
        new_alerts: List[Alert] = []
        # Simple endpoints
        for ep in self.simple:
            d = await self.api.call(ep)
            if d is None:
                continue
            prev = self.prev.get(ep)
            # list addition
            if isinstance(prev, list) and isinstance(d, list):
                if len(d) > len(prev):
                    diff = len(d) - len(prev)
                    new_alerts.append(self._make_alert(AlertLevel.WARNING, "NEW", ep, f"+{diff} items", {"diff": diff}))
            # price dictionary
            if isinstance(prev, dict) and isinstance(d, dict):
                for k, v in d.items():
                    if isinstance(v, (int, float)):
                        old = prev.get(k)
                        if isinstance(old, (int, float)) and old != 0:
                            change = ((v - old) / abs(old)) * 100.0
                            if abs(change) >= self.price_threshold:
                                lvl = AlertLevel.CRITICAL if abs(change) >= self.price_critical else AlertLevel.WARNING
                                new_alerts.append(self._make_alert(lvl, "PRICE", ep, f"{k}: {change:+.2f}%", {"old": old, "new": v, "pct": change}))
            self.prev[ep] = d

        # Paginated
        for ep in self.paginated:
            d = await self.api.call(ep, {"page": 1, "limit": 50})
            if d is None:
                continue
            prev = self.prev.get(ep)
            if isinstance(prev, list) and isinstance(d, list):
                if len(d) > len(prev):
                    diff = len(d) - len(prev)
                    new_alerts.append(self._make_alert(AlertLevel.INFO, "MORE", ep, f"+{diff}", {"diff": diff}))
            self.prev[ep] = d

        # special
        for ep, params in self.special.items():
            d = await self.api.call(ep, params)
            if d is None:
                continue
            prev = self.prev.get(ep)
            if isinstance(prev, dict) and isinstance(d, dict):
                pi = prev.get("items")
                di = d.get("items")
                if isinstance(pi, list) and isinstance(di, list) and pi and di:
                    if pi[0].get("_id") != di[0].get("_id"):
                        new_alerts.append(self._make_alert(AlertLevel.WARNING, "RANK", ep, "Top changed", {"old": pi[0], "new": di[0]}))
            self.prev[ep] = d

        self.prev["_last_scan"] = datetime.now(timezone.utc).isoformat()
        return new_alerts

monitor = WarEraMonitor(war_api)

# monitor loop
@tasks.loop(seconds=DEFAULT_DASH_INTERVAL)
async def monitor_loop():
    try:
        if not monitor.running:
            return
        alerts = await monitor.scan()
        if alerts and ALERT_CHANNEL_ID:
            ch = bot.get_channel(int(ALERT_CHANNEL_ID))
            if ch:
                summary = f"**🚨 {len(alerts)} alerts**\n"
                by = {}
                for a in alerts:
                    by.setdefault(a.category,0)
                    by[a.category]+=1
                for c, cnt in by.items():
                    summary += f"• {c}: {cnt}\n"
                await ch.send(summary)
                for a in alerts[:10]:
                    emb = discord.Embed(title=f"{a.level.value} {a.category} — {a.title}", description=a.message, timestamp=datetime.fromisoformat(a.timestamp),
                                         color=(discord.Color.red() if a.level == AlertLevel.CRITICAL else discord.Color.gold()))
                    for k, v in a.data.items():
                        emb.add_field(name=str(k), value=safe_truncate(json.dumps(v, default=str), 256), inline=True)
                    await ch.send(embed=emb)
                if len(alerts) > 10:
                    await ch.send(f"⚠️ {len(alerts)-10} more suppressed")
    except Exception as e:
        print("[monitor_loop] error:", e)

# ---------------- Pagination & toggle view ----------------
class GameDevView(View):
    def __init__(self, game_pages: List[discord.Embed], dev_pages: Optional[List[discord.Embed]] = None, *, timeout: int = 180):
        super().__init__(timeout=timeout)
        self.game_pages = game_pages
        self.dev_pages = dev_pages or []
        self.mode = "game"
        self.idx = 0
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
        length = len(self.game_pages) if self.mode == "game" else len(self.dev_pages)
        self.prev_btn.disabled = self.idx <= 0 or length <= 1
        self.next_btn.disabled = self.idx >= (length - 1) or length <= 1
        self.toggle_btn.label = "🧠 Dev View" if self.mode == "game" else "🎮 Game View"

    async def on_prev(self, interaction: discord.Interaction):
        await interaction.response.defer()
        self.idx = max(0, self.idx - 1)
        await self._refresh(interaction)

    async def on_next(self, interaction: discord.Interaction):
        await interaction.response.defer()
        length = len(self.game_pages) if self.mode == "game" else len(self.dev_pages)
        self.idx = min(length-1, self.idx + 1)
        await self._refresh(interaction)

    async def on_toggle(self, interaction: discord.Interaction):
        await interaction.response.defer()
        self.mode = "dev" if self.mode == "game" else "game"
        length = len(self.game_pages) if self.mode == "game" else len(self.dev_pages)
        if length == 0:
            self.mode = "game" if self.mode == "dev" else "dev"
            await interaction.followup.send("No data for that view", ephemeral=True)
            return
        if self.idx >= length:
            self.idx = length - 1
        await self._refresh(interaction)

    async def _refresh(self, interaction: discord.Interaction):
        embed = (self.game_pages[self.idx] if self.mode == "game" else self.dev_pages[self.idx]) or discord.Embed(title="No data")
        self._update()
        await interaction.followup.edit_message(interaction.message.id, embed=embed, view=self)

# JSON modal & button
class JsonModal(Modal):
    def __init__(self, endpoint: str):
        super().__init__(title=f"View JSON — {endpoint}")
        self.endpoint = endpoint
        self.key = TextInput(label="Optional key (dot path)", required=False, placeholder="items.0 or id")
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
            sub = data
            try:
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
        emb = discord.Embed(title=f"🧠 {self.endpoint} JSON", description=f"```json\n{json.dumps(data, indent=2, default=str)[:1900]}\n```", timestamp=now_utc(), color=discord.Color.dark_grey())
        await interaction.followup.send(embed=emb, ephemeral=True)

class JsonButton(Button):
    def __init__(self, endpoint: str):
        super().__init__(label="🔍 View JSON", style=discord.ButtonStyle.secondary)
        self.endpoint = endpoint
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(JsonModal(self.endpoint))

# ---------------- Renderers ----------------
async def safe_defer(interaction: discord.Interaction, ephemeral=False):
    if not interaction.response.is_done():
        try:
            await interaction.response.defer(ephemeral=ephemeral)
        except:
            pass

async def send_game_dev(interaction: discord.Interaction, game_pages: List[discord.Embed], dev_pages: Optional[List[discord.Embed]] = None):
    await safe_defer(interaction)
    view = GameDevView(game_pages, dev_pages or [])
    await interaction.followup.send(embed=game_pages[0], view=view)

def pretty_ranking_pages(endpoint: str, items: List[Any]) -> Tuple[List[discord.Embed], List[discord.Embed]]:
    g, d = [], []
    total = len(items)
    for i in range(0, total, PAGE_SIZE):
        chunk = items[i : i + PAGE_SIZE]
        e = discord.Embed(title=endpoint, description=f"{i+1}-{i+len(chunk)} / {total}", color=discord.Color.dark_gold(), timestamp=now_utc())
        for idx, it in enumerate(chunk, start=i+1):
            if isinstance(it, dict):
                name = it.get("name") or it.get("user") or it.get("_id") or str(idx)
                uid = it.get("user") or it.get("_id") or it.get("id")
                url = profile_url(uid)
                tier = it.get("tier", "")
                medal = medal_for_tier(tier)
                val = it.get("value") or it.get("damage") or it.get("score") or 0
                val_s = fmt_num(val)
                name_disp = f"[{safe_truncate(name,36)}]({url})" if url else safe_truncate(name,36)
                fval = f"{medal} **{name_disp}**\n⚔️ **{val_s}**"
                if tier:
                    fval += f"\n· {safe_truncate(str(tier),20)}"
                e.add_field(name=f"#{idx}", value=fval, inline=False)
            else:
                e.add_field(name=f"#{idx}", value=safe_truncate(str(it),200), inline=False)
        g.append(e)
        d.append(JsonModal(endpoint))  # dummy pages for dev (or embed)
    if not g:
        g.append(discord.Embed(title=endpoint, description="No results", color=discord.Color.greyple()))
        d.append(discord.Embed(title=f"{endpoint} (dev)", description="No data", color=discord.Color.dark_grey()))
    return g, d

def list_summary_pages(endpoint: str, items: List[Any]) -> Tuple[List[discord.Embed], List[discord.Embed]]:
    g, d = [], []
    total = len(items)
    for i in range(0, total, PAGE_SIZE):
        chunk = items[i : i + PAGE_SIZE]
        e = discord.Embed(title=endpoint, description=f"{i+1}-{i+len(chunk)} / {total}", timestamp=now_utc())
        for idx, it in enumerate(chunk, start=i+1):
            if isinstance(it, dict):
                name = it.get("name") or it.get("title") or it.get("_id") or str(idx)
                keys = []
                for k in ("region","status","price","value","rank","company","members"):
                    if k in it:
                        keys.append(f"{k}:{fmt_num(it[k])}")
                e.add_field(name=f"#{idx} {safe_truncate(name,30)}", value=", ".join(keys) or "—", inline=False)
            else:
                e.add_field(name=f"#{idx}", value=safe_truncate(str(it),200), inline=False)
        g.append(e)
        d.append(json_dev_embed(endpoint + " (dev)", chunk, {"chunk": i+1}))
    if not g:
        g.append(discord.Embed(title=endpoint, description="No data"))
        d.append(json_dev_embed(endpoint + " (dev)", items, {}))
    return g, d

def single_object_pages(endpoint: str, obj: Dict[str,Any]) -> Tuple[List[discord.Embed], List[discord.Embed]]:
    e = discord.Embed(title=endpoint, timestamp=now_utc())
    small = {k:v for k,v in obj.items() if isinstance(v, (str,int,float,bool))}
    add_small_fields(e, small, limit=12)
    return [e], [json_dev_embed(endpoint + " (dev)", obj, {"single":True})]

async def render_endpoint_full(endpoint: str, params: Optional[Dict] = None) -> Tuple[List[discord.Embed], List[discord.Embed]]:
    data = await war_api.call(endpoint, params)
    if data is None:
        return [discord.Embed(title=endpoint, description="❌ Fetch error", color=discord.Color.red())], [json_dev_embed(endpoint + " (dev)", {}, {})]
    if isinstance(data, dict):
        if "items" in data and isinstance(data["items"], list):
            return pretty_ranking_pages(endpoint, data["items"])
        if "results" in data and isinstance(data["results"], list):
            return list_summary_pages(endpoint, data["results"])
        if "data" in data and isinstance(data["data"], list):
            return list_summary_pages(endpoint, data["data"])
        if any(k in data for k in ("name","members","region","price","user","title")):
            return single_object_pages(endpoint, data)
        e = discord.Embed(title=endpoint, timestamp=now_utc())
        small = {k:v for k,v in data.items() if isinstance(v,(str,int,float,bool))}
        add_small_fields(e, small, limit=10)
        return [e], [json_dev_embed(endpoint + " (dev)", data, {"keys": list(small.keys())})]
    if isinstance(data, list):
        return list_summary_pages(endpoint, data)
    return [discord.Embed(title=endpoint, description=str(data), timestamp=now_utc())], [json_dev_embed(endpoint + " (dev)", data, {})]

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

@tree.command(name="help", description="Show commands")
async def help_cmd(interaction: discord.Interaction):
    await safe_defer(interaction)
    e = discord.Embed(title="WarEra Dashboard Commands", timestamp=now_utc(), color=discord.Color.gold())
    commands_list = [
        ("/rankings <type>", "Leaderboards"),
        ("/countries", "List countries"),
        ("/companies", "List companies"),
        ("/company <id>", "Company by ID"),
        ("/battles", "Active battles"),
        ("/battle <id>", "Battle by ID"),
        ("/workoffers", "Work offers"),
        ("/workoffer <id>", "Work offer by ID"),
        ("/mu", "Military units"),
        ("/mu_by_id <id>", "MU by ID"),
        ("/articles", "List articles"),
        ("/article <id>", "Article by ID"),
        ("/prices", "Item prices"),
        ("/transactions", "Recent transactions"),
        ("/users <id>", "User info"),
        ("/search <q>", "Search API"),
        ("/dashboard", "Show combined dashboard & controls"),
        ("/jsondebug", "Paste JSON, see embed"),
    ]
    for c,d in commands_list:
        e.add_field(name=c, value=d, inline=False)
    await interaction.followup.send(embed=e)

@tree.command(name="rankings", description="View rankings")
@app_commands.choices(ranking_type=RANKING_CHOICES)
@app_commands.describe(ranking_type="Ranking type")
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
    game, dev = await render_endpoint_full("company.getCompanies", {"page":1,"limit":50})
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
@app_commands.describe(offer_id="Offer ID")
async def workoffer_cmd(interaction: discord.Interaction, offer_id: str):
    await safe_defer(interaction)
    game, dev = await render_endpoint_full("workOffer.getById", {"workOfferId": offer_id})
    await send_game_dev(interaction, game, dev)

@tree.command(name="mu", description="List military units")
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

@tree.command(name="transactions", description="List transactions")
async def transactions_cmd(interaction: discord.Interaction):
    await safe_defer(interaction)
    game, dev = await render_endpoint_full("transaction.getPaginatedTransactions", {"page":1,"limit":50})
    await send_game_dev(interaction, game, dev)

@tree.command(name="users", description="User by ID")
@app_commands.describe(user_id="User ID")
async def users_cmd(interaction: discord.Interaction, user_id: str):
    await safe_defer(interaction)
    game, dev = await render_endpoint_full("user.getUserLite", {"userId": user_id})
    await send_game_dev(interaction, game, dev)

@tree.command(name="search", description="Search anything")
@app_commands.describe(query="Search text")
async def search_cmd(interaction: discord.Interaction, query: str):
    await safe_defer(interaction)
    game, dev = await render_endpoint_full("search.searchAnything", {"searchText": query})
    await send_game_dev(interaction, game, dev)

@tree.command(name="jsondebug", description="Paste JSON to pretty-print")
async def jsondebug_cmd(interaction: discord.Interaction):
    await interaction.response.send_modal(JsonModal("manual"))

@tree.command(name="dashboard", description="Show world dashboard & controls")
async def dashboard_cmd(interaction: discord.Interaction):
    await safe_defer(interaction)
    # build pieces: ranking / prices / battles / alerts
    game_p, dev_p = await render_endpoint_full("ranking.getRanking", {"rankingType": "userDamages"})
    ranking_embed = game_p[0] if game_p else make_game_embed("Ranking")
    # prices
    pr = await war_api.call("itemTrading.getPrices")
    pe = make_game_embed("💰 Prices")
    if isinstance(pr, dict):
        count=0
        for k,v in pr.items():
            if count >= 6: break
            pe.add_field(name=safe_truncate(k,24), value=fmt_num(v), inline=True)
            count+=1
    else:
        pe.description = safe_truncate(str(pr),200)
    # battles
    bd = await war_api.call("battle.getBattles")
    be = make_game_embed("⚔️ Battles")
    if isinstance(bd, list):
        for b in bd[:5]:
            if isinstance(b, dict):
                a = b.get("attackerCountry") or b.get("attacker")
                d = b.get("defenderCountry") or b.get("defender")
                s = b.get("status") or b.get("phase")
                be.add_field(name=f"{a} vs {d}", value=safe_truncate(str(s),30), inline=False)
            else:
                be.add_field(name=str(b), value="—", inline=False)
    else:
        be.description = safe_truncate(str(bd),200)
    # alerts summary
    ae = make_game_embed("🚨 Alerts")
    if monitor.alerts:
        for a in monitor.alerts[:5]:
            ae.add_field(name=f"{a.level.value} {a.category}", value=safe_truncate(a.message,80), inline=False)
    else:
        ae.description = "No alerts"
    pages = [ranking_embed, pe, be, ae]
    dev_pages = [json_dev_embed("ranking (dev)", {}, {}), json_dev_embed("prices (dev)", pr or {}, {}), json_dev_embed("battle (dev)", bd or {}, {}), json_dev_embed("alerts (dev)", [a.__dict__ for a in monitor.alerts], {})]
    view = GameDevView(pages, dev_pages)
    control = DashboardControlView()
    msg = await interaction.followup.send(embed=pages[0], view=view)
    try:
        await interaction.followup.send("Dashboard controls:", view=control, ephemeral=True)
    except:
        await interaction.followup.send("Dashboard controls (public):", view=control)

@bot.event
async def on_ready():
    print(f"[WarEra] Bot logged in as {bot.user} (ID {bot.user.id})")
    try:
        await tree.sync()
        print("[WarEra] Slash commands synced.")
    except Exception as e:
        print("[WarEra] Sync error:", e)
    if not monitor_loop.is_running():
        monitor_loop.start()

if __name__ == "__main__":
    if DISCORD_TOKEN == "YOUR_TOKEN_HERE":
        print("Please set DISCORD_BOT_TOKEN environment variable.")
    else:
        bot.run(DISCORD_TOKEN)
