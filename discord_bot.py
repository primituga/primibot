import time
import asyncio
import random
import base64
import logging
from datetime import datetime, timedelta

import discord
from discord.ext import commands as d_commands
import state
import scope
from config import (
    GUILD_ID, AUTO_MOD, LOG_CHANNEL_ID, WELCOME_CHANNEL_ID, WELCOME_MESSAGE, AUTO_ROLE_ID,
    RPS_DISCORD_TIMEOUT, IMAGE_NEEDS_COMMAND, MAX_IMAGE_BYTES,
)
import guild_config
from storage import (
    clear_history, check_rate_limit, get_stats, add_memory, clear_memory, set_persona,
    set_faq, clear_faq, get_faq, format_stats_block,
)
from notify import log_event, send_webhook
from ai_core import ask_ai_logic, debug_report
from safety import analyze_message_safety
import suspects
from games import play_rps, eight_ball, slots_spin
from twitch_api import live_status_text
from helpers import send_discord_chunks, build_discord_help

logger = logging.getLogger("AIBot")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
discord_client = d_commands.Bot(command_prefix="!", intents=intents, help_command=None)


@discord_client.event
async def on_ready():
    state._log_channels = {}
    scope.clear_guild_cache()
    logger.info(f"Discord Bot Online: {discord_client.user}")
    if GUILD_ID:
        try:
            guild = discord.Object(id=GUILD_ID)
            # tree.command() regista em _global_commands; sync(guild=...) sobe
            # _guild_commands (vazio) e apaga o que a guild tinha. Sem esta copia
            # os slash commands nunca aparecem na guild.
            discord_client.tree.copy_global_to(guild=guild)
            synced = await discord_client.tree.sync(guild=guild)
            logger.info(f"Slash commands sync'd for guild: {[c.name for c in synced]}")
        except Exception as e:
            logger.warning(f"Slash sync failed: {e}")
    if LOG_CHANNEL_ID:
        await log_event("🟢 Bot online", f"{discord_client.user} ligado e pronto!", color=0x00ff00)
    await send_webhook("🟢 **primiBOT online**")


def _unit(message) -> str:
    """Unit desta sala: discord:{guild_id}:{channel_id}. DMs usam guild 'dm'."""
    return scope.discord_unit(getattr(message.guild, "id", None), message.channel.id)


def _user_key(author) -> str:
    """Identificador estavel do utilizador. O id nao muda, o display_name muda."""
    return str(author.id)


async def handle_ai_question_discord(message, q, base64_image=None):
    """Common pipeline for any AI question on Discord (streaming UX)."""
    session_id = f"discord_{message.channel.id}"
    rl = await check_rate_limit(_unit(message), _user_key(message.author))
    if rl:
        await message.channel.send(rl)
        return
    stream_cb = None
    draft = None
    if base64_image is None:
        draft = await message.channel.send("🤔")
        buf = []
        last = 0.0

        async def on_chunk(t):
            nonlocal last
            buf.append(t)
            now = time.monotonic()
            if now - last >= 1.2:
                last = now
                try:
                    await draft.edit(content="".join(buf)[:1950])
                except Exception:
                    pass
        stream_cb = on_chunk
    async with message.channel.typing():
        ans = await ask_ai_logic(q, session_id, platform="discord", base64_image=base64_image,
                                 user=message.author.display_name,
                                 user_key=_user_key(message.author), stream_cb=stream_cb)
    if draft is not None:
        try:
            await draft.edit(content=ans[:1950])
        except Exception:
            pass
        if len(ans) > 1950:
            await send_discord_chunks(message.channel, ans[1950:])
    else:
        await send_discord_chunks(message.channel, ans)


