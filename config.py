import os
import re
import logging
from dotenv import load_dotenv

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
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENCODE_ZEN_API_KEY = os.getenv("OPENCODE_ZEN_API_KEY")
SEARXNG_URL = os.getenv("SEARXNG_URL", "http://searxng:8080/search")
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

# Discord logging & slash commands
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0") or 0)                 # canal de logs do Discord
LOG_CHANNEL_ID_TWITCH = int(os.getenv("LOG_CHANNEL_ID_TWITCH", "0") or 0)   # canal de logs da Twitch
GUILD_ID = int(os.getenv("GUILD_ID", "0") or 0)

# Twitch API (Helix) — para follows, redemptions e estado de stream
TWITCH_CLIENT_ID = os.getenv("TWITCH_CLIENT_ID")
TWITCH_CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET")
TWITCH_BROADCASTER_ID = os.getenv("TWITCH_BROADCASTER_ID")
# Token de utilizador do broadcaster (MESMA app/client_id) para endpoints com scope
# (channel:read:redemptions). O token de chat pode ser de outra app e não serve para Helix.
TWITCH_USER_TOKEN = os.getenv("TWITCH_USER_TOKEN")
if TWITCH_USER_TOKEN and TWITCH_USER_TOKEN.startswith("oauth:"):
    TWITCH_USER_TOKEN = TWITCH_USER_TOKEN[len("oauth:"):]

TWITCH_CHANNELS = os.getenv("TWITCH_CHANNEL").split(",") if os.getenv("TWITCH_CHANNEL") else []

# --- Bot Configuration ---
# Models list for the Cloud Fallback (Groq). The bot tries them in order.
# NOTE: apenas modelos disponíveis na conta Groq atual (verificado via /models).
GROQ_MODELS = [
    "groq/compound",
    "groq/compound-mini",
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b"
]
# Modelo de visão via OpenRouter (a Groq já não tem modelos de visão nesta conta)
OPENROUTER_VISION_MODEL = "qwen/qwen2.5-vl-72b-instruct"
MAX_HISTORY = 10
IGNORED_TWITCH_USERS = ["nightbot", "streamlabs", "streamelements", "fossabot", "moobot", "soundalerts"]

# --- Anti-abuse Configuration ---
RATE_LIMIT_COOLDOWN = int(os.getenv("RATE_LIMIT_COOLDOWN", "15"))   # segundos entre pedidos por utilizador
RATE_LIMIT_HOURLY = int(os.getenv("RATE_LIMIT_HOURLY", "20"))       # máximo de pedidos por hora por utilizador
MAX_IMAGE_BYTES = 5 * 1024 * 1024                                   # cap de 5MB para attachments
IMAGE_NEEDS_COMMAND = os.getenv("IMAGE_NEEDS_COMMAND", "false").lower() == "true"  # se true, imagens só com "!ai"
AUTO_MOD = os.getenv("AUTO_MOD", "true").lower() == "true"          # moderação leve (links suspeitos / scams)

# --- Anti-suspeitos (suspects.py) ---
DREP_API_KEY = os.getenv("DREP_API_KEY", "").strip()                # opcional: discordrep.com (reputação Discord)
SUSPECT_ACCOUNT_AGE_DAYS = int(os.getenv("SUSPECT_ACCOUNT_AGE_DAYS", "7"))
SUSPECT_LINK_SCAN = os.getenv("SUSPECT_LINK_SCAN", "true").lower() == "true"      # URLhaus nos links publicados
SUSPECT_AUTO_DELETE = os.getenv("SUSPECT_AUTO_DELETE", "false").lower() == "true" # Discord: apagar msg com link mau

# --- Proteção anti-ban Twitch (humanização) ---
TWITCH_HUMANIZED = os.getenv("TWITCH_HUMANIZED", "true").lower() == "true"          # liga/desliga a humanização
TWITCH_MIN_TYPING = float(os.getenv("TWITCH_MIN_TYPING", "0.8"))   # delay extra mínimo antes de responder
TWITCH_MAX_TYPING = float(os.getenv("TWITCH_MAX_TYPING", "2.5"))   # delay extra máximo antes de responder
TWITCH_MIN_MSG_GAP = float(os.getenv("TWITCH_MIN_MSG_GAP", "1.2")) # intervalo mínimo entre mensagens do bot no mesmo canal
TWITCH_MAX_RESPONSES = int(os.getenv("TWITCH_MAX_RESPONSES", "5")) # máx de respostas em 60s por canal (evita flood)
HEARTBEAT_FILE = os.getenv("HEARTBEAT_FILE", "/tmp/agent_heartbeat")
AI_TRIGGER_REWARDS = [x.strip().lower() for x in os.getenv("AI_TRIGGER_REWARDS", "ask ai,pergunta ao bot,ai").split(",")]

