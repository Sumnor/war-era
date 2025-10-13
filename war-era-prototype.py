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
            'large_trade_threshold': 1000000,
        }
    
    def fetch_endpoint(self, endpoint: str, params: Optional[Dict] = None) -> Optional[Dict]:
        try:
            url = f"{self.base_url}/{endpoint}"
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
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
    
    def monitor_item_prices(self) -> List[Alert]:
        new_alerts = []
        data = self.fetch_endpoint("itemTrading.getPrices")
        
        if not data:
            return new_alerts
        
        if 'itemTrading.getPrices' in self.previous_data:
            old_data = self.previous_data['itemTrading.getPrices']
            new_alerts.extend(self.detect_price_changes(old_data, data))
        
        self.previous_data['itemTrading.getPrices'] = data
        return new_alerts
    
    def detect_price_changes(self, old_data: Dict, new_data: Dict) -> List[Alert]:
        alerts = []
        try:
            if isinstance(new_data, dict) and isinstance(old_data, dict):
                for item_id, new_price in new_data.items():
                    if item_id in old_data:
                        old_price = old_data[item_id]
                        if old_price > 0:
                            change_percent = ((new_price - old_price) / old_price) * 100
                            
                            if abs(change_percent) >= self.thresholds['price_change_percent']:
                                level = AlertLevel.WARNING if abs(change_percent) < 50 else AlertLevel.CRITICAL
                                alert = self.add_alert(
                                    level,
                                    "PRICE_CHANGE",
                                    f"Item {item_id} price changed by {change_percent:.1f}%",
                                    {
                                        'item_id': item_id,
                                        'old_price': old_price,
                                        'new_price': new_price,
                                        'change_percent': change_percent
                                    }
                                )
                                alerts.append(alert)
        except Exception as e:
            print(f"Error detecting price changes: {e}")
        return alerts
    
    def monitor_nation_builds(self, nation_id: Optional[str] = None) -> List[Alert]:
        new_alerts = []
        endpoints = ["nation.getStats", "nation.getBuilds", "nation.getRecent"]
        
        for endpoint in endpoints:
            params = {'nationId': nation_id} if nation_id else None
            data = self.fetch_endpoint(endpoint, params)
            
            if data:
                cache_key = f"{endpoint}_{nation_id}" if nation_id else endpoint
                
                if cache_key in self.previous_data:
                    new_alerts.extend(self.detect_build_changes(cache_key, self.previous_data[cache_key], data))
                
                self.previous_data[cache_key] = data
        return new_alerts
    
    def detect_build_changes(self, cache_key: str, old_data: Dict, new_data: Dict) -> List[Alert]:
        alerts = []
        try:
            if isinstance(new_data, dict) and isinstance(old_data, dict):
                war_indicators = ['military', 'defense', 'attack', 'soldiers', 'tanks']
                econ_indicators = ['farms', 'factories', 'banks', 'commerce']
                
                for key in war_indicators:
                    if key in new_data and key in old_data:
                        if new_data[key] > old_data[key] * 1.15:
                            alert = self.add_alert(
                                AlertLevel.WARNING,
                                "WAR_BUILD",
                                f"Nation increasing war capability: {key} +{((new_data[key]/old_data[key])-1)*100:.1f}%",
                                {
                                    'cache_key': cache_key,
                                    'stat': key,
                                    'old_value': old_data[key],
                                    'new_value': new_data[key]
                                }
                            )
                            alerts.append(alert)
                
                for key in econ_indicators:
                    if key in new_data and key in old_data:
                        if new_data[key] < old_data[key] * 0.85:
                            alert = self.add_alert(
                                AlertLevel.INFO,
                                "ECON_REDUCTION",
                                f"Nation reducing econ: {key} -{((1-new_data[key]/old_data[key]))*100:.1f}%",
                                {
                                    'cache_key': cache_key,
                                    'stat': key,
                                    'old_value': old_data[key],
                                    'new_value': new_data[key]
                                }
                            )
                            alerts.append(alert)
        except Exception as e:
            print(f"Error detecting build changes: {e}")
        return alerts
    
    def monitor_war_activity(self) -> List[Alert]:
        new_alerts = []
        endpoints = ["war.getActive", "war.getRecent", "alliance.getWars", "nation.getAttacks"]
        
        for endpoint in endpoints:
            data = self.fetch_endpoint(endpoint)
            
            if data:
                if endpoint in self.previous_data:
                    new_alerts.extend(self.detect_war_changes(endpoint, self.previous_data[endpoint], data))
                
                self.previous_data[endpoint] = data
        return new_alerts
    
    def detect_war_changes(self, endpoint: str, old_data: Dict, new_data: Dict) -> List[Alert]:
        alerts = []
        try:
            old_count = len(old_data) if isinstance(old_data, list) else 0
            new_count = len(new_data) if isinstance(new_data, list) else 0
            
            if new_count > old_count:
                alert = self.add_alert(
                    AlertLevel.CRITICAL,
                    "WAR_ACTIVITY",
                    f"New war activity: {new_count - old_count} new entries in {endpoint}",
                    {
                        'endpoint': endpoint,
                        'old_count': old_count,
                        'new_count': new_count
                    }
                )
                alerts.append(alert)
        except Exception as e:
            print(f"Error detecting war changes: {e}")
        return alerts
    
    def run_full_scan(self) -> List[Alert]:
        all_alerts = []
        all_alerts.extend(self.monitor_item_prices())
        all_alerts.extend(self.monitor_nation_builds())
        all_alerts.extend(self.monitor_war_activity())
        return all_alerts


