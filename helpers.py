import asyncio
import random
import time
import state
from config import (
    DISCORD_PREFIX_COMMANDS, TWITCH_CMD_INFO,
    TWITCH_MIN_MSG_GAP, TWITCH_MAX_RESPONSES, TWITCH_HUMANIZED,
    TWITCH_MIN_TYPING, TWITCH_MAX_TYPING,
)


async def human_typing():
    """Pequeno delay variável antes de responder, simulando tempo de escrita."""
    if not TWITCH_HUMANIZED:
        return
    await asyncio.sleep(random.uniform(TWITCH_MIN_TYPING, TWITCH_MAX_TYPING))


def build_discord_help() -> str:
    lines = ["📚 **Comandos do primiBOT**"]
    for name, (desc, args, mod) in DISCORD_PREFIX_COMMANDS.items():
        argtxt = f" {args}" if args else ""
        lines.append(f"• `!{name}{argtxt}` — {desc}" + (" (mods)" if mod else ""))
    try:
        slashes = [f"`/{c.name}`" for c in state.discord_client.tree.get_commands()]
        if slashes:
            lines.append("Slash: " + ", ".join(slashes))
    except Exception:
        pass
    return "\n".join(lines)


def build_twitch_help(bot) -> str:
    bits = []
    for name in sorted(bot.commands.keys()):
        desc, args, mod = TWITCH_CMD_INFO.get(name, ("", "", False))
        argtxt = f" {args}" if args else ""
        label = f"!{name}{argtxt}"
        bits.append(f"{label} — {desc}" + (" (mods)" if mod else "") if desc else label)
    return "📚 Comandos: " + " • ".join(bits)


def format_stats(stats: dict, platform: str) -> str:
    """NAO usar. get_stats(unit) devolve {"unit":…, "global":…}; esta funcao espera
    o dict cru de uma unit. Passar o resultado de get_stats aqui daria zeros em
    silencio. Os handlers chamam storage.format_stats_block directamente."""
    from storage import format_stats_block
    return format_stats_block(stats or {}, platform, "global")


async def send_discord_chunks(channel, text: str):
    """Send long messages to Discord by splitting them safely at line breaks or spaces."""
    if not text: return
    chunk_size = 1900
    while len(text) > 0:
        if len(text) <= chunk_size:
            await channel.send(text)
            break
        split_at = text.rfind('\n', 0, chunk_size)
        if split_at == -1: split_at = text.rfind(' ', 0, chunk_size)
        if split_at == -1: split_at = chunk_size
        chunk = text[:split_at]
        await channel.send(chunk)
        text = text[split_at:].lstrip()


async def send_twitch_chunks(channel, author_name: str, text: str):
    """Send long messages to Twitch safely, preventing spam bans.
    Aplica humanização (delay variável entre mensagens + taxa máxima por canal)."""
    if not text: return
    first_message = True
    while len(text) > 0:
        await _twitch_throttle(channel.name if hasattr(channel, "name") else "")
        prefix = f"@{author_name}: " if first_message else ""
        chunk_size = 480 - len(prefix) if first_message else 480
        if len(text) <= chunk_size:
            await channel.send(f"{prefix}{text}")
            break
        split_at = text.rfind(' ', 0, chunk_size)
        if split_at == -1: split_at = chunk_size
        chunk = text[:split_at]
        await channel.send(f"{prefix}{chunk}")
        text = text[split_at:].lstrip()
        first_message = False
        await asyncio.sleep(1.5)  # Crucial to avoid Twitch chat bans


_tw_evt = {}  # ch -> [(timestamp, resposta)]  ... janela deslizante de 60s


async def _twitch_throttle(channel: str):
    """Limita o ritmo de envio do bot por canal, imitando um humano.

    - Não envia mais do que TWITCH_MAX_RESPONSES em 60s por canal.
    - Garante pelo menos TWITCH_MIN_MSG_GAP*s entre respostas consecutivas
      (com pequena variação aleatória para não parecer um timer exato).
    """
    if not TWITCH_HUMANIZED:
        return
    now = time.monotonic()
    key = channel or "_"
    hist = _tw_evt.setdefault(key, [])
    hist[:] = [t for t in hist if now - t < 60.0]

    if len(hist) >= TWITCH_MAX_RESPONSES:
        wait = 60.0 - (now - hist[0]) + random.uniform(0.3, 1.5)
        if wait > 0:
            await asyncio.sleep(wait)
        now = time.monotonic()
        _tw_evt[key] = []

    if hist:
        gap = TWITCH_MIN_MSG_GAP + random.uniform(0.2, 1.0)
        elapsed = now - hist[-1]
        if elapsed < gap:
            await asyncio.sleep(gap - elapsed)

    _tw_evt.setdefault(key, []).append(time.monotonic())
