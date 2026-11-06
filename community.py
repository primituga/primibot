import json
import time
import random
import asyncio
import logging
import datetime as dt

import state
from config import TWITCH_CHANNELS
from twitch_api import twitch_helix, get_broadcaster_id
from helpers import _twitch_throttle

logger = logging.getLogger("AIBot")

_timers_started = False
_timer_last = {}


def _mod(ctx):
    return bool(ctx.author.is_mod or ctx.author.is_broadcaster)


# ============ Comandos personalizados (por canal) ============

def _ck(ch, suffix):
    return f"twitch:{ch.lower()}:{suffix}"


async def add_custom_cmd(ch, name, response):
    await state.redis_client.hset(_ck(ch, "customcmds"), name.lower(), response[:400])


async def del_custom_cmd(ch, name):
    return await state.redis_client.hdel(_ck(ch, "customcmds"), name.lower()) > 0


async def resolve_custom_cmd(ch, name):
    try:
        return await state.redis_client.hget(_ck(ch, "customcmds"), name.lower())
    except Exception:
        return None


async def list_custom_cmds(ch):
    try:
        return await state.redis_client.hgetall(_ck(ch, "customcmds")) or {}
    except Exception:
        return {}


# ============ Timers (por canal; só disparam se esse canal estiver live) ============

async def timer_add(ch, minutes, message):
    tid = str(int(time.time()))
    await state.redis_client.hset(_ck(ch, "timers"), tid, json.dumps({"interval": minutes, "message": message[:400]}))
    return tid


async def timer_del(ch, tid):
    return await state.redis_client.hdel(_ck(ch, "timers"), tid) > 0


async def timers_loop(bot):
    while True:
        try:
            for ch_name in TWITCH_CHANNELS:
                st = await state.redis_client.get(f"twitch:{ch_name.lower()}:stream_status")
                if st != "online":
                    continue
                ch_obj = bot.get_channel(ch_name)
                if not ch_obj:
                    continue
                timers = await state.redis_client.hgetall(_ck(ch_name, "timers")) or {}
                now = time.time()
                for tid, raw in timers.items():
                    t = json.loads(raw)
                    k = (ch_name.lower(), tid)
                    if now - _timer_last.get(k, 0) >= t["interval"] * 60:
                        _timer_last[k] = now
                        await _twitch_throttle(ch_name)
                        await ch_obj.send(t["message"][:450])
        except Exception as e:
            logger.warning(f"Timers loop: {e}")
        await asyncio.sleep(30)


# ============ Shoutout / Uptime ============

async def shoutout_text(target):
    target = target.lstrip("@").strip()
    if not target:
        return None
    u = await twitch_helix(path="users", params={"login": target})
    d = u.get("data", [])
    if not d:
        return None
    disp = d[0].get("display_name", target)
    c = await twitch_helix(path="channels", params={"broadcaster_id": d[0]["id"]})
    cd = c.get("data", [])
    game = cd[0].get("game_name", "") if cd else ""
    title = cd[0].get("title", "") if cd else ""
    txt = f"🡒 Vão seguir {disp}! twitch.tv/{target}"
    if game:
        txt += f" Joga {game}!"
    if title:
        txt += f' — "{title}"'
    return txt


async def uptime_text(ch=""):
    if ch:
        bid_row = await twitch_helix(path="users", params={"login": ch})
        rows = bid_row.get("data", [])
        bid = rows[0]["id"] if rows else ""
    else:
        bid = await get_broadcaster_id()
    if not bid:
        return "❓ Sem ID do broadcaster."
    data = await twitch_helix(path="streams", params={"user_id": bid})
    d = data.get("data", [])
    if not d:
        return "⚪ Stream offline."
    started = dt.datetime.fromisoformat(d[0]["started_at"].replace("Z", "+00:00"))
    secs = int((dt.datetime.now(dt.timezone.utc) - started).total_seconds())
    h, m = secs // 3600, (secs % 3600) // 60
    return f"⏱️ Live há {h}h{m:02d}m."


# ============ Quotes (por canal) ============

async def quote_add(ch, text):
    await state.redis_client.rpush(_ck(ch, "quotes"), text[:400])
    return int(await state.redis_client.llen(_ck(ch, "quotes")))


async def quote_get(ch, n=None):
    key = _ck(ch, "quotes")
    total = int(await state.redis_client.llen(key) or 0)
    if not total:
        return None, 0
    idx = random.randrange(total) if n is None else n - 1
    if idx < 0 or idx >= total:
        return None, total
    return await state.redis_client.lindex(key, idx), total


async def quote_del(ch, n):
    key = _ck(ch, "quotes")
    val = await state.redis_client.lindex(key, n - 1)
    if val is None:
        return False
    await state.redis_client.lset(key, n - 1, "__DEL__")
    await state.redis_client.lrem(key, 1, "__DEL__")
    return True


# ============ Death counter (por canal) ============

async def deaths_change(ch, delta=0):
    key = _ck(ch, "deaths")
    if delta:
        return int(await state.redis_client.incrby(key, delta))
    v = await state.redis_client.get(key)
    return int(v or 0)


async def deaths_reset(ch):
    await state.redis_client.set(_ck(ch, "deaths"), 0)


# ============ Giveaway (por canal) ============

