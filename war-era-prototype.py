import discord
from discord.ext import commands, tasks
import requests
import json
from datetime import datetime
from typing import Dict, List, Optional
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

class WarEraMonitor:
    def __init__(self, base_url: str = "https://api2.warera.io/trpc"):
        self.base_url = base_url
        self.previous_data = {}
        self.alerts = []
        
        self.thresholds = {
            'price_change_percent': 20,
            'build_change_threshold': 15,
            'stat_change_threshold': 10,
            'large_trade_threshold': 1000000,
        }
        
        # All possible endpoints to monitor
        self.endpoints = [
            # Trading & Economy
            "itemTrading.getPrices",
            "itemTrading.getRecent",
            "itemTrading.getOrders",
            "itemTrading.getHistory",
            "market.getPrices",
            "market.getOrders",
            "market.getRecent",
            "market.getTrades",
            "bank.getLoans",
            "bank.getInterestRates",
            "trade.getActive",
            "trade.getRecent",
            "economy.getStats",
            "economy.getTopProducers",
            "economy.getTopTraders",
            "economy.getGDP",
            
            # Nations
            "nation.getStats",
            "nation.getBuilds",
            "nation.getRecent",
            "nation.getAttacks",
            "nation.getDefenses",
            "nation.getResources",
            "nation.getActivity",
            "nation.getChanges",
            "nation.getMilitary",
            "nation.getEconomy",
            "nation.getProduction",
            "nation.getRevenue",
            "nation.getInfrastructure",
            "nation.getLand",
            "nation.getTechnology",
            
            # Rankings/Leaderboards
            "rankings.getTopNations",
            "rankings.getTopAlliances",
            "rankings.getTopMilitary",
            "rankings.getTopEconomy",
            "rankings.getTopProduction",
            "rankings.getTopRevenue",
            "rankings.getTopGDP",
            "rankings.getTopScore",
            "rankings.getTopLand",
            "rankings.getTopInfrastructure",
            "leaderboard.getNations",
            "leaderboard.getAlliances",
            "leaderboard.getMilitary",
            "leaderboard.getEconomy",
            "stats.getTopProducers",
            "stats.getTopNations",
            "stats.getGlobal",
            
            # War
            "war.getActive",
            "war.getRecent",
            "war.getAttacks",
            "war.getDefenses",
            "war.getHistory",
            "war.getStats",
            "war.getTopAttackers",
            "war.getTopDefenders",
            
            # Alliance
            "alliance.getWars",
            "alliance.getMembers",
            "alliance.getStats",
            "alliance.getRecent",
            "alliance.getTreaties",
            "alliance.getActivity",
            "alliance.getRankings",
            "alliance.getTopAlliances",
            
            # User/Player
            "user.getStats",
            "user.getActivity",
            "user.getRecent",
            
            # Global/World
            "world.getStats",
            "world.getActivity",
            "world.getProduction",
            "world.getResources",
            "global.getStats",
            "global.getProduction",
        ]
    
    def fetch_endpoint(self, endpoint: str, params: Optional[Dict] = None) -> Optional[Dict]:
        try:
            url = f"{self.base_url}/{endpoint}"
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
            
            # tRPC often wraps data in a result object
            if isinstance(data, dict):
                if 'result' in data and 'data' in data['result']:
                    return data['result']['data']
                elif 'result' in data:
                    return data['result']
            
            return data
        except requests.exceptions.RequestException as e:
            # Only log non-404 errors
            if hasattr(e, 'response') and e.response is not None:
                if e.response.status_code != 404:
                    print(f"Error fetching {endpoint}: {e}")
            return None
    
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
    
    def detect_generic_changes(self, endpoint: str, old_data, new_data) -> List[Alert]:
        """Generic change detection for any endpoint"""
        alerts = []
        
        try:
            # Handle list data (new items added)
            if isinstance(old_data, list) and isinstance(new_data, list):
                old_count = len(old_data)
                new_count = len(new_data)
                
                if new_count > old_count:
                    diff = new_count - old_count
                    level = AlertLevel.CRITICAL if diff > 5 or 'war' in endpoint.lower() or 'attack' in endpoint.lower() else AlertLevel.WARNING
                    
                    alert = self.add_alert(
                        level,
                        self._categorize_endpoint(endpoint),
                        f"New activity in {endpoint}: +{diff} entries",
                        {
                            'endpoint': endpoint,
                            'old_count': old_count,
                            'new_count': new_count,
                            'increase': diff
                        }
                    )
                    alerts.append(alert)
            
            # Handle dict data (value changes)
            elif isinstance(old_data, dict) and isinstance(new_data, dict):
                for key, new_val in new_data.items():
                    if key in old_data:
                        old_val = old_data[key]
                        
                        # Check numeric changes
                        if isinstance(old_val, (int, float)) and isinstance(new_val, (int, float)) and old_val != 0:
                            change_percent = ((new_val - old_val) / abs(old_val)) * 100
                            
                            # Different thresholds for different types
                            threshold = self._get_threshold_for_key(key, endpoint)
                            
                            if abs(change_percent) >= threshold:
                                level = self._determine_alert_level(change_percent, key, endpoint)
                                
                                alert = self.add_alert(
                                    level,
                                    self._categorize_endpoint(endpoint),
                                    f"{endpoint}.{key}: {change_percent:+.1f}% change",
                                    {
                                        'endpoint': endpoint,
                                        'key': key,
                                        'old_value': old_val,
                                        'new_value': new_val,
                                        'change_percent': change_percent
                                    }
                                )
                                alerts.append(alert)
                        
                        # Check nested dicts
                        elif isinstance(old_val, dict) and isinstance(new_val, dict):
                            nested_alerts = self.detect_generic_changes(f"{endpoint}.{key}", old_val, new_val)
                            alerts.extend(nested_alerts)
        
        except Exception as e:
            print(f"Error detecting changes in {endpoint}: {e}")
        
        return alerts
    
    def _categorize_endpoint(self, endpoint: str) -> str:
        """Categorize endpoint for alert grouping"""
        endpoint_lower = endpoint.lower()
        
        if any(x in endpoint_lower for x in ['war', 'attack', 'defense']):
            return "WAR_ACTIVITY"
        elif any(x in endpoint_lower for x in ['price', 'trade', 'market', 'bank', 'loan']):
            return "ECONOMY"
        elif any(x in endpoint_lower for x in ['build', 'military', 'soldier', 'tank']):
            return "BUILD_CHANGE"
        elif any(x in endpoint_lower for x in ['ranking', 'leaderboard', 'top', 'stats']):
            return "RANKINGS"
        elif any(x in endpoint_lower for x in ['production', 'producer', 'revenue', 'gdp']):
            return "PRODUCTION"
        elif 'alliance' in endpoint_lower:
            return "ALLIANCE"
        elif 'nation' in endpoint_lower:
            return "NATION"
        else:
            return "GENERAL"
    
    def _get_threshold_for_key(self, key: str, endpoint: str) -> float:
        """Get appropriate threshold based on key name"""
        key_lower = key.lower()
        
        # Price changes - more sensitive
        if 'price' in key_lower or 'cost' in key_lower:
            return self.thresholds.get('price_change_percent', 20)
        
        # Military/war - very sensitive
        if any(x in key_lower for x in ['military', 'attack', 'defense', 'soldier', 'tank', 'weapon']):
            return self.thresholds.get('build_change_threshold', 15)
        
        # Economy - moderate sensitivity
        if any(x in key_lower for x in ['bank', 'factory', 'farm', 'commerce', 'money', 'resource']):
            return self.thresholds.get('stat_change_threshold', 10)
        
        # Default
        return 25
    
    def _determine_alert_level(self, change_percent: float, key: str, endpoint: str) -> AlertLevel:
        """Determine alert level based on change magnitude and context"""
        key_lower = key.lower()
        endpoint_lower = endpoint.lower()
        
        # Critical thresholds
        if abs(change_percent) > 100:
            return AlertLevel.CRITICAL
        
        # War-related changes are more critical
        if any(x in key_lower or x in endpoint_lower for x in ['war', 'attack', 'military', 'defense']):
            return AlertLevel.CRITICAL if abs(change_percent) > 30 else AlertLevel.WARNING
        
        # Price changes
        if 'price' in key_lower:
            return AlertLevel.CRITICAL if abs(change_percent) > 50 else AlertLevel.WARNING
        
        # Default
        if abs(change_percent) > 50:
            return AlertLevel.WARNING
        
        return AlertLevel.INFO
    
    def run_full_scan(self) -> List[Alert]:
        """Scan all endpoints and detect changes"""
        new_alerts = []
        
        for endpoint in self.endpoints:
            data = self.fetch_endpoint(endpoint)
            
            if data is not None:
                # Compare with previous data if exists
                if endpoint in self.previous_data:
                    alerts = self.detect_generic_changes(endpoint, self.previous_data[endpoint], data)
                    new_alerts.extend(alerts)
                
                # Store current data
                self.previous_data[endpoint] = data
        
        return new_alerts
    
    def get_active_endpoints(self) -> List[str]:
        """Discover which endpoints are actually available"""
        active = []
        for endpoint in self.endpoints:
            data = self.fetch_endpoint(endpoint)
            if data is not None:
                active.append(endpoint)
        return active


