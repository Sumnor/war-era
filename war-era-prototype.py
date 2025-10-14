import os
import json
import asyncio
import aiohttp
import urllib.parse
from typing import Optional, Dict, Any, List
from datetime import datetime
import discord
from discord import app_commands
from discord.ext import tasks, commands
from discord.ui import View, Button, Modal, TextInput

# ---------------- Config ----------------
API_BASE = os.getenv("WARERA_API_BASE", "https://api2.warera.io/trpc")
DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "YOUR_TOKEN_HERE")
DASH_CHANNEL_ID = os.getenv("WARERA_DASH_CHANNEL")  # optional channel id for auto dashboard
REQUEST_TIMEOUT = float(os.getenv("WARERA_REQUEST_TIMEOUT", "10"))
RETRY_ATTEMPTS = int(os.getenv("WARERA_RETRY_ATTEMPTS", "3"))
RETRY_BACKOFF = float(os.getenv("WARERA_RETRY_BACKOFF", "0.7"))
DEFAULT_DASH_INTERVAL = int(os.getenv("WARERA_DASH_INTERVAL", "60"))  # seconds
PAGE_SIZE = int(os.getenv("WARERA_PAGE_SIZE", "8"))

# Custom emoji URLs for game aesthetic (replace with your CDN / assets)
CUSTOM_EMOJIS = {
    "master": "https://i.imgur.com/8YgXGkX.png",   # sample medal images
    "gold": "https://i.imgur.com/4YFQx4y.png",
    "silver": "https://i.imgur.com/1H4Zb6C.png",
    "bronze": "https://i.imgur.com/7h7k8G1.png",
    "mu": "https://i.imgur.com/d6QfFv6.png",
    "country": "https://i.imgur.com/3k8QH8x.png",
    "company": "https://i.imgur.com/9u9p1Yy.png",
    "fire": "https://i.imgur.com/7y2KQ8b.png",
}

# ---------------- Bot Setup ----------------
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# Shared aiohttp session
_session: Optional[aiohttp.ClientSession] = None

async def get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT))
    return _session

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
                    # unwrap common tRPC envelope
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

# ---------------- Utilities & Formatting ----------------
MAX_DESC = 2048
MAX_FIELD = 1024

def safe_truncate(s: str, n: int) -> str:
    if s is None:
        return ""
    s = str(s)
    return s if len(s) <= n else s[: n - 3] + "..."

def fmt_num(n: Any) -> str:
    try:
        if isinstance(n, (int, float)):
            return f"{int(n):,}"
        return str(n)
    except:
        return str(n)

def medal_for_tier(tier: str) -> str:
    t = (tier or "").lower()
    if t.startswith("maste") or t == "master": return CUSTOM_EMOJIS.get("master") or "🥇"
    if "gold" in t: return CUSTOM_EMOJIS.get("gold") or "🥈"
    if "silver" in t: return CUSTOM_EMOJIS.get("silver") or "🥉"
    if "bronze" in t: return CUSTOM_EMOJIS.get("bronze") or "🏅"
    return CUSTOM_EMOJIS.get("mu") or "🏵️"

def avatar_for_user(user_id: Optional[str]) -> str:
    # WarEra profile link pattern — change if needed
    if not user_id: return ""
    return f"https://warera.io/avatar/{user_id}.png"

def profile_url(user_id: str) -> str:
    return f"https://warera.io/profile/{user_id}"

