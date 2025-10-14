import discord
from discord.ext import commands, tasks
import requests
import json
from datetime import datetime
from typing import Dict, List, Optional, Any
from dataclasses import dataclass
from enum import Enum

class AlertLevel(Enum):
    INFO = "🔵"
    WARNING = "🟡"
    CRITICAL = "🔴"

@dataclass
class Alert:
    timestamp: str
    level: AlertLevel
    category: str
    message: str
    data: Dict

class WarEraAPI:
    """Handles all WarEra API interactions"""
    
    def __init__(self, base_url: str = "https://api2.warera.io/trpc"):
        self.base_url = base_url
        self.cache = {}
    
    def call(self, endpoint: str, params: Optional[Dict] = None) -> Optional[Any]:
        """Universal API caller - uses tRPC ?input={} format"""
        try:
            url = f"{self.base_url}/{endpoint}"
            
            # tRPC uses GET with ?input={json} query parameter
            if params:
                import urllib.parse
                input_json = json.dumps(params)
                encoded_input = urllib.parse.quote(input_json)
                url = f"{url}?input={encoded_input}"
            else:
                # For endpoints that don't need params, still add ?input={}
                url = f"{url}?input={{}}"
            
            response = requests.get(url, timeout=10)
            
            if response.status_code != 200:
                return None
            
            data = response.json()
            
            # Unwrap tRPC response
            if isinstance(data, dict):
                if 'result' in data and 'data' in data['result']:
                    return data['result']['data']
                elif 'result' in data:
                    return data['result']
            
            return data
        except Exception as e:
            print(f"API error for {endpoint}: {e}")
            return None