# Discord Bot Setup - FIXED PREFIX
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

monitor = WarEraMonitor()
alert_channel_id = None


def create_alert_embed(alert: Alert) -> discord.Embed:
    """Create a rich embed for an alert"""
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
        # Limit fields to prevent embed size issues
        count = 0
        for key, value in alert.data.items():
            if count >= 10:  # Discord embed field limit
                break
            if isinstance(value, (int, float, str)) and len(str(value)) < 1024:
                embed.add_field(name=key.replace('_', ' ').title(), value=str(value), inline=True)
                count += 1
    
    return embed


@bot.event
async def on_ready():
    print(f'{bot.user} has connected to Discord!')
    print(f'Bot is in {len(bot.guilds)} guilds')
    
    # Discover active endpoints
    print('Discovering active endpoints...')
    active = monitor.get_active_endpoints()
    print(f'Found {len(active)} active endpoints')
    
    if not monitor_task.is_running():
        monitor_task.start()
        print('Monitoring task started')


@tasks.loop(minutes=1)
async def monitor_task():
    """Background task that runs monitoring scans"""
    if alert_channel_id is None:
        return
    
    channel = bot.get_channel(alert_channel_id)
    if channel is None:
        return
    
    try:
        alerts = monitor.run_full_scan()
        
        if alerts:
            # Group alerts by category
            by_category = {}
            for alert in alerts:
                if alert.category not in by_category:
                    by_category[alert.category] = []
                by_category[alert.category].append(alert)
            
            # Send summary
            summary = f"**🚨 War Room Scan - {len(alerts)} alerts**\n"
            for category, cat_alerts in by_category.items():
                summary += f"• {category}: {len(cat_alerts)}\n"
            
            await channel.send(summary)
            
            # Send individual alerts (limit to prevent spam)
            for alert in alerts[:20]:  # Max 20 alerts per scan
                embed = create_alert_embed(alert)
                await channel.send(embed=embed)
            
            if len(alerts) > 20:
                await channel.send(f"⚠️ {len(alerts) - 20} additional alerts suppressed to prevent spam")
    
    except Exception as e:
        print(f"Error in monitor task: {e}")