# ---------------- Embed Builders ----------------
def game_card_for_mu(mu: Dict[str, Any]) -> discord.Embed:
    name = mu.get("name", mu.get("_id", "MU"))
    e = discord.Embed(title=f"🎖️ {name}", color=discord.Color.dark_teal(), timestamp=datetime.utcnow())
    # Top row summary
    members = mu.get("members", [])
    region = mu.get("region", "Unknown")
    managers = mu.get("roles", {}).get("managers", []) if isinstance(mu.get("roles"), dict) else []
    e.add_field(name="Members", value=str(len(members)), inline=True)
    e.add_field(name="Region", value=region, inline=True)
    e.add_field(name="Managers", value=str(len(managers)), inline=True)
    # Small details field
    invest = mu.get("investedMoneyByUsers", {})
    if isinstance(invest, dict):
        total_invest = sum(int(v) for v in invest.values()) if invest else 0
        e.add_field(name="Invested", value=f"{total_invest:,}", inline=True)
    # created / updated
    created = mu.get("createdAt") or mu.get("created")
    if created:
        e.add_field(name="Created", value=created.split("T")[0], inline=True)
    # metadata
    e.set_footer(text=f"ID: {mu.get('_id','')}")
    # optional thumbnail
    if mu.get("animatedAvatarUrl"):
        e.set_thumbnail(url=mu.get("animatedAvatarUrl"))
    return e

def game_leaderboard_pages(title: str, items: List[Dict[str, Any]]) -> List[discord.Embed]:
    pages: List[discord.Embed] = []
    total = len(items)
    for i in range(0, total, PAGE_SIZE):
        chunk = items[i : i + PAGE_SIZE]
        e = discord.Embed(title=title, description=f"Showing {i+1}-{i+len(chunk)} of {total}", color=discord.Color.dark_gold(), timestamp=datetime.utcnow())
        for idx, it in enumerate(chunk, start=i+1):
            # try to display human-friendly label
            name = it.get("name") or it.get("user") or it.get("id") or str(idx)
            uid = it.get("user") or it.get("_id") or it.get("id")
            # tier & medal
            tier = it.get("tier", "")
            medal = medal_for_tier(tier)
            # value (damage / score)
            val = it.get("value") or it.get("damage") or it.get("score") or it.get("wealth") or 0
            val_s = fmt_num(val)
            # build inline value and URL if user
            url = profile_url(uid) if uid else None
            name_line = f"[{safe_truncate(name, 36)}]({url})" if url else safe_truncate(name, 36)
            # field value; bold the number with fire icon
            field_val = f"{medal} **{name_line}**\n⚔️ **{val_s}**"
            # include tier text
            if tier:
                field_val += f"\n· {tier.capitalize()}"
            e.add_field(name=f"#{idx}", value=field_val, inline=False)
        pages.append(e)
    if not pages:
        pages.append(discord.Embed(title=title, description="No results", color=discord.Color.greyple()))
    return pages

def game_country_pages(title: str, countries: List[Dict[str, Any]]) -> List[discord.Embed]:
    pages: List[discord.Embed] = []
    total = len(countries)
    for i in range(0, total, PAGE_SIZE):
        chunk = countries[i : i + PAGE_SIZE]
        e = discord.Embed(title=title, description=f"Showing {i+1}-{i+len(chunk)} of {total}", color=discord.Color.green(), timestamp=datetime.utcnow())
        for country in chunk:
            name = country.get("name") or country.get("id")
            cid = country.get("id")
            # small stats: ranking/population/economy values if present
            pop = country.get("population") or country.get("citizens") or country.get("size")
            econ = country.get("economy") or country.get("gdp") or country.get("wealth")
            summary = []
            if pop: summary.append(f"Population: {fmt_num(pop)}")
            if econ: summary.append(f"Econ: {fmt_num(econ)}")
            # link to country page if known (change if needed)
            url = f"https://warera.io/country/{cid}" if cid else None
            name_field = f"[{name}]({url})" if url else name
            e.add_field(name=name_field, value="\n".join(summary) or "—", inline=False)
        pages.append(e)
    if not pages:
        pages.append(discord.Embed(title=title, description="No countries", color=discord.Color.dark_green()))
    return pages

def json_dev_embed(endpoint: str, data: Any, meta: Optional[Dict[str,Any]] = None) -> discord.Embed:
    # pretty JSON with metadata header
    j = json.dumps(data, indent=2, default=str)
    if len(j) > 1900:
        j = j[:1897] + "..."
    title = f"🧠 DEV: {endpoint}"
    desc = ""
    if meta:
        meta_lines = [f"{k}: {v}" for k,v in (meta.items())]
        desc += "• " + " • ".join(meta_lines) + "\n\n"
    desc += f"```json\n{j}\n```"
    return discord.Embed(title=title, description=desc, color=discord.Color.dark_theme(), timestamp=datetime.utcnow())

