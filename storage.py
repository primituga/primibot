import json
import logging
import asyncio
import state
import scope
from config import MAX_HISTORY, RATE_LIMIT_COOLDOWN, RATE_LIMIT_HOURLY, FAQ_TEXT

logger = logging.getLogger("AIBot")

# Lock curto só para o read-modify-write do histórico, por unit. Distinto do lock de
# ai_core (por session_id), que serializa perguntas inteiras.
_history_locks = {}


def _history_lock(unit: str) -> asyncio.Lock:
    return _history_locks.setdefault(unit, asyncio.Lock())


# ─────────────── histórico ───────────────

async def get_history(unit: str) -> list:
    """Retrieve chat history from Redis. Fails soft if Redis is down."""
    try:
        data = await state.redis_client.get(scope.k_history(unit))
        return json.loads(data) if data else []
    except Exception as e:
        logger.warning(f"Redis get_history failed (continuando sem histórico): {e}")
        return []


async def save_history(unit: str, history: list, base: int = 0):
    """Save chat history to Redis with a 24-hour expiration time. Fails soft.

    `base` = len() do histórico tal como foi lido antes de acrescentar as mensagens
    novas. Como o histórico é partilhado por toda a unit (canal Twitch, sala Discord),
    duas perguntas em simultâneo partilham a mesma lista. O lock de ai_core é por
    session_id (por utilizador), pelo que nao chega para evitar a corrida de
    read-modify-write: sem isto, o utilizador B lia a lista antes de A gravar e o save
    de B apagava a mensagem de A. Aqui relemos dentro de um lock por unit e fundimos só
    a cauda nova, preservando o que os outros gravaram entretanto."""
    async with _history_lock(unit):
        try:
            raw = await state.redis_client.get(scope.k_history(unit))
            stored = json.loads(raw) if raw else []
            if not stored:
                stored = history[:base]
            merged = (stored + history[base:])[-MAX_HISTORY:]
            await state.redis_client.set(scope.k_history(unit), json.dumps(merged), ex=86400)
        except Exception as e:
            logger.warning(f"Redis save_history failed: {e}")


async def clear_history(unit: str):
    """Limpa o histórico E o resumo desta unit.

    Antes so apagava history:{sid}: o summary:{sid} sobrevivia ao reset e voltava
    a ser injectado no prompt na conversa seguinte (ver AGENTS.md)."""
    try:
        await state.redis_client.delete(scope.k_history(unit), scope.k_summary(unit))
    except Exception as e:
        logger.warning(f"Redis clear_history failed: {e}")


# ─────────────── rate limit (por unit) ───────────────

