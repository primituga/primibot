"""Resolução de namespaces.

Cada "unidade" isolada (um canal Twitch, uma sala de Discord) é um namespace próprio.
Todas as chaves de estado do bot derivam de uma unit, por isso nenhuma feature consegue
ler nem escrever o estado de outra unidade.

    unit Twitch  : twitch:{canal}
    unit Discord : discord:{guild_id}:{channel_id}

Ver AGENTS.md, secção "Redis is the only persistence".
"""
import state


# guild_id por channel_id — evita um get_channel() por cada pedido ao AI
_guild_cache = {}

GLOBAL = "_global"


def clear_guild_cache():
    """Limpa o cache channel->guild (on_ready chama isto)."""
    _guild_cache.clear()


def twitch_unit(channel: str) -> str:
    if not channel:
        return "twitch:_"
    return f"twitch:{channel.lower()}"


def discord_unit(guild_id, channel_id) -> str:
    g = guild_id if guild_id else "dm"
    return f"discord:{g}:{channel_id}"


def guild_of_channel(channel_id) -> object:
    """Devolve o guild_id de um canal Discord, ou None em DM. Com cache."""
    try:
        cid = int(channel_id)
    except (TypeError, ValueError):
        return None
    if cid in _guild_cache:
        return _guild_cache[cid]
    gid = None
    try:
        client = state.discord_client
        if client is not None:
            obj = client.get_channel(cid)
            gid = getattr(getattr(obj, "guild", None), "id", None)
    except Exception:
        gid = None
    _guild_cache[cid] = gid
    return gid


def unit_for_session(session_id: str, platform: str = "") -> str:
    """Deriva a unit a partir de um session_id.

    Formatos em uso:
        twitch_{canal}_{user}   -> twitch:{canal}
        discord_{channel_id}    -> discord:{guild_id}:{channel_id}
    """
    if not session_id:
        return f"{platform or 'bot'}:_"
    try:
        if session_id.startswith("twitch_"):
            return twitch_unit(session_id.split("_", 2)[1])
        if session_id.startswith("discord_"):
            cid = session_id.split("_", 1)[1]
            return discord_unit(guild_of_channel(cid), cid)
    except Exception:
        pass
    return f"{platform or 'bot'}:_"


# ─────────────── construtores de chaves ───────────────

def k_history(unit: str) -> str:
    return f"history:{unit}"


def k_summary(unit: str) -> str:
    return f"summary:{unit}"


def k_memory(unit: str, user_key: str) -> str:
    return f"memory:{unit}:{user_key}"


def k_persona(unit: str) -> str:
    return f"persona:{unit}"


def k_faq(unit: str) -> str:
    return f"faq:{unit}"


def k_rl_cooldown(unit: str, user_key: str) -> str:
    return f"rl:{unit}:{user_key}"


def k_rl_hourly(unit: str, user_key: str) -> str:
    return f"rlh:{unit}:{user_key}"


def k_health(unit: str, provider: str, status: str) -> str:
    return f"health:{unit}:{provider}:{status}"


def k_stats(unit: str) -> str:
    """Redis HASH: campo 'total', 'user:{user_key}', 'model:{model}'.

    Um hash por unit em vez de uma chave por elemento: nao precisa de SCAN
    para agregar, e a unit pode conter ':' sem ambiguidade ao fazer parse."""
    return f"stats:{unit}"


def k_live(login: str, field: str) -> str:
    """Estado da live de um streamer. Isto NÃO é uma unit: é global por canal
    porque o estado de uma transmissão é único. Só a *injeção* é filtrada por unit."""
    return f"twitch:{login.lower()}:{field}"
