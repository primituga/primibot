import os
import json
import time
import uuid
import asyncio
import logging
from collections import deque
from urllib.parse import urlparse

import state
import scope
import guild_config
from config import (
    FLOW_URL, FLOW_URL_PI, GROQ_MODELS, OPENROUTER_API_KEY, OPENROUTER_VISION_MODEL,
    OPENCODE_ZEN_API_KEY, SEARXNG_URL, MAX_HISTORY, DISCORD_PROMPT, TWITCH_PROMPT,
    HEARTBEAT_FILE, TWITCH_CHANNELS, CONTEXT_MODE,
    MAX_CONTEXT_STREAMERS,
)
from storage import (
    get_history, save_history, get_persona, bump_stats, health_ok, health_fail,
    set_last_error, get_session_summary, set_session_summary, get_memory, get_faq,
)
from safety import filter_output
from notify import send_webhook, log_event

logger = logging.getLogger("AIBot")


# --- Contexto do chat Twitch (ring buffer) ---
def push_chat(channel: str, author: str, text: str):
    key = channel.lower()
    if key not in state._chat_ring:
        state._chat_ring[key] = deque(maxlen=20)
    state._chat_ring[key].append(f"{author}: {text[:200]}")


def get_chat_context_for(channel: str, exclude_author: str) -> str:
    key = channel.lower()
    ring = state._chat_ring.get(key)
    if not ring: return ""
    lines = [m for m in list(ring) if not m.lower().startswith(f"{exclude_author.lower()}:")]
    return "\n".join(lines[-10:])


# --- Isolamento: cada unit tem o seu estado ---
# A unit e derivada do session_id (ver scope.py) e e o namespace de TODA a
# persistencia: history, summary, memory, persona, faq, rate limits, stats e health.
# Uma sala de Discord nunca le o estado de outra, nem o de um canal Twitch.


async def context_streamers(unit: str, guild_id) -> list:
    """Que streamers entram no prompt desta unit. Vazio = nao injecta nada.

    Twitch: o proprio canal (o contexto e sempre o do canal onde se perguntou).
    Discord: SO os streamers que a guild followed explicitamente com !ai_twitch add.
    Nao ha fallback para TWITCH_CHANNEL — nem sequer na guild casa. A guild e uma
    unidade isolada: se nao segue ninguem, nao recebe contexto de ninguem. O fallback
    punha no prompt de toda a gente os 3 canais do env, e o modeloIa despeja-los em
    perguntas-genericas do tipo 'ola'.

    CONTEXT_MODE:
      tracked    -> so os seguidos explicitamente (default; comportamento acima)
      home_only  -> igual a tracked,kept apenas para retro-compatibilidade
      env        -> todos os canais de TWITCH_CHANNEL em qualquer sala (comportamento
                    antigo, partilhado; reintroduz a mistura entre salas)
      off        -> nunca injectar contexto de live, nem no Discord nem na Twitch"""
    if not unit or CONTEXT_MODE == "off":
        return []
    if unit.startswith("twitch:"):
        ch = unit.split(":", 1)[1]
        return [ch] if ch and ch != "_" else []
    try:
        gid = int(guild_id) if guild_id else None
    except (TypeError, ValueError):
        gid = None
    if gid is None:
        return []
    if CONTEXT_MODE == "env":
        return [c.lower() for c in TWITCH_CHANNELS if c][:MAX_CONTEXT_STREAMERS]
    tracked = await guild_config.get_twitch_tracked_strict(gid)
    seen, out = set(), []
    for c in tracked:
        c = (c or "").lower().lstrip("@")
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out[:MAX_CONTEXT_STREAMERS]