@bot.command()
@commands.has_permissions(administrator=True)
async def setchannel(ctx):
    """Set the current channel as the alert channel"""
    global alert_channel_id
    alert_channel_id = ctx.channel.id
    await ctx.send(f"✅ Alert channel set to {ctx.channel.mention}")


@bot.command()
async def scan(ctx):
    """Run a manual scan immediately"""
    await ctx.send("🔍 Running manual scan...")
    
    alerts = monitor.run_full_scan()
    
    if not alerts:
        await ctx.send("✅ Scan complete. No alerts found.")
        return
    
    # Group by category
    by_category = {}
    for alert in alerts:
        if alert.category not in by_category:
            by_category[alert.category] = []
        by_category[alert.category].append(alert)
    
    summary = f"**🚨 Scan Complete - {len(alerts)} alerts**\n"
    for category, cat_alerts in by_category.items():
        summary += f"• {category}: {len(cat_alerts)}\n"
    
    await ctx.send(summary)
    
    # Send alerts (limit to 10)
    for alert in alerts[:10]:
        embed = create_alert_embed(alert)
        await ctx.send(embed=embed)
    
    if len(alerts) > 10:
        await ctx.send(f"⚠️ Showing first 10 of {len(alerts)} alerts. Use `!status` for full summary")


@bot.command()
async def status(ctx):
    """Check bot status and statistics"""
    embed = discord.Embed(
        title="🎯 War Room Status",
        color=discord.Color.green()
    )
    
    embed.add_field(name="Alert Channel", value=f"<#{alert_channel_id}>" if alert_channel_id else "Not set", inline=False)
    embed.add_field(name="Total Alerts", value=str(len(monitor.alerts)), inline=True)
    embed.add_field(name="Monitoring Active", value="✅ Yes" if monitor_task.is_running() else "❌ No", inline=True)
    embed.add_field(name="Scan Interval", value="1 minute", inline=True)
    
    # Active endpoints
    active = monitor.get_active_endpoints()
    embed.add_field(name="Active Endpoints", value=str(len(active)), inline=True)
    
    # Count alerts by level
    by_level = {}
    for alert in monitor.alerts:
        by_level[alert.level] = by_level.get(alert.level, 0) + 1
    
    levels_text = "\n".join([f"{level.value} {level.name}: {count}" for level, count in by_level.items()])
    if levels_text:
        embed.add_field(name="Alerts by Level", value=levels_text, inline=False)
    
    # Count by category
    by_category = {}
    for alert in monitor.alerts:
        by_category[alert.category] = by_category.get(alert.category, 0) + 1
    
    if by_category:
        cat_text = "\n".join([f"• {cat}: {count}" for cat, count in sorted(by_category.items(), key=lambda x: x[1], reverse=True)[:5]])
        embed.add_field(name="Top Categories", value=cat_text, inline=False)
    
    await ctx.send(embed=embed)


