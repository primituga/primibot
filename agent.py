import discord
from twitchio.ext import commands as twitch_commands
import httpx
import os
import asyncio
import json
import logging
import redis.asyncio as aioredis

# --- NOVA INJEÇÃO: Ler o .env à força ---
from dotenv import load_dotenv
load_dotenv() 
# ----------------------------------------

# --- Configuração de Logging ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(name)s | %(message)s')
logger = logging.getLogger("AIBot")

# --- Variáveis de Ambiente ---
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
TWITCH_TOKEN = os.getenv("TWITCH_TOKEN")
# Garante que o token da Twitch tem o prefixo correto
if TWITCH_TOKEN and not TWITCH_TOKEN.startswith("oauth:"):
    TWITCH_TOKEN = f"oauth:{TWITCH_TOKEN}"

FLOW_ID = os.getenv("FLOW_ID")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
FLOW_URL = f"http://flowise:3000/api/v1/prediction/{FLOW_ID}"
SEARXNG_URL = "http://searxng:8080/search"
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")

CANAIS_PARA_ENTRAR = os.getenv("TWITCH_CHANNEL").split(",")

GROQ_MODELS = [
    "llama-3.3-70b-versatile", 
    "llama-3.1-8b-instant", 
    "mixtral-8x7b-32768", 
    "gemma2-9b-it"
]
MAX_HISTORY = 14 

# --- Persistência Real com Redis ---
redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)

async def get_history(session_id):
    data = await redis_client.get(f"history:{session_id}")
    return json.loads(data) if data else []

async def save_history(session_id, history):
    # Salva no Redis e expira a memória após 24h para economizar espaço
    await redis_client.set(f"history:{session_id}", json.dumps(history[-MAX_HISTORY:]), ex=86400)

async def clear_history(session_id):
    await redis_client.delete(f"history:{session_id}")

# --- Funções Auxiliares ---
async def search_web(query):
    try:
        async with httpx.AsyncClient() as client:
            res = await client.get(SEARXNG_URL, params={"q": query, "format": "json"}, timeout=5.0)
            data = res.json()
            results = [f"- {i.get('title')} ({i.get('url')})" for i in data.get("results", [])[:3]]
            return "\n".join(results)
    except Exception as e:
        logger.warning(f"SearXNG falhou: {e}")
        return ""

# --- Funções Auxiliares ---

async def send_discord_chunks(channel, text):
    """Divide mensagens longas de forma inteligente (sem cortar palavras) e sem atrasos artificiais."""
    if not text: return
    
    chunk_size = 1900
    while len(text) > 0:
        # Se o texto já for menor que o limite, envia tudo de uma vez
        if len(text) <= chunk_size:
            await channel.send(text)
            break
        
        # Procura a última quebra de linha (\n) dentro do limite seguro
        split_at = text.rfind('\n', 0, chunk_size)
        
        # Se não houver quebra de linha, procura o último espaço
        if split_at == -1:
            split_at = text.rfind(' ', 0, chunk_size)
            
        # Se for uma palavra/link gigante sem espaços (muito raro), corta no limite exato
        if split_at == -1:
            split_at = chunk_size
            
        # Extrai o pedaço e envia instantaneamente
        chunk = text[:split_at]
        await channel.send(chunk)
        
        # Prepara o resto do texto para o próximo ciclo, removendo espaços no início
        text = text[split_at:].lstrip()


async def send_twitch_chunks(channel, author_name, text):
    """Divide mensagens longas para a Twitch sem cortar palavras."""
    if not text: return
    
    prefix = f"@{author_name}: "
    chunk_size = 480 - len(prefix)
    primeira_mensagem = True
    
    while len(text) > 0:
        if len(text) <= chunk_size:
            msg = f"{prefix}{text}" if primeira_mensagem else text
            await channel.send(msg)
            break
        
        # Procura o último espaço para não cortar a palavra a meio
        split_at = text.rfind(' ', 0, chunk_size)
        if split_at == -1:
            split_at = chunk_size
            
        chunk = text[:split_at]
        msg = f"{prefix}{chunk}" if primeira_mensagem else chunk
        await channel.send(msg)
        
        text = text[split_at:].lstrip()
        primeira_mensagem = False
        chunk_size = 480 # Aumenta o espaço porque o prefixo já não é usado
        
        # 🚨 VITAL: Pausa de 1.5s entre mensagens para a Twitch não banir o bot por Spam
        await asyncio.sleep(1.5)