class WarEraMonitor:
    def __init__(self):
        self.api = WarEraAPI()
        self.previous_data = {}
        self.alerts = []
        
        self.thresholds = {
            'price_change_percent': 20,
            'battle_threshold': 1,
            'ranking_change': 5,
        }
        
        # All endpoints from docs
        self.endpoints = {
            # No params needed
            'simple': [
                'itemTrading.getPrices',
                'battle.getBattles',
                'country.getAllCountries',
                'region.getRegionsObject',
                'gameConfig.getDates',
                'gameConfig.getGameConfig',
            ],
            # Requires ID
            'by_id': [
                'company.getById',
                'country.getCountryById',
                'region.getById',
                'battle.getById',
                'round.getById',
                'itemOffer.getById',
                'workOffer.getById',
                'user.getUserLite',
                'article.getArticleById',
                'mu.getById',
            ],
            # Requires pagination
            'paginated': [
                'company.getCompanies',
                'workOffer.getWorkOffersPaginated',
                'article.getArticlesPaginated',
                'mu.getManyPaginated',
                'transaction.getPaginatedTransactions',
            ],
            # Special params
            'special': {
                'ranking.getRanking': {'rankingType': 'weeklyCountryDamages'},
                'battleRanking.getRanking': {},
                'tradingOrder.getTopOrders': {'itemType': 'FOOD'},
                'government.getByCountryId': {'countryId': 1},
                'battle.getLiveBattleData': {'battleId': 1},
                'round.getLastHits': {'roundId': 1},
                'workOffer.getWorkOfferByCompanyId': {'companyId': 1},
                'user.getUsersByCountry': {'countryId': 1},
                'search.searchAnything': {'searchText': ''},
                'upgrade.getUpgradeByTypeAndEntity': {'type': '', 'entity': ''},
            }
        }
    
    def add_alert(self, level: AlertLevel, category: str, message: str, data: Dict = None):
        alert = Alert(
            timestamp=datetime.now().isoformat(),
            level=level,
            category=category,
            message=message,
            data=data or {}
        )
        self.alerts.append(alert)
        return alert
    
    def detect_changes(self, endpoint: str, old_data, new_data) -> List[Alert]:
        """Generic change detection"""
        alerts = []
        
        try:
            # List changes (new items)
            if isinstance(old_data, list) and isinstance(new_data, list):
                old_count = len(old_data)
                new_count = len(new_data)
                
                if new_count > old_count:
                    diff = new_count - old_count
                    level = AlertLevel.CRITICAL if 'battle' in endpoint else AlertLevel.WARNING
                    
                    alert = self.add_alert(
                        level,
                        self._categorize(endpoint),
                        f"🆕 {endpoint}: +{diff} new items",
                        {'endpoint': endpoint, 'new_items': diff}
                    )
                    alerts.append(alert)
            
            # Dict changes (prices, stats)
            elif isinstance(old_data, dict) and isinstance(new_data, dict):
                for key, new_val in new_data.items():
                    if key in old_data and isinstance(new_val, (int, float)) and isinstance(old_data[key], (int, float)):
                        old_val = old_data[key]
                        if old_val != 0:
                            change_pct = ((new_val - old_val) / abs(old_val)) * 100
                            
                            threshold = 20 if 'price' in key.lower() else 30
                            
                            if abs(change_pct) >= threshold:
                                level = AlertLevel.CRITICAL if abs(change_pct) > 50 else AlertLevel.WARNING
                                alert = self.add_alert(
                                    level,
                                    self._categorize(endpoint),
                                    f"📊 {key}: {change_pct:+.1f}%",
                                    {'key': key, 'old': old_val, 'new': new_val, 'change': change_pct}
                                )
                                alerts.append(alert)
        except Exception as e:
            pass
        
        return alerts
    
    def _categorize(self, endpoint: str) -> str:
        """Categorize endpoint for alerts"""
        e = endpoint.lower()
        if 'battle' in e or 'round' in e: return "⚔️ BATTLE"
        if 'price' in e or 'trading' in e or 'offer' in e: return "💰 ECONOMY"
        if 'company' in e or 'work' in e: return "🏢 COMPANIES"
        if 'ranking' in e: return "🏆 RANKINGS"
        if 'country' in e or 'government' in e: return "🌍 COUNTRIES"
        if 'user' in e: return "👤 USERS"
        if 'mu' in e: return "🎖️ MILITARY"
        if 'article' in e: return "📰 NEWS"
        return "📋 GENERAL"
    
    def scan_simple_endpoints(self) -> List[Alert]:
        """Scan all no-param endpoints"""
        alerts = []
        
        for endpoint in self.endpoints['simple']:
            data = self.api.call(endpoint)
            
            if data is not None:
                if endpoint in self.previous_data:
                    new_alerts = self.detect_changes(endpoint, self.previous_data[endpoint], data)
                    alerts.extend(new_alerts)
                
                self.previous_data[endpoint] = data
        
        return alerts
    
    def scan_paginated_endpoints(self) -> List[Alert]:
        """Scan paginated endpoints"""
        alerts = []
        
        for endpoint in self.endpoints['paginated']:
            data = self.api.call(endpoint, {'page': 1, 'limit': 50})
            
            if data is not None:
                if endpoint in self.previous_data:
                    new_alerts = self.detect_changes(endpoint, self.previous_data[endpoint], data)
                    alerts.extend(new_alerts)
                
                self.previous_data[endpoint] = data
        
        return alerts
    
    def scan_special_endpoints(self) -> List[Alert]:
        """Scan special param endpoints"""
        alerts = []
        
        for endpoint, params in self.endpoints['special'].items():
            if params:  # Only call if we have valid params
                data = self.api.call(endpoint, params)
                
                if data is not None:
                    if endpoint in self.previous_data:
                        new_alerts = self.detect_changes(endpoint, self.previous_data[endpoint], data)
                        alerts.extend(new_alerts)
                    
                    self.previous_data[endpoint] = data
        
        return alerts
    
    def run_full_scan(self) -> List[Alert]:
        """Run complete scan"""
        all_alerts = []
        all_alerts.extend(self.scan_simple_endpoints())
        all_alerts.extend(self.scan_paginated_endpoints())
        all_alerts.extend(self.scan_special_endpoints())
        return all_alerts
    
    def get_active_endpoints(self) -> List[str]:
        """Find which endpoints work"""
        active = []
        
        # Test simple endpoints
        for endpoint in self.endpoints['simple']:
            if self.api.call(endpoint) is not None:
                active.append(endpoint)
        
        # Test paginated
        for endpoint in self.endpoints['paginated']:
            if self.api.call(endpoint, {'page': 1}) is not None:
                active.append(endpoint)
        
        # Test special
        for endpoint, params in self.endpoints['special'].items():
            if params and self.api.call(endpoint, params) is not None:
                active.append(endpoint)
        
        return active