@bot.command()
async def endpoints(ctx):
    """Show all active endpoints being monitored"""
    await ctx.send("🔍 Scanning all endpoints... this may take a moment")
    
    active = monitor.get_active_endpoints()
    
    if not active:
        await ctx.send("⚠️ No active endpoints found. The API might be down or endpoints have changed.")
        return
    
    # Group by category
    by_type = {}
    for endpoint in active:
        prefix = endpoint.split('.')[0]
        if prefix not in by_type:
            by_type[prefix] = []
        by_type[prefix].append(endpoint)
    
    embed = discord.Embed(
        title=f"📡 Active Endpoints ({len(active)})",
        description="These endpoints are responding and being monitored",
        color=discord.Color.blue()
    )
    
    for prefix, endpoints in sorted(by_type.items()):
        endpoint_list = "\n".join([f"• `{e}`" for e in endpoints[:10]])
        if len(endpoints) > 10:
            endpoint_list += f"\n... and {len(endpoints) - 10} more"
        embed.add_field(name=f"📂 {prefix.upper()}", value=endpoint_list, inline=False)
    
    await ctx.send(embed=embed)


@bot.command()
async def top(ctx, category: str = "nations"):
    """Show top nations/producers. Usage: !top nations, !top production, !top military, !top economy"""
    await ctx.send(f"🔍 Fetching top {category}...")
    
    # Map category to endpoints
    endpoint_map = {
        'nations': ['rankings.getTopNations', 'leaderboard.getNations', 'stats.getTopNations'],
        'production': ['economy.getTopProducers', 'stats.getTopProducers', 'rankings.getTopProduction'],
        'military': ['rankings.getTopMilitary', 'war.getTopAttackers'],
        'economy': ['rankings.getTopEconomy', 'economy.getTopTraders', 'rankings.getTopGDP'],
        'alliances': ['rankings.getTopAlliances', 'leaderboard.getAlliances', 'alliance.getTopAlliances'],
        'revenue': ['rankings.getTopRevenue'],
        'gdp': ['rankings.getTopGDP'],
        'attackers': ['war.getTopAttackers'],
        'defenders': ['war.getTopDefenders'],
    }
    
    endpoints = endpoint_map.get(category.lower(), endpoint_map['nations'])
    
    data = None
    used_endpoint = None
    
    # Try each endpoint until one works
    for endpoint in endpoints:
        data = monitor.fetch_endpoint(endpoint)
        if data is not None:
            used_endpoint = endpoint
            break
    
    if data is None:
        await ctx.send(f"❌ Could not fetch data for category: {category}\nTried endpoints: {', '.join(endpoints)}")
        return
    
    embed = discord.Embed(
        title=f"🏆 Top {category.title()}",
        description=f"Data from: `{used_endpoint}`",
        color=discord.Color.gold()
    )
    
    # Format the data
    if isinstance(data, list):
        # List of items
        items_text = ""
        for i, item in enumerate(data[:10], 1):
            if isinstance(item, dict):
                # Try to extract name and relevant stat
                name = item.get('name', item.get('nation', item.get('id', 'Unknown')))
                
                # Find the most relevant stat
                stat_keys = ['score', 'production', 'gdp', 'revenue', 'military', 'attacks', 'value']
                stat_value = None
                stat_name = None
                
                for key in stat_keys:
                    if key in item:
                        stat_value = item[key]
                        stat_name = key
                        break
                
                if stat_value is not None:
                    items_text += f"{i}. **{name}** - {stat_name.title()}: {stat_value:,}\n"
                else:
                    items_text += f"{i}. **{name}**\n"
            else:
                items_text += f"{i}. {item}\n"
        
        if items_text:
            embed.add_field(name="Rankings", value=items_text, inline=False)
        else:
            embed.add_field(name="Data", value=f"```json\n{json.dumps(data[:3], indent=2)[:500]}```", inline=False)
    
    elif isinstance(data, dict):
        # Dictionary of items
        items_text = ""
        for i, (key, value) in enumerate(list(data.items())[:10], 1):
            if isinstance(value, (int, float)):
                items_text += f"{i}. **{key}**: {value:,}\n"
            else:
                items_text += f"{i}. **{key}**: {value}\n"
        
        if items_text:
            embed.add_field(name="Rankings", value=items_text, inline=False)
    
    await ctx.send(embed=embed)