@discord_client.event
async def on_message(message):
    if message.author.bot: return

    # Scan de links maliciosos (URLhaus)
    try:
        hits = await suspects.scan_links("discord", f"{message.guild.name} #{message.channel}" if message.guild else "DM", str(message.author), message.content)
        if hits and await suspects.discord_autodelete_allowed(message):
            try:
                await message.delete()
                await message.author.send("⚠️ A tua mensagem foi removida por conter um link malicioso. Se achas que foi engano, contacta um mod.")
            except Exception:
                pass
    except Exception as e:
        logger.warning(f"Link scan failed: {e}")

    # Auto-mod (por-guild)
    try:
        cfg = await guild_config.get_guild_config(message.guild.id) if message.guild else {}
        auto_mod = str(cfg.get("auto_mod", AUTO_MOD)).lower() in ("1", "true", "yes", "sim")
    except Exception:
        auto_mod = AUTO_MOD
    if auto_mod:
        reason = analyze_message_safety(message.content)
        if reason:
            try:
                await message.delete()
                await message.author.send(f"⚠️ A tua mensagem foi removida por conter algo suspeito: **{reason}**. Se achas que foi engano, contacta um mod.")
            except Exception:
                pass
            await log_event("🛡️ Auto-mod (Discord)", f"**{message.author}** em #{message.channel}: {message.content[:300]}\nMotivo: {reason}", color=0xff0000, guild_id=message.guild.id if message.guild else None)
            return

    session_id = f"discord_{message.channel.id}"
    unit = _unit(message)
    low = message.content.lower()

    if message.content == "!ai_reset":
        perms = message.author.guild_permissions
        if not (perms.manage_messages or perms.administrator):
            await message.channel.send("🚫 Only moderators can clear my memory!")
            return
        await clear_history(unit)
        await message.channel.send("🧹 Memory cleared successfully!")
        return

    if message.content == "!ai_credits":
        await message.channel.send("Hello, I am primiBOT, an AI chat bot for Discord and Twitch.")
        return

    if message.content in ("!help", "!ai_help"):
        await send_discord_chunks(message.channel, build_discord_help())
        return

    if low.startswith("!ai_config"):
        await handle_ai_config_command(message)
        return

    if low.startswith("!ai_twitch"):
        await handle_ai_twitch_command(message)
        return

    if low.startswith("!ai_persona"):
        perms = message.author.guild_permissions
        if not (perms.manage_messages or perms.administrator):
            await message.channel.send("🚫 Only moderators can change my persona!")
            return
        text = message.content[len("!ai_persona"):].strip()
        ok = await set_persona(unit, text)
        if text:
            await message.channel.send("🧠 Personalidade atualizada!" if ok else "⚠️ Falha ao guardar.")
        else:
            await message.channel.send("🧠 Personalidade limpa (voltei ao default)!")
        return

    if low.startswith("!ai_faq"):
        perms = message.author.guild_permissions
        if not (perms.manage_messages or perms.administrator):
            await message.channel.send("🚫 Only moderators!")
            return
        parts = message.content.split(" ", 2)
        sub = parts[1].lower() if len(parts) > 1 else ""
        if sub == "set":
            if len(parts) < 3:
                await message.channel.send("Uso: `!ai_faq set <texto>`")
                return
            ok = await set_faq(unit, parts[2].strip())
            await message.channel.send("📚 FAQ desta sala guardada!" if ok else "⚠️ Falha ao guardar.")
            return
        await clear_faq(unit)
        await message.channel.send("📚 FAQ desta sala limpa (voltei ao FAQ global).")
        return

    if message.content == "!debug":
        perms = message.author.guild_permissions
        if not (perms.manage_messages or perms.administrator):
            await message.channel.send("🚫 Only moderators!")
            return
        await send_discord_chunks(message.channel, await debug_report(unit))
        return

    if message.content == "!ai_stats":
        stats = await get_stats(unit)
        await send_discord_chunks(message.channel, format_stats_block(stats["unit"], "discord", "nesta sala"))
        if stats["global"]["total"]:
            await send_discord_chunks(
                message.channel,
                format_stats_block(stats["global"], "discord", f"global · `{unit}`"))
        return

    if low.startswith("!ai_remember "):
        fact = message.content[len("!ai_remember "):].strip()
        if not fact: return
        ok = await add_memory(unit, _user_key(message.author), fact)
        await message.channel.send("🧠 Vou lembrar-me disso!" if ok else "⚠️ Não consegui guardar (Redis indisponível).")
        return

    if message.content == "!ai_forget":
        await clear_memory(unit, _user_key(message.author))
        await message.channel.send("🧠 Memória sobre ti apagada!")
        return

    if low.startswith("!ai_pick "):
        opts = message.content[len("!ai_pick "):].strip()
        if not opts: return
        choices = [o.strip() for o in opts.split(",") if o.strip()]
        if not choices:
            await message.channel.send("Opções separadas por vírgula: `!ai_pick a, b, c`")
            return
        await message.channel.send(f"🎯 {random.choice(choices)}")
        return

    if low == "!ai_joke":
        await handle_ai_question_discord(message, "Conta uma piada curta e engraçada.")
        return

    if low.startswith("!ai_roast"):
        target = message.content[len("!ai_roast "):].strip() if low.startswith("!ai_roast ") else message.author.display_name
        await handle_ai_question_discord(message, f"Faz um roast engraçado e carinhoso de {target} em 2 frases.")
        return

    if low == "!ai_live":
        await message.channel.send(await live_status_text())
        return

    if low.startswith("!rps "):
        text, outcome = play_rps(message.content[len("!rps "):].strip())
        if outcome == "lose":
            is_staff = message.author.guild_permissions.manage_messages or message.author.guild_permissions.administrator or message.author.guild_permissions.moderate_members
            if RPS_DISCORD_TIMEOUT and not is_staff:
                try:
                    await message.author.timeout(datetime.now() + timedelta(seconds=30), reason="Perdeu no RPS 😈")
                    text += " ⏰ Timeout 30s!"
                except Exception:
                    text += " 😅 Sem permissão para timeout aqui."
            else:
                text += " 😅 Sem timeout aqui!"
        await message.channel.send(text)
        return

    if low.startswith("!coin"):
        guess = message.content[len("!coin"):].strip().lower()
        result = random.choice(["cara", "coroa"])
        line = f"🪙 {result.capitalize()}!"
        if guess in ("cara", "coroa"):
            line += " 🎯 Acertaste!" if guess == result else " ❌ Erraste!"
        await message.channel.send(line)
        return

    if low == "!dice":
        await message.channel.send(f"🎲 {random.randint(1, 6)}")
        return

    if low == "!roll":
        await message.channel.send(f"🎲 {random.randint(1, 100)}")
        return

    if low.startswith("!8ball "):
        q = message.content[len("!8ball "):].strip()
        if not q: return
        await message.channel.send(f"🎱 {eight_ball()}")
        return

    if low == "!slots":
        await message.channel.send(slots_spin())
        return

    if low.startswith("!ai ") and not message.attachments:
        q = message.content[4:].strip()
        if not q: return
        await handle_ai_question_discord(message, q)

    elif message.attachments:
        attachment = message.attachments[0]
        if any(attachment.filename.lower().endswith(ext) for ext in ['.png', '.jpg', '.jpeg', '.webp']):
            if IMAGE_NEEDS_COMMAND and not message.content.lower().startswith("!ai "):
                return
            if attachment.size > MAX_IMAGE_BYTES:
                await message.channel.send(f"⚠️ Image too large ({attachment.size // (1024*1024) + 1}MB). Maximum is 5MB.")
                return
            async with message.channel.typing():
                image_bytes = await attachment.read()
                base64_image = base64.b64encode(image_bytes).decode('utf-8')
                q = message.content[4:].strip() if message.content.lower().startswith("!ai ") else message.content
                await handle_ai_question_discord(message, q, base64_image=base64_image)


