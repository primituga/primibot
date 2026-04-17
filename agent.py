import os
import json
import base64
import asyncio
import logging
import httpx
import discord
from twitchio.ext import commands as twitch_commands
import redis.asyncio as aioredis
from groq import AsyncGroq
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# --- Logging Configuration ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(name)s | %(message)s')
logger = logging.getLogger("AIBot")

# --- Environment Variables ---
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
TWITCH_TOKEN = os.getenv("TWITCH_TOKEN")

# Ensure Twitch token has the correct prefix
if TWITCH_TOKEN and not TWITCH_TOKEN.startswith("oauth:"):
    TWITCH_TOKEN = f"oauth:{TWITCH_TOKEN}"

# Flowise & External APIs Configuration
FLOWISE_USER = os.getenv("FLOWISE_USERNAME", "admin")
FLOWISE_PASS = os.getenv("FLOWISE_PASSWORD", "admin")
FLOW_ID = os.getenv("FLOW_ID")
FLOW_ID_PI = os.getenv("FLOW_ID_PI")
FLOW_URL = f"http://flowise:3000/api/v1/prediction/{FLOW_ID}"
FLOW_URL_PI = f"http://flowise:3000/api/v1/prediction/{FLOW_ID_PI}"

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
SEARXNG_URL = os.getenv("SEARXNG_URL", "http://searxng:8080/search")
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")

TWITCH_CHANNELS = os.getenv("TWITCH_CHANNEL").split(",") if os.getenv("TWITCH_CHANNEL") else []

# --- Bot Configuration ---
# Models list for the Cloud Fallback (Groq). The bot tries them in order.
GROQ_MODELS = [
    "groq/compound", 
    "llama-3.3-70b-versatile", 
    "llama-3.1-8b-instant", 
    "mixtral-8x7b-32768", 
    "gemma2-9b-it"
]
MAX_HISTORY = 10
IGNORED_TWITCH_USERS = ["nightbot", "streamlabs", "streamelements", "fossabot", "moobot", "soundalerts"]

# Initialize Groq Client
groq_client = AsyncGroq(api_key=GROQ_API_KEY)

# --- System Prompts ---
DISCORD_PROMPT = (
    "You are a helpful, smart, and friendly assistant on Discord. "
    "CRITICAL RULE: You MUST ALWAYS reply in the EXACT SAME LANGUAGE that the user used in their prompt. "
    "Format your answers cleanly: use short paragraphs and ALWAYS use bullet points ('-') when listing information or steps."
)

TWITCH_PROMPT = (
    "You are a helpful, smart, and fast assistant on Twitch. "
    "CRITICAL RULE: You MUST ALWAYS reply in the EXACT SAME LANGUAGE that the user used in their prompt. "
    "CRITICAL RULE 2: Keep your answers EXTREMELY SHORT and concise. Do not use long paragraphs. "
    "Avoid using markdown like bolding (**) or bullet points unless necessary, as Twitch chat doesn't format them well."
)

# --- Redis Persistence ---
redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)

async def get_history(session_id: str) -> list:
    """Retrieve chat history from Redis."""
    data = await redis_client.get(f"history:{session_id}")
    return json.loads(data) if data else []

async def save_history(session_id: str, history: list):
    """Save chat history to Redis with a 24-hour expiration time."""
    await redis_client.set(f"history:{session_id}", json.dumps(history[-MAX_HISTORY:]), ex=86400)

async def clear_history(session_id: str):
    """Clear chat history from Redis."""
    await redis_client.delete(f"history:{session_id}")
    # Note: If your Flowise endpoint requires an API Key (Bearer) instead of Basic Auth, 
    # you must implement the Authorization header here to avoid 401 Unauthorized errors.

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

# --- Helper Functions ---
async def search_web(query: str) -> str:
    """Perform a web search using a local SearXNG instance."""
    try:
        async with httpx.AsyncClient() as client:
            res = await client.get(SEARXNG_URL, params={"q": query, "format": "json"}, timeout=5.0)
            data = res.json()
            results = [f"- {i.get('title')} ({i.get('url')}): {i.get('content', 'No summary')}" for i in data.get("results", [])[:3]]
            return "\n".join(results)
    except Exception as e:
        logger.warning(f"SearXNG search failed: {e}")
        return ""

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
    """Send long messages to Twitch safely, preventing spam bans by using asyncio.sleep."""
    if not text: return
    prefix = f"@{author_name}: "
    chunk_size = 480 - len(prefix)
    first_message = True
    while len(text) > 0:
        if len(text) <= chunk_size:
            msg = f"{prefix}{text}" if first_message else text
            await channel.send(msg)
            break
        split_at = text.rfind(' ', 0, chunk_size)
        if split_at == -1: split_at = chunk_size
        chunk = text[:split_at]
        msg = f"{prefix}{chunk}" if first_message else chunk
        await channel.send(msg)
        text = text[split_at:].lstrip()
        first_message = False
        chunk_size = 480
        await asyncio.sleep(1.5)  # Crucial to avoid Twitch chat bans

