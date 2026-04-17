# PrimiBot: Multi-Platform AI Assistant with Local & Cloud Fallback

![Python](https://img.shields.io/badge/Python-3.11-blue?style=for-the-badge&logo=python)
![Docker](https://img.shields.io/badge/Docker-Ready-2496ED?style=for-the-badge&logo=docker)
![Discord](https://img.shields.io/badge/Discord-Bot-5865F2?style=for-the-badge&logo=discord)
![Twitch](https://img.shields.io/badge/Twitch-Bot-9146FF?style=for-the-badge&logo=twitch)
![License](https://img.shields.io/badge/License-MIT-green?style=for-the-badge)
[![Buy me a beer](https://img.shields.io/badge/Buy_me_a_beer-FFDD00?style=for-the-badge&logo=buy-me-a-coffee&logoColor=black)](https://paypal.me/primiSC)

PrimiBot is an AI-powered assistant that runs simultaneously on **Twitch** and **Discord**. It was built to solve the common problem of downtime when running local LLMs, using a tiered fallback system to ensure the bot remains responsive even if your local hardware goes offline.

---

## 🛠 Fallback Logic

The bot handles requests through three prioritized layers:

1.  **Primary (Local GPU):** Uses **Flowise/Ollama** on your local machine. This is the preferred method to keep costs at zero and data private.
2.  **Secondary (Cloud):** If the local server is unreachable, it switches to the **Groq API** (`groq/compound`). It uses native web search and code execution to provide accurate, real-time answers.
3.  **Tertiary (Low-Power Backup):** As a final resort, it can connect to a secondary Flowise instance running on a low-power device (like a **Raspberry Pi**), ensuring basic text functionality is always available.

---

## ✨ Key Features

* **Multi-Platform Support:** Independent handling for Twitch chat and Discord channels using `asyncio`.
* **Vision Capabilities:** Supports image analysis on Discord via Llama 3.2 Vision.
* **Real-Time Web Context:** Integrates with **SearXNG** or Groq's native search to answer questions about current events.
* **Automatic Memory Management:** A custom `trim_history_safe` function monitors the character count of the chat history. This prevents the "Payload Too Large" (413) errors common when agents inject too much web data into the context window.
* **Official SDK Integration:** Built using the official Groq Python library for better stability.

---

## ⚙️ Setup

### Prerequisites
* Docker and Docker Compose.
* A Discord Bot Token and Twitch OAuth credentials.
* A Groq API Key (Optional, for cloud fallback).

### Configuration
1.  Copy `.env.example` to `.env`.
2.  Fill in your platform tokens and server URLs:

```env
DISCORD_TOKEN=your_token
TWITCH_TOKEN=oauth:your_token
TWITCH_CHANNEL=your_channel

# Flowise Endpoints
FLOW_URL=http://local-ip:3000/api/v1/prediction/ID_1
FLOW_URL_PI=http://pi-ip:3000/api/v1/prediction/ID_2

GROQ_API_KEY=your_key
```

### Installation
Run the entire stack (Redis, SearXNG, and the Bot) using Docker Compose:

```bash
docker compose -f ollama.yaml up -d --build
```

---

## 🤖 Commands

| Command | Description |
| :--- | :--- |
| `!ai <query>` | Ask the AI a question (Supports images on Discord). |
| `!ai_reset` | Clears the conversation history for the current session. |
| `!ai_credits` | Links to the project repository. |

---

## 🤝 Contributing

This is an open-source project. If you find a bug or have a suggestion to improve the fallback efficiency, feel free to open an issue or a pull request.

---

## 🍻 Support

If you find this project useful, you can support its development:

[![Donate with PayPal](https://img.shields.io/badge/Donate-PayPal-blue.svg?style=flat-square&logo=paypal)](https://paypal.me/primiSC)

---

**License:** MIT