# --- Discord: logging de eventos ---
async def _is_discord_mod(author) -> bool:
    perms = author.guild_permissions
    return bool(perms.manage_messages or perms.manage_guild or perms.administrator)


def _parse_config_value(message, key, value):
    """Converte o valor conforme o tipo do schema. Devolve (ok, parsed, msg)."""
    meta = guild_config.G_CONFIG_SCHEMA.get(key)
    if not meta:
        return False, None, "Chave desconhecida. Usa `!ai_config` para ver a lista."
    typ = meta[1]
    v = value.strip()
    if typ == "channel":
        import re as _re
        m = _re.search(r"<#(\d+)>", v)
        if m:
            return True, int(m.group(1)), ""
        if v.isdigit():
            return True, int(v), ""
        return False, None, "Valor deve ser `#canal` ou o ID do canal."
    if typ == "role":
        import re as _re
        m = _re.search(r"<@&(\d+)>", v)
        if m:
            return True, int(m.group(1)), ""
        if v.isdigit():
            return True, int(v), ""
        return False, None, "Valor deve ser `@role` ou o ID do role."
    if typ == "bool":
        if v.lower() in ("1", "true", "yes", "sim", "on", "ligado"):
            return True, True, ""
        if v.lower() in ("0", "false", "no", "nao", "off", "desligado"):
            return True, False, ""
        return False, None, "Valor deve ser `true` ou `false`."
    if typ == "text":
        return True, v, ""
    return False, None, "Tipo não suportado."