# Discord Bot
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

monitor = WarEraMonitor()
alert_channel_id = None  # Set this via !war setchannel


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
        for key, value in alert.data.items():
            if isinstance(value, (int, float, str)):
                embed.add_field(name=key.replace('_', ' ').title(), value=str(value), inline=True)
    
    return embed


@bot.event
async def on_ready():
    print(f'{bot.user} has connected to Discord!')
    print(f'Bot is in {len(bot.guilds)} guilds')
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
    
    alerts = monitor.run_full_scan()
    
    if alerts:
        await channel.send(f"**🚨 War Room Scan Complete - {len(alerts)} alerts found**")
        for alert in alerts:
            embed = create_alert_embed(alert)
            await channel.send(embed=embed)


@bot.command(name='setchannel')
@commands.has_permissions(administrator=True)
async def set_channel(ctx):
    """Set the current channel as the alert channel"""
    global alert_channel_id
    alert_channel_id = ctx.channel.id
    await ctx.send(f"✅ Alert channel set to {ctx.channel.mention}")


@bot.command(name='scan')
async def manual_scan(ctx):
    """Run a manual scan immediately"""
    await ctx.send("🔍 Running manual scan...")
    
    alerts = monitor.run_full_scan()
    
    if not alerts:
        await ctx.send("✅ Scan complete. No alerts found.")
        return
    
    await ctx.send(f"**🚨 Scan Complete - {len(alerts)} alerts found**")
    for alert in alerts:
        embed = create_alert_embed(alert)
        await ctx.send(embed=embed)


@bot.command(name='status')
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
    
    # Count alerts by level
    by_level = {}
    for alert in monitor.alerts:
        by_level[alert.level] = by_level.get(alert.level, 0) + 1
    
    levels_text = "\n".join([f"{level.value} {level.name}: {count}" for level, count in by_level.items()])
    if levels_text:
        embed.add_field(name="Alerts by Level", value=levels_text, inline=False)
    
    await ctx.send(embed=embed)


@bot.command(name='threshold')
@commands.has_permissions(administrator=True)
async def set_threshold(ctx, setting: str, value: float):
    """Set alert thresholds. Usage: !war threshold price_change_percent 15"""
    if setting in monitor.thresholds:
        monitor.thresholds[setting] = value
        await ctx.send(f"✅ Set `{setting}` to `{value}`")
    else:
        available = ", ".join(monitor.thresholds.keys())
        await ctx.send(f"❌ Unknown setting. Available: {available}")


@bot.command(name='interval')
@commands.has_permissions(administrator=True)
async def set_interval(ctx, minutes: int):
    """Change the monitoring interval in minutes"""
    if minutes < 1:
        await ctx.send("❌ Interval must be at least 1 minute")
        return
    
    monitor_task.change_interval(minutes=minutes)
    await ctx.send(f"✅ Monitoring interval set to {minutes} minute(s)")


@bot.command(name='start')
@commands.has_permissions(administrator=True)
async def start_monitoring(ctx):
    """Start the monitoring task"""
    if monitor_task.is_running():
        await ctx.send("⚠️ Monitoring is already running")
    else:
        monitor_task.start()
        await ctx.send("✅ Monitoring started")


@bot.command(name='stop')
@commands.has_permissions(administrator=True)
async def stop_monitoring(ctx):
    """Stop the monitoring task"""
    if monitor_task.is_running():
        monitor_task.cancel()
        await ctx.send("✅ Monitoring stopped")
    else:
        await ctx.send("⚠️ Monitoring is not running")


@bot.command(name='clear')
@commands.has_permissions(administrator=True)
async def clear_alerts(ctx):
    """Clear all stored alerts"""
    count = len(monitor.alerts)
    monitor.alerts.clear()
    await ctx.send(f"✅ Cleared {count} alerts")


@bot.command(name='help')
async def help_command(ctx):
    """Show all available commands"""
    embed = discord.Embed(
        title="🎯 War Room Bot Commands",
        description="Monitor War Era for price changes, build resets, and war activity",
        color=discord.Color.blue()
    )
    
    commands_list = [
        ("!war setchannel", "Set current channel for alerts (Admin)"),
        ("!war scan", "Run manual scan immediately"),
        ("!war status", "Show bot status and statistics"),
        ("!war threshold <setting> <value>", "Set alert thresholds (Admin)"),
        ("!war interval <minutes>", "Change scan interval (Admin)"),
        ("!war start", "Start monitoring (Admin)"),
        ("!war stop", "Stop monitoring (Admin)"),
        ("!war clear", "Clear all alerts (Admin)"),
        ("!war help", "Show this help message"),
    ]
    
    for cmd, desc in commands_list:
        embed.add_field(name=cmd, value=desc, inline=False)
    
    embed.add_field(
        name="Thresholds",
        value="• `price_change_percent` (default: 20)\n• `build_change_threshold` (default: 15)\n• `large_trade_threshold` (default: 1000000)",
        inline=False
    )
    
    await ctx.send(embed=embed)


# Run the bot
if __name__ == "__main__":
    import os
    
    # Set your Discord bot token here or use environment variable
    TOKEN = os.getenv('DISCORD_BOT_TOKEN')
    
    if TOKEN == 'YOUR_BOT_TOKEN_HERE':
        print("⚠️  Please set your Discord bot token!")
        print("Either:")
        print("1. Replace 'YOUR_BOT_TOKEN_HERE' in the code")
        print("2. Set DISCORD_BOT_TOKEN environment variable")
    else:
        bot.run(TOKEN)
