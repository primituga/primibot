import json
import time
import asyncio
import logging

import httpx
import state
from config import (
    TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET, TWITCH_BROADCASTER_ID, TWITCH_USER_TOKEN,
    TWITCH_TOKEN, TWITCH_CHANNELS, AI_TRIGGER_REWARDS,
)
from notify import log_event, send_webhook
from helpers import send_twitch_chunks
from ai_core import ask_ai_logic
import guild_config

logger = logging.getLogger("AIBot")


async def twitch_app_token() -> str:
    now = time.time()
    if state._app_token_cache["token"] and state._app_token_cache["exp"] > now + 60:
        return state._app_token_cache["token"]
    res = await state.http_client.post(
        "https://id.twitch.tv/oauth2/token",
        data={"client_id": TWITCH_CLIENT_ID, "client_secret": TWITCH_CLIENT_SECRET, "grant_type": "client_credentials"},
        timeout=15.0
    )
    res.raise_for_status()
    j = res.json()
    state._app_token_cache["token"] = j["access_token"]
    state._app_token_cache["exp"] = now + j["expires_in"]
    return state._app_token_cache["token"]


async def _twitch_helix(method: str, path: str, params, data, use_user_token: bool):
    if use_user_token:
        token = TWITCH_USER_TOKEN or (TWITCH_TOKEN[6:] if TWITCH_TOKEN and TWITCH_TOKEN.startswith("oauth:") else TWITCH_TOKEN)
    else:
        token = await twitch_app_token()
    headers = {"Client-ID": TWITCH_CLIENT_ID, "Authorization": f"Bearer {token}"}
    url = f"https://api.twitch.tv/helix/{path}"
    if method == "PATCH":
        res = await state.http_client.patch(url, params=params, headers=headers, json=data, timeout=15.0)
    else:
        res = await state.http_client.get(url, params=params or {}, headers=headers, timeout=15.0)
    res.raise_for_status()
    return res.json()


async def twitch_helix(method: str = "GET", path: str = "", params=None, data=None, use_user_token: bool = False):
    """
    GET/PATCH na API Helix. Tenta com app token e, se falhar com 401/403
    (falta de scope), re-tenta automaticamente com o token de utilizador (chat).
    """
    try:
        return await _twitch_helix(method, path, params, data, use_user_token)
    except httpx.HTTPStatusError as e:
        if not use_user_token and e.response.status_code in (401, 403):
            logger.warning(f"Twitch app token falhou ({e.response.status_code}) em '{path}'; retry com token de utilizador")
            return await _twitch_helix(method, path, params, data, True)
        raise


async def get_broadcaster_id() -> str:
    if state._broadcaster_id_cache: return state._broadcaster_id_cache
    if TWITCH_BROADCASTER_ID:
        state._broadcaster_id_cache = TWITCH_BROADCASTER_ID
        return state._broadcaster_id_cache
    if not TWITCH_CHANNELS: return ""
    try:
        data = await twitch_helix(path="users", params={"login": TWITCH_CHANNELS[0]})
        d = data.get("data", [])
        if d:
            state._broadcaster_id_cache = d[0]["id"]
            return state._broadcaster_id_cache
    except Exception as e:
        logger.warning(f"Could not resolve broadcaster id: {e}")
    return ""


