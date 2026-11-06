import random

# --- Mini-jogos ---
RPS_CHOICES = {"pedra": "🗿", "papel": "📄", "tesoura": "✂️"}
RPS_BEATS = {"pedra": "tesoura", "tesoura": "papel", "papel": "pedra"}
MAGIC8 = [
    "Sim!", "Não.", "Talvez...", "Claramente!", "Nem penses.", "Pergunta outra vez mais tarde.",
    "Sem dúvida!", "As estrelas dizem que sim.", "As estrelas dizem que não.", "És capaz de adivinhar."
]
SLOT_EMOJIS = ["🍒", "🍋", "🍇", "🔔", "💎", "7️⃣"]


def play_rps(user_choice: str):
    """Returns (text, outcome) where outcome in ('win','lose','draw', None)."""
    user_choice = user_choice.lower()
    if user_choice not in RPS_CHOICES:
        return "🎮 Escolhe: `rps pedra`, `rps papel` ou `rps tesoura`.", None
    bot_choice = random.choice(list(RPS_CHOICES))
    if bot_choice == user_choice:
        outcome = "draw"
    elif RPS_BEATS[user_choice] == bot_choice:
        outcome = "win"
    else:
        outcome = "lose"
    line = f"🎮 {RPS_CHOICES[user_choice]} vs {RPS_CHOICES[bot_choice]} → " + {
        "draw": "🤝 Empate!",
        "win": "🎉 Ganhaste!",
        "lose": "😈 Perdeste!",
    }[outcome]
    return line, outcome


def eight_ball() -> str:
    return random.choice(MAGIC8)


def slots_spin() -> str:
    a, b, c = random.choices(SLOT_EMOJIS, k=3)
    if a == b == c:
        verdict = "💎 JACKPOT!"
    elif a == b or b == c or a == c:
        verdict = "🤑 Quase!"
    else:
        verdict = "😅 Perdeste..."
    return f"🎰 {a} {b} {c} — {verdict}"
