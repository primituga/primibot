import os
import json
import time
import asyncio
import logging
from datetime import datetime, timedelta

import discord
import state
import scope
from config import LOG_CHANNEL_ID, LOG_CHANNEL_ID_TWITCH, WEBHOOK_URL, HEARTBEAT_FILE
from storage import get_stats
import guild_config

logger = logging.getLogger("AIBot")


# --- Webhook (alertas críticos + digest) ---
async def send_webhook(text: str):
    """Sends a message to the configured Discord webhook. Fails soft."""
    if not WEBHOOK_URL: return
    try:
        await state.http_client.post(WEBHOOK_URL, json={"content": text[:1900]}, timeout=10.0)
    except Exception as e:
        logger.warning(f"Webhook send failed: {e}")


# --- Logging para canais dedicados do Discord ---
async def get_log_channel(source: str, guild_id: int = None):
    if guild_id:
        cfg = await guild_config.get_guild_config(guild_id)
        key = "log_channel_id_twitch" if source == "twitch" else "log_channel_id"
        try:
            ch_id = int(cfg.get(key) or 0)
        except (ValueError, TypeError):
            ch_id = 0
    else:
        ch_id = LOG_CHANNEL_ID_TWITCH if source == "twitch" else LOG_CHANNEL_ID
    if not ch_id:
        return None
    cache_key = f"{guild_id or 'global'}:{source}"
    if cache_key not in state._log_channels:
        state._log_channels[cache_key] = state.discord_client.get_channel(ch_id)
    return state._log_channels[cache_key]


async def log_event(title: str, desc: str, color: int = 0x5865F2, source: str = "discord", guild_id: int = None):
    """Sends an embed to the dedicated log channel ('discord' ou 'twitch'). Fails soft."""
    ch = await get_log_channel(source, guild_id)
    if not ch: return
    try:
        embed = discord.Embed(title=title, description=desc[:1000], color=color, timestamp=datetime.now())
        await ch.send(embed=embed)
    except Exception as e:
        logger.warning(f"Log channel send failed: {e}")


# --- Heartbeat ---
async def heartbeat_loop():
    """
    Writes a periodic heartbeat file so the Docker healthcheck can detect
    a hung event loop (file older than 2 minutes => unhealthy).
    """
    while True:
        try:
            with open(HEARTBEAT_FILE, "w") as f:
                f.write(str(time.time()))
        except Exception as e:
            logger.warning(f"Heartbeat write failed: {e}")
        await asyncio.sleep(30)


# --- Digest diário (06:00) via webhook ---
async def digest_loop():
    while True:
        now = datetime.now()
        target = now.replace(hour=6, minute=0, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        stats = await get_stats(scope.GLOBAL)
        try:
            followers = await state.redis_client.get("twitch:followers")
            fc = len(json.loads(followers)) if followers else 0
        except Exception:
            fc = 0
        top = ", ".join(f"{u} ({n})" for u, n in stats["global"]["models"][:3]) or "—"
        msg = (
            f"📊 **Digest diário**\n"
            f"• Pedidos ao bot: **{stats['global']['total']}**\n"
            f"• Modelos: {top}\n"
            f"• Followers: **{fc}**"
        )
        await send_webhook(msg)
