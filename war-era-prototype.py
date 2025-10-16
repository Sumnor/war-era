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

def link_for_entity(item: Any) -> Tuple[str, Optional[str], str]:
    """
    Returns (name_with_link, avatar_url, icon_emoji)
    Handles nested objects and bare IDs.
    """
    if not isinstance(item, dict):
        s = str(item)
        if is_likely_id(s):
            return (f"[`{safe_truncate(s,24)}`]({URLS['user']}{s})", None, ICON_USER)
        return (safe_truncate(s,40), None, ICON_USER)

    # nested user object (highest priority)
    if isinstance(item.get("user"), dict):
        u = item["user"]
        uid = u.get("_id") or u.get("id")
        name = u.get("name") or u.get("username") or uid
        if uid:
            return (f"[{safe_truncate(name,40)}]({URLS['user']}{uid})", extract_avatar(u), ICON_USER)
    
    # Check for other resolved user objects
    for key in ("resolved_user", "resolved_userId", "resolved_from", "resolved_to", "resolved_buyer", "resolved_seller", "resolved_attacker", "resolved_defender"):
        if isinstance(item.get(key), dict):
            u = item[key]
            uid = u.get("_id") or u.get("id") or u.get("user")
            name = u.get("name") or u.get("username") or uid
            if uid:
                return (f"[{safe_truncate(name,40)}]({URLS['user']}{uid})", extract_avatar(u), ICON_USER)

    # direct user id with name in item
    uid = item.get("user") or item.get("userId") or item.get("_id") or item.get("id")
    if is_likely_id(uid) and not item.get("countryId"):
        name = item.get("name") or item.get("username") or uid
        if "wealth" in item or "damage" in item:
             return (f"[{safe_truncate(name,40)}]({URLS['user']}{uid})", extract_avatar(item), ICON_USER)
    
    # country
    cc = item.get("countryId") or item.get("country")
    if isinstance(cc, dict):
        cc_id = cc.get("_id") or cc.get("id")
        name = cc.get("name") or cc_id
        if cc_id:
            return (f"[{safe_truncate(name,40)}]({URLS['country']}{cc_id})", extract_avatar(cc), ICON_COUNTRY)
    elif cc and is_likely_id(cc) and not item.get("companyId"):
        name = item.get("name") or item.get("countryName") or cc
        return (f"[{safe_truncate(name,40)}]({URLS['country']}{cc})", extract_avatar(item), ICON_COUNTRY)
    
    # company
    cid = item.get("companyId") or item.get("company")
    if isinstance(cid, dict):
        cid_id = cid.get("_id") or cid.get("id")
        name = cid.get("name") or cid.get("title") or cid_id
        if cid_id:
            return (f"[{safe_truncate(name,40)}]({URLS['company']}{cid_id})", extract_avatar(cid), ICON_COMPANY)
    elif cid and is_likely_id(cid):
        name = item.get("name") or item.get("title") or cid
        return (f"[{safe_truncate(name,40)}]({URLS['company']}{cid})", extract_avatar(item), ICON_COMPANY)
    
    # region
    rid = item.get("regionId") or item.get("region")
    if isinstance(rid, dict):
        rid_id = rid.get("_id") or rid.get("id")
        name = rid.get("name") or rid_id
        if rid_id:
            return (f"[{safe_truncate(name,40)}]({URLS['region']}{rid_id})", extract_avatar(rid), ICON_REGION)
    elif rid and is_likely_id(rid):
        name = item.get("name") or item.get("regionName") or rid
        return (f"[{safe_truncate(name,40)}]({URLS['region']}{rid})", extract_avatar(item), ICON_REGION)
    
    # mu (check for 'members' field)
    if item.get("members") is not None or item.get("muId"):
        mid_ = item.get("muId") or item.get("_id") or item.get("id")
        name = item.get("name") or mid_
        if mid_:
            return (f"[{safe_truncate(name,40)}]({URLS['mu']}{mid_})", extract_avatar(item), ICON_MU)
    
    # party
    pid = item.get("partyId") or item.get("party")
    if pid and is_likely_id(pid):
        name = item.get("name") or item.get("partyName") or pid
        return (f"[{safe_truncate(name,40)}]({URLS['party']}{pid})", extract_avatar(item), ICON_PARTY)
    
    # battle
    if item.get("battleId") or (item.get("_id") and ("attacker" in item or "defender" in item)):
        bid = item.get("battleId") or item.get("_id") or item.get("id")
        name = item.get("title") or bid
        if bid:
            return (f"[{safe_truncate(name,40)}]({URLS['battle']}{bid})", extract_avatar(item), ICON_BATTLE)
    
    # article (check for title field)
    if item.get("title") and item.get("_id") and is_likely_id(item.get("_id")) and "content" in item:
        aid = item.get("_id")
        name = item.get("title")
        return (f"[{safe_truncate(name,40)}]({URLS['article']}{aid})", extract_avatar(item), ICON_ARTICLE)
    
    # fallback: generic _id
    final_id = uid or item.get("_id") or item.get("id")
    if final_id and is_likely_id(final_id):
        name = item.get("name") or item.get("title") or item.get("username") or str(final_id)
        icon = get_entity_icon(item)
        
        url_map = {
            ICON_USER: 'user', ICON_COMPANY: 'company', ICON_COUNTRY: 'country',
            ICON_REGION: 'region', ICON_MU: 'mu', ICON_BATTLE: 'battle',
            ICON_ARTICLE: 'article', ICON_PARTY: 'party'
        }
        
        url_key = url_map.get(icon, 'user')
        link_url = URLS.get(url_key, URLS['user'])

        return (f"[{safe_truncate(name,40)}]({link_url}{final_id})", extract_avatar(item), icon)
    
    # final fallback (no link)
    name = item.get("name") or item.get("title") or item.get("username") or str(final_id)
    return (safe_truncate(name,40), extract_avatar(item), ICON_USER)

