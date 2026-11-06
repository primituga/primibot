import logging

import state
from config import (
    LOG_CHANNEL_ID, LOG_CHANNEL_ID_TWITCH, WELCOME_CHANNEL_ID, WELCOME_MESSAGE, AUTO_ROLE_ID, AUTO_MOD,
    TWITCH_CHANNELS, GUILD_ID,
)

logger = logging.getLogger("AIBot")

# Schema de configuração por-guild: key -> (label, tipo, default, ajuda)
# tipos: "channel" | "role" | "text" | "bool"
G_CONFIG_SCHEMA = {
    "log_channel_id": ("Canal de logs Discord", "channel", LOG_CHANNEL_ID, "Canal onde o bot envia os logs do Discord"),
    "log_channel_id_twitch": ("Canal de logs Twitch", "channel", LOG_CHANNEL_ID_TWITCH, "Canal onde o bot envia os logs da Twitch"),
    "welcome_channel_id": ("Canal de boas-vindas", "channel", WELCOME_CHANNEL_ID, "Canal onde chegam os membros novos"),
    "auto_role_id": ("Role automático", "role", AUTO_ROLE_ID, "Role atribuído automaticamente a membros novos"),
    "welcome_message": ("Mensagem de boas-vindas", "text", WELCOME_MESSAGE, "Mensagem de boas-vindas ({user} = menção)"),
    "auto_mod": ("Auto-mod", "bool", AUTO_MOD, "Liga/desliga a moderação automática"),
}


def _gkey(guild_id):
    return f"discord:{guild_id}:config"


async def get_guild_config(guild_id) -> dict:
    """Devolve a config da guild (Redis) fundida com os defaults do env."""
    raw = {}
    try:
        raw = await state.redis_client.hgetall(_gkey(guild_id))
    except Exception as e:
        logger.warning(f"get_guild_config failed: {e}")
    out = {}
    for key, (label, typ, default, help_text) in G_CONFIG_SCHEMA.items():
        out[key] = raw.get(key, default)
    return out


async def get_setting(guild_id, key):
    cfg = await get_guild_config(guild_id)
    return cfg.get(key)


async def set_setting(guild_id, key, value) -> tuple:
    """Define uma config por-guild. Devolve (ok, erro_msg)."""
    if key not in G_CONFIG_SCHEMA:
        return False, "chave desconhecida"
    try:
        await state.redis_client.hset(_gkey(guild_id), key, str(value))
        return True, ""
    except Exception as e:
        logger.warning(f"set_setting failed: {e}")
        return False, str(e)


async def reset_setting(guild_id, key) -> tuple:
    """Volta a config para o default do env. Devolve (ok, erro_msg)."""
    if key not in G_CONFIG_SCHEMA:
        return False, "chave desconhecida"
    try:
        await state.redis_client.hdel(_gkey(guild_id), key)
        return True, ""
    except Exception as e:
        logger.warning(f"reset_setting failed: {e}")
        return False, str(e)


def format_config(cfg: dict) -> str:
    """Texto de listagem da config para uma guild."""
    lines = []
    for key, (label, typ, default, help_text) in G_CONFIG_SCHEMA.items():
        val = cfg.get(key, default)
        lines.append(f"• **{label}** (`{key}`): `{val}`")
    return "\n".join(lines)


# ============ Streamers a acompanhar (logs Twitch) por-guild ============
# Guardado como Redis SET `discord:{guild_id}:twitch_tracked`.
# Sem fallback: se a guild nao configurou nada, nao acompanha ninguem.

def _tkey(guild_id):
    return f"discord:{guild_id}:twitch_tracked"


async def get_twitch_tracked_strict(guild_id) -> list:
    """Só os streamers explicitamente seguidos por esta guild. Sem fallback para o env.

    Uma guild que nunca configurou nada nao recebe contexto de live: as unidades sao
    isoladas e nao ha guild 'casa' que herde os canais do env. Serve tanto para o prompt
    do AI (ai_core.context_streamers) como para mostrar a lista ao utilizador
    (!ai_twitch / /ai_twitch), para que o que ele ve e o que o bot usa nao divirjam.
    """
    try:
        vals = await state.redis_client.smembers(_tkey(guild_id))
        if vals:
            return sorted(vals)
    except Exception as e:
        logger.warning(f"get_twitch_tracked_strict failed: {e}")
    return []


async def add_twitch_tracked(guild_id, login) -> tuple:
    login = login.strip().lower().lstrip("@")
    if not login:
        return False, "nome de canal inválido"
    try:
        await state.redis_client.sadd(_tkey(guild_id), login)
        return True, ""
    except Exception as e:
        logger.warning(f"add_twitch_tracked failed: {e}")
        return False, str(e)


async def rem_twitch_tracked(guild_id, login) -> tuple:
    login = login.strip().lower().lstrip("@")
    if not login:
        return False, "nome de canal inválido"
    try:
        removed = await state.redis_client.srem(_tkey(guild_id), login)
        return removed > 0, ""
    except Exception as e:
        logger.warning(f"rem_twitch_tracked failed: {e}")
        return False, str(e)


async def all_tracked_channels() -> list:
    """União de canais a acompanhar: env + todas as guilds configuradas."""
    channels = set(c.lower() for c in TWITCH_CHANNELS if c)
    try:
        async for key in state.redis_client.scan_iter(match="discord:*:twitch_tracked", count=100):
            vals = await state.redis_client.smembers(key)
            channels.update(vals)
    except Exception as e:
        logger.warning(f"all_tracked_channels scan failed: {e}")
    return sorted(c for c in channels if c)


async def guilds_tracking(login: str) -> list:
    """Guilds que seguem explicitamente este streamer. Fallback: env -> guild principal."""
    login = login.lower().lstrip("@")
    guilds = []
    try:
        async for key in state.redis_client.scan_iter(match="discord:*:twitch_tracked", count=100):
            if await state.redis_client.sismember(key, login):
                gid = key.split(":")[1]
                try:
                    guilds.append(int(gid))
                except ValueError:
                    pass
    except Exception as e:
        logger.warning(f"guilds_tracking scan failed: {e}")
    if not guilds and login in (c.lower() for c in TWITCH_CHANNELS):
        guilds.append(GUILD_ID)
    return guilds