@bot.command()
async def production(ctx):
    """Show top producing nations"""
    await top(ctx, "production")
    """Test a specific API endpoint. Usage: !testapi itemTrading.getPrices"""
    if endpoint is None:
        await ctx.send("⚠️ Usage: `!testapi <endpoint>`\nExample: `!testapi itemTrading.getPrices`")
        return
    
    await ctx.send(f"🔍 Testing endpoint: `{endpoint}`")
    
    data = monitor.fetch_endpoint(endpoint)
    
    if data is None:
        await ctx.send(f"❌ Endpoint `{endpoint}` returned no data or errored")
        return
    
    # Show data structure
    embed = discord.Embed(
        title=f"✅ Endpoint Active: {endpoint}",
        color=discord.Color.green()
    )
    
    data_type = type(data).__name__
    embed.add_field(name="Data Type", value=data_type, inline=True)
    
    if isinstance(data, list):
        embed.add_field(name="Item Count", value=str(len(data)), inline=True)
        if data:
            embed.add_field(name="Sample Item", value=f"```json\n{json.dumps(data[0], indent=2)[:500]}```", inline=False)
    elif isinstance(data, dict):
        embed.add_field(name="Keys", value=str(len(data.keys())), inline=True)
        embed.add_field(name="Key Names", value=", ".join(list(data.keys())[:10]), inline=False)
        embed.add_field(name="Sample Data", value=f"```json\n{json.dumps(data, indent=2)[:500]}```", inline=False)
    else:
        embed.add_field(name="Raw Data", value=f"```{str(data)[:500]}```", inline=False)
    
    await ctx.send(embed=embed)