# ---------------- Make multi-item embed (10 per page) ----------------
def make_multi_item_embed(items_batch: List[Tuple[int, Dict]], total: int, page_num: int, total_pages: int, title: str, icon: str) -> discord.Embed:
    """Create an embed with up to 10 items per page"""
    emb = discord.Embed(
        title=f"{icon} {title}",
        color=discord.Color.dark_gold(),
        timestamp=now_utc()
    )
    
    desc_lines = []
    for idx, item in items_batch:
        name_link, avatar, item_icon = link_for_entity(item)
        
        val = (item.get("value") or item.get("damage") or item.get("score") or 
               item.get("wealth") or item.get("gdp") or item.get("treasury") or 
               item.get("population") or item.get("price") or 0)
        
        val_s = fmt_num(val)
        
        is_ranking_or_price = "Top" in title or "Damage" in title or "Wealth" in title or "Prices" in title
        
        line = f"**#{idx}** {item_icon} {name_link}"
        
        try:
            if is_ranking_or_price or float(val) != 0.0: 
                line += f" • `{val_s}`"
        except (ValueError, TypeError):
            line += f" • `{val_s}`"

        if item.get("tier"):
            line += f" • *{item.get('tier')}*"
        
        desc_lines.append(line)
    
    emb.description = "\n".join(desc_lines)
    emb.set_footer(text=f"Page {page_num}/{total_pages} • Total: {total} entries")
    
    return emb