# Discord Bot
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

monitor = WarEraMonitor()
alert_channel_id = None


def create_embed(alert: Alert) -> discord.Embed:
    colors = {
        AlertLevel.INFO: discord.Color.blue(),
        AlertLevel.WARNING: discord.Color.gold(),
        AlertLevel.CRITICAL: discord.Color.red()
    }
    
    embed = discord.Embed(
        title=f"{alert.level.value} {alert.category}",
        description=alert.message,
        color=colors.get(alert.level, discord.Color.greyple()),
        timestamp=datetime.fromisoformat(alert.timestamp)
    )
    
    if alert.data:
        for key, value in list(alert.data.items())[:10]:
            if isinstance(value, (int, float, str)) and len(str(value)) < 1024:
                embed.add_field(name=key.replace('_', ' ').title(), value=str(value), inline=True)
    
    return embed


@bot.event
async def on_ready():
    print(f'{bot.user} connected!')
    print(f'In {len(bot.guilds)} guilds')
    
    print('🔍 Discovering active endpoints...')
    active = monitor.get_active_endpoints()
    print(f'✅ Found {len(active)} active endpoints')
    for ep in active[:10]:
        print(f'  • {ep}')
    
    if not monitor_task.is_running():
        monitor_task.start()
        print('✅ Monitoring started')


@tasks.loop(minutes=1)
async def monitor_task():
    if alert_channel_id is None:
        return
    
    channel = bot.get_channel(alert_channel_id)
    if channel is None:
        return
    
    try:
        alerts = monitor.run_full_scan()
        
        if alerts:
            by_cat = {}
            for alert in alerts:
                if alert.category not in by_cat:
                    by_cat[alert.category] = []
                by_cat[alert.category].append(alert)
            
            summary = f"**🚨 War Room Scan - {len(alerts)} alerts**\n"
            for cat, cat_alerts in by_cat.items():
                summary += f"• {cat}: {len(cat_alerts)}\n"
            
            await channel.send(summary)
            
            for alert in alerts[:15]:
                embed = create_embed(alert)
                await channel.send(embed=embed)
            
            if len(alerts) > 15:
                await channel.send(f"⚠️ {len(alerts) - 15} more alerts suppressed")
    except Exception as e:
        print(f"Monitor error: {e}")


@bot.command()
@commands.has_permissions(administrator=True)
async def setchannel(ctx):
    global alert_channel_id
    alert_channel_id = ctx.channel.id
    await ctx.send(f"✅ Alert channel set to {ctx.channel.mention}")


@bot.command()
async def scan(ctx):
    await ctx.send("🔍 Running scan...")
    
    alerts = monitor.run_full_scan()
    
    if not alerts:
        await ctx.send("✅ No alerts")
        return
    
    by_cat = {}
    for alert in alerts:
        if alert.category not in by_cat:
            by_cat[alert.category] = []
        by_cat[alert.category].append(alert)
    
    summary = f"**🚨 {len(alerts)} alerts**\n"
    for cat, cat_alerts in by_cat.items():
        summary += f"• {cat}: {len(cat_alerts)}\n"
    
    await ctx.send(summary)
    
    for alert in alerts[:10]:
        embed = create_embed(alert)
        await ctx.send(embed=embed)


