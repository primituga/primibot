import httpx
import redis.asyncio as aioredis
from groq import AsyncGroq
from config import GROQ_API_KEY, REDIS_URL

# Shared HTTP client (reused across all requests to avoid connection churn)
http_client = httpx.AsyncClient(timeout=60.0)

# Redis client (fail-soft: bot keeps working if Redis is down)
redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)

# Groq Client
groq_client = AsyncGroq(api_key=GROQ_API_KEY)

# Per-session locks: prevents concurrent requests corrupting the same chat history
_history_locks = {}

# Ring buffer do chat Twitch (contexto recente)
_chat_ring = {}

# Cache de canais de log
_log_channels = {}

# Throttle de alertas críticos
_last_critical_alert = 0.0

# Twitch app token cache
_app_token_cache = {"token": None, "exp": 0.0}
_broadcaster_id_cache = None

# Referência ao bot Twitch (definida no main) — usada para responder a redemptions
twitch_bot_ref = None

# Cliente Discord (definido no discord_bot)
discord_client = None
