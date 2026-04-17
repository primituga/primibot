# 🚀 PrimiBot - Hybrid AI Assistant with 3-Tier Resilience

![Python](https://img.shields.io/badge/Python-3.11-blue?style=for-the-badge&logo=python)
![Docker](https://img.shields.io/badge/Docker-Ready-2496ED?style=for-the-badge&logo=docker)
![Discord](https://img.shields.io/badge/Discord-Bot-5865F2?style=for-the-badge&logo=discord)
![Twitch](https://img.shields.io/badge/Twitch-Bot-9146FF?style=for-the-badge&logo=twitch)
![License](https://img.shields.io/badge/License-MIT-green?style=for-the-badge)
[![Buy me a beer](https://img.shields.io/badge/Buy_me_a_beer-FFDD00?style=for-the-badge&logo=buy-me-a-coffee&logoColor=black)](https://paypal.me/primiSC)

**PrimiBot** is a next-generation AI assistant designed to operate simultaneously on **Twitch** and **Discord**. 

Its core innovation is the **3-Tier Fallback Architecture ("The Parachute")**, which guarantees that the bot never goes offline. It seamlessly switches between local processing (GPU), ultra-fast cloud APIs (Groq), and emergency backup power (Raspberry Pi) in milliseconds if any layer fails.

---

## 🧠 The 3-Tier Fallback Architecture

The bot is built with industrial-level resilience. If one tier fails (e.g., API rate limits, hardware crash, network issues), the next one takes over instantly:

1. **Tier 1: Primary Local Server (Main GPU):** Prioritizes free and private local text generation using your own hardware (e.g., RTX 3080) via Ollama and Flowise.
2. **Tier 2: Groq Cloud (Ultra-Fast):** If the local PC is off or crashes, the bot automatically triggers the **Groq API** using the `groq/compound` agent. This tier includes native web search and a code interpreter. If the agent hits a limit, it falls back to `llama-3.3` paired with a local SearXNG instance.
3. **Tier 3: Emergency Backup (Raspberry Pi):** If the cloud fails or hits a hard rate limit, the bot resorts to a secondary, low-power Flowise instance running on a Raspberry Pi to ensure the chat never goes unanswered.

---

## ✨ Key Features

* 👁️ **Vision Support:** Users on Discord can send images, and the bot will dynamically switch to a Vision model (like Llama 3.2 Vision) to analyze and answer questions about them.
* 🌐 **Dynamic Web Search:** Capable of searching the internet in real-time to answer questions about current events, utilizing either Groq's native tools or a local SearXNG engine.
* 🛡️ **Smart Memory (Anti-413 Shield):** Features a custom `trim_history_safe` algorithm that weighs the chat history in characters rather than message count, preventing API `413 Payload Too Large` errors when reading long web pages.
* ⚡ **Official Groq SDK:** Fully integrated with the official Groq Python library for maximum performance and tool calling.
* 💬 **Multi-Platform:** Manages Twitch chats and Discord channels independently and concurrently using `asyncio.gather` with compartmentalized error handling.

---

## ⚙️ Configuration (.env)

Copy the `.env.example` file to `.env` and fill in your credentials. The bot is configured to read these variables automatically:

```env
# Discord & Twitch Tokens
DISCORD_TOKEN=your_discord_token
TWITCH_TOKEN=oauth:your_twitch_token
TWITCH_CHANNEL=channel1,channel2

# AI Endpoints (Flowise)
FLOW_URL=http://your-pc-ip:3000/api/v1/prediction/PRIMARY_ID
FLOW_URL_PI=http://your-pi-ip:3000/api/v1/prediction/BACKUP_ID

# Groq Cloud
GROQ_API_KEY=gsk_your_key

# Infrastructure
SEARXNG_URL=http://searxng:8080/search
REDIS_URL=redis://redis:6379/0
```

---

## 🚀 Getting Started

The project is fully containerized with Docker for easy deployment:

```bash
# 1. Clone the repository
git clone [https://github.com/primituga/primibot.git](https://github.com/primituga/primibot.git)
cd primibot

# 2. Set up your environment variables
cp .env.example .env
nano .env # Fill in your credentials

# 3. Launch the infrastructure (Redis, SearXNG, Bot)
docker compose -f ollama.yaml up -d --build
```

---

## 🤖 Commands

| Command | Platform | Description |
| :--- | :--- | :--- |
| `!ai <question>` | Twitch & Discord | Asks the Artificial Intelligence a question. (Supports image attachments on Discord). |
| `!ai_reset` | Twitch & Discord | Clears the current conversation memory (useful to change topics and free up context window). |
| `!ai_credits` | Discord | Shows information about the bot's creator. |

---

## 🤝 Contributing

Contributions are always welcome! If you want to improve the fallback logic, add support for new platforms, or optimize the code:
1. Fork the Project.
2. Create your Feature Branch (`git checkout -b feature/AmazingFeature`).
3. Commit your Changes (`git commit -m 'Add some AmazingFeature'`).
4. Push to the Branch (`git push origin feature/AmazingFeature`).
5. Open a Pull Request.

---

## 🍻 Support the Project

If PrimiBot made your streams more interactive or saved you some API tokens, consider buying me a beer! It helps keep the Raspberry Pi running and fuels the late-night coding sessions.

[![Donate with PayPal](https://img.shields.io/badge/Donate-PayPal-blue.svg?style=for-the-badge&logo=paypal)](https://paypal.me/primiSC)

---

## ⚖️ License

This project is licensed under the MIT License.