# --- Core AI Logic (3-Tier Fallback System) ---
async def ask_ai_logic(question: str, session_id: str, platform: str = "discord", base64_image: str = None) -> str:
    """
    Handles the AI logic with a robust 3-tier fallback architecture:
    1. Primary Flowise Server
    2. Cloud Groq API (with dynamic SearXNG context)
    3. Secondary Flowise Server (Low-power backup)
    """
    history = await get_history(session_id)
    history = trim_history_safe(history, max_chars=6000)
    is_vision = base64_image is not None 

    # ==========================================
    # LEVEL 1: Primary Flowise Server
    # ==========================================
    try:
        async with httpx.AsyncClient() as client:
            payload = {"question": question, "overrideConfig": {"sessionId": session_id}}
            if is_vision:
                payload["uploads"] = [{
                    "data": f"data:image/jpeg;base64,{base64_image}",
                    "type": "file",
                    "name": "vision_input.jpg",
                    "mime": "image/jpeg"
                }]

            res = await client.post(FLOW_URL, json=payload, timeout=45.0)
            res.raise_for_status() 
            ans = res.json().get("text")
            
            history_msg = f"[Image sent] {question}" if is_vision else question
            history.extend([{"role": "user", "content": history_msg}, {"role": "assistant", "content": ans}])
            await save_history(session_id, history)
            return ans
            
    except Exception as e:
        logger.warning(f"Level 1 (Primary Server) failed. Reason: {e}")

    # ==========================================
    # LEVEL 2: Cloud Fallback (Groq API)
    # ==========================================
    if is_vision:
        vision_model = "llama-3.2-11b-vision-preview"
        sys_prompt = "You are a helpful assistant. Describe the image or answer the user's question about it. Always reply in the EXACT SAME LANGUAGE as the user."
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
        try:
            res = await groq_client.chat.completions.create(
                model=vision_model,
                messages=messages_payload,
                temperature=0.5
            )
            ans = res.choices[0].message.content
            ans = f"👁️ ({vision_model}) {ans}"
            
            history.extend([{"role": "user", "content": f"[Image sent] {question}"}, {"role": "assistant", "content": ans}])
            await save_history(session_id, history)
            return ans
        except Exception as e:
            logger.warning(f"Groq Vision failed: {e}")
    else:
        sys_prompt_base = TWITCH_PROMPT if platform == "twitch" else DISCORD_PROMPT
        web_context = None

        for model in GROQ_MODELS:
            try:
                if "compound" in model:
                    # Agentic models require minimal context to avoid HTTP 413 Payload Too Large
                    # Max completion tokens are explicitly set to save context window space.
                    light_payload = [
                        {"role": "system", "content": sys_prompt_base},
                        {"role": "user", "content": question}
                    ]
                    
                    res = await groq_client.chat.completions.create(
                        model=model,
                        messages=light_payload,
                        temperature=0.5,
                        stream=False,
                        max_completion_tokens=1024,
                        compound_custom={
                            "tools": {"enabled_tools": ["web_search", "code_interpreter", "visit_website"]}
                        }
                    )
                else:
                    # Standard LLMs use local web search (SearXNG) and full history
                    current_sys_prompt = sys_prompt_base
                    
                    if web_context is None: 
                        web_context = await search_web(question)
                    
                    if web_context:
                        current_sys_prompt += f"\n\nUpdated Web Context:\n{web_context}"
                    
                    messages_payload = [{"role": "system", "content": current_sys_prompt}] + history + [{"role": "user", "content": question}]

                    res = await groq_client.chat.completions.create(
                        model=model,
                        messages=messages_payload,
                        temperature=0.5,
                        stream=False
                    )
                
                ans = res.choices[0].message.content
                ans = f"⚡ ({model}) {ans}"
                
                history.extend([{"role": "user", "content": question}, {"role": "assistant", "content": ans}])
                await save_history(session_id, history)
                return ans
                
            except Exception as e:
                logger.warning(f"Groq Model '{model}' failed: {e}")
                continue 

    logger.warning("Level 2 (Cloud Fallback) completely failed. Falling back to Level 3...")

    # ==========================================
    # LEVEL 3: Secondary Server (Low-power Backup)
    # ==========================================
    if is_vision:
        return "⚠️ Error: Primary server and Cloud fallback are down. Currently running on emergency backup, which does not support image processing. Please send text only."

    try:
        async with httpx.AsyncClient() as client:
            payload = {"question": question, "overrideConfig": {"sessionId": session_id}}
            res = await client.post(FLOW_URL_PI, json=payload, timeout=30.0)
            res.raise_for_status() 
            
            ans = res.json().get("text")
            ans = f"🍓 (Backup Server) {ans}"
            history.extend([{"role": "user", "content": question}, {"role": "assistant", "content": ans}])
            await save_history(session_id, history)
            return ans
    except Exception as e:
        logger.error(f"Critical Failure at Level 3: {e}")
        
    return "⚠️ Fatal Error: All servers and fallbacks are currently down. Please try again later."

