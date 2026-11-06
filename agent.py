import asyncio
import logging

import state
from config import DISCORD_TOKEN, TWITCH_TOKEN
from notify import send_webhook, heartbeat_loop, digest_loop
from twitch_api import twitch_events_loop
import discord_bot
from twitch_bot import MyTwitchBot

logger = logging.getLogger("AIBot")


async def _watch(coro, name: str):
    """Envolve cada serviço para falhas aparecerem de imediato no log/webhook."""
    try:
        await coro
    except Exception as e:
        logger.error(f"Serviço '{name}' falhou: {e}")
        await send_webhook(f"🚨 **{name} crash**: `{e}`")


async def main():
    tasks = [
        _watch(heartbeat_loop(), "heartbeat"),
        _watch(twitch_events_loop(), "twitch_events"),
        _watch(digest_loop(), "digest"),
    ]
    if DISCORD_TOKEN:
        tasks.append(_watch(discord_bot.discord_client.start(DISCORD_TOKEN), "discord"))
    else:
        logger.warning("DISCORD_TOKEN não definido — bot Discord desativado.")
    if TWITCH_TOKEN:
        state.twitch_bot_ref = MyTwitchBot()
        tasks.append(_watch(state.twitch_bot_ref.start(), "twitch_chat"))
    else:
        logger.warning("TWITCH_TOKEN não definido — bot Twitch (chat) desativado.")

    if not tasks:
        logger.error("No valid tokens provided! Exiting...")
        return

    # Run both bots concurrently. return_exceptions=True prevents one bot's crash from killing the other.
    try:
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        await state.http_client.aclose()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot shut down gracefully.")