# ---------------- Views: Pagination + Toggle ----------------
class GameDevPageView(View):
    """
    View with Prev / Next and Toggle (Game <-> Dev)
    When user clicks Toggle, embed content switches.
    """
    def __init__(self, game_pages: List[discord.Embed], dev_pages: Optional[List[discord.Embed]] = None, *, timeout: int = 180):
        super().__init__(timeout=timeout)
        self.game_pages = game_pages
        self.dev_pages = dev_pages or []
        self.mode = "game"  # or "dev"
        self.current = 0
        self.max = max(len(self.game_pages), len(self.dev_pages)) if self.dev_pages else len(self.game_pages)
        # Buttons
        self.prev = Button(emoji="◀️", style=discord.ButtonStyle.secondary)
        self.next = Button(emoji="▶️", style=discord.ButtonStyle.secondary)
        self.toggle = Button(label="🧠 Dev View", style=discord.ButtonStyle.primary)
        self.add_item(self.prev)
        self.add_item(self.toggle)
        self.add_item(self.next)
        self.prev.callback = self.on_prev
        self.next.callback = self.on_next
        self.toggle.callback = self.on_toggle
        self._update_buttons()

    def _update_buttons(self):
        self.prev.disabled = self.current <= 0
        self.next.disabled = self.current >= (self.max - 1)
        # toggle label depends on mode
        self.toggle.label = "🧠 Dev View" if self.mode == "game" else "🎮 Game View"

    async def on_prev(self, interaction: discord.Interaction):
        await interaction.response.defer()
        if self.current > 0:
            self.current -= 1
        await self._refresh(interaction)

    async def on_next(self, interaction: discord.Interaction):
        await interaction.response.defer()
        # determine page length for mode
        length = len(self.game_pages) if self.mode == "game" else len(self.dev_pages)
        if self.current < (length - 1):
            self.current += 1
        await self._refresh(interaction)

    async def on_toggle(self, interaction: discord.Interaction):
        await interaction.response.defer()
        # switch mode
        self.mode = "dev" if self.mode == "game" else "game"
        # clamp current to available pages
        length = len(self.game_pages) if self.mode == "game" else len(self.dev_pages)
        if length == 0:
            # nothing to show in target mode; flip back and notify
            self.mode = "game" if self.mode == "dev" else "dev"
            await interaction.followup.send("No data for that view.", ephemeral=True)
            return
        if self.current >= length:
            self.current = length - 1
        await self._refresh(interaction)

    async def _refresh(self, interaction: discord.Interaction):
        if self.mode == "game":
            embed = self.game_pages[self.current] if self.current < len(self.game_pages) else discord.Embed(title="No data")
        else:
            embed = self.dev_pages[self.current] if self.current < len(self.dev_pages) else discord.Embed(title="No data (dev)")
        self._update_buttons()
        await interaction.followup.edit_message(interaction.message.id, embed=embed, view=self)