async def handle_ai_config_command(message):
    if not message.guild:
        await message.channel.send("⚠️ Este comando só funciona num servidor Discord.")
        return
    if not await _is_discord_mod(message.author):
        await message.channel.send("🚫 Só mods/admins podem configurar o bot!")
        return
    gid = message.guild.id
    parts = message.content.split(" ", 2)
    sub = parts[1].lower() if len(parts) > 1 else ""

    if sub == "set":
        if len(parts) < 3:
            await message.channel.send("Uso: `!ai_config set <chave> <valor>`")
            return
        key, value = parts[2].split(" ", 1) if " " in parts[2] else (parts[2], "")
        ok, parsed, msg = _parse_config_value(message, key, value)
        if not ok:
            await message.channel.send(f"❌ {msg}")
            return
        ok2, err = await guild_config.set_setting(gid, key, parsed)
        if ok2:
            await message.channel.send(f"✅ `{key}` definido para `{parsed}` neste servidor.")
        else:
            await message.channel.send(f"⚠️ Erro ao guardar: {err}")
        return

    if sub == "reset":
        if len(parts) < 3:
            await message.channel.send("Uso: `!ai_config reset <chave>`")
            return
        ok, err = await guild_config.reset_setting(gid, parts[2])
        await message.channel.send(f"✅ `{parts[2]}` voltou ao default do env." if ok else f"⚠️ {err}")
        return

    if sub == "wizard":
        await _ai_config_wizard(message)
        return

    cfg = await guild_config.get_guild_config(gid)
    header = f"⚙️ **Config deste servidor** (`{message.guild.name}`)\n"
    footer = "\n\nComandos:\n- `!ai_config set <chave> <valor>`\n- `!ai_config reset <chave>`\n- `!ai_config wizard`"
    await send_discord_chunks(message.channel, header + guild_config.format_config(cfg) + footer)


async def _ai_config_wizard(message):
    """Percorre cada chave perguntando o valor; 'pular' mantém, 'sair' termina."""
    gid = message.guild.id
    for key, (label, typ, default, help_text) in guild_config.G_CONFIG_SCHEMA.items():
        cfg = await guild_config.get_guild_config(gid)
        current = cfg.get(key, default)
        await message.channel.send(
            f"**{label}** (`{key}`) — {help_text}\nAtual: `{current}`\n"
            f"Envia o novo valor, `pular` para manter, ou `sair` para terminar. (timeout 45s)"
        )
        try:
            reply = await discord_client.wait_for(
                "message",
                check=lambda m: m.author == message.author and m.channel == message.channel,
                timeout=45.0,
            )
        except asyncio.TimeoutError:
            await message.channel.send("⏰ Tempo esgotado. Wizard terminado.")
            return
        rv = reply.content.strip()
        if rv.lower() in ("sair", "exit", "cancelar"):
            await message.channel.send("🚪 Wizard terminado.")
            return
        if rv.lower() in ("pular", "skip"):
            continue
        ok, parsed, msg = _parse_config_value(message, key, rv)
        if not ok:
            await message.channel.send(f"❌ {msg} (a definir `{key}` novamente)")
            continue
        ok2, err = await guild_config.set_setting(gid, key, parsed)
        if not ok2:
            await message.channel.send(f"⚠️ Erro: {err}")
    await message.channel.send("✅ Configuração concluída!")
    cfg = await guild_config.get_guild_config(gid)
    await send_discord_chunks(message.channel, "⚙️ **Config final:**\n" + guild_config.format_config(cfg))


