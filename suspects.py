"""Sistema anti-suspeitos: blacklist cruzada Twitch, DiscordRep,
heuristica de contas novas e scan de links (URLhaus)."""
import json
import re
import hashlib
import logging
from datetime import datetime, timezone
from urllib.parse import urlparse

import state
from config import DREP_API_KEY, SUSPECT_ACCOUNT_AGE_DAYS, SUSPECT_LINK_SCAN, SUSPECT_AUTO_DELETE
from notify import log_event

logger = logging.getLogger("AIBot")

GIBBERISH_RE = re.compile(r'^[a-z]{4,14}\d{4,}$')
URL_RE = re.compile(r'https?://[^\s<>"\')\]]+', re.IGNORECASE)
SAFE_DOMAINS = {
    "discord.com", "discord.gg", "discordapp.com", "cdn.discordapp.com",
    "twitch.tv", "clips.twitch.tv", "help.twitch.tv", "blog.twitch.tv",
    "youtube.com", "youtu.be", "x.com", "twitter.com",
    "instagram.com", "tiktok.com", "reddit.com", "redd.it",
    "github.com", "spotify.com", "open.spotify.com",
    "google.com", "imgur.com", "tenor.com", "giphy.com",
}
ALERT_TTL = 6 * 3600  # nao repetir o mesmo alerta ao utilizador/canal em 6h


async def _alerted_once(key: str) -> bool:
    """True se este alerta ja foi emitido recentemente."""
    created = await state.redis_client.set(f"suspects:seen:{key}", "1", ex=ALERT_TTL, nx=True)
    return created is None


def _domain(url: str) -> str:
    try:
        d = urlparse(url).netloc.lower()
        return d[4:] if d.startswith("www.") else d
    except Exception:
        return ""


# ─────────────────────────── TWITCH ───────────────────────────

async def check_twitch_user(channel: str, login: str):
    """Blacklist cruzada + heuristica de conta nova.
    Devolve lista de alertas (strings), vazia se estiver tudo bem."""
    alerts = []
    login_l = login.lower()

    # 1) Banido noutro canal monitorizado?
    raw = await state.redis_client.hget("suspects:twitch_globalbans", login_l)
    if raw:
        try:
            info = json.loads(raw)
        except Exception:
            info = None
        if info and info.get("channel") != channel:
            if not await _alerted_once(f"xb_{login_l}_{channel}"):
                days_txt = ""
                at = info.get("at") or ""
                if at:
                    try:
                        d = (datetime.now(timezone.utc) - datetime.fromisoformat(at.replace("Z", "+00:00"))).days
                        days_txt = f", ha {d} dia(s)" if d else ", recentemente"
                    except Exception:
                        pass
                reason = info.get("reason") or "sem motivo registado"
                alerts.append(
                    f"🚫 **@{login}** escreveu em #{channel} mas esta banido em **{info.get('channel')}** "
                    f"({reason}{days_txt})"
                )

    # 2) Heuristica de conta nova / nome de bot (cache 24h por user)
    age_key = f"suspects:age:{login_l}"
    cached = await state.redis_client.get(age_key)
    if cached is None:
        cached = await _twitch_age_lookup(login)
        if cached is not None:
            await state.redis_client.set(age_key, json.dumps(cached), ex=86400)
    if isinstance(cached, str):
        try:
            cached = json.loads(cached)
        except Exception:
            cached = None
    if cached and cached.get("flag"):
        if not await _alerted_once(f"age_{login_l}_{channel}"):
            alerts.append(f"⚠️ **@{login}** em #{channel}: {cached['flag']}")
    return alerts


async def _twitch_age_lookup(login: str):
    """Procura dados da conta na Helix. Devolve dict com flag ou None se falhar."""
    try:
        from twitch_api import twitch_helix  # lazy para evitar import circular
        data = await twitch_helix(path="users", params={"login": login})
        users = data.get("data") or []
        if not users:
            return {"flag": ""}
        u = users[0]
        created = u.get("created_at", "")
        avatar = u.get("profile_image_url", "") or ""
        try:
            dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
            days = (datetime.now(timezone.utc) - dt).days
        except Exception:
            return {"flag": ""}
        pts = []
        if days < SUSPECT_ACCOUNT_AGE_DAYS:
            pts.append(f"conta criada ha {max(days, 0)} dia(s)")
        elif days < 30:
            pts.append("conta recente (<30d)")
        if GIBBERISH_RE.match((u.get("login") or "").lower()):
            pts.append("nome com padrao de bot")
        if ("default" in avatar) or ("jtv_user_picture" in avatar):
            pts.append("avatar padrao")
        flag = "; ".join(pts) if len(pts) >= 2 else ""
        return {"flag": flag}
    except Exception as e:
        logger.warning(f"Twitch age lookup failed ({login}): {e}")
        return None


# ─────────────────────────── DISCORD ───────────────────────────