# ------------ JSON Modal for paste/debug ------------
class PasteJsonModal(Modal):
    def __init__(self, endpoint: str = "manual"):
        super().__init__(title=f"JSON Debugger ({endpoint})")
        self.endpoint = endpoint
        self.json_input = TextInput(label="Paste JSON here", style=discord.TextStyle.long, required=True, placeholder='{"id": "..."}')
        self.add_item(self.json_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        raw = self.json_input.value
        try:
            parsed = json.loads(raw)
        except Exception as e:
            await interaction.followup.send(f"❌ Invalid JSON: {e}", ephemeral=True)
            return
        embed = json_dev_embed(self.endpoint, parsed, {"source": "manual paste", "length": len(raw)})
        await interaction.followup.send(embed=embed, ephemeral=True)

# ---------------- Core Bot Class ----------------
class WarEraBot:
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.api = WarEraAPI()
        self.dash_message: Optional[discord.Message] = None

    # Generic renderer for endpoint: returns (game_pages, dev_pages)
    async def render_endpoint(self, endpoint: str, params: Optional[Dict] = None) -> (List[discord.Embed], List[discord.Embed]):
        data = await self.api.call(endpoint, params)
        # Build game pages + dev pages
        # Many endpoints return either list or dict with 'items'
        if data is None:
            game = [discord.Embed(title="Error", description="Failed to fetch data", color=discord.Color.red())]
            dev = [json_dev_embed(endpoint, {"error": "fetch failed"}, {"endpoint": endpoint})]
            return game, dev

        # Rankings (special)
        if isinstance(data, dict) and "items" in data and isinstance(data["items"], list):
            items = data["items"]
            game_pages = game_leaderboard_pages(f"🏆 {endpoint}", items)
            dev_pages = [json_dev_embed(endpoint, data, {"count": len(items)})]
            return game_pages, dev_pages

        # If list of MUs
        if isinstance(data, list):
            # detect MU by endpoint name
            if "mu" in endpoint.lower():
                game_pages = []
                for i in range(0, len(data), PAGE_SIZE):
                    chunk = data[i : i + PAGE_SIZE]
                    embeds = [game_card_for_mu(item) for item in chunk]
                    # group multiple MU cards onto one page by concatenating fields of a new embed
                    page = discord.Embed(title=f"🎖️ MUs — {i+1}-{i+len(chunk)}", color=discord.Color.dark_blue(), timestamp=datetime.utcnow())
                    for ent in chunk:
                        # small summary field for each MU
                        name = ent.get("name") or ent.get("_id")
                        members = len(ent.get("members", []))
                        region = ent.get("region", "—")
                        page.add_field(name=name, value=f"Members: {members}\nRegion: {region}", inline=False)
                    game_pages.append(page)
                dev_pages = [json_dev_embed(endpoint, data, {"count": len(data)})]
                return game_pages, dev_pages

            # generic list -> pages with summary fields
            pages = []
            for i in range(0, len(data), PAGE_SIZE):
                chunk = data[i : i + PAGE_SIZE]
                e = discord.Embed(title=f"{endpoint}", description=f"Showing {i+1}-{i+len(chunk)} of {len(data)}", color=discord.Color.blurple(), timestamp=datetime.utcnow())
                for j, item in enumerate(chunk, start=i+1):
                    if isinstance(item, dict):
                        name = item.get("name") or item.get("title") or item.get("id") or str(j)
                        # summary select keys
                        keys = []
                        for k in ("region", "status", "price", "value", "rank", "user", "company", "title"):
                            if k in item:
                                keys.append(f"{k}:{fmt_num(item[k])}")
                        e.add_field(name=f"{j}. {safe_truncate(name, 36)}", value=", ".join(keys) or "—", inline=False)
                    else:
                        e.add_field(name=f"{j}", value=str(item)[:200], inline=False)
                pages.append(e)
            dev_pages = [json_dev_embed(endpoint, data, {"count": len(data)})]
            return pages, dev_pages

        # dict single -> show fields
        if isinstance(data, dict):
            # If countries
            if "name" in data and ("region" in data or "id" in data):
                # single country object
                game = [discord.Embed(title=f"🌍 {data.get('name')}", description=None, color=discord.Color.green(), timestamp=datetime.utcnow())]
                # show some keys nicely
                for k in ("id", "region", "population", "economy", "leader"):
                    if k in data:
                        game[0].add_field(name=k.capitalize(), value=safe_truncate(fmt_num(data[k]), 40), inline=True)
                dev = [json_dev_embed(endpoint, data, {"single": True})]
                return game, dev

            # fallback dict
            game_embed = discord.Embed(title=endpoint, description=None, color=discord.Color.blurple(), timestamp=datetime.utcnow())
            small = {}
            for k, v in data.items():
                # pick small scalars
                if isinstance(v, (str, int, float, bool)):
                    small[k] = v
            add_small_fields(game_embed, small, limit=10)
            dev = [json_dev_embed(endpoint, data, {"keys": list(small.keys())})]
            return [game_embed], dev

        # fallback
        game = [discord.Embed(title=endpoint, description=str(data)[:1000], color=discord.Color.dark_gray())]
        dev = [json_dev_embed(endpoint, data, {})]
        return game, dev

    # helper to send paginated Game/Dev view
    async def send_game_dev_paginated(self, interaction: discord.Interaction, game_pages: List[discord.Embed], dev_pages: Optional[List[discord.Embed]] = None):
        # ensure we defer then respond (some actions heavy)
        await interaction.response.defer()
        view = GameDevPageView(game_pages, dev_pages or [])
        # send initial game embed
        message = await interaction.followup.send(embed=game_pages[0], view=view)
        # attach message to view if needed (for editing)
        # Interaction followup doesn't set message id to interaction.message, but we can let View edit by message id
        return message

    # dashboard helper
    async def refresh_dashboard_post(self, channel: discord.TextChannel):
        game_pages, dev_pages = await self.render_endpoint("ranking.getRanking", {"rankingType": "userDamages"})
        # post first page with view
        view = GameDevPageView(game_pages, dev_pages)
        if self.dash_message is None:
            self.dash_message = await channel.send(embed=game_pages[0], view=view)
        else:
            await self.dash_message.edit(embed=game_pages[0], view=view)

# ---------------- Instantiate ----------------
war_api = WarEraAPI()
war_bot = WarEraBot(bot)

# ---------------- Slash Commands (major endpoints) ----------------

@bot.tree.command(name="help", description="Show available WarEra commands")
async def help_cmd(interaction: discord.Interaction):
    await interaction.response.defer()
    e = discord.Embed(title="🎮 WarEra Game Dashboard — Commands", color=discord.Color.gold())
    commands_list = [
        ("/rankings <type>", "Leaderboards (userDamages, weeklyUserDamages, userWealth, userLevel)"),
        ("/countries", "Country list (game cards)"),
        ("/companies", "Companies (summary)"),
        ("/battles", "Active battles"),
        ("/workoffers", "Work offers"),
        ("/mu", "Military units"),
        ("/articles", "News articles"),
        ("/prices", "Item prices"),
        ("/transactions", "Recent transactions"),
        ("/users <id>", "User info (profile)"),
        ("/dashboard", "Post/refresh auto dashboard (channel env)"),
        ("/jsondebug", "Paste JSON and pretty-print (dev)"),
    ]
    for k, d in commands_list:
        e.add_field(name=k, value=d, inline=False)
    await interaction.followup.send(embed=e)

@bot.tree.command(name="rankings", description="View various rankings (leaderboards)")
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
@app_commands.describe(ranking_type="ranking type, e.g. userDamages")
async def rankings_cmd(interaction: discord.Interaction, ranking_type: app_commands.Choice[str]):
    await interaction.response.defer()
    game_pages, dev_pages = await war_bot.render_endpoint("ranking.getRanking", {"rankingType": ranking_type.value})
    await war_bot.send_game_dev_paginated(interaction, game_pages, dev_pages)

@bot.tree.command(name="countries", description="Show countries (game dashboard cards)")
async def countries_cmd(interaction: discord.Interaction):
    await interaction.response.defer()
    game_pages, dev_pages = await war_bot.render_endpoint("country.getAllCountries")
    await war_bot.send_game_dev_paginated(interaction, game_pages, dev_pages)

@bot.tree.command(name="companies", description="Show companies")
async def companies_cmd(interaction: discord.Interaction):
    await interaction.response.defer()
    game_pages, dev_pages = await war_bot.render_endpoint("company.getCompanies", {"page":1, "limit":50})
    await war_bot.send_game_dev_paginated(interaction, game_pages, dev_pages)

@bot.tree.command(name="battles", description="Active battles")
async def battles_cmd(interaction: discord.Interaction):
    await interaction.response.defer()
    game_pages, dev_pages = await war_bot.render_endpoint("battle.getBattles")
    await war_bot.send_game_dev_paginated(interaction, game_pages, dev_pages)

@bot.tree.command(name="workoffers", description="Work offers")
async def workoffers_cmd(interaction: discord.Interaction):
    await interaction.response.defer()
    game_pages, dev_pages = await war_bot.render_endpoint("workOffer.getWorkOffersPaginated", {"page":1, "limit":50})
    await war_bot.send_game_dev_paginated(interaction, game_pages, dev_pages)

@bot.tree.command(name="mu", description="Military Units (MU)")
async def mu_cmd(interaction: discord.Interaction):
    await interaction.response.defer()
    game_pages, dev_pages = await war_bot.render_endpoint("mu.getManyPaginated", {"page":1, "limit":50})
    await war_bot.send_game_dev_paginated(interaction, game_pages, dev_pages)

@bot.tree.command(name="articles", description="News / articles")
async def articles_cmd(interaction: discord.Interaction):
    await interaction.response.defer()
    game_pages, dev_pages = await war_bot.render_endpoint("article.getArticlesPaginated", {"page":1, "limit":50})
    await war_bot.send_game_dev_paginated(interaction, game_pages, dev_pages)

@bot.tree.command(name="prices", description="Item / economy prices")
async def prices_cmd(interaction: discord.Interaction):
    await interaction.response.defer()
    game_pages, dev_pages = await war_bot.render_endpoint("itemTrading.getPrices")
    await war_bot.send_game_dev_paginated(interaction, game_pages, dev_pages)

@bot.tree.command(name="transactions", description="Recent transactions")
async def transactions_cmd(interaction: discord.Interaction):
    await interaction.response.defer()
    game_pages, dev_pages = await war_bot.render_endpoint("transaction.getPaginatedTransactions", {"page":1, "limit":50})
    await war_bot.send_game_dev_paginated(interaction, game_pages, dev_pages)

@bot.tree.command(name="users", description="Get user info by ID")
@app_commands.describe(user_id="user id (hex string)")
async def users_cmd(interaction: discord.Interaction, user_id: str):
    await interaction.response.defer()
    game_pages, dev_pages = await war_bot.render_endpoint("user.getUserLite", {"userId": user_id})
    await war_bot.send_game_dev_paginated(interaction, game_pages, dev_pages)

@bot.tree.command(name="jsondebug", description="Paste JSON to pretty-print (developer view)")
async def jsondebug_cmd(interaction: discord.Interaction):
    # open modal to paste JSON
    modal = PasteJsonModal("manual")
    await interaction.response.send_modal(modal)

@bot.tree.command(name="dashboard", description="Post/refresh live dashboard (auto-posts to WARERA_DASH_CHANNEL if set)")
async def dashboard_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if DASH_CHANNEL_ID:
        channel = bot.get_channel(int(DASH_CHANNEL_ID))
        if channel:
            await war_bot.refresh_dashboard_post(channel)
            await interaction.followup.send("✅ Dashboard posted/updated.", ephemeral=True)
            return
        else:
            await interaction.followup.send("❌ DASH channel not found on this bot.", ephemeral=True)
            return
    await interaction.followup.send("❌ WARERA_DASH_CHANNEL not configured.", ephemeral=True)

# ---------------- Loop to auto-refresh dashboard (if configured) ----------------
@tasks.loop(seconds=DEFAULT_DASH_INTERVAL)
async def dash_loop():
    if DASH_CHANNEL_ID:
        ch = bot.get_channel(int(DASH_CHANNEL_ID))
        if ch:
            await war_bot.refresh_dashboard_post(ch)

# ---------------- Bot Events ----------------
@bot.event
async def on_ready():
    print(f"[WarEraBot] Logged in as {bot.user} (id: {bot.user.id})")
    try:
        await bot.tree.sync()
        print("[WarEraBot] Slash commands synced.")
    except Exception as e:
        print("[WarEraBot] Command sync failed:", e)
    if DASH_CHANNEL_ID:
        dash_loop.start()

# ---------------- Run ----------------
if __name__ == "__main__":
    if DISCORD_TOKEN == "YOUR_TOKEN_HERE":
        print("Set DISCORD_BOT_TOKEN environment variable and restart.")
    else:
        bot.run(DISCORD_TOKEN)