@bot.command()
async def prices(ctx):
    await ctx.send("💰 Fetching prices...")
    
    data = monitor.api.call("itemTrading.getPrices")
    
    if not data:
        await ctx.send("❌ Could not fetch prices")
        return
    
    embed = discord.Embed(title="💰 Item Prices", color=discord.Color.gold())
    
    if isinstance(data, dict):
        price_text = ""
        for item, price in list(data.items())[:15]:
            if isinstance(price, (int, float)):
                price_text += f"**{item}**: {price:,.2f}\n"
        if price_text:
            embed.add_field(name="Prices", value=price_text, inline=False)
    
    await ctx.send(embed=embed)


@bot.command()
async def battles(ctx):
    await ctx.send("⚔️ Fetching battles...")
    
    data = monitor.api.call("battle.getBattles")
    
    if not data:
        await ctx.send("❌ Could not fetch battles")
        return
    
    embed = discord.Embed(title="⚔️ Active Battles", color=discord.Color.red())
    
    battles = data if isinstance(data, list) else [data]
    
    for i, battle in enumerate(battles[:5], 1):
        if isinstance(battle, dict):
            info = ""
            for key in ['id', 'attackerCountry', 'defenderCountry', 'region', 'status']:
                if key in battle:
                    info += f"**{key}**: {battle[key]}\n"
            if info:
                embed.add_field(name=f"Battle {i}", value=info, inline=False)
    
    await ctx.send(embed=embed)


@bot.command()
async def rankings(ctx, ranking_type: str = "weeklyCountryDamages"):
    await ctx.send(f"🏆 Fetching {ranking_type}...")
    
    data = monitor.api.call("ranking.getRanking", {"rankingType": ranking_type})
    
    if not data:
        await ctx.send(f"❌ Failed. Try: weeklyCountryDamages, etc")
        return
    
    embed = discord.Embed(title=f"🏆 {ranking_type}", color=discord.Color.gold())
    
    items = data
    if isinstance(data, dict):
        for key in ['items', 'rankings', 'data', 'results']:
            if key in data:
                items = data[key]
                break
    
    if isinstance(items, list):
        text = ""
        for i, item in enumerate(items[:10], 1):
            if isinstance(item, dict):
                name = item.get('name', item.get('country', item.get('username', str(i))))
                value = item.get('damage', item.get('score', item.get('value', '')))
                if value:
                    text += f"{i}. **{name}**: {value:,}\n"
                else:
                    text += f"{i}. **{name}**\n"
        if text:
            embed.add_field(name="Rankings", value=text, inline=False)
    
    await ctx.send(embed=embed)


@bot.command()
async def search(ctx, *, query: str):
    await ctx.send(f"🔍 Searching: {query}")
    
    data = monitor.api.call("search.searchAnything", {"searchText": query})
    
    if not data:
        await ctx.send("❌ No results")
        return
    
    embed = discord.Embed(title=f"🔍 Results: {query}", color=discord.Color.blue())
    
    if isinstance(data, dict):
        for category, items in data.items():
            if isinstance(items, list) and items:
                text = ""
                for item in items[:5]:
                    if isinstance(item, dict):
                        name = item.get('name', item.get('username', item.get('title', str(item.get('id')))))
                        text += f"• {name}\n"
                if text:
                    embed.add_field(name=category.title(), value=text, inline=False)
    
    await ctx.send(embed=embed)


@bot.command()
async def countries(ctx):
    await ctx.send("🌍 Fetching countries...")
    
    data = monitor.api.call("country.getAllCountries")
    
    if not data:
        await ctx.send("❌ Failed")
        return
    
    embed = discord.Embed(title="🌍 Countries", color=discord.Color.green())
    
    countries = data if isinstance(data, list) else []
    
    text = ""
    for country in countries[:20]:
        if isinstance(country, dict):
            name = country.get('name', country.get('id', ''))
            text += f"• {name}\n"
    
    if text:
        embed.add_field(name="Countries", value=text, inline=False)
    
    await ctx.send(embed=embed)