async def get_live_context(unit: str, guild_id=None) -> str:
    """Estado da live (jogo + título) APENAS dos streamers desta unit.

    Antes lia as chaves globais `twitch:game|title` do broadcaster sempre que a unit
    era Discord, o que fazia qualquer sala receber o jogo e o titulo da Twitch
    principal. E nao havia forma de o !ai_reset limpar: sao reescritas de 60 em 60s.
    """
    try:
        channels = await context_streamers(unit, guild_id)
        if not channels:
            return ""
        parts = []
        for login in channels:
            prefix = f"twitch:{login}"
            status = await state.redis_client.get(f"{prefix}:stream_status")
            game = await state.redis_client.get(f"{prefix}:game")
            title = await state.redis_client.get(f"{prefix}:title")
            if not game and not title:
                continue
            # Na Twitch o contexto e o do proprio canal, entao nomeia-se o canal
            # ("O canal principal"). No Discord sao canais de terceiros, por isso
            # identifica-se o streamer ("O streamer exemplo") para o modelo
            # nao os confundir com a sala onde esta a falar.
            label = login if unit.startswith("twitch:") else f"o streamer {login}"
            parts.append(
                f"O canal {label} está "
                + ("LIVE agora" if status == "online"
                   else "offline neste momento, mas as pessoas continuam a perguntar sobre as streams")
                + "."
            )
            if game:
                parts.append(f"Jogo principal deste canal agora: {game}")
            if title:
                parts.append(f"Título da stream: {title}")
        if not parts:
            return ""
        parts.append(
            "Isto e fundo de referencia, NAO um tema de conversa. So o menciona se a "
            "pessoa perguntar explicitamente sobre uma stream, um jogo ou um canal. "
            "NUNCA liste os canais, nem faças um inventario de 'opcoes' de streams ao "
            "saudar, ao agradecer ou a responder a uma pergunta que nao seja sobre isso."
        )
        parts.append(
            "Resolução de referências vagas ('o jogo' / 'isso'): (1) se a pessoa nomeia "
            "um jogo, usa SEMPRE esse; (2) senao, o tema da conversa recente; (3) so em "
            "ultimo caso, o jogo do canal acima."
        )
        return "\n".join(parts)
    except Exception:
        return ""


# --- Contexto extra injetado no prompt (memória + FAQ + chat + resumo) ---
async def build_context(platform: str, unit: str, guild_id, user_key: str,
                        include_all: bool, user: str = "") -> str:
    """user_key e um identificador ESTAVEL do utilizador (id), nao o nome —
    um display_name muda e a memoria deixava de ser encontrada.
    `user` e so o nome legivel, usado para excluir a propria mensagem do chat ring."""
    parts = []
    live = await get_live_context(unit, guild_id)
    if live:
        parts.append("Contexto da live:\n" + live)
    if user_key:
        mem = await get_memory(unit, user_key)
        if mem:
            parts.append("Factos conhecidos sobre quem perguntou:\n- " + "\n- ".join(mem[-8:]))
    if include_all:
        # Ring buffer do chat Twitch: ja e por canal (state._chat_ring), portanto
        # nao cruza unidades. So entra em prompts de Twitch.
        if platform == "twitch" and unit.startswith("twitch:"):
            ch = unit.split(":", 1)[1]
            if ch and ch != "_":
                ctx = get_chat_context_for(ch, user or user_key)
                if ctx:
                    parts.append("Mensagens recentes do chat (do que o chat anda a falar):\n" + ctx)
        faq = await get_faq(unit)
        if faq:
            parts.append(f"FAQ deste canal/sala (usa quando for relevante):\n{faq[:2500]}")
        summary = await get_session_summary(unit)
        if summary:
            parts.append(f"Resumo da conversa anterior:\n{summary[:1500]}")
    return "\n\n".join(parts) if parts else ""


def trim_history_safe(history: list, max_chars: int = 6000) -> list:
    """
    Prevents HTTP 413 Payload Too Large errors by trimming older messages 
    if the total character count exceeds the safe limit.
    """
    trimmed = []
    current_length = 0
    for msg in reversed(history):
        msg_len = len(msg.get("content", ""))
        if current_length + msg_len > max_chars:
            break
        trimmed.insert(0, msg)
        current_length += msg_len
    return trimmed


# --- Sumarização de histórico antigo ---
async def summarize_history(messages: list) -> str:
    """Summarizes an old conversation via a cheap Groq model."""
    try:
        res = await state.groq_client.chat.completions.create(
            model=GROQ_MODELS[-1],
            messages=[
                {"role": "system", "content": "Summarize this conversation concisely, in the same language as the conversation, in 2-3 short bullet points, keeping key facts, names and decisions."},
                *messages
            ],
            temperature=0.3,
            max_completion_tokens=300
        )
        return (res.choices[0].message.content or "").strip()
    except Exception as e:
        logger.warning(f"History summarize failed: {e}")
        return ""