# Welcome + auto-role (Discord)
WELCOME_CHANNEL_ID = int(os.getenv("WELCOME_CHANNEL_ID", "0") or 0)
WELCOME_MESSAGE = os.getenv("WELCOME_MESSAGE", "👋 Bem-vindo(a) {user} ao servidor!")
AUTO_ROLE_ID = int(os.getenv("AUTO_ROLE_ID", "0") or 0)

# Guard de output (palavras que o bot nunca deve dizer)
DENYLIST_WORDS = [w.strip().lower() for w in os.getenv("DENYLIST_WORDS", "fdp,pqp,puta,caralho").split(",") if w.strip()]

# Mini-jogos
RPS_DISCORD_TIMEOUT = os.getenv("RPS_DISCORD_TIMEOUT", "false").lower() == "true"  # timeout de 30s no Discord ao perder

# --- Contexto de live: que streamers entram no prompt de cada sala ---
# Modo so se aplica ao Discord — na Twitch o contexto e sempre o do proprio canal.
# Uma guild so recebe contexto dos streamers que ela propria followed (!ai_twitch add).
# Nao ha guild "casa": o fallback para TWITCH_CHANNEL foi removido de proposito, punha
# os mesmos canais no prompt de todas as salas e o modelo despejava-os em 'ola'.
# "tracked"      -> so os seguidos explicitamente (default)
# "home_only"    -> obsoleto, aceite como alias de "tracked"
# "env"          -> todos os canais de TWITCH_CHANNEL em qualquer sala (comportamento
#                   antigo, partilhado; reintroduz a mistura entre salas)
# "off"          -> nunca injectar contexto de live, nem no Discord nem na Twitch
CONTEXT_MODE = os.getenv("CONTEXT_MODE", "tracked").strip().lower()
MAX_CONTEXT_STREAMERS = int(os.getenv("MAX_CONTEXT_STREAMERS", "3"))

# --- FAQ (RAG leve) ---
# FAQ_FILE e apenas a semente global. O FAQ real vive em Redis por unit (scope.k_faq)
# e sobrepoe-se a esta semente quando existe conteudo por unit.
FAQ_FILE = os.getenv("FAQ_FILE", "faq.txt")

def load_faq() -> str:
    """Le a semente global. Linhas '#' sao comentarios e sao descartadas, senao
    um faq.txt so com o template ia injectar lixo em todos os prompts."""
    try:
        with open(FAQ_FILE, encoding="utf-8") as f:
            lines = [ln.strip() for ln in f if ln.strip() and not ln.strip().startswith("#")]
            return "\n".join(lines).strip()
    except Exception:
        return ""

FAQ_TEXT = load_faq()

# --- System Prompts ---
DISCORD_PROMPT = (
    "You are a helpful, smart, and friendly assistant on Discord. "
    #"CRITICAL RULE: You MUST ALWAYS reply in the EXACT SAME LANGUAGE that the user used in their prompt. "
    "Format your answers cleanly: use short paragraphs and ALWAYS use bullet points ('-') when listing information or steps."
)

TWITCH_PROMPT = (
    "You are a helpful, smart, and fast assistant on Twitch. "
    "CRITICAL RULE: You MUST ALWAYS reply in the EXACT SAME LANGUAGE that the user used in their prompt. "
    "CRITICAL RULE 2: Keep your answers EXTREMELY SHORT and concise. Do not use long paragraphs. "
    "Avoid using markdown like bolding (**) or bullet points unless necessary, as Twitch chat doesn't format them well. "
    "CRITICAL RULE 3: For vague references like 'the game' / 'o jogo' / 'isso', resolve the game in this priority: (1) a game the user names explicitly, (2) the topic of recent conversation messages, (3) the channel's main game from context. Never switch to an unrelated game on your own."
)