@bot.command()
async def endpoints(ctx):
    await ctx.send("🔍 Scanning endpoints...")
    
    active = monitor.get_active_endpoints()
    
    embed = discord.Embed(
        title=f"📡 Active Endpoints ({len(active)})",
        color=discord.Color.blue()
    )
    
    by_type = {}
    for ep in active:
        prefix = ep.split('.')[0]
        if prefix not in by_type:
            by_type[prefix] = []
        by_type[prefix].append(ep)
    
    for prefix, eps in sorted(by_type.items()):
        text = "\n".join([f"• `{e}`" for e in eps[:10]])
        if len(eps) > 10:
            text += f"\n... +{len(eps)-10} more"
        embed.add_field(name=f"📂 {prefix.upper()}", value=text, inline=False)
    
    await ctx.send(embed=embed)


@bot.command()
async def status(ctx):
    embed = discord.Embed(title="🎯 War Room Status", color=discord.Color.green())
    
    embed.add_field(name="Alert Channel", value=f"<#{alert_channel_id}>" if alert_channel_id else "Not set", inline=False)
    embed.add_field(name="Total Alerts", value=str(len(monitor.alerts)), inline=True)
    embed.add_field(name="Monitoring", value="✅ Yes" if monitor_task.is_running() else "❌ No", inline=True)
    
    by_level = {}
    for alert in monitor.alerts:
        by_level[alert.level] = by_level.get(alert.level, 0) + 1
    
    if by_level:
        text = "\n".join([f"{level.value} {level.name}: {count}" for level, count in by_level.items()])
        embed.add_field(name="Alerts by Level", value=text, inline=False)
    
    await ctx.send(embed=embed)


@bot.command()
@commands.has_permissions(administrator=True)
async def interval(ctx, minutes: int):
    if minutes < 1:
        await ctx.send("❌ Min 1 minute")
        return
    monitor_task.change_interval(minutes=minutes)
    await ctx.send(f"✅ Interval set to {minutes} min")


@bot.command()
@commands.has_permissions(administrator=True)
async def start(ctx):
    if monitor_task.is_running():
        await ctx.send("⚠️ Already running")
    else:
        monitor_task.start()
        await ctx.send("✅ Started")


@bot.command()
@commands.has_permissions(administrator=True)
async def stop(ctx):
    if monitor_task.is_running():
        monitor_task.cancel()
        await ctx.send("✅ Stopped")
    else:
        await ctx.send("⚠️ Not running")


@bot.command()
@commands.has_permissions(administrator=True)
async def clear(ctx):
    count = len(monitor.alerts)
    monitor.alerts.clear()
    await ctx.send(f"✅ Cleared {count} alerts")


@bot.command()
async def help(ctx):
    embed = discord.Embed(
        title="🎯 War Room Bot",
        description="Monitor everything in War Era",
        color=discord.Color.blue()
    )
    
    cmds = [
        ("!setchannel", "Set alert channel (Admin)"),
        ("!scan", "Manual scan now"),
        ("!status", "Bot statistics"),
        ("!endpoints", "List active endpoints"),
        ("!prices", "Item prices"),
        ("!battles", "Active battles"),
        ("!rankings [type]", "Rankings (weeklyCountryDamages, etc)"),
        ("!search <query>", "Search anything"),
        ("!countries", "List all countries"),
        ("!interval <min>", "Change scan interval (Admin)"),
        ("!start/stop", "Control monitoring (Admin)"),
        ("!clear", "Clear alerts (Admin)"),
    ]
    
    for cmd, desc in cmds:
        embed.add_field(name=cmd, value=desc, inline=False)
    
    await ctx.send(embed=embed)


if __name__ == "__main__":
    import os
    TOKEN = os.getenv('DISCORD_BOT_TOKEN', 'YOUR_BOT_TOKEN_HERE')
    
    if TOKEN == 'YOUR_BOT_TOKEN_HERE':
        print("⚠️ Set DISCORD_BOT_TOKEN environment variable!")
    else:
        bot.run(TOKEN)