_url_domain = lambda u: (urlparse(u).netloc.lower().removeprefix("www.")) if (u and "//" in u) else ""


async def search_web(query: str) -> str:
    """Search using local SearXNG. Returns títulos + snippets (content) reais para o modelo usar.
    Deduplica por domínio e prefere resultados com conteúdo factual."""  # corrected string
    try:
        res = await state.http_client.get(SEARXNG_URL, params={"q": query, "format": "json"}, timeout=8.0)
        res.raise_for_status()
        data = res.json()
        results = data.get("results", [])
        if not results:
            return ""
        seen = set()
        chosen = []
        for i in sorted(results, key=lambda r: r.get("score") or 0, reverse=True):
            dom = _url_domain(i.get("url") or "")
            if dom in seen:
                continue  # mesmo site apareceu em vários resultados/motores
            seen.add(dom)
            title = (i.get("title") or "").strip()
            url = (i.get("url") or "").strip()
            content = (i.get("content") or "").strip()
            snippet = content[:280] if content else "(sem resumo)"
            chosen.append(f"- {title} ({dom}): {snippet}")
            if len(chosen) >= 6:
                break
        return "\n".join(chosen)
    except Exception as e:
        logger.warning(f"SearXNG search failed: {e}")
        return ""


PROVIDERS = ["flowise", "openrouter", "groq", "zen", "backup-pi"]


async def debug_report(unit: str = scope.GLOBAL) -> str:
    """Saude dos providers por unit E global.

    O global e o sinal que importa para alertas: uma key do Groq ou uma queda do
    Flowise e uma unica falha para as duas plataformas. O por-unit mostra que
    plataforma esta a gerar o erro."""
    try:
        lines = [f"**Unidade:** `{unit}`"]
        lines.append("")
        lines.append("• **{:>12} · ✅ ok / ❌ fail**".format("provider"))
        for provider in PROVIDERS:
            ok = await state.redis_client.get(f"health:{provider}:ok") or "0"
            fail = await state.redis_client.get(f"health:{provider}:fail") or "0"
            uok = await state.redis_client.get(scope.k_health(unit, provider, "ok")) or "0"
            ufail = await state.redis_client.get(scope.k_health(unit, provider, "fail")) or "0"
            lines.append(f"• **{provider}**: global ✅ {ok} / ❌ {fail} · aqui ✅ {uok} / ❌ {ufail}")
        err = await state.redis_client.get("debug:last_error") or "—"
        lines.append(f"• **Último erro**: `{err[:300]}`")
        lines.append(f"• **Heartbeat**: {'✅ ok' if os.path.exists(HEARTBEAT_FILE) else '⚠️ sem ficheiro'}")
        return "🔍 **Debug**\n" + "\n".join(lines)
    except Exception:
        return "🔍 **Debug**\n⚠️ Redis indisponível."


# --- Core AI Logic (4-Tier Fallback System) ---
async def ask_ai_logic(question: str, session_id: str, platform: str = "discord",
                      base64_image: str = None, user: str = "", user_key: str = "",
                      stream_cb=None) -> str:
    """
    Public entry point. Serializes requests per session to prevent history
    races and out-of-order answers, then delegates to the fallback logic.
    stream_cb: optional async callable(delta:str) called as tokens arrive (streaming UX).
    user: nome legivel (stats, ring buffer). user_key: identificador estavel (memoria).
    """
    lock = state._history_locks.setdefault(session_id, asyncio.Lock())
    if lock.locked():
        return "⏳ I'm still answering the previous question — one at a time!"
    async with lock:
        ans = await _ask_ai_logic_inner(question, session_id, platform, base64_image,
                                        user, user_key or user, stream_cb)
        return filter_output(ans)