async def handle_ai_twitch_command(message):
    if not message.guild:
        await message.channel.send("⚠️ Este comando só funciona num servidor Discord.")
        return
    gid = message.guild.id
    parts = message.content.split(" ", 2)
    sub = parts[1].lower() if len(parts) > 1 else ""

    if sub in ("add", "rem", "del", "remove"):
        if not await _is_discord_mod(message.author):
            await message.channel.send("🚫 Só mods/admins podem gerir os streamers!")
            return
        if len(parts) < 3:
            await message.channel.send(f"Uso: `!ai_twitch {sub} <streamer>`")
            return
        login = parts[2].strip().lower().lstrip("@")
        if sub == "add":
            ok, err = await guild_config.add_twitch_tracked(gid, login)
            await message.channel.send(f"✅ `{login}` adicionado aos logs Twitch deste servidor." if ok else f"⚠️ {err}")
        else:
            ok, err = await guild_config.rem_twitch_tracked(gid, login)
            await message.channel.send(f"✅ `{login}` removido dos logs Twitch." if ok else f"❌ `{login}` não estava na lista.")
        return

    # Lista estrita: e exactamente o que entra no prompt, sem fallback do env.
    tracked = await guild_config.get_twitch_tracked_strict(gid)
    if not tracked:
        await message.channel.send(
            "Sem streamers configurados neste servidor — o bot não injecta contexto "
            "de live nesta sala.\nUsa `!ai_twitch add <streamer>` para adicionar um."
        )
        return
    await message.channel.send(f"📺 **Streamers com log Twitch neste servidor:**\n- " + "\n- ".join(f"`{t}`" for t in tracked))


@discord_client.event
async def on_member_join(member):
    await log_event("✅ Entrou no servidor", f"**{member.mention}** (`{member}`) entrou em **{member.guild.name}** · conta criada em {member.created_at.strftime('%d/%m/%Y')}", color=0x00ff00, guild_id=member.guild.id)
    # Sistema anti-suspeitos (conta nova + DiscordRep)
    try:
        for alert in await suspects.check_discord_user(member):
            await log_event("🕵️ Suspeito (Discord)", f"{alert}\nServidor: **{member.guild.name}**", color=0xff8800, guild_id=member.guild.id)
    except Exception as e:
        logger.warning(f"Suspects check failed (join): {e}")
    try:
        cfg = await guild_config.get_guild_config(member.guild.id)
    except Exception:
        cfg = {}
    auto_role_id = int(cfg.get("auto_role_id") or 0)
    welcome_channel_id = int(cfg.get("welcome_channel_id") or 0)
    welcome_message = cfg.get("welcome_message") or WELCOME_MESSAGE
    if auto_role_id:
        try:
            role = member.guild.get_role(auto_role_id)
            if role:
                await member.add_roles(role)
                await log_event("🎖️ Auto-role", f"**{member}** recebeu {role.mention}", color=0x5865f2, guild_id=member.guild.id)
        except Exception as e:
            logger.warning(f"Auto-role failed: {e}")
    if welcome_channel_id:
        try:
            ch = discord_client.get_channel(welcome_channel_id)
            if ch:
                await ch.send(welcome_message.format(user=member.mention))
        except Exception as e:
            logger.warning(f"Welcome failed: {e}")


@discord_client.event
async def on_member_remove(member):
    await log_event("❌ Saiu do servidor", f"**{member}** (`{member.id}`) saiu de **{member.guild.name}**", color=0xff0000, guild_id=member.guild.id)


@discord_client.event
async def on_member_ban(guild, user):
    await log_event("🔨 Ban", f"**{user}** (`{user.id}`) foi banido de **{guild}**", color=0xff0000, guild_id=guild.id)


@discord_client.event
async def on_member_unban(guild, user):
    await log_event("🔓 Unban", f"**{user}** (`{user.id}`) foi desbanido de **{guild}**", color=0x00ff00, guild_id=guild.id)


@discord_client.event
async def on_message_delete(message):
    if message.author.bot: return
    if not message.content and not message.attachments: return
    gname = f" em **{message.guild.name}**" if message.guild else ""
    att = f" ({message.attachments[0].filename})" if message.attachments else ""
    await log_event("🗑️ Mensagem apagada", f"**{message.author}**#{gname} em #{message.channel}: {message.content[:500]}{att}", color=0xffa500, guild_id=message.guild.id if message.guild else None)