# --- Registo de comandos (fonte única para os helps dinâmicos) ---
# (desc, args, mods)
DISCORD_PREFIX_COMMANDS = {
    "ai":          ("Perguntar ao bot", "<pergunta>", False),
    "ai_pick":     ("O bot escolhe uma opção", "<a, b, c>", False),
    "ai_joke":     ("Conta uma piada", "", False),
    "ai_roast":    ("Roast carinhoso", "[@user]", False),
    "ai_live":     ("Estado da stream", "", False),
    "ai_stats":    ("Estatísticas do bot", "", False),
    "ai_remember": ("O bot lembra-se de ti", "<facto>", False),
    "ai_forget":   ("Apaga o que o bot sabe de ti", "", False),
    "ai_reset":    ("Limpa memória do canal", "", True),
    "ai_config":   ("Config do servidor (list/set/reset/wizard)", "[set <chave> <valor>]", True),
    "ai_twitch":   ("Streamers com log Twitch (add/rem/list)", "[add <streamer>]", True),
    "ai_persona":  ("Muda a personalidade (sem texto = limpar)", "[texto]", True),
    "ai_faq":      ("FAQ desta sala (set/limpar)", "[set <texto>]", True),
    "debug":       ("Diagnóstico do bot", "", True),
    "help":        ("Esta lista (alias: !ai_help)", "", False),
    "ai_credits":  ("Créditos do bot", "", False),
    "rps":         ("Pedra-papel-tesoura (perdeste? timeout!)", "<pedra|papel|tesoura>", False),
    "coin":        ("Cara ou coroa", "[cara|coroa]", False),
    "dice":        ("Dado 1-6", "", False),
    "roll":        ("Número aleatório 1-100", "", False),
    "8ball":       ("Bola mágica", "<pergunta>", False),
    "slots":       ("Slot machine", "", False),
}

TWITCH_CMD_INFO = {
    "ai":        ("Perguntar ao bot", "<pergunta>", False),
    "pick":      ("O bot escolhe uma opção", "<a, b, c>", False),
    "joke":      ("Conta uma piada", "", False),
    "roast":     ("Roast carinhoso", "@user", False),
    "live":      ("Estado da stream", "", False),
    "uptime":    ("Tempo de stream decorrido", "", False),
    "ai_stats":   ("Estatísticas do bot (desta sala/canal)", "", False),
    "remember":  ("O bot lembra-se de ti", "<facto>", False),
    "forget":    ("Apaga o que o bot sabe de ti", "", False),
    "ai_reset":  ("Limpa memória do canal", "", True),
    "persona":   ("Muda a personalidade", "[texto]", True),
    "faq":       ("FAQ deste canal (set/limpar)", "[set <texto>]", True),
    "debug":     ("Diagnóstico do bot", "", True),
    "help":      ("Esta lista", "", False),
    "rps":       ("Pedra-papel-tesoura", "<pedra|papel|tesoura>", False),
    "coin":      ("Cara ou coroa", "[cara|coroa]", False),
    "dice":      ("Dado 1-6", "", False),
    "roll":      ("Número aleatório 1-100", "", False),
    "8ball":     ("Bola mágica", "<pergunta>", False),
    "slots":     ("Slot machine", "", False),
    "addcmd":    ("Cria comando personalizado", "<nome> <resposta>", True),
    "editcmd":   ("Edita comando personalizado", "<nome> <resposta>", True),
    "delcmd":    ("Apaga comando personalizado", "<nome>", True),
    "cmdlist":   ("Lista comandos personalizados", "", False),
    "ptimer":    ("Timers automáticos (add/del/list)", "add <min> <msg>", True),
    "so":        ("Shoutout a outro canal", "<@canal>", True),
    "quote":     ("Quotes (add/N/del)", "[add <frase>|N]", False),
    "deaths":    ("Contador de mortes", "[+N|-N|reset]", False),
    "giveaway":  ("Giveaway (start/pick/cancel)", "start <prémio>", False),
    "join":      ("Entra no giveaway", "", False),
    "poll":      ("Votação (pergunta | op1 | op2)", "[end]", False),
    "vote":      ("Vota numa opção", "<número>", False),
    "q":         ("Fila de jogo (open/join/next…)", "join", False),
}