def items_to_paginated_embeds(items: List[Dict], title: str, icon: str = ICON_DAMAGE) -> Tuple[List[discord.Embed], List[str]]:
    """Convert items list to paginated embeds (10 per page)"""
    pages = []
    dev_json = []
    total = len(items)
    
    for page_idx in range(0, total, 10):
        batch = []
        for idx in range(page_idx, min(page_idx + 10, total)):
            item = items[idx]
            if isinstance(item, dict):
                batch.append((idx + 1, item))
            else:
                batch.append((idx + 1, {"name": str(item), "_id": str(item)}))
        
        page_num = (page_idx // 10) + 1
        total_pages = (total + 9) // 10
        
        emb = make_multi_item_embed(batch, total, page_num, total_pages, title, icon)
        pages.append(emb)
        
        dev_json.append(json.dumps([items[i] for i in range(page_idx, min(page_idx + 10, total))], default=str))
    
    if not pages:
        pages.append(discord.Embed(title=title, description="No data available", timestamp=now_utc()))
        dev_json.append("[]")
    
    return pages, dev_json

# ---------------- Name Resolution for Generic Lists ----------------
async def resolve_user_names_in_list(items: List[Any]) -> List[Dict]:
    """
    Resolves 'user' IDs and other entity IDs to names in a list concurrently.
    """
    uids_to_fetch = set()
    uid_map = {}
    
    uid_fields = ("user", "userId", "from", "to", "buyer", "seller", "attacker", "defender", "currentPresident", 
                  "countryId", "companyId", "regionId", "muId", "partyId", "battleId")
    
    for item in items:
        if not isinstance(item, dict): continue
        for k in uid_fields:
            uid = item.get(k)
            if isinstance(uid, str) and is_likely_id(uid): 
                uids_to_fetch.add(uid)

    fetch_tasks = [war_api.call("user.getUserLite", {"userId": uid}) for uid in uids_to_fetch]
    
    if fetch_tasks:
        results = await asyncio.gather(*fetch_tasks, return_exceptions=True)
        uids_list = list(uids_to_fetch)
        for uid, r in zip(uids_list, results):
            if isinstance(r, dict):
                uid_map[uid] = r
            
    for item in items:
        if not isinstance(item, dict): continue
        
        for k in uid_fields:
            uid = item.get(k)
            if isinstance(uid, str) and uid in uid_map:
                item[k] = uid_map[uid] 
                
    return items

# ---------------- Name Enrichment for Entity Lists ----------------
async def enrich_entity_names(items: List[Any], endpoint: str) -> List[Dict]:
    """
    Enriches entity lists with full details (names, etc.) by making concurrent API calls.
    """
    if not items:
        return items
    
    is_company = "company" in endpoint.lower()
    is_country = "country" in endpoint.lower()
    is_region = "region" in endpoint.lower()
    is_mu = "mu" in endpoint.lower()
    is_battle = "battle" in endpoint.lower()
    
    ids_to_fetch = []
    for item in items:
        if not isinstance(item, dict):
            continue
        
        item_id = item.get("_id") or item.get("id")
        if item_id and is_likely_id(item_id):
            ids_to_fetch.append((item, item_id))
    
    if not ids_to_fetch:
        return items
    
    fetch_tasks = []
    for item, item_id in ids_to_fetch:
        if is_company:
            fetch_tasks.append(war_api.call("company.getById", {"companyId": item_id}))
        elif is_country:
            fetch_tasks.append(war_api.call("country.getCountryById", {"countryId": item_id}))
        elif is_region:
            fetch_tasks.append(war_api.call("region.getById", {"regionId": item_id}))
        elif is_mu:
            fetch_tasks.append(war_api.call("mu.getById", {"muId": item_id}))
        elif is_battle:
            fetch_tasks.append(war_api.call("battle.getById", {"battleId": item_id}))
        else:
            fetch_tasks.append(None)
    
    if fetch_tasks and any(t is not None for t in fetch_tasks):
        results = await asyncio.gather(*fetch_tasks, return_exceptions=True)
        
        for (item, item_id), result in zip(ids_to_fetch, results):
            if isinstance(result, dict):
                item["name"] = result.get("name") or result.get("title") or item.get("name")
                
                if "avatarUrl" in result:
                    item["avatarUrl"] = result["avatarUrl"]
                if "flag" in result:
                    item["flag"] = result["flag"]
                if "owner" in result:
                    item["owner"] = result["owner"]
    
    return items

# ---------------- Single Object Rendering ----------------
def process_single_object(data: Dict, title: str) -> Tuple[List[discord.Embed], List[str]]:
    """
    Process a single object into an embed, resolving key IDs to links and formatting values.
    """
    # Detect entity type
    is_battle = "attacker" in data or "defender" in data or "rounds" in data
    is_article = "content" in data and "author" in data and "category" in data
    is_company = "itemCode" in data or "production" in data
    is_country = "gdp" in data or "treasury" in data or "regions" in data
    is_region = "resourceMultiplier" in data or ("countryId" in data and "name" in data)
    is_mu = "members" in data or "commander" in data
    is_user = "wealth" in data or "damage" in data or ("experience" in data and "level" in data)
    
    # Use specialized renderers for specific entity types
    if is_battle:
        return render_battle_embed(data, title)
    elif is_article:
        return render_article_embed(data, title)
    elif is_company:
        return render_company_embed(data, title)
    elif is_country:
        return render_country_embed(data, title)
    elif is_region:
        return render_region_embed(data, title)
    elif is_mu:
        return render_mu_embed(data, title)
    elif is_user:
        return render_user_embed(data, title)
    
    # Fallback to generic renderer
    return render_generic_embed(data, title)

def render_battle_embed(data: Dict, title: str) -> Tuple[List[discord.Embed], List[str]]:
    """Render a battle entity with specialized formatting"""
    e = discord.Embed(title=f"⚔️ {title}", timestamp=now_utc(), color=discord.Color.red())
    
    bid = data.get("_id") or data.get("id")
    if bid:
        e.url = f"{URLS['battle']}{bid}"
    
    # Get attacker and defender
    attacker_id = data.get("attacker")
    defender_id = data.get("defender")
    
    # Create description
    desc_parts = []
    if attacker_id:
        desc_parts.append(f"🗡️ **Attacker:** [{attacker_id}]({URLS['country']}{attacker_id})")
    if defender_id:
        desc_parts.append(f"🛡️ **Defender:** [{defender_id}]({URLS['country']}{defender_id})")
    
    e.description = "\n".join(desc_parts) if desc_parts else "Battle details"
    
    # Add key fields
    if data.get("isActive"):
        e.add_field(name="Status", value="🟢 Active", inline=True)
    else:
        e.add_field(name="Status", value="⚫ Ended", inline=True)
    
    if data.get("roundsToWin"):
        e.add_field(name="Rounds to Win", value=str(data.get("roundsToWin")), inline=True)
    
    if data.get("isResistance"):
        e.add_field(name="Type", value="🔥 Resistance War", inline=True)
    
    if data.get("createdAt"):
        e.add_field(name="Started", value=format_date_iso(data.get("createdAt")), inline=False)
    
    return [e], [json.dumps(data, default=str)]

def render_article_embed(data: Dict, title: str) -> Tuple[List[discord.Embed], List[str]]:
    """Render an article entity with specialized formatting"""
    article_title = data.get("title") or title
    e = discord.Embed(title=f"📰 {article_title}", timestamp=now_utc(), color=discord.Color.blue())
    
    aid = data.get("_id") or data.get("id")
    if aid:
        e.url = f"{URLS['article']}{aid}"
        e.description = f"[🔗 Read full article on WarEra]({URLS['article']}{aid})"
    
    # Author
    author_id = data.get("author")
    if author_id and is_likely_id(author_id):
        e.add_field(name="✍️ Author", value=f"[Profile]({URLS['user']}{author_id})", inline=True)
    
    # Category
    if data.get("category"):
        e.add_field(name="📂 Category", value=data.get("category").title(), inline=True)
    
    # Language
    if data.get("language"):
        e.add_field(name="🌐 Language", value=data.get("language").upper(), inline=True)
    
    # Published date
    if data.get("publishedAt"):
        e.add_field(name="📅 Published", value=format_date_iso(data.get("publishedAt")), inline=False)
    
    return [e], [json.dumps(data, default=str)]

def render_company_embed(data: Dict, title: str) -> Tuple[List[discord.Embed], List[str]]:
    """Render a company entity with specialized formatting"""
    company_name = data.get("name") or title
    e = discord.Embed(title=f"🏢 {company_name}", timestamp=now_utc(), color=discord.Color.gold())
    
    cid = data.get("_id") or data.get("id")
    if cid:
        e.url = f"{URLS['company']}{cid}"
    
    # Item produced
    item_code = data.get("itemCode")
    if item_code:
        e.add_field(name="📦 Produces", value=item_code.title(), inline=True)
    
    # Production rate
    if data.get("production"):
        e.add_field(name="⚙️ Production", value=fmt_num(data.get("production")), inline=True)
    
    # Value
    if data.get("estimatedValue"):
        e.add_field(name="💰 Estimated Value", value=f"${fmt_num(data.get('estimatedValue'))}", inline=True)
    
    # Region
    region_id = data.get("region")
    if region_id and is_likely_id(region_id):
        e.add_field(name="📍 Location", value=f"[Region]({URLS['region']}{region_id})", inline=True)
    
    # Worker count
    workers = data.get("workers")
    if isinstance(workers, list):
        e.add_field(name="👥 Workers", value=str(len(workers)), inline=True)
    
    # Full status
    if data.get("isFull") is not None:
        status = "✅ Full" if data.get("isFull") else "🟢 Hiring"
        e.add_field(name="Status", value=status, inline=True)
    
    return [e], [json.dumps(data, default=str)]

def render_country_embed(data: Dict, title: str) -> Tuple[List[discord.Embed], List[str]]:
    """Render a country entity with specialized formatting"""
    country_name = data.get("name") or title
    e = discord.Embed(title=f"🌍 {country_name}", timestamp=now_utc(), color=discord.Color.green())
    
    cid = data.get("_id") or data.get("id")
    if cid:
        e.url = f"{URLS['country']}{cid}"
    
    # Flag
    if data.get("flag"):
        e.set_thumbnail(url=data.get("flag"))
    
    # GDP
    if data.get("gdp"):
        e.add_field(name="💰 GDP", value=f"${fmt_num(data.get('gdp'))}", inline=True)
    
    # Treasury
    if data.get("treasury"):
        e.add_field(name="🏦 Treasury", value=f"${fmt_num(data.get('treasury'))}", inline=True)
    
    # Population
    if data.get("population"):
        e.add_field(name="👥 Population", value=fmt_num(data.get("population")), inline=True)
    
    # President
    president_id = data.get("currentPresident")
    if president_id and is_likely_id(president_id):
        e.add_field(name="👑 President", value=f"[Profile]({URLS['user']}{president_id})", inline=True)
    
    # Regions
    regions = data.get("regions")
    if isinstance(regions, list):
        e.add_field(name="🏔️ Regions", value=str(len(regions)), inline=True)
    
    return [e], [json.dumps(data, default=str)]

def render_region_embed(data: Dict, title: str) -> Tuple[List[discord.Embed], List[str]]:
    """Render a region entity with specialized formatting"""
    region_name = data.get("name") or title
    e = discord.Embed(title=f"🏔️ {region_name}", timestamp=now_utc(), color=discord.Color.teal())
    
    rid = data.get("_id") or data.get("id")
    if rid:
        e.url = f"{URLS['region']}{rid}"
    
    # Country
    country_id = data.get("countryId")
    if country_id and is_likely_id(country_id):
        e.add_field(name="🌍 Country", value=f"[View]({URLS['country']}{country_id})", inline=True)
    
    # Resource
    if data.get("resource"):
        e.add_field(name="💎 Resource", value=data.get("resource").title(), inline=True)
    
    # Resource multiplier
    if data.get("resourceMultiplier"):
        e.add_field(name="📈 Multiplier", value=f"×{fmt_num(data.get('resourceMultiplier'))}", inline=True)
    
    return [e], [json.dumps(data, default=str)]

def render_mu_embed(data: Dict, title: str) -> Tuple[List[discord.Embed], List[str]]:
    """Render a military unit entity with specialized formatting"""
    mu_name = data.get("name") or title
    e = discord.Embed(title=f"🎖️ {mu_name}", timestamp=now_utc(), color=discord.Color.purple())
    
    mid = data.get("_id") or data.get("id")
    if mid:
        e.url = f"{URLS['mu']}{mid}"
    
    # Members
    members = data.get("members")
    if isinstance(members, list):
        e.add_field(name="👥 Members", value=fmt_num(len(members)), inline=True)
    
    # Commander
    commander_id = data.get("commander")
    if commander_id and is_likely_id(commander_id):
        e.add_field(name="👑 Commander", value=f"[Profile]({URLS['user']}{commander_id})", inline=True)
    
    # Country
    country_id = data.get("countryId")
    if country_id and is_likely_id(country_id):
        e.add_field(name="🌍 Country", value=f"[View]({URLS['country']}{country_id})", inline=True)
    
    return [e], [json.dumps(data, default=str)]

def render_user_embed(data: Dict, title: str) -> Tuple[List[discord.Embed], List[str]]:
    """Render a user entity with specialized formatting"""
    user_name = data.get("name") or data.get("username") or title
    e = discord.Embed(title=f"👤 {user_name}", timestamp=now_utc(), color=discord.Color.blue())
    
    uid = data.get("_id") or data.get("id")
    if uid:
        e.url = f"{URLS['user']}{uid}"
    
    # Avatar
    avatar = extract_avatar(data)
    if avatar:
        e.set_thumbnail(url=avatar)
    
    # Level
    if data.get("level"):
        e.add_field(name="⭐ Level", value=fmt_num(data.get("level")), inline=True)
    
    # Wealth
    if data.get("wealth"):
        e.add_field(name="💰 Wealth", value=f"${fmt_num(data.get('wealth'))}", inline=True)
    
    # Damage
    if data.get("damage"):
        e.add_field(name="⚔️ Damage", value=fmt_num(data.get("damage")), inline=True)
    
    # Country
    country_id = data.get("countryId")
    if country_id and is_likely_id(country_id):
        e.add_field(name="🌍 Country", value=f"[View]({URLS['country']}{country_id})", inline=True)
    
    return [e], [json.dumps(data, default=str)]

def render_generic_embed(data: Dict, title: str) -> Tuple[List[discord.Embed], List[str]]:
    """Fallback generic renderer"""
    e = discord.Embed(title=title, timestamp=now_utc(), color=discord.Color.blue())
    
    name_link, avatar, icon = link_for_entity(data)
    e.description = f"{icon} {name_link}"
    
    if avatar:
        try:
            e.set_thumbnail(url=avatar)
        except:
            pass
    
    processed_fields = {}
    
    for k, v in data.items():
        if k in ("_id", "id", "name", "title", "user", "country", "__v"):
            continue

        if isinstance(v, dict):
            nested_link, _, nested_icon = link_for_entity(v)
            processed_fields[k] = f"{nested_icon} {nested_link}"
        
        elif isinstance(v, list):
            processed_fields[k] = f"[{len(v)} item(s)]"
        
        elif isinstance(v, (str, int, float, bool)):
            v_str = str(v)
            
            is_id = False
            entity_id = None
            link_prefix = None
            
            if is_likely_id(v):
                is_id = True
                entity_id = v
                
                if k in ("user", "userId", "currentPresident", "author", "commander"):
                    link_prefix = URLS.get("user")
                elif k in ("company", "companyId"):
                    link_prefix = URLS.get("company")
                elif k in ("country", "countryId", "attackerCountry", "defenderCountry"):
                    link_prefix = URLS.get("country")
                elif k in ("region", "regionId"):
                    link_prefix = URLS.get("region")
                elif k in ("mu", "muId"):
                    link_prefix = URLS.get("mu")
                elif k in ("battleId", "currentRound", "battle"):
                    link_prefix = URLS.get("battle")
                elif k in ("articleId",):
                    link_prefix = URLS.get("article")
            
            if is_id and link_prefix:
                v_str = f"[{safe_truncate(entity_id, 24)}]({link_prefix}{entity_id})"
            
            elif isinstance(v, (int, float)):
                v_str = fmt_num(v)
            
            elif isinstance(v, bool):
                v_str = "✅ True" if v else "❌ False"
            
            elif "T" in v_str and ("Z" in v_str or "+" in v_str):
                v_str = format_date_iso(v_str)

            processed_fields[k] = v_str
    
    field_keys = list(processed_fields.keys())
    for k in field_keys:
        field_name = safe_truncate(str(k), 25) 
        e.add_field(name=field_name, value=processed_fields[k], inline=True)
    
    return [e], [json.dumps(data, default=str)]

# ---------------- Render endpoint to pages ----------------
async def render_endpoint_to_pages(endpoint:str, params:Optional[Dict]=None, title_override:str=None, enrich_names:bool=True) -> Tuple[List[discord.Embed], List[str]]:
    data = await war_api.call(endpoint, params)
    display_title = title_override or endpoint
    
    if data is None:
        return [discord.Embed(title=display_title, description="❌ Failed to fetch data", color=discord.Color.red(), timestamp=now_utc())], [json.dumps({"error":"fetch failed"})]
    
    if isinstance(data, dict):
        for list_key in ("items","results","data","countries","regions","battles","companies","users"):
            if list_key in data and isinstance(data[list_key], list):
                items = data[list_key]
                
                if enrich_names:
                    items = await enrich_entity_names(items, endpoint)
                
                items = await resolve_user_names_in_list(items)
                icon = get_entity_icon(items[0] if items else {})
                return items_to_paginated_embeds(items, display_title, icon)
        
        return process_single_object(data, display_title)
    
    if isinstance(data, list):
        items = data
        
        if enrich_names:
            items = await enrich_entity_names(items, endpoint)
        
        items = await resolve_user_names_in_list(items)
        icon = get_entity_icon(items[0] if items else {})
        return items_to_paginated_embeds(items, display_title, icon)
    
    return [discord.Embed(title=display_title, description=safe_truncate(str(data),1000), timestamp=now_utc())], [json.dumps(data, default=str)]

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
    e = discord.Embed(
        title="🎮 WarEra Bot — Command Guide", 
        color=discord.Color.gold(), 
        timestamp=now_utc()
    )
    e.description = "Comprehensive WarEra game data at your fingertips!\n*Use slash commands: type `/` to see all*"
    
    e.add_field(
        name=f"{ICON_DAMAGE} Rankings & Leaderboards", 
        value=(
            f"`/rankings` → All ranking types\n"
            f"`/topdamage` → {ICON_DAMAGE} Top damage dealers\n"
            f"`/topwealth` → {ICON_WEALTH} Wealthiest players\n"
            f"`/topland` → {ICON_GROUND} Top land producers\n"
            f"`/toplevel` → {ICON_LEVEL} Highest level players\n"
            f"`/topreferrals` → {ICON_REFERRAL} Top referrers"
        ), 
        inline=False
    )
    
    e.add_field(
        name=f"{ICON_COUNTRY} Countries & Regions", 
        value=(
            f"`/countries` → List all countries\n"
            f"`/country <id>` → Country details\n"
            f"`/topcountries [metric]` → Top by GDP, avg wealth, or total wealth\n"
            f"`/regions [country_id]` → List regions (optionally filter by country)\n"
            f"`/region <id>` → Region details"
        ), 
        inline=False
    )
    
    e.add_field(
        name=f"{ICON_BATTLE} Military & Combat", 
        value=(
            f"`/battles` → Active battles\n"
            f"`/battle <id>` → Battle details\n"
            f"`/topmu` → {ICON_MU} Top military units\n"
            f"`/mu` → List military units\n"
            f"`/mu_details <id>` → MU details"
        ), 
        inline=False
    )
    
    e.add_field(
        name=f"{ICON_WEALTH} Economy & Business", 
        value=(
            f"`/companies` → List companies\n"
            f"`/company <id>` → Company details\n"
            f"`/prices` → {ICON_WEALTH} Item market prices\n"
            f"`/transactions` → Recent transactions\n"
            f"`/workoffers` → Available job offers"
        ), 
        inline=False
    )
    
    e.add_field(
        name=f"🔧 Other Commands", 
        value=(
            f"`/user <id>` → {ICON_USER} User profile\n"
            f"`/articles` → {ICON_ARTICLE} Latest articles\n"
            f"`/search <query>` → 🔍 Search anything\n"
            f"`/dashboard` → 📊 Live dashboard\n"
            f"`/alerts` → 🔔 Alert subscriptions\n"
            f"`/jsondebug` → 🧪 Format JSON"
        ), 
        inline=False
    )
    
    e.set_footer(text="WarEra Bot | Powered by api2.warera.io")
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
async def aggregate_users_from_ranking(ranking_type: str, limit: int = 500) -> List[Tuple[str, float, Dict]]:
    """Returns list of (user_id, value, user_data). Limit controls how many to return."""
    sums: Dict[str, float] = {}
    user_data: Dict[str, Dict] = {}
    
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
                if str(uid) not in user_data:
                    user_data[str(uid)] = it
            except: 
                pass
    
    sorted_users = sorted(sums.items(), key=lambda kv: kv[1], reverse=True)
    
    fetch_limit = min(limit, len(sorted_users))
    uids_to_fetch = [uid for uid, _ in sorted_users[:fetch_limit]]
    
    fetch_tasks = [war_api.call("user.getUserLite", {"userId": uid}) for uid in uids_to_fetch]
        
    results = await asyncio.gather(*fetch_tasks, return_exceptions=True)
    
    for uid, r in zip(uids_to_fetch, results):
        if isinstance(r, dict):
            user_data[uid]["name"] = r.get("name") or r.get("username")
            user_data[uid]["avatarUrl"] = r.get("avatarUrl") or r.get("animatedAvatarUrl")

    return [(uid, val, user_data.get(uid, {})) for uid, val in sorted_users[:limit]]

def ranking_list_to_pages(title: str, ranked: List[Tuple[str, float, Dict]]) -> Tuple[List[discord.Embed], List[str]]:
    items = []
    for uid, val, udata in ranked:
        item = udata.copy()
        item["user"] = udata.get("user") or {"_id": uid, "name": udata.get("name"), "avatarUrl": udata.get("avatarUrl")}
        item["value"] = val
        items.append(item)
        
    icon = get_entity_icon(items[0] if items else {})
    return items_to_paginated_embeds(items, title, icon)

@tree.command(name="topdamage", description="⚔️ Top damage dealers (aggregated)")
async def topdamage_cmd(interaction: discord.Interaction):
    await safe_defer(interaction)
    ranked = await aggregate_users_from_ranking("userDamages", limit=500)
    pages, dev = ranking_list_to_pages(f"{ICON_DAMAGE} Top Damage Dealers", ranked)
    view = LeaderboardView(pages, dev)
    await interaction.followup.send(embed=pages[0], view=view)

@tree.command(name="topwealth", description="💰 Wealthiest players (aggregated)")
async def topwealth_cmd(interaction: discord.Interaction):
    await safe_defer(interaction)
    ranked = await aggregate_users_from_ranking("userWealth", limit=500)
    pages, dev = ranking_list_to_pages(f"{ICON_WEALTH} Top Wealth", ranked)
    view = LeaderboardView(pages, dev)
    await interaction.followup.send(embed=pages[0], view=view)

@tree.command(name="topland", description="🌾 Top land producers (aggregated)")
async def topland_cmd(interaction: discord.Interaction):
    await safe_defer(interaction)
    ranked = await aggregate_users_from_ranking("userTerrain", limit=500)
    pages, dev = ranking_list_to_pages(f"{ICON_GROUND} Top Land Producers", ranked)
    view = LeaderboardView(pages, dev)
    await interaction.followup.send(embed=pages[0], view=view)

@tree.command(name="toplevel", description="⭐ Highest level players")
async def toplevel_cmd(interaction: discord.Interaction):
    await safe_defer(interaction)
    ranked = await aggregate_users_from_ranking("userLevel", limit=500)
    pages, dev = ranking_list_to_pages(f"{ICON_LEVEL} Highest Levels", ranked)
    view = LeaderboardView(pages, dev)
    await interaction.followup.send(embed=pages[0], view=view)

@tree.command(name="topreferrals", description="🔗 Top referrers")
async def topreferrals_cmd(interaction: discord.Interaction):
    await safe_defer(interaction)
    ranked = await aggregate_users_from_ranking("userReferrals", limit=500)
    pages, dev = ranking_list_to_pages(f"{ICON_REFERRAL} Top Referrers", ranked)
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
@app_commands.describe(metric="Metric to rank by")
@app_commands.choices(metric=[
    app_commands.Choice(name="💰 GDP + Treasury", value="combined"),
    app_commands.Choice(name="💎 Average Citizen Wealth", value="avg_wealth"),
    app_commands.Choice(name="📊 Total Citizen Wealth", value="total_wealth"),
])
async def topcountries_cmd(interaction: discord.Interaction, metric: app_commands.Choice[str] = None):
    await safe_defer(interaction)
    data = await war_api.call("country.getAllCountries")
    
    countries = []
    if isinstance(data, dict) and "countries" in data:
        countries = data["countries"]
    elif isinstance(data, list):
        countries = data
    
    if not metric or metric.value == "combined":
        # Original: GDP + Treasury
        scored = []
        for c in countries:
            if isinstance(c, dict):
                gdp = c.get("gdp") or 0
                treasury = c.get("treasury") or 0
                try:
                    score = float(gdp) + float(treasury)
                    new_c = c.copy()
                    new_c["value"] = score
                    scored.append(new_c)
                except:
                    pass
        
        scored.sort(key=lambda x: x["value"], reverse=True)
        pages, dev = items_to_paginated_embeds(scored[:100], "🏆 Top Countries (GDP + Treasury)", ICON_COUNTRY)
    
    elif metric.value == "avg_wealth":
        # Average citizen wealth per country
        scored = []
        for c in countries:
            if not isinstance(c, dict):
                continue
            
            country_id = c.get("_id") or c.get("id")
            if not country_id:
                continue
            
            # Fetch citizens of this country from rankings
            wealth_data = await war_api.call("ranking.getRanking", {"rankingType": "userWealth"})
            
            if isinstance(wealth_data, dict) and isinstance(wealth_data.get("items"), list):
                citizens = []
                for user in wealth_data["items"]:
                    if isinstance(user, dict) and user.get("countryId") == country_id:
                        wealth = user.get("value") or user.get("wealth") or 0
                        try:
                            citizens.append(float(wealth))
                        except:
                            pass
                
                if citizens:
                    avg_wealth = sum(citizens) / len(citizens)
                    new_c = c.copy()
                    new_c["value"] = avg_wealth
                    new_c["citizen_count"] = len(citizens)
                    scored.append(new_c)
        
        scored.sort(key=lambda x: x["value"], reverse=True)
        
        # Custom embed for average wealth
        items_for_display = []
        for c in scored[:50]:
            c_display = c.copy()
            c_display["tier"] = f"Avg: ${fmt_num(c['value'])} ({c['citizen_count']} citizens)"
            items_for_display.append(c_display)
        
        pages, dev = items_to_paginated_embeds(items_for_display, "💎 Top Countries by Avg Citizen Wealth", ICON_COUNTRY)
    
    elif metric.value == "total_wealth":
        # Total citizen wealth per country
        scored = []
        for c in countries:
            if not isinstance(c, dict):
                continue
            
            country_id = c.get("_id") or c.get("id")
            if not country_id:
                continue
            
            # Fetch citizens of this country from rankings
            wealth_data = await war_api.call("ranking.getRanking", {"rankingType": "userWealth"})
            
            if isinstance(wealth_data, dict) and isinstance(wealth_data.get("items"), list):
                total_wealth = 0
                citizen_count = 0
                for user in wealth_data["items"]:
                    if isinstance(user, dict) and user.get("countryId") == country_id:
                        wealth = user.get("value") or user.get("wealth") or 0
                        try:
                            total_wealth += float(wealth)
                            citizen_count += 1
                        except:
                            pass
                
                if total_wealth > 0:
                    new_c = c.copy()
                    new_c["value"] = total_wealth
                    new_c["citizen_count"] = citizen_count
                    scored.append(new_c)
        
        scored.sort(key=lambda x: x["value"], reverse=True)
        
        # Custom embed for total wealth
        items_for_display = []
        for c in scored[:50]:
            c_display = c.copy()
            c_display["tier"] = f"Total: ${fmt_num(c['value'])} ({c['citizen_count']} citizens)"
            items_for_display.append(c_display)
        
        pages, dev = items_to_paginated_embeds(items_for_display, "📊 Top Countries by Total Citizen Wealth", ICON_COUNTRY)
    
    view = LeaderboardView(pages, dev)
    await interaction.followup.send(embed=pages[0], view=view)

# ==================== REGION COMMANDS ====================

@tree.command(name="regions", description="🏔️ List all regions")
@app_commands.describe(country_id="Optional: Filter by country ID")
async def regions_cmd(interaction: discord.Interaction, country_id: str = None):
    if country_id:
        await safe_defer(interaction)
        data = await war_api.call("region.getRegionsObject")
        
        filtered_regions = []
        if isinstance(data, dict):
            for region_id, region_data in data.items():
                if isinstance(region_data, dict) and region_data.get("countryId") == country_id:
                    region_data["_id"] = region_id
                    filtered_regions.append(region_data)
        
        if not filtered_regions:
            await interaction.followup.send(f"❌ No regions found for country ID: {country_id}", ephemeral=True)
            return
        
        filtered_regions = await enrich_entity_names(filtered_regions, "region")
        
        pages, dev = items_to_paginated_embeds(filtered_regions, f"🏔️ Regions in Country: {country_id}", ICON_REGION)
        view = LeaderboardView(pages, dev)
        await interaction.followup.send(embed=pages[0], view=view)
    else:
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
        
        new_mu = mu.copy()
        new_mu["value"] = score
        scored.append(new_mu)
    
    scored.sort(key=lambda x: x["value"], reverse=True)
    
    pages, dev = items_to_paginated_embeds(scored, "Top Military Units", ICON_MU)
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
    
    e = discord.Embed(title="💰 Item Market Prices", color=discord.Color.gold(), timestamp=now_utc())
    e.description = "Current trading prices for all items"
    
    item_icons = {
        "cocain": "💊", "cocaine": "💊",
        "case1": "📦", "case": "📦",
        "cookedFish": "🍣", "fish": "🐟",
        "steak": "🥩", "livestock": "🐄",
        "bread": "🍞", "grain": "🌾",
        "steel": "⚙️", "iron": "⛏️",
        "concrete": "🧱", "limestone": "🪨",
        "oil": "🛢️", "petroleum": "⛽",
        "ammo": "💣", "heavyAmmo": "💥", "lightAmmo": "🔫",
        "lead": "🔩", "coca": "🌿",
        "diamonds": "💎", "gold": "🥇", "silver": "🥈", "copper": "🥉"
    }

    items_list = []
    if isinstance(data, dict):
        for k, v in data.items():
            items_list.append({
                "name": k.replace("_", " ").title(),
                "price": v,
                "value": v,
                "_id": k
            })
        
        items_list.sort(key=lambda x: float(x["price"]) if isinstance(x["price"], (int, float)) else 0, reverse=True)
        
        for item in items_list[:25]:
            icon = item_icons.get(item["_id"], "📊")
            e.add_field(
                name=f"{icon} {safe_truncate(item['name'], 20)}", 
                value=f"**${fmt_num(item['price'])}**", 
                inline=True
            )
        
        pages, dev_json = items_to_paginated_embeds(items_list, "💰 Item Market Prices", ICON_WEALTH)
        
    else:
        e.description = safe_truncate(str(data), 1000)
        pages = [e]
        dev_json = [json.dumps(data, default=str)]
    
    view = LeaderboardView(pages, dev_json)
    await interaction.followup.send(embed=pages[0], view=view)

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
    await send_endpoint_pages(interaction, "article.getArticlesPaginated", {"page":1,"limit":50, "type": "last"}, "📰 Latest Articles")

@tree.command(name="article", description="📰 Get article by ID")
@app_commands.describe(article_id="Article ID")
async def article_cmd(interaction: discord.Interaction, article_id: str):
    await send_endpoint_pages(interaction, "article.getArticleById", {"articleId": article_id}, f"📰 Article: {article_id}")

# ==================== SEARCH COMMAND ====================

@tree.command(name="search", description="🔍 Search anything")
@app_commands.describe(query="Search query")
async def search_cmd(interaction: discord.Interaction, query: str):
    await send_endpoint_pages(interaction, "search.searchAnything", {"searchText": query}, f"🔍 Search: {query}")

# ==================== JSON DEBUG ----------------
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

# ==================== MONITOR & ALERTS ----------------

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

# ==================== DASHBOARD ----------------

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

# ==================== BOT LIFECYCLE ----------------

@bot.event
async def on_ready():
    print(f"✅ WarEra Bot logged in as {bot.user} (ID: {bot.user.id})")
    print(f"📊 Serving {len(bot.guilds)} guild(s)")
    
    try:
        synced = await tree.sync()
        print(f"✅ Synced {len(synced)} slash command(s)")
    except Exception as e:
        print(f"❌ Failed to sync commands: {e}")
    
    monitor.prev = state.get("monitor_prev", {})
    monitor.alerts = state.get("monitor_alerts", [])
    
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

# ==================== MAIN ----------------

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