@discord_client.event
async def on_member_update(before, after):
    if before.nick != after.nick and (before.nick or after.nick):
        await log_event("✏️ Nick alterado", f"**{after}** em **{after.guild.name}**: `{before.nick or after.name}` → `{after.nick or after.name}`", color=0x5865f2, guild_id=after.guild.id)
    added_roles = [r.name for r in after.roles if r not in before.roles and r.id != after.guild.id]
    removed_roles = [r.name for r in before.roles if r not in after.roles and r.id != before.guild.id]
    if added_roles:
        await log_event("🛡️ Roles +", f"**{after}** recebeu em **{after.guild.name}**: {', '.join(added_roles)}", color=0x00ff00, guild_id=after.guild.id)
    if removed_roles:
        await log_event("🛡️ Roles −", f"**{after}** perdeu em **{after.guild.name}**: {', '.join(removed_roles)}", color=0xff0000, guild_id=after.guild.id)
    if before.timed_out_until != after.timed_out_until:
        if after.timed_out_until is None:
            await log_event("🛡️ Timeout removido", f"**{after}** saiu de timeout em **{after.guild.name}**", color=0x00ff00, guild_id=after.guild.id)
        else:
            await log_event("🛡️ Timeout", f"**{after}** em timeout em **{after.guild.name}** até {after.timed_out_until.strftime('%d/%m/%Y %H:%M')}", color=0xff0000, guild_id=after.guild.id)