@bot.command()
@commands.has_permissions(administrator=True)
async def threshold(ctx, setting: str, value: float):
    """Set alert thresholds. Usage: !threshold price_change_percent 15"""
    if setting in monitor.thresholds:
        monitor.thresholds[setting] = value
        await ctx.send(f"✅ Set `{setting}` to `{value}`")
    else:
        available = ", ".join(monitor.thresholds.keys())
        await ctx.send(f"❌ Unknown setting. Available: {available}")


@bot.command()
@commands.has_permissions(administrator=True)
async def interval(ctx, minutes: int):
    """Change the monitoring interval in minutes"""
    if minutes < 1:
        await ctx.send("❌ Interval must be at least 1 minute")
        return
    
    monitor_task.change_interval(minutes=minutes)
    await ctx.send(f"✅ Monitoring interval set to {minutes} minute(s)")


@bot.command()
@commands.has_permissions(administrator=True)
async def start(ctx):
    """Start the monitoring task"""
    if monitor_task.is_running():
        await ctx.send("⚠️ Monitoring is already running")
    else:
        monitor_task.start()
        await ctx.send("✅ Monitoring started")


@bot.command()
@commands.has_permissions(administrator=True)
async def stop(ctx):
    """Stop the monitoring task"""
    if monitor_task.is_running():
        monitor_task.cancel()
        await ctx.send("✅ Monitoring stopped")
    else:
        await ctx.send("⚠️ Monitoring is not running")


@bot.command()
@commands.has_permissions(administrator=True)
async def clear(ctx):
    """Clear all stored alerts"""
    count = len(monitor.alerts)
    monitor.alerts.clear()
    await ctx.send(f"✅ Cleared {count} alerts")


@bot.command()
async def help(ctx):
    """Show all available commands"""
    embed = discord.Embed(
        title="🎯 War Room Bot Commands",
        description="Monitor War Era for ALL activity: prices, builds, wars, economy, trades, and more",
        color=discord.Color.blue()
    )
    
    commands_list = [
        ("!setchannel", "Set current channel for alerts (Admin)"),
        ("!scan", "Run manual scan immediately"),
        ("!status", "Show bot status and statistics"),
        ("!endpoints", "List all active endpoints being monitored"),
        ("!testapi <endpoint>", "Test a specific endpoint and see its data"),
        ("!top <category>", "Show rankings: nations, production, military, economy, alliances"),
        ("!production", "Show top producing nations (shortcut)"),
        ("!threshold <setting> <value>", "Set alert thresholds (Admin)"),
        ("!interval <minutes>", "Change scan interval (Admin)"),
        ("!start", "Start monitoring (Admin)"),
        ("!stop", "Stop monitoring (Admin)"),
        ("!clear", "Clear all alerts (Admin)"),
        ("!help", "Show this help message"),
    ]
    
    for cmd, desc in commands_list:
        embed.add_field(name=cmd, value=desc, inline=False)
    
    embed.add_field(
        name="Thresholds",
        value="• `price_change_percent` (default: 20)\n• `build_change_threshold` (default: 15)\n• `stat_change_threshold` (default: 10)\n• `large_trade_threshold` (default: 1000000)",
        inline=False
    )
    
    await ctx.send(embed=embed)


# Run the bot
if __name__ == "__main__":
    import os
    
    TOKEN = os.getenv('DISCORD_BOT_TOKEN', 'YOUR_BOT_TOKEN_HERE')
    
    if TOKEN == 'YOUR_BOT_TOKEN_HERE':
        print("⚠️  Please set your Discord bot token!")
        print("Either:")
        print("1. Replace 'YOUR_BOT_TOKEN_HERE' in the code")
        print("2. Set DISCORD_BOT_TOKEN environment variable")
    else:
        bot.run(TOKEN)