async def check_discord_user(member):
    """Heuristica de conta nova + DiscordRep.
    Devolve lista de alertas (strings)."""
    alerts = []
    pts = []
    age_days = (datetime.now(timezone.utc) - member.created_at).days
    if age_days < SUSPECT_ACCOUNT_AGE_DAYS:
        pts.append(f"conta criada ha {max(age_days, 0)} dia(s)")
    elif age_days < 30:
        pts.append("conta recente (<30d)")
    name = (member.name or "").lower()
    if GIBBERISH_RE.match(name):
        pts.append("nome com padrao de bot")
    try:
        if member.avatar is None and member.default_avatar == member.display_avatar:
            pts.append("avatar padrao")
    except Exception:
        pass
    if len(pts) >= 2:
        if not await _alerted_once(f"dage_{member.id}"):
            alerts.append(f"⚠️ **{member}** entrou no servidor: {'; '.join(pts)}")
    elif pts:
        alerts.append(f"ℹ️ {member}: {'; '.join(pts)} (informativo)")

    rep = await discordrep_check(member.id)
    if rep:
        if not await _alerted_once(f"drep_{member.id}"):
            alerts.append(f"🚨 **{member}** tem reputacao negativa: {rep}")
    return alerts


async def discordrep_check(user_id: int) -> str:
    """Consulta o DiscordRep (se houver API key). Devolve resumo ou ''."""
    if not DREP_API_KEY:
        return ""
    key = f"suspects:drep:{user_id}"
    cached = await state.redis_client.get(key)
    if cached is None:
        headers = {"Authorization": DREP_API_KEY, "Accept": "application/json"}
        parts = []
        try:
            r = await state.http_client.get(
                f"https://discordrep.com/api/v3/infraction/{user_id}", headers=headers, timeout=10)
            if r.status_code == 200:
                j = r.json()
                ban = j.get("ban")
                warn = j.get("warn")
                n_warn = len(warn) if isinstance(warn, list) else (1 if warn else 0)
                if ban:
                    reason = (ban.get("reason") if isinstance(ban, dict) else "") or ""
                    parts.append(f"BAN partilhado{' (' + reason[:80] + ')' if reason else ''}")
                if n_warn:
                    parts.append(f"{n_warn} warn(s) partilhados")
        except Exception:
            pass
        try:
            r = await state.http_client.get(
                f"https://discordrep.com/api/v3/rep/{user_id}", headers=headers, timeout=10)
            if r.status_code == 200:
                j = r.json()
                down = int(j.get("downvotes") or 0)
                up = int(j.get("upvotes") or 0)
                rank = j.get("rank") or ""
                if down >= 3:
                    parts.append(f"{down} downvotes (rank {rank})")
        except Exception:
            pass
        cached = " | ".join(parts)
        await state.redis_client.set(key, cached or "-", ex=86400)
    if not cached or cached == "-":
        return ""
    return cached


# ─────────────────────────── LINKS ───────────────────────────

async def scan_links(platform: str, place: str, author: str, text: str) -> list:
    """Verifica URLs da mensagem contra o URLhaus. Devolve URLs maliciosos."""
    if not SUSPECT_LINK_SCAN:
        return []
    hits = []
    for url in URL_RE.findall(text or ""):
        dom = _domain(url)
        if not dom or dom in SAFE_DOMAINS:
            continue
        key = "suspects:url:" + hashlib.sha1(url.encode()).hexdigest()[:20]
        verdict = await state.redis_client.get(key)
        if verdict is None:
            verdict = await _urlhaus_lookup(url)
            ttl = 7 * 86400 if verdict == "bad" else 86400
            await state.redis_client.set(key, verdict, ex=ttl)
        if verdict != "bad":
            continue
        if await _alerted_once(f"url_{key}_{place}"):
            continue
        hits.append(url)
        extra = ""
        if platform == "discord" and SUSPECT_AUTO_DELETE:
            extra = " → mensagem apagada"
        await log_event(
            "🦠 Link malicioso",
            f"**{author}** em {place} ({platform}) publicou:\n{url}{extra}",
            color=0xff0000,
            source=platform,
        )
    return hits


async def _urlhaus_lookup(url: str) -> str:
    """Consulta o URLhaus (abuse.ch, sem API key). 'bad' | 'ok' | 'error'."""
    try:
        r = await state.http_client.post(
            "https://urlhaus-api.abuse.ch/v1/url/",
            data={"url": url},
            timeout=10,
            headers={"User-Agent": "primibot-antiphish"},
        )
        if r.status_code != 200:
            return "error"
        j = r.json()
        qs = j.get("query_status")
        if qs == "ok":
            return "bad"
        if qs in ("no_reference", "not_found"):
            return "ok"
        return "error"
    except Exception as e:
        logger.warning(f"URLhaus lookup failed: {e}")
        return "error"


async def discord_autodelete_allowed(message) -> bool:
    return SUSPECT_AUTO_DELETE and message.guild is not None and message.guild.me.guild_permissions.manage_messages