# --- Discord: slash commands ---
if GUILD_ID:
    def _unit_i(interaction) -> str:
        return scope.discord_unit(getattr(interaction.guild, "id", None), interaction.channel_id)

    @discord_client.tree.command(name="ai", description="Pergunta ao primiBOT")
    async def slash_ai(interaction: discord.Interaction, pergunta: str):
        await interaction.response.defer()
        rl = await check_rate_limit(_unit_i(interaction), str(interaction.user.id))
        if rl:
            await interaction.followup.send(rl)
            return
        ans = await ask_ai_logic(pergunta, f"discord_{interaction.channel_id}", platform="discord",
                                 user=interaction.user.display_name,
                                 user_key=str(interaction.user.id))
        await send_discord_chunks(interaction.followup, ans)

    @discord_client.tree.command(name="stats", description="Estatísticas desta sala")
    async def slash_stats(interaction: discord.Interaction):
        await interaction.response.defer()
        unit = _unit_i(interaction)
        stats = await get_stats(unit)
        await send_discord_chunks(interaction.followup, format_stats_block(stats["unit"], "discord", "nesta sala"))
        if stats["global"]["total"]:
            await send_discord_chunks(interaction.followup,
                                      format_stats_block(stats["global"], "discord", f"global · `{unit}`"))

    @discord_client.tree.command(name="ai_reset", description="Limpar a memória do canal (mods)")
    async def slash_reset(interaction: discord.Interaction):
        perms = interaction.user.guild_permissions
        if not (perms.manage_messages or perms.administrator):
            await interaction.response.send_message("🚫 Only moderators can clear my memory!", ephemeral=True)
            return
        await clear_history(_unit_i(interaction))
        await interaction.response.send_message("🧹 Memory cleared successfully!")

    @discord_client.tree.command(name="live", description="Estado da stream")
    async def slash_live(interaction: discord.Interaction):
        await interaction.response.defer()
        await interaction.followup.send(await live_status_text())

    @discord_client.tree.command(name="help", description="Lista de comandos")
    async def slash_help(interaction: discord.Interaction):
        await interaction.response.defer()
        await send_discord_chunks(interaction.followup, build_discord_help())

    @discord_client.tree.command(name="persona", description="Muda a personalidade do bot (mods). Sem texto = limpar")
    async def slash_persona(interaction: discord.Interaction, texto: str = None):
        perms = interaction.user.guild_permissions
        if not (perms.manage_messages or perms.administrator):
            await interaction.response.send_message("🚫 Only moderators!", ephemeral=True)
            return
        unit = _unit_i(interaction)
        if texto:
            ok = await set_persona(unit, texto)
            await interaction.response.send_message("🧠 Personalidade atualizada!" if ok else "⚠️ Falha ao guardar.")
        else:
            await set_persona(unit, "")
            await interaction.response.send_message("🧠 Personalidade limpa (voltei ao default)!")

    @discord_client.tree.command(name="faq", description="FAQ desta sala (mods). Sem texto = limpar")
    async def slash_faq(interaction: discord.Interaction, texto: str = None):
        perms = interaction.user.guild_permissions
        if not (perms.manage_messages or perms.administrator):
            await interaction.response.send_message("🚫 Only moderators!", ephemeral=True)
            return
        if texto:
            ok = await set_faq(_unit_i(interaction), texto)
            await interaction.response.send_message(
                "📚 FAQ desta sala guardada!" if ok else "⚠️ Falha ao guardar.", ephemeral=True)
        else:
            await clear_faq(_unit_i(interaction))
            await interaction.response.send_message(
                "📚 FAQ desta sala limpa (voltei ao FAQ global).", ephemeral=True)

    @discord_client.tree.command(name="config", description="Config do servidor (mods). Sem args lista as configs")
    async def slash_config(interaction: discord.Interaction, chave: str = None, valor: str = None):
        perms = interaction.user.guild_permissions
        if not (perms.manage_messages or perms.manage_guild or perms.administrator):
            await interaction.response.send_message("🚫 Only moderators!", ephemeral=True)
            return
        gid = interaction.guild.id if interaction.guild else GUILD_ID
        if chave and valor:
            ok, parsed, msg = _parse_config_value(interaction, chave, valor)
            if not ok:
                await interaction.response.send_message(f"❌ {msg}", ephemeral=True)
                return
            ok2, err = await guild_config.set_setting(gid, chave, parsed)
            await interaction.response.send_message(
                f"✅ `{chave}` definido para `{parsed}`." if ok2 else f"⚠️ Erro: {err}", ephemeral=True)
            return
        if chave:
            ok, err = await guild_config.reset_setting(gid, chave)
            await interaction.response.send_message(
                f"✅ `{chave}` resetado." if ok else f"⚠️ {err}", ephemeral=True)
            return
        cfg = await guild_config.get_guild_config(gid)
        await interaction.response.defer()
        await send_discord_chunks(interaction.followup, "⚙️ **Config deste servidor**\n" + guild_config.format_config(cfg))

    @discord_client.tree.command(name="twitch", description="Gestão de streamers com log Twitch (mods)")
    async def slash_twitch(interaction: discord.Interaction, acao: str = "list", streamer: str = None):
        perms = interaction.user.guild_permissions
        if not (perms.manage_messages or perms.manage_guild or perms.administrator):
            await interaction.response.send_message("🚫 Only moderators!", ephemeral=True)
            return
        gid = interaction.guild.id if interaction.guild else GUILD_ID
        acao = acao.lower()
        if acao in ("add", "rem", "del", "remove"):
            if not streamer:
                await interaction.response.send_message("❌ Precisas do nome do streamer.", ephemeral=True)
                return
            login = streamer.strip().lower().lstrip("@")
            if acao == "add":
                ok, err = await guild_config.add_twitch_tracked(gid, login)
                await interaction.response.send_message(f"✅ `{login}` adicionado." if ok else f"⚠️ {err}", ephemeral=True)
            else:
                ok, err = await guild_config.rem_twitch_tracked(gid, login)
                await interaction.response.send_message(f"✅ `{login}` removido." if ok else f"❌ `{login}` não estava na lista.", ephemeral=True)
            return
        tracked = await guild_config.get_twitch_tracked_strict(gid)
        if not tracked:
            await interaction.response.send_message(
                "Sem streamers configurados — o bot não injecta contexto de live nesta "
                "sala. Usa `!ai_twitch add <streamer>`.", ephemeral=True)
            return
        await interaction.response.send_message("📺 **Streamers com log Twitch:**\n- " + "\n- ".join(f"`{t}`" for t in tracked), ephemeral=True)

    @discord_client.tree.command(name="debug", description="Diagnóstico do bot (mods)")
    async def slash_debug(interaction: discord.Interaction):
        perms = interaction.user.guild_permissions
        if not (perms.manage_messages or perms.administrator):
            await interaction.response.send_message("🚫 Only moderators!", ephemeral=True)
            return
        await interaction.response.defer()
        await interaction.followup.send(await debug_report(_unit_i(interaction)))


state.discord_client = discord_client