async def _ga_get(ch):
    raw = await state.redis_client.get(_ck(ch, "giveaway"))
    return json.loads(raw) if raw else None


async def ga_start(ch, prize):
    await state.redis_client.set(_ck(ch, "giveaway"), json.dumps({"prize": prize[:200], "open": True, "entrants": []}))


async def ga_join(ch, user):
    g = await _ga_get(ch)
    if not g or not g["open"]:
        return "Não há giveaway aberto!"
    if user in g["entrants"]:
        return "Já estás inscrito!"
    g["entrants"].append(user)
    await state.redis_client.set(_ck(ch, "giveaway"), json.dumps(g))
    return f"Inscrito em '{g['prize']}'! ({len(g['entrants'])} participantes)"


async def ga_pick(ch):
    g = await _ga_get(ch)
    if not g or not g["open"]:
        return None
    if not g["entrants"]:
        g["open"] = False
        await state.redis_client.set(_ck(ch, "giveaway"), json.dumps(g))
        return None
    winner = random.choice(g["entrants"])
    g["open"] = False
    await state.redis_client.set(_ck(ch, "giveaway"), json.dumps(g))
    return winner, g["prize"], len(g["entrants"])


async def ga_cancel(ch):
    await state.redis_client.delete(_ck(ch, "giveaway"))


async def ga_status(ch):
    g = await _ga_get(ch)
    if not g or not g["open"]:
        return "Não há giveaway ativo."
    return f"🎁 Giveaway: '{g['prize']}' — {len(g['entrants'])} participantes. !join para entrares!"


# ============ Polls (por canal) ============

async def _poll_get(ch):
    raw = await state.redis_client.get(_ck(ch, "poll"))
    return json.loads(raw) if raw else None


async def poll_open(ch, question, opts):
    await state.redis_client.set(_ck(ch, "poll"), json.dumps({"q": question[:200], "opts": opts, "votes": {}, "open": True}))


async def poll_vote(ch, user, n):
    p = await _poll_get(ch)
    if not p or not p["open"]:
        return "Não há votação aberta!"
    if n < 1 or n > len(p["opts"]):
        return f"Opção inválida! Usa 1-{len(p['opts'])}."
    p["votes"][user] = n
    await state.redis_client.set(_ck(ch, "poll"), json.dumps(p))
    return f"Voto registado: {n}. {p['opts'][n-1]}"


async def poll_results(p):
    counts = [0] * len(p["opts"])
    for v in p["votes"].values():
        if 1 <= v <= len(counts):
            counts[v - 1] += 1
    total = sum(counts)
    lines = [f"📊 {p['q']}"]
    for i, (o, c) in enumerate(zip(p["opts"], counts)):
        pct = int(c * 100 / total) if total else 0
        bar = "█" * (pct // 10)
        lines.append(f"{i+1}. {o}: {c} ({pct}%) {bar}")
    lines.append(f"Total: {total} votos")
    return "\n".join(lines)[:480]


async def poll_end(ch):
    p = await _poll_get(ch)
    if not p or not p["open"]:
        return None
    p["open"] = False
    await state.redis_client.set(_ck(ch, "poll"), json.dumps(p))
    return await poll_results(p)


async def poll_status(ch):
    p = await _poll_get(ch)
    if not p or not p["open"]:
        return "Não há votação ativa."
    opts = " | ".join(f"{i+1}.{o}" for i, o in enumerate(p["opts"]))
    return f"📊 {p['q']} → {opts} (vota com !vote <número>)"


# ============ Queue (por canal) ============

async def _q_get(ch):
    raw = await state.redis_client.get(_ck(ch, "queue"))
    return json.loads(raw) if raw else {"open": False, "list": []}


async def q_open(ch, open_):
    q = await _q_get(ch)
    q["open"] = open_
    await state.redis_client.set(_ck(ch, "queue"), json.dumps(q))


async def q_join(ch, user):
    q = await _q_get(ch)
    if not q["open"]:
        return "A fila está fechada!"
    if user in q["list"]:
        return f"Já estás na fila (posição {q['list'].index(user)+1})!"
    q["list"].append(user)
    await state.redis_client.set(_ck(ch, "queue"), json.dumps(q))
    return f"Na fila em {len(q['list'])}º!"


async def q_leave(ch, user):
    q = await _q_get(ch)
    if user in q["list"]:
        q["list"].remove(user)
        await state.redis_client.set(_ck(ch, "queue"), json.dumps(q))
        return "Saíste da fila."
    return "Não estás na fila."


async def q_next(ch):
    q = await _q_get(ch)
    if not q["list"]:
        return None
    user = q["list"].pop(0)
    await state.redis_client.set(_ck(ch, "queue"), json.dumps(q))
    return user, len(q["list"])


async def q_clear(ch):
    await state.redis_client.set(_ck(ch, "queue"), json.dumps({"open": False, "list": []}))


async def q_list(ch):
    q = await _q_get(ch)
    if not q["list"]:
        return "Fila vazia." + (" (aberta)" if q["open"] else "")
    head = ", ".join(q["list"][:10])
    extra = f" … +{len(q['list'])-10}" if len(q["list"]) > 10 else ""
    return f"Fila ({len(q['list'])}): {head}{extra}"
