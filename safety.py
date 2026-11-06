import re
from config import DENYLIST_WORDS

# --- Guard de output (denylist) ---
def filter_output(text: str) -> str:
    if not DENYLIST_WORDS or not text:
        return text
    result = text
    for w in DENYLIST_WORDS:
        result = re.sub(r"\b" + re.escape(w) + r"\b", "*" * len(w), result, flags=re.IGNORECASE)
    return result

# --- Auto-mod leve (links suspeitos / scams) ---
SUSPICIOUS_KEYWORDS = ["free nitro", "steam gift", "giveaway", "steam-gift", "steamcommunity.com/gift", "claim your", "you won a", "verify your account"]
SUSPICIOUS_LINK_RE = re.compile(r"(bit\.ly/|tinyurl\.com/|is\.gd/|rb\.gy/|cutt\.ly/|rebrand\.ly/|t\.co/|goo\.gl/|ow\.ly/|t\.me/|wa\.me/)", re.I)


def analyze_message_safety(content: str):
    """Returns a reason string if the message looks dangerous, else None."""
    low = content.lower()
    for kw in SUSPICIOUS_KEYWORDS:
        if kw in low:
            return f"scam phrase ('{kw}')"
    if SUSPICIOUS_LINK_RE.search(low):
        return "suspicious shortlink/redirect"
    return None