async def check_rate_limit(unit: str, user_key: str):
    """
    Sliding cooldown + hourly cap per user, scoped to one unit.
    Returns None if allowed, or a friendly rejection message.
    Fails OPEN if Redis is down (bot keeps working, just unthrottled).
    """
    try:
        cd_key = scope.k_rl_cooldown(unit, user_key)
        if await state.redis_client.set(cd_key, "1", ex=RATE_LIMIT_COOLDOWN, nx=True):
            h_key = scope.k_rl_hourly(unit, user_key)
            count = await state.redis_client.incr(h_key)
            if count == 1:
                await state.redis_client.expire(h_key, 3600)
            if count > RATE_LIMIT_HOURLY:
                ttl = await state.redis_client.ttl(h_key)
                mins = max(1, (ttl or 3600) // 60)
                return f"🚫 Hourly limit reached ({RATE_LIMIT_HOURLY} requests/hour). Try again in ~{mins} min."
            return None
        ttl = await state.redis_client.ttl(cd_key)
        return f"⏳ Slow down! Try again in {max(ttl or 1, 1)}s."
    except Exception as e:
        logger.warning(f"Rate limit check failed (fail-open): {e}")
        return None


# ─────────────── stats (por unit + global) ───────────────

async def bump_stats(unit: str, user: str, model: str):
    """Incrementa os contadores da unit e os globais. Fails soft."""
    try:
        pipe = state.redis_client.pipeline()
        for scope_id in {unit, scope.GLOBAL}:
            sk = scope.k_stats(scope_id)
            pipe.hincrby(sk, "total", 1)
            pipe.hincrby(sk, f"model:{model}", 1)
            if user:
                pipe.hincrby(sk, f"user:{user}", 1)
        await pipe.execute()
    except Exception as e:
        logger.warning(f"Stats bump failed: {e}")


async def _top(h: dict, prefix: str, n: int = 3):
    items = [(k[len(prefix):], int(v)) for k, v in (h or {}).items() if k.startswith(prefix)]
    items.sort(key=lambda x: -x[1])
    return items[:n]


async def _read_stats(unit: str) -> dict:
    try:
        h = await state.redis_client.hgetall(scope.k_stats(unit)) or {}
        total = int(h.get("total") or 0)
        return {
            "total": total,
            "users": await _top(h, "user:"),
            "models": await _top(h, "model:"),
        }
    except Exception as e:
        logger.warning(f"Stats read failed: {e}")
        return {"total": 0, "users": [], "models": []}


async def get_stats(unit: str) -> dict:
    """Stats da unit e as globais. Nunca levanta: devolve zeros em falha."""
    return {"unit": await _read_stats(unit), "global": await _read_stats(scope.GLOBAL)}


async def format_stats_block(stats: dict, platform: str, label: str) -> str:
    """Renderiza stats para o bot. Usa-se daqui para fora para manter o formato
    identico entre Discord e Twitch."""
    total = stats.get("total", 0)
    if not total:
        return f"📊 Ainda não há pedidos aqui — sê o primeiro a usar !ai!"
    top_users = ", ".join(f"{u} ({n})" for u, n in stats.get("users", [])) or "—"
    top_models = ", ".join(f"{m} ({n})" for m, n in stats.get("models", [])) or "—"
    if platform == "twitch":
        return f"📊 [{label}] {total} pedidos · Top: {top_users} · Modelos: {top_models}"
    return (
        f"📊 **Stats — {label}**\n"
        f"• Total requests: **{total}**\n"
        f"• 🏆 Top chatters: {top_users}\n"
        f"• 🤖 Top models: {top_models}"
    )


# ─────────────── memória (por unit + user) ───────────────

async def get_memory(unit: str, user_key: str) -> list:
    try:
        data = await state.redis_client.get(scope.k_memory(unit, user_key))
        return json.loads(data) if data else []
    except Exception:
        return []


async def add_memory(unit: str, user_key: str, fact: str) -> bool:
    try:
        mem = await get_memory(unit, user_key)
        mem.append(fact)
        await state.redis_client.set(scope.k_memory(unit, user_key), json.dumps(mem[-20:]), ex=86400 * 30)
        return True
    except Exception as e:
        logger.warning(f"add_memory failed: {e}")
        return False


async def clear_memory(unit: str, user_key: str) -> bool:
    try:
        await state.redis_client.delete(scope.k_memory(unit, user_key))
        return True
    except Exception as e:
        logger.warning(f"clear_memory failed: {e}")
        return False


# ─────────────── persona (por unit) ───────────────

async def get_persona(unit: str) -> str:
    try:
        return await state.redis_client.get(scope.k_persona(unit)) or ""
    except Exception:
        return ""


async def set_persona(unit: str, text: str) -> bool:
    try:
        if text:
            await state.redis_client.set(scope.k_persona(unit), text[:2000], ex=86400 * 30)
        else:
            await state.redis_client.delete(scope.k_persona(unit))
        return True
    except Exception as e:
        logger.warning(f"set_persona failed: {e}")
        return False


# ─────────────── FAQ (por unit, com semente global) ───────────────

async def get_faq(unit: str) -> str:
    """FAQ da unit. Se a unit nao tiver, cai na semente do faq.txt global."""
    try:
        v = await state.redis_client.get(scope.k_faq(unit))
        if v:
            return v
    except Exception:
        pass
    return FAQ_TEXT


async def set_faq(unit: str, text: str) -> bool:
    try:
        if text:
            await state.redis_client.set(scope.k_faq(unit), text[:4000], ex=86400 * 90)
        else:
            await state.redis_client.delete(scope.k_faq(unit))
        return True
    except Exception as e:
        logger.warning(f"set_faq failed: {e}")
        return False


async def clear_faq(unit: str) -> bool:
    try:
        await state.redis_client.delete(scope.k_faq(unit))
        return True
    except Exception as e:
        logger.warning(f"clear_faq failed: {e}")
        return False


# ─────────────── saúde dos providers ───────────────
# Global E por unit. O global e o que dispara alertas: uma key do Groq ou uma queda
# do Flowise e uma unica falha para as duas plataformas. O por-unit serve para ver
# que plataforma esta a generar o erro (ver debug_report).

async def health_ok(unit: str, provider: str):
    try:
        pipe = state.redis_client.pipeline()
        pipe.incr(f"health:{provider}:ok")
        pipe.incr(scope.k_health(unit, provider, "ok"))
        await pipe.execute()
    except Exception:
        pass


async def health_fail(unit: str, provider: str):
    try:
        pipe = state.redis_client.pipeline()
        pipe.incr(f"health:{provider}:fail")
        pipe.incr(scope.k_health(unit, provider, "fail"))
        await pipe.execute()
    except Exception:
        pass


async def set_last_error(text: str):
    try:
        await state.redis_client.set("debug:last_error", str(text)[:2000], ex=86400 * 7)
    except Exception:
        pass


# ─────────────── sumarização de histórico antigo ───────────────

async def get_session_summary(unit: str) -> str:
    try:
        return await state.redis_client.get(scope.k_summary(unit)) or ""
    except Exception:
        return ""


async def set_session_summary(unit: str, text: str):
    try:
        await state.redis_client.set(scope.k_summary(unit), text, ex=86400)
    except Exception:
        pass