# --- Lógica de IA (Resiliência Total) ---
async def ask_ai_logic(question, session_id):
    history = await get_history(session_id)
    
    # 1. TENTATIVA: Flowise (Timeout reduzido para 15s para não travar a Twitch)
    try:
        async with httpx.AsyncClient() as client:
            payload = {"question": question, "overrideConfig": {"sessionId": session_id}}
            res = await client.post(FLOW_URL, json=payload, timeout=45.0)
            if res.status_code == 200:
                ans = res.json().get("text")
                history.extend([{"role": "user", "content": question}, {"role": "assistant", "content": ans}])
                await save_history(session_id, history)
                return ans
    except Exception as e:
        logger.warning(f"Fallback acionado para {session_id}. Motivo: {e}")

    # 2. TENTATIVA: Groq Multicamadas + SearXNG
    web_context = await search_web(question)
    
    # O "Cérebro" da IA: Exigimos formatação e Português
    sys_prompt = (
        "És um assistente prestativo, inteligente e amigável. "
        "Responde SEMPRE em Português de Portugal. "
        "Usa uma formatação limpa: separa os teus pensamentos em parágrafos curtos e "
        "usa sempre marcadores (bullet points com '-') quando estiveres a listar informações ou a dar passos."
    )
    if web_context: 
        sys_prompt += f"\n\nContexto Web Atualizado para te ajudar:\n{web_context}"
    
    messages_payload = [{"role": "system", "content": sys_prompt}] + history + [{"role": "user", "content": question}]

    for model in GROQ_MODELS:
        try:
            async with httpx.AsyncClient() as client:
                headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
                res = await client.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers=headers,
                    json={"model": model, "messages": messages_payload, "temperature": 0.6},
                    timeout=10.0
                )
                if res.status_code == 200:
                    ans = res.json()["choices"][0]["message"]["content"]
                    history.extend([{"role": "user", "content": question}, {"role": "assistant", "content": ans}])
                    await save_history(session_id, history)
                    return f"⚡ ({model}) {ans}"
        except Exception:
            continue
            
    return "⚠️ Nossos satélites perderam conexão com a IA no momento. Tente novamente."

# --- Módulo Discord ---
intents = discord.Intents.default()
intents.message_content = True
discord_client = discord.Client(intents=intents)

@discord_client.event
async def on_ready():
    logger.info(f"Discord Online: {discord_client.user}")

@discord_client.event
async def on_message(message):
    if message.author.bot: return
    session_id = f"discord_{message.channel.id}"

    if message.content == "!ai_reset":
        await clear_history(session_id)
        await message.channel.send("🧹 Memória limpa com sucesso!")
        return

    if message.content.startswith("!ai "):
        q = message.content.replace("!ai ", "").strip()
        if not q: return
        async with message.channel.typing():
            ans = await ask_ai_logic(q, session_id)
            await send_discord_chunks(message.channel, ans)

# --- Módulo Twitch ---
class MyTwitchBot(twitch_commands.Bot):
    def __init__(self):
        super().__init__(
            token=TWITCH_TOKEN,
            prefix='!',
            initial_channels=CANAIS_PARA_ENTRAR
        )

    async def event_ready(self):
        nome = self.nick if hasattr(self, 'nick') else "Bot"
        logger.info(f"Twitch Online: {nome} nos canais {CANAIS_PARA_ENTRAR}")

    async def event_message(self, message):
        if message.echo or message.author is None: return
        # Isto diz à biblioteca para processar comandos oficiais
        await self.handle_commands(message)

    # ✨ O Segredo para esconder os erros de "Command Not Found"
    async def event_command_error(self, context, error):
        if isinstance(error, twitch_commands.CommandNotFound):
            return  # Se alguém escrever "!ola", o bot ignora em vez de dar erro
        logger.error(f"Erro Twitch: {error}")

    # Transformamos o !ai num comando oficial!
    @twitch_commands.command(name='ai')
    async def ask_ai(self, ctx):
        q = ctx.message.content.replace("!ai ", "").strip()
        if not q: return
        
        sid = f"twitch_{ctx.channel.name}_{ctx.author.name}"
        ans = await ask_ai_logic(q, sid)
        
        # Usa a nossa nova função inteligente de divisão
        await send_twitch_chunks(ctx.channel, ctx.author.name, ans)

    @twitch_commands.command(name='ai_reset')
    async def reset_ai(self, ctx):
        sid = f"twitch_{ctx.channel.name}_{ctx.author.name}"
        await clear_history(sid)
        await ctx.send(f"@{ctx.author.name} 🧹 Memória reiniciada!")

# --- Runner ---
async def main():
    tasks = []
    if DISCORD_TOKEN: tasks.append(discord_client.start(DISCORD_TOKEN))
    if TWITCH_TOKEN: tasks.append(MyTwitchBot().start())
    
    if not tasks:
        logger.error("Nenhum token fornecido! Saindo...")
        return
        
    # 🔥 A MAGIA: return_exceptions=True cria compartimentos estanques!
    # Se a Twitch explodir por tokens errados, o Discord continua vivo e vice-versa.
    resultados = await asyncio.gather(*tasks, return_exceptions=True)
    
    # Verifica se algum deles crashou e avisa no terminal sem matar o programa
    for res in resultados:
        if isinstance(res, Exception):
            logger.error(f"🚨 Alerta Crítico: Um dos bots falhou ao arrancar -> {res}")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("👋 Bot desligado com sucesso.")
