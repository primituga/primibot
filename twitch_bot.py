import json
import random
import asyncio
import logging

from twitchio.ext import commands as twitch_commands
import state
import scope
import community
from config import TWITCH_TOKEN, TWITCH_CHANNELS, AUTO_MOD, IGNORED_TWITCH_USERS
from storage import (
    clear_history, check_rate_limit, get_stats, add_memory, clear_memory, set_persona,
    set_faq, clear_faq, format_stats_block,
)
from notify import send_webhook, log_event
from ai_core import ask_ai_logic, debug_report, push_chat
from safety import analyze_message_safety
import suspects
from games import play_rps, eight_ball, slots_spin
from twitch_api import live_status_text
from helpers import send_twitch_chunks, build_twitch_help, human_typing

logger = logging.getLogger("AIBot")


class MyTwitchBot(twitch_commands.Bot):
    def __init__(self):
        super().__init__(
            token=TWITCH_TOKEN,
            prefix='!',
            initial_channels=TWITCH_CHANNELS
        )

    async def event_ready(self):
        bot_name = self.nick if hasattr(self, 'nick') else "Bot"
        logger.info(f"Twitch Bot Online: {bot_name} connected to {TWITCH_CHANNELS}")
        await send_webhook(f"🟢 **primiBOT online no Twitch** ({TWITCH_CHANNELS})")
        if not community._timers_started:
            community._timers_started = True
            asyncio.create_task(community.timers_loop(self))

    async def event_message(self, message):
        if message.echo or message.author is None: return
        if message.author.name.lower() in IGNORED_TWITCH_USERS: return
        ch = message.channel.name if message.channel else ""
        # Sistema anti-suspeitos (blacklist cruzada + conta nova + links)
        try:
            for alert in await suspects.check_twitch_user(ch, message.author.name):
                await log_event("🕵️ Suspeito (Twitch)", alert, color=0xff8800, source="twitch")
            await suspects.scan_links("twitch", f"#{ch}", message.author.name, message.content)
        except Exception as e:
            logger.warning(f"Suspects check failed: {e}")
        if ch and not message.content.startswith("!"):
            push_chat(ch, message.author.name, message.content)
        if AUTO_MOD:
            reason = analyze_message_safety(message.content)
            if reason:
                await log_event("🛡️ Auto-mod (Twitch)", f"**{message.author.name}**: {message.content[:300]}\nMotivo: {reason}", color=0xff0000, source="twitch")
        low = message.content.strip().lower()
        if low.startswith("!"):
            word = low.split(" ", 1)[0][1:]
            resp = await community.resolve_custom_cmd(ch, word)
            if resp:
                await message.channel.send(resp.replace("{user}", message.author.name)[:480])
                return
        await self.handle_commands(message)

    async def event_command_error(self, context, error):
        # Ignore "Command Not Found" errors to prevent console spam
        if isinstance(error, twitch_commands.CommandNotFound): return
        logger.error(f"Twitch Command Error: {error}")

    def _unit(self, ctx) -> str:
        return scope.twitch_unit(ctx.channel.name)

    @twitch_commands.command(name='ai')
    async def ask_ai(self, ctx):
        parts = ctx.message.content.split(" ", 1)
        q = parts[1].strip() if len(parts) > 1 else ""
        if not q: return

        rl = await check_rate_limit(self._unit(ctx), ctx.author.name)
        if rl:
            await ctx.send(f"@{ctx.author.name} {rl}")
            return

        if len(q) > 380:
            await ctx.send(f"@{ctx.author.name} 🇵🇹 ✋ demasiado texto, encurta um pouco")
            return
        sid = f"twitch_{ctx.channel.name}_{ctx.author.name}"
        await human_typing()
        ans = await ask_ai_logic(q, sid, platform="twitch", user=ctx.author.name,
                                 user_key=ctx.author.name)
        await send_twitch_chunks(ctx.channel, ctx.author.name, ans)

    @twitch_commands.command(name='ai_reset')
    async def reset_ai(self, ctx):
        if not (ctx.author.is_mod or ctx.author.is_broadcaster):
            await ctx.send(f"@{ctx.author.name} 🚫 Only mods can clear my memory!")
            return
        await clear_history(self._unit(ctx))
        await ctx.send(f"@{ctx.author.name} 🧹 Memory cleared!")

    @twitch_commands.command(name='ai_stats')
    async def show_stats(self, ctx):
        stats = await get_stats(self._unit(ctx))
        await send_twitch_chunks(ctx.channel, ctx.author.name,
                                 format_stats_block(stats["unit"], "twitch", "neste canal"))
        if stats["global"]["total"]:
            await send_twitch_chunks(ctx.channel, ctx.author.name,
                                     format_stats_block(stats["global"], "twitch", f"global · `{self._unit(ctx)}`"))

    @twitch_commands.command(name='help')
    async def help_cmd(self, ctx):
        await send_twitch_chunks(ctx.channel, ctx.author.name, build_twitch_help(self))

    @twitch_commands.command(name='persona')
    async def persona(self, ctx):
        if not (ctx.author.is_mod or ctx.author.is_broadcaster):
            await ctx.send(f"@{ctx.author.name} 🚫 Só mods!")
            return
        parts = ctx.message.content.split(" ", 1)
        text = parts[1].strip() if len(parts) > 1 else ""
        ok = await set_persona(self._unit(ctx), text)
        if text:
            await ctx.send(f"@{ctx.author.name} 🧠 Personalidade atualizada!" if ok else f"@{ctx.author.name} ⚠️ Falha ao guardar.")
        else:
            await ctx.send(f"@{ctx.author.name} 🧠 Personalidade limpa (voltei ao default)!")

    @twitch_commands.command(name='faq')
    async def faq(self, ctx):
        if not (ctx.author.is_mod or ctx.author.is_broadcaster):
            await ctx.send(f"@{ctx.author.name} 🚫 Só mods!")
            return
        parts = ctx.message.content.split(" ", 2)
        sub = parts[1].lower() if len(parts) > 1 else ""
        if sub == "set":
            if len(parts) < 3:
                await ctx.send(f"@{ctx.author.name} uso: !faq set <texto>")
                return
            ok = await set_faq(self._unit(ctx), parts[2].strip())
            await ctx.send(f"@{ctx.author.name} 📚 FAQ deste canal guardada!" if ok else f"@{ctx.author.name} ⚠️ Falha ao guardar.")
            return
        await clear_faq(self._unit(ctx))
        await ctx.send(f"@{ctx.author.name} 📚 FAQ deste canal limpa (voltei ao FAQ global).")

    @twitch_commands.command(name='debug')
    async def debug(self, ctx):
        if not (ctx.author.is_mod or ctx.author.is_broadcaster):
            await ctx.send(f"@{ctx.author.name} 🚫 Só mods!")
            return
        await send_twitch_chunks(ctx.channel, ctx.author.name, await debug_report(self._unit(ctx)))

    @twitch_commands.command(name='rps')
    async def rps(self, ctx):
        parts = ctx.message.content.split(" ", 1)
        choice = parts[1].strip() if len(parts) > 1 else ""
        text, outcome = play_rps(choice)
        if outcome == "lose" and not (ctx.author.is_mod or ctx.author.is_broadcaster):
            try:
                await ctx.channel.timeout(ctx.author, 30, reason="Perdeu no RPS 😈")
                text += " ⏰ Timeout 30s!"
            except Exception:
                text += " 😅 Não tenho mod para dar timeout."
        await ctx.send(f"@{ctx.author.name} {text}")

    @twitch_commands.command(name='coin')
    async def coin(self, ctx):
        parts = ctx.message.content.split(" ", 1)
        guess = parts[1].strip().lower() if len(parts) > 1 else ""
        result = random.choice(["cara", "coroa"])
        line = f"🪙 {result.capitalize()}!"
        if guess in ("cara", "coroa"):
            line += " 🎯 Acertaste!" if guess == result else " ❌ Erraste!"
        await ctx.send(f"@{ctx.author.name} {line}")

    @twitch_commands.command(name='dice')
    async def dice(self, ctx):
        await ctx.send(f"@{ctx.author.name} 🎲 {random.randint(1, 6)}")

    @twitch_commands.command(name='roll')
    async def roll(self, ctx):
        await ctx.send(f"@{ctx.author.name} 🎲 {random.randint(1, 100)}")

    @twitch_commands.command(name='8ball')
    async def ball(self, ctx):
        parts = ctx.message.content.split(" ", 1)
        q = parts[1].strip() if len(parts) > 1 else ""
        if not q:
            await ctx.send(f"@{ctx.author.name} 🎱 Faz uma pergunta! Ex: !8ball vou ganhar hoje?")
            return
        await ctx.send(f"@{ctx.author.name} 🎱 {eight_ball()}")

    @twitch_commands.command(name='slots')
    async def slots(self, ctx):
        await ctx.send(f"@{ctx.author.name} {slots_spin()}")

    @twitch_commands.command(name='remember')
    async def remember(self, ctx):
        parts = ctx.message.content.split(" ", 1)
        fact = parts[1].strip() if len(parts) > 1 else ""
        if not fact: return
        ok = await add_memory(self._unit(ctx), ctx.author.name, fact)
        await ctx.send(f"@{ctx.author.name} 🧠 Vou lembrar-me disso!" if ok else f"@{ctx.author.name} ⚠️ Não consegui guardar.")

    @twitch_commands.command(name='forget')
    async def forget(self, ctx):
        await clear_memory(self._unit(ctx), ctx.author.name)
        await ctx.send(f"@{ctx.author.name} 🧠 Memória apagada!")

    @twitch_commands.command(name='pick')
    async def pick(self, ctx):
        parts = ctx.message.content.split(" ", 1)
        opts = parts[1].strip() if len(parts) > 1 else ""
        if not opts: return
        choices = [o.strip() for o in opts.split(",") if o.strip()]
        if not choices:
            await ctx.send(f"@{ctx.author.name} opções separadas por vírgula: !pick a, b, c")
            return
        await ctx.send(f"@{ctx.author.name} 🎯 {random.choice(choices)}")

    @twitch_commands.command(name='joke')
    async def joke(self, ctx):
        sid = f"twitch_{ctx.channel.name}_{ctx.author.name}"
        ans = await ask_ai_logic("Conta uma piada curta e engraçada.", sid, platform="twitch",
                                 user=ctx.author.name, user_key=ctx.author.name)
        await send_twitch_chunks(ctx.channel, ctx.author.name, ans)

    @twitch_commands.command(name='roast')
    async def roast(self, ctx):
        parts = ctx.message.content.split(" ", 1)
        target = parts[1].strip() if len(parts) > 1 else ctx.author.name
        sid = f"twitch_{ctx.channel.name}_{ctx.author.name}"
        ans = await ask_ai_logic(f"Faz um roast engraçado e carinhoso de {target} em 2 frases.", sid,
                                 platform="twitch", user=ctx.author.name, user_key=ctx.author.name)
        await send_twitch_chunks(ctx.channel, ctx.author.name, ans)

    @twitch_commands.command(name='live')
    async def live(self, ctx):
        await ctx.send(f"@{ctx.author.name} {await live_status_text(ctx.channel.name)}")

    # ===== Comandos personalizados =====

    @twitch_commands.command(name='addcmd')
    async def addcmd(self, ctx):
        if not community._mod(ctx):
            await ctx.send(f"@{ctx.author.name} 🚫 Só mods!")
            return
        parts = ctx.message.content.split(" ", 2)
        if len(parts) < 3:
            await ctx.send(f"@{ctx.author.name} uso: !addcmd <nome> <resposta>")
            return
        name = parts[1].lstrip("!").lower()
        await community.add_custom_cmd(ctx.channel.name, name, parts[2])
        await ctx.send(f"@{ctx.author.name} ✅ Comando !{name} criado!")

    @twitch_commands.command(name='editcmd')
    async def editcmd(self, ctx):
        if not community._mod(ctx):
            await ctx.send(f"@{ctx.author.name} 🚫 Só mods!")
            return
        parts = ctx.message.content.split(" ", 2)
        if len(parts) < 3:
            await ctx.send(f"@{ctx.author.name} uso: !editcmd <nome> <nova resposta>")
            return
        name = parts[1].lstrip("!").lower()
        if not await community.resolve_custom_cmd(ctx.channel.name, name):
            await ctx.send(f"@{ctx.author.name} ❌ !{name} não existe (usa !addcmd).")
            return
        await community.add_custom_cmd(ctx.channel.name, name, parts[2])
        await ctx.send(f"@{ctx.author.name} ✅ Comando !{name} atualizado!")

    @twitch_commands.command(name='delcmd')
    async def delcmd(self, ctx):
        if not community._mod(ctx):
            await ctx.send(f"@{ctx.author.name} 🚫 Só mods!")
            return
        parts = ctx.message.content.split(" ", 1)
        if len(parts) < 2:
            await ctx.send(f"@{ctx.author.name} uso: !delcmd <nome>")
            return
        ok = await community.del_custom_cmd(ctx.channel.name, parts[1].lstrip("!"))
        await ctx.send(f"@{ctx.author.name} {'🗑️ Apagado!' if ok else '❌ Não existe.'}")

    @twitch_commands.command(name='cmdlist')
    async def cmdlist(self, ctx):
        cmds = await community.list_custom_cmds(ctx.channel.name)
        if not cmds:
            await ctx.send(f"@{ctx.author.name} Não há comandos personalizados.")
            return
        names = "!" + " !".join(sorted(cmds.keys()))
        await send_twitch_chunks(ctx.channel, ctx.author.name, f"Comandos personalizados: {names}"[:480])

    # ===== Timers =====

    @twitch_commands.command(name='ptimer')
    async def ptimer(self, ctx):
        if not community._mod(ctx):
            await ctx.send(f"@{ctx.author.name} 🚫 Só mods!")
            return
        parts = ctx.message.content.split(" ", 3)
        sub = parts[1].lower() if len(parts) > 1 else ""
        if sub == "add":
            if len(parts) < 4 or not parts[2].isdigit():
                await ctx.send(f"@{ctx.author.name} uso: !ptimer add <minutos> <mensagem>")
                return
            tid = await community.timer_add(ctx.channel.name, int(parts[2]), parts[3])
            await ctx.send(f"@{ctx.author.name} ⏰ Timer #{tid} criado ({parts[2]}min).")
        elif sub == "del":
            if len(parts) < 3:
                await ctx.send(f"@{ctx.author.name} uso: !ptimer del <id>")
                return
            ok = await community.timer_del(ctx.channel.name, parts[2])
            await ctx.send(f"@{ctx.author.name} {'🗑️ Timer apagado!' if ok else '❌ Timer não existe.'}")
        else:
            timers = await state.redis_client.hgetall(community._ck(ctx.channel.name, "timers")) or {}
            if not timers:
                await ctx.send(f"@{ctx.author.name} Sem timers. Cria com: !ptimer add <min> <msg>")
                return
            lines = []
            for tid, raw in timers.items():
                try:
                    t = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                lines.append(f"#{tid}: {int(t.get('interval', 0))}min — {t.get('message', '')[:60]}")
            if not lines:
                await ctx.send(f"@{ctx.author.name} Sem timers válidos.")
                return
            await send_twitch_chunks(ctx.channel, ctx.author.name, "\n".join(lines)[:480])

    # ===== Shoutout / Uptime =====

    @twitch_commands.command(name='so')
    async def so(self, ctx):
        if not community._mod(ctx):
            await ctx.send(f"@{ctx.author.name} 🚫 Só mods!")
            return
        parts = ctx.message.content.split(" ", 1)
        target = parts[1].strip() if len(parts) > 1 else ""
        try:
            txt = await community.shoutout_text(target)
        except Exception:
            txt = None
        await ctx.send(txt if txt else f"@{ctx.author.name} ❌ Canal não encontrado.")

    @twitch_commands.command(name='uptime')
    async def uptime(self, ctx):
        try:
            txt = await community.uptime_text(ctx.channel.name)
        except Exception:
            txt = "❓ Não consegui obter o estado."
        await ctx.send(f"@{ctx.author.name} {txt}")

    # ===== Quotes =====

    @twitch_commands.command(name='quote')
    async def quote(self, ctx):
        parts = ctx.message.content.split(" ", 1)
        arg = parts[1].strip() if len(parts) > 1 else ""
        if arg.lower().startswith("add "):
            n = await community.quote_add(ctx.channel.name, arg[4:].strip())
            await ctx.send(f"@{ctx.author.name} 💬 Quote #{n} guardada!")
            return
        if arg.lower().startswith("del"):
            if not community._mod(ctx):
                await ctx.send(f"@{ctx.author.name} 🚫 Só mods!")
                return
            num = arg.split(" ", 1)[1].strip() if " " in arg else ""
            if not num.isdigit():
                await ctx.send(f"@{ctx.author.name} uso: !quote del <número>")
                return
            ok = await community.quote_del(ctx.channel.name, int(num))
            await ctx.send(f"@{ctx.author.name} {'🗑️ Quote apagada!' if ok else '❌ Não existe.'}")
            return
        q, total = await community.quote_get(ctx.channel.name, int(arg) if arg.isdigit() else None)
        if q is None:
            await ctx.send(f"@{ctx.author.name} 💬 Sem quotes ainda! Adiciona com !quote add <frase>" if not total else f"@{ctx.author.name} ❌ Só há {total} quotes.")
            return
        await ctx.send(f"💬 [{arg if arg else 'random'}/{total}] {q}"[:480])

    # ===== Death counter =====

    @twitch_commands.command(name='deaths')
    async def deaths(self, ctx):
        parts = ctx.message.content.split(" ", 1)
        arg = parts[1].strip() if len(parts) > 1 else ""
        if arg.lower() == "reset":
            if not community._mod(ctx):
                await ctx.send(f"@{ctx.author.name} 🚫 Só mods!")
                return
            await community.deaths_reset(ctx.channel.name)
            await ctx.send(f"@{ctx.author.name} ☠️ Contador a zero!")
            return
        delta = 0
        if arg.startswith(("+", "-")) and arg[1:].isdigit():
            delta = int(arg)
        elif arg.isdigit():
            delta = int(arg)
        n = await community.deaths_change(ctx.channel.name, delta)
        await ctx.send(f"☠️ Mortes nesta stream: {n}")

    # ===== Giveaway =====

    @twitch_commands.command(name='giveaway')
    async def giveaway(self, ctx):
        parts = ctx.message.content.split(" ", 1)
        arg = parts[1].strip() if len(parts) > 1 else ""
        if arg.lower().startswith("start "):
            if not community._mod(ctx):
                await ctx.send(f"@{ctx.author.name} 🚫 Só mods!")
                return
            prize = arg[6:].strip()
            await community.ga_start(ctx.channel.name, prize)
            await ctx.send(f"🎁 GIVEAWAY: '{prize}'! Escreve !join para participares!")
            return
        if arg.lower() == "pick":
            if not community._mod(ctx):
                await ctx.send(f"@{ctx.author.name} 🚫 Só mods!")
                return
            res = await community.ga_pick(ctx.channel.name)
            if not res:
                await ctx.send(f"@{ctx.author.name} ❌ Sem giveaway ativo ou sem participantes.")
                return
            winner, prize, total = res
            await ctx.send(f"🎉 O VENCEDOR de '{prize}' é... @{winner}!! ({total} participantes)")
            return
        if arg.lower() == "cancel":
            if not community._mod(ctx):
                await ctx.send(f"@{ctx.author.name} 🚫 Só mods!")
                return
            await community.ga_cancel(ctx.channel.name)
            await ctx.send(f"@{ctx.author.name} Giveaway cancelado.")
            return
        await ctx.send(f"@{ctx.author.name} {await community.ga_status(ctx.channel.name)}")

    @twitch_commands.command(name='join')
    async def join(self, ctx):
        msg = await community.ga_join(ctx.channel.name, ctx.author.name)
        await ctx.send(f"@{ctx.author.name} {msg}")

    # ===== Polls =====

    @twitch_commands.command(name='poll')
    async def poll(self, ctx):
        parts = ctx.message.content.split(" ", 1)
        arg = parts[1].strip() if len(parts) > 1 else ""
        if arg.lower() == "end":
            if not community._mod(ctx):
                await ctx.send(f"@{ctx.author.name} 🚫 Só mods!")
                return
            res = await community.poll_end(ctx.channel.name)
            await send_twitch_chunks(ctx.channel, ctx.author.name, res if res else "Não há votação ativa.")
            return
        if not arg:
            await ctx.send(f"@{ctx.author.name} {await community.poll_status(ctx.channel.name)}")
            return
        if not community._mod(ctx):
            await ctx.send(f"@{ctx.author.name} 🚫 Só mods!")
            return
        bits = [b.strip() for b in arg.split("|") if b.strip()]
        if len(bits) < 3 or len(bits) > 7:
            await ctx.send(f"@{ctx.author.name} uso: !poll pergunta | opção1 | opção2 [| opção3…]")
            return
        await community.poll_open(ctx.channel.name, bits[0], bits[1:])
        opts = " | ".join(f"{i+1}.{o}" for i, o in enumerate(bits[1:]))
        await ctx.send(f"📊 VOTAÇÃO: {bits[0]} → {opts}. Vota com !vote <número>!")

    @twitch_commands.command(name='vote')
    async def vote(self, ctx):
        parts = ctx.message.content.split(" ", 1)
        arg = parts[1].strip() if len(parts) > 1 else ""
        if not arg.isdigit():
            return
        msg = await community.poll_vote(ctx.channel.name, ctx.author.name, int(arg))
        await ctx.send(f"@{ctx.author.name} {msg}")

    # ===== Queue =====

    @twitch_commands.command(name='q')
    async def q(self, ctx):
        parts = ctx.message.content.split(" ", 1)
        sub = parts[1].strip().lower() if len(parts) > 1 else "list"
        user = ctx.author.name
        if sub == "open":
            if not community._mod(ctx): return
            await community.q_open(ctx.channel.name, True)
            await ctx.send("📋 Fila ABERTA! !q join para entrares!")
        elif sub == "close":
            if not community._mod(ctx): return
            await community.q_open(ctx.channel.name, False)
            await ctx.send("📋 Fila fechada.")
        elif sub == "join":
            msg = await community.q_join(ctx.channel.name, user)
            await ctx.send(f"@{user} {msg}")
        elif sub == "leave":
            msg = await community.q_leave(ctx.channel.name, user)
            await ctx.send(f"@{user} {msg}")
        elif sub == "next":
            if not community._mod(ctx): return
            res = await community.q_next(ctx.channel.name)
            await ctx.send(f"➡️ Próximo: @{res[0]}! ({res[1]} na fila)" if res else "Fila vazia.")
        elif sub == "clear":
            if not community._mod(ctx): return
            await community.q_clear(ctx.channel.name)
            await ctx.send("📋 Fila limpa e fechada.")
        else:
            await ctx.send(await community.q_list(ctx.channel.name))