async def poll_twitch_events():
    """Detects follows/unfollows, stream status changes and pending channel point redemptions."""
    if not (TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET):
        return
    bid = await get_broadcaster_id()
    if not bid:
        return

    # 1) Follows / unfollows (endpoint antigo "users/follows" foi removido -> usar "channels/followers")
    try:
        data = await twitch_helix(path="channels/followers", params={"broadcaster_id": bid, "first": 100})
        current = {f["user_login"]: f["user_id"] for f in data.get("data", [])}
        prev_raw = await state.redis_client.get("twitch:followers")
        prev = json.loads(prev_raw) if prev_raw else None
        if prev is not None:
            for n in current:
                if n not in prev:
                    await log_event("⭐ Novo follower", n, color=0x00ff00, source="twitch")
                    await send_webhook(f"⭐ **{n}** começou a seguir!")
            for n in prev:
                if n not in current:
                    await log_event("💔 Unfollow", n, color=0xff0000, source="twitch")
        await state.redis_client.set("twitch:followers", json.dumps(current))
    except Exception as e:
        logger.warning(f"Follow poll failed: {e}")

    # 2) Estado de stream + jogo atual (endpoint "channels" funciona mesmo offline)
    try:
        data = await twitch_helix(path="streams", params={"user_id": bid})
        online = bool(data.get("data"))
        try:
            chdata = await twitch_helix(path="channels", params={"broadcaster_id": bid})
            cd = chdata.get("data", [{}])[0] if chdata.get("data") else {}
            game = cd.get("game_name", "") or ""
            title = cd.get("title", "") or ""
            if game:
                await state.redis_client.set("twitch:game", game)
            if title:
                await state.redis_client.set("twitch:title", title)
        except Exception as e:
            logger.warning(f"Channel info fetch failed: {e}")
        status = await state.redis_client.get("twitch:stream_status")
        if status is None:
            await state.redis_client.set("twitch:stream_status", "online" if online else "offline")
        elif status == "offline" and online:
            await log_event("🔴 Stream online", f"**{title}**\n🎮 {game}", color=0x9146ff, source="twitch")
            await send_webhook(f"🔴 **Stream online**: {title} ({game})")
            await state.redis_client.set("twitch:stream_status", "online")
        elif status == "online" and not online:
            await log_event("⚪ Stream offline", "", color=0x808080, source="twitch")
            await state.redis_client.set("twitch:stream_status", "offline")
    except Exception as e:
        logger.warning(f"Stream status poll failed: {e}")

    # 3) Redemptions de channel points (exigem user token do broadcaster com scope)
    if not TWITCH_USER_TOKEN:
        logger.warning("TWITCH_USER_TOKEN não definido — redemptions de channel points desativadas.")
    else:
        try:
            rewards = await twitch_helix(use_user_token=True, path="channel_points/custom_rewards", params={"broadcaster_id": bid})
            for rw in rewards.get("data", []):
                data = await twitch_helix(use_user_token=True, path="channel_points/custom_rewards/redemptions", params={"broadcaster_id": bid, "reward_id": rw["id"], "status": "UNFULFILLED", "sort": "OLDEST"})
                for r in data.get("data", [])[:10]:
                    reward = r["reward"]["title"]
                    uname = r.get("user_login") or r["user_id"]
                    ui = r.get("user_input") or ""
                    await log_event("🎯 Channel points", f"**{uname}** resgatou **{reward}**" + (f"\n> {ui}" if ui else ""), color=0xffc800, source="twitch")
                    await send_webhook(f"🎯 **{uname}** resgatou **{reward}**" + (f" ({ui})" if ui else ""))
                    # se for um reward de IA, responder no chat
                    if reward.lower() in AI_TRIGGER_REWARDS and ui and state.twitch_bot_ref and TWITCH_CHANNELS:
                        ch = state.twitch_bot_ref.get_channel(TWITCH_CHANNELS[0])
                        if ch:
                            sid = f"twitch_{TWITCH_CHANNELS[0]}_{uname}"
                            ans = await ask_ai_logic(ui, sid, platform="twitch", user=uname)
                            await send_twitch_chunks(ch, uname, ans)
                    # marcar como cumprido
                    try:
                        await twitch_helix("PATCH", path="channel_points/custom_rewards/redemptions", params={"broadcaster_id": bid, "id": r["id"], "status": "FULFILLED"}, use_user_token=True)
                    except Exception as e:
                        logger.warning(f"Redemption fulfil failed: {e}")
        except Exception as e:
            logger.warning(f"Redemptions poll failed: {e}")

    # 4) Estado + jogo POR CANAL — contexto de IA isolado quando o bot está em várias lives
    try:
        channels = await guild_config.all_tracked_channels()
        if not channels:
            return
        udata = await twitch_helix(path="users", params={"login": channels})
        id_by_login = {u["login"].lower(): u["id"] for u in udata.get("data", [])}
        if id_by_login:
            ids = list(id_by_login.values())
            try:
                cdata = await twitch_helix(path="channels", params={"broadcaster_id": ids})
                for c in cdata.get("data", []):
                    login = c.get("broadcaster_login", "").lower()
                    if login:
                        await state.redis_client.set(f"twitch:{login}:game", c.get("game_name") or "")
                        await state.redis_client.set(f"twitch:{login}:title", c.get("title") or "")
            except Exception as e:
                logger.warning(f"Per-channel channel-info fetch failed: {e}")
            sdata = await twitch_helix(path="streams", params={"user_id": ids})
            online_logins = {s["user_login"].lower() for s in sdata.get("data", [])}
            for login in id_by_login:
                new_status = "online" if login in online_logins else "offline"
                old_status = await state.redis_client.get(f"twitch:{login}:stream_status")
                await state.redis_client.set(f"twitch:{login}:stream_status", new_status)
                if old_status and old_status != new_status:
                    guilds = await guild_config.guilds_tracking(login)
                    for gid in guilds:
                        if new_status == "online":
                            await log_event("🔴 Stream online", f"**{login}** entrou em live!", color=0x9146ff, source="twitch", guild_id=gid)
                            await send_webhook(f"🔴 **{login}** entrou em live!")
                        else:
                            await log_event("⚪ Stream offline", f"**{login}** terminou a stream", color=0x808080, source="twitch", guild_id=gid)
    except Exception as e:
        logger.warning(f"Per-channel stream poll failed: {e}")


async def twitch_events_loop():
    while True:
        await poll_twitch_events()
        await asyncio.sleep(60)


async def live_status_text(channel: str = "") -> str:
    try:
        key = f"twitch:{channel.lower()}:stream_status" if channel else "twitch:stream_status"
        st = await state.redis_client.get(key)
        if st == "online":
            return "🔴 Estou LIVE! Vem ver a stream!"
        if st == "offline":
            return "⚪ Offline de momento."
    except Exception:
        pass
    return "❓ Ainda sem dados de estado de stream."