# --- Discord Module ---
intents = discord.Intents.default()
intents.message_content = True
discord_client = discord.Client(intents=intents)

@discord_client.event
async def on_ready():
    logger.info(f"Discord Bot Online: {discord_client.user}")

@discord_client.event
async def on_message(message):
    if message.author.bot: return
    session_id = f"discord_{message.channel.id}"

    if message.content == "!ai_reset":
        await clear_history(session_id)
        await message.channel.send("🧹 Memory cleared successfully!")
        return

    if message.content == "!ai_credits":
        await message.channel.send("Hello, I am primiBOT, developed by primiSC. \nhttps://github.com/primituga/primibot")
        return

    if message.content.lower().startswith("!ai ") and not message.attachments:
        q = message.content[4:].strip()
        if not q: return
        async with message.channel.typing():
            ans = await ask_ai_logic(q, session_id, platform="discord")
            await send_discord_chunks(message.channel, ans)

    elif message.attachments:
        attachment = message.attachments[0]
        if any(attachment.filename.lower().endswith(ext) for ext in ['.png', '.jpg', '.jpeg', '.webp']):
            async with message.channel.typing():
                image_bytes = await attachment.read()
                base64_image = base64.b64encode(image_bytes).decode('utf-8')
                
                q = message.content[4:].strip() if message.content.lower().startswith("!ai ") else message.content
                
                ans = await ask_ai_logic(q, session_id, platform="discord", base64_image=base64_image)
                await send_discord_chunks(message.channel, ans)

# --- Twitch Module ---
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

    async def event_message(self, message):
        if message.echo or message.author is None: return
        if message.author.name.lower() in IGNORED_TWITCH_USERS: return
        await self.handle_commands(message)

    async def event_command_error(self, context, error):
        # Ignore "Command Not Found" errors to prevent console spam
        if isinstance(error, twitch_commands.CommandNotFound): return
        logger.error(f"Twitch Command Error: {error}")

    @twitch_commands.command(name='ai')
    async def ask_ai(self, ctx):
        q = ctx.message.content.replace("!ai ", "").strip()
        if not q: return
        
        sid = f"twitch_{ctx.channel.name}_{ctx.author.name}"
        ans = await ask_ai_logic(q, sid, platform="twitch")
        await send_twitch_chunks(ctx.channel, ctx.author.name, ans)

    @twitch_commands.command(name='ai_reset')
    async def reset_ai(self, ctx):
        sid = f"twitch_{ctx.channel.name}_{ctx.author.name}"
        await clear_history(sid)
        await ctx.send(f"@{ctx.author.name} 🧹 Memory cleared!")

# --- Main Runner ---
async def main():
    tasks = []
    if DISCORD_TOKEN: tasks.append(discord_client.start(DISCORD_TOKEN))
    if TWITCH_TOKEN: tasks.append(MyTwitchBot().start())
    
    if not tasks:
        logger.error("No valid tokens provided! Exiting...")
        return
        
    # Run both bots concurrently. return_exceptions=True prevents one bot's crash from killing the other.
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    for res in results:
        if isinstance(res, Exception):
            logger.error(f"Critical Service Alert: A bot failed to start -> {res}")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot shut down gracefully.")