async def _ask_ai_logic_inner(question: str, session_id: str, platform: str = "discord",
                              base64_image: str = None, user: str = "", user_key: str = "",
                              stream_cb=None) -> str:
    """
    Handles the AI logic with a robust 4-tier fallback architecture:
    1. Primary Flowise Server
    2. Cloud Groq API (with dynamic SearXNG context)
    3. opencode Zen (free models first, then paid)
    4. Secondary Flowise Server (Low-power backup)

    Todo o estado lido/escrito aqui e enderecado pela unit derivada do session_id,
    por isso uma sala nunca enxerga o contexto de outra.
    """
    unit = scope.unit_for_session(session_id, platform)
    guild_id = None
    if session_id.startswith("discord_"):
        guild_id = scope.guild_of_channel(session_id.split("_", 1)[1])

    history = await get_history(unit)
    if len(history) > MAX_HISTORY:
        dropped = history[:-MAX_HISTORY]
        history = history[-MAX_HISTORY:]
        new_sum = await summarize_history(dropped)
        if new_sum:
            old_sum = await get_session_summary(unit)
            merged = (old_sum + "\n" + new_sum).strip()[-2000:]
            await set_session_summary(unit, merged)
    history = trim_history_safe(history, max_chars=6000)
    # Tamanho da base tal como foi lida: save_history funde a partir daqui, para não
    # apagar o que outro utilizador da mesma unit tenha gravado entretanto.
    history_base = len(history)
    is_vision = base64_image is not None

    # ==========================================
    # LEVEL 1: Primary Flowise Server
    # ==========================================
    try:
        # Enriquecer com contexto web (SearXNG) para o modelo local pesquisar de verdade.
        if not is_vision and question and not question.lower().lstrip().startswith(("!", "/")):
            wctx = await search_web(question)
            agora = time.strftime("%A, %d de %B de %Y, %H:%M")  # data/hora real "hoje"
            if wctx:
                q_to_flow = (
                    f"[DATA ATUAL: {agora}. Hoje é esta data; usa-a para responder sobre "
                    f"'hoje'/'agora', nunca inventes outra data.]\n\n"
                    f"{question}\n\n[CONTEXTO WEB (usa estes dados factuais para responder, "
                    f"e menciona se vierem da web):]\n{wctx}"
                )
            else:
                q_to_flow = (
                    f"[DATA ATUAL: {agora}. Responde como se fosse hoje esta data.]\n\n"
                    f"{question}"
                )
            history_for_flow = history
        else:
            q_to_flow = question
            history_for_flow = history

        payload = {"question": q_to_flow, "overrideConfig": {"sessionId": f"{session_id}:{uuid.uuid4().hex[:8]}"}}
        if is_vision:
            payload["uploads"] = [{
                "data": f"data:image/jpeg;base64,{base64_image}",
                "type": "file",
                "name": "vision_input.jpg",
                "mime": "image/jpeg"
            }]

        res = await state.http_client.post(FLOW_URL, json=payload, timeout=45.0)
        res.raise_for_status()
        ans = res.json().get("text")
        if not ans:
            raise ValueError("Flowise devolveu resposta vazia")

        history_msg = f"[Image sent] {question}" if is_vision else question
        history.extend([{"role": "user", "content": history_msg}, {"role": "assistant", "content": ans}])
        await save_history(unit, history, history_base)
        await bump_stats(unit, user, "flowise")
        await health_ok(unit, "flowise")
        return ans

    except Exception as e:
        logger.warning(f"Level 1 (Primary Server) failed. Reason: {e}")
        await health_fail(unit, "flowise")

    # ==========================================
    # LEVEL 2: Cloud Fallback (OpenRouter Vision / Groq API)
    # ==========================================
    if is_vision:
        sys_prompt = "You are a helpful assistant. Describe the image or answer the user's question about it. Always reply in the EXACT SAME LANGUAGE as the user."
        persona = await get_persona(unit)
        if persona:
            sys_prompt += f"\n\nPersonality/behavior override:\n{persona}"
        messages_payload = [
            {"role": "system", "content": sys_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": question if question else "What do you see in this image?"},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}
                ]
            }
        ]
        # Visão via OpenRouter (a Groq já não serve modelos de visão nesta conta)
        if OPENROUTER_API_KEY:
            try:
                res = await state.http_client.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}"},
                    json={
                        "model": OPENROUTER_VISION_MODEL,
                        "messages": messages_payload,
                        "temperature": 0.5,
                        "max_tokens": 1024
                    },
                    timeout=60.0
                )
                res.raise_for_status()
                ans = res.json()["choices"][0]["message"]["content"]
                ans = f"👁️ ({OPENROUTER_VISION_MODEL}) {ans}"

                history.extend([{"role": "user", "content": f"[Image sent] {question}"}, {"role": "assistant", "content": ans}])
                await save_history(unit, history, history_base)
                await bump_stats(unit, user, OPENROUTER_VISION_MODEL)
                await health_ok(unit, "openrouter")
                return ans
            except Exception as e:
                logger.warning(f"OpenRouter Vision failed: {e}")
                await health_fail(unit, "openrouter")
        else:
            logger.warning("OPENROUTER_API_KEY não definida — visão indisponível no Level 2.")
    else:
        base_prompt = TWITCH_PROMPT if platform == "twitch" else DISCORD_PROMPT
        persona = await get_persona(unit)
        if persona:
            base_prompt += f"\n\nPersonality/behavior override:\n{persona}"
        light_ctx = await build_context(platform, unit, guild_id, user_key, include_all=False, user=user)
        full_ctx = await build_context(platform, unit, guild_id, user_key, include_all=True, user=user)
        web_context = None

        for model in GROQ_MODELS:
            try:
                if "compound" in model:
                    async def _compound_call(msgs):
                        return await state.groq_client.chat.completions.create(
                            model=model,
                            messages=msgs,
                            temperature=0.5,
                            stream=False,
                            max_completion_tokens=1024,
                            compound_custom={
                                "tools": {"enabled_tools": ["web_search", "code_interpreter", "visit_website"]}
                            }
                        )
                    # Agentic models: contexto leve mas COM histórico recente,
                    # para resolver referências como "o jogo" / "isso" nas follow-ups
                    recent_history = trim_history_safe(history[-6:], max_chars=1200)
                    light_payload = [
                        {"role": "system", "content": (base_prompt + "\n\n" + light_ctx).strip()},
                        *recent_history,
                        {"role": "user", "content": question}
                    ]
                    try:
                        res = await _compound_call(light_payload)
                    except Exception as ce:
                        if "413" in str(ce) or "too_large" in str(ce) or "Too Large" in str(ce):
                            logger.warning(f"{model} devolveu 413 → retry com payload mínimo (sem histórico)")
                            minimal_payload = [
                                {"role": "system", "content": (base_prompt + "\n\n" + light_ctx).strip()},
                                {"role": "user", "content": question}
                            ]
                            res = await _compound_call(minimal_payload)
                        else:
                            raise
                    ans = res.choices[0].message.content
                else:
                    # Standard LLMs use local web search (SearXNG) and full context
                    agora = time.strftime("%A, %d de %B de %Y, %H:%M")
                    current_sys_prompt = (
                        f"[DATA ATUAL: {agora}. Responde como se fosse hoje "
                        f"esta data; nunca inventes outra data.]\n\n"
                        f"{base_prompt}\n\n{full_ctx}"
                    ).strip()

                    if web_context is None:
                        web_context = await search_web(question)

                    if web_context:
                        current_sys_prompt += f"\n\nUpdated Web Context:\n{web_context}"

                    messages_payload = [{"role": "system", "content": current_sys_prompt}] + history + [{"role": "user", "content": question}]

                    if stream_cb:
                        stream_resp = await state.groq_client.chat.completions.create(
                            model=model,
                            messages=messages_payload,
                            temperature=0.5,
                            stream=True
                        )
                        parts = []
                        async for chunk in stream_resp:
                            if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
                                delta = chunk.choices[0].delta.content
                                parts.append(delta)
                                await stream_cb(delta)
                        ans = "".join(parts)
                    else:
                        res = await state.groq_client.chat.completions.create(
                            model=model,
                            messages=messages_payload,
                            temperature=0.5,
                            stream=False
                        )
                        ans = res.choices[0].message.content

                if not ans:
                    raise ValueError("resposta vazia")
                ans = f"⚡ ({model}) {ans}"

                history.extend([{"role": "user", "content": question}, {"role": "assistant", "content": ans}])
                await save_history(unit, history, history_base)
                await bump_stats(unit, user, model)
                await health_ok(unit, "groq")
                return ans

            except Exception as e:
                logger.warning(f"Groq Model '{model}' failed: {e}")
                await health_fail(unit, "groq")
                continue

    logger.warning("Level 2 (Cloud Fallback) completely failed. Falling back to Level 3...")

    # ==========================================
    # LEVEL 3: opencode Zen (fallback cloud adicional)
    # ==========================================
    if is_vision:
        return "⚠️ Error: Primary server and Cloud fallback are down. Currently running on emergency backup, which does not support image processing. Please send text only."

    if OPENCODE_ZEN_API_KEY:
        zen_sys_prompt = (TWITCH_PROMPT if platform == "twitch" else DISCORD_PROMPT) + "\n\n" + await build_context(platform, unit, guild_id, user_key, include_all=True, user=user)
        for zen_model in ["x-preview-f-free", "mimo-v2.5-free", "deepseek-v4-flash"]:
            try:
                zen_messages = [{"role": "system", "content": zen_sys_prompt}] + history + [{"role": "user", "content": question}]
                if stream_cb:
                    async with state.http_client.stream(
                        "POST",
                        "https://opencode.ai/zen/v1/chat/completions",
                        headers={"Authorization": f"Bearer {OPENCODE_ZEN_API_KEY}"},
                        json={"model": zen_model, "messages": zen_messages, "temperature": 0.5, "max_tokens": 1024, "stream": True},
                        timeout=45.0
                    ) as resp:
                        resp.raise_for_status()
                        parts = []
                        async for line in resp.aiter_lines():
                            if not line.startswith("data:"):
                                continue
                            payload = line[5:].strip()
                            if payload == "[DONE]":
                                break
                            try:
                                delta = json.loads(payload)["choices"][0]["delta"].get("content")
                            except Exception:
                                continue
                            if delta:
                                parts.append(delta)
                                await stream_cb(delta)
                        ans = "".join(parts)
                else:
                    res = await state.http_client.post(
                        "https://opencode.ai/zen/v1/chat/completions",
                        headers={"Authorization": f"Bearer {OPENCODE_ZEN_API_KEY}"},
                        json={"model": zen_model, "messages": zen_messages, "temperature": 0.5, "max_tokens": 1024},
                        timeout=45.0
                    )
                    res.raise_for_status()
                    ans = res.json()["choices"][0]["message"]["content"]
                if not ans:
                    raise ValueError("resposta vazia")
                ans = f"🧠 ({zen_model}) {ans}"

                history.extend([{"role": "user", "content": question}, {"role": "assistant", "content": ans}])
                await save_history(unit, history, history_base)
                await bump_stats(unit, user, zen_model)
                await health_ok(unit, "zen")
                return ans
            except Exception as e:
                logger.warning(f"Zen model '{zen_model}' failed: {e}")
                await health_fail(unit, "zen")
                continue

    # ==========================================
    # LEVEL 4: Secondary Server (Low-power Backup)
    # ==========================================
    try:
        payload = {"question": question, "overrideConfig": {"sessionId": f"{session_id}:{uuid.uuid4().hex[:8]}"}}
        res = await state.http_client.post(FLOW_URL_PI, json=payload, timeout=300.0)
        res.raise_for_status()

        ans = res.json().get("text")
        if not ans:
            raise ValueError("Backup Flowise devolveu resposta vazia")
        ans = f"🍓 (Backup Server) {ans}"
        history.extend([{"role": "user", "content": question}, {"role": "assistant", "content": ans}])
        await save_history(unit, history, history_base)
        await bump_stats(unit, user, "backup-pi")
        await health_ok(unit, "backup-pi")
        return ans
    except Exception as e:
        logger.error(f"Critical Failure at Level 4: {e}")
        await health_fail(unit, "backup-pi")
        await set_last_error(f"Level 4: {e}")
        # Alerta crítico (throttled para não inundar)
        now = time.time()
        if now - state._last_critical_alert > 300:
            state._last_critical_alert = now
            await send_webhook(f"🚨 **TODOS os níveis falharam!**\nPlataforma: {platform}\nUtilizador: {user or '?'}\nErro: {e}")
        await log_event("🚨 Todos os níveis falharam", f"`{e}`", color=0xff0000)

    return "⚠️ Fatal Error: All servers and fallbacks are currently down. Please try